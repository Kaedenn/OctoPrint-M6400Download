# coding=utf-8
# OctoPrint requires the package name to match its case-sensitive plugin identifier.
# pylint: disable=invalid-name
"""
Transport support for Marlin's M6400 base64 SD-card download command.
"""

import base64
import binascii
import io
import os
import re
import tempfile
import threading

import octoprint.plugin
from octoprint.access.permissions import Permissions

_B64_BEGIN = re.compile(r"^B64_BEGIN\s+(?P<filename>\S+)\s+(?P<size>\d+)\s*$")
_B64_DATA = re.compile(r"^B64_DATA\s+(?P<data>[A-Za-z0-9+/=]+)\s*$")


class M6400DownloadPlugin(  # pylint: disable=too-many-ancestors
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    """
    Receive M6400 responses without blocking OctoPrint's serial read loop.
    """

    def __init__(self):  # pylint: disable=super-init-not-called
        self._download_lock = threading.RLock()
        self._download_buffer = io.StringIO()
        self._download_filename = None
        self._download_size = None
        self._download_force = False
        self._download_status = "idle"
        self._download_error = None

    ##~~ Download transport

    def get_api_commands(self):
        """
        Provide a simple API command for download requests.
        """
        return {"download": ["filename"]}

    def is_api_protected(self):
        """
        Require an authenticated OctoPrint user for download requests.
        """
        return True

    @Permissions.FILES_DOWNLOAD.require(403)
    def on_api_command(self, command, data):
        """
        Handle a download request.
        """
        if command == "download":
            try:
                self.request_download(data["filename"], force=data.get("force", False))
            except FileExistsError:
                return {"error": "file_exists"}, 409
        return None

    def request_download(self, filename, force=False):
        """
        Queue ``M6400 <filename>`` with optional overwrite permission.

        The resulting text can be obtained with :meth:`get_download_base64`
        after the firmware sends ``B64_END``. Only one transfer is supported at
        a time, because the firmware protocol has no transfer identifier.
        """
        if not isinstance(filename, str) or not filename or filename != filename.strip():
            raise ValueError("filename must be a non-empty, trimmed string")
        if any(character.isspace() for character in filename):
            raise ValueError("M6400 filenames cannot contain whitespace")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")

        with self._download_lock:
            if self._download_status in ("waiting", "receiving", "saving"):
                raise RuntimeError("an M6400 download is already in progress")
            if not force and os.path.lexists(self._download_path(filename)):
                raise FileExistsError(f"a local file named '{filename}' already exists")

            self._download_buffer = io.StringIO()
            self._download_filename = filename
            self._download_size = None
            self._download_force = force
            self._download_status = "waiting"
            self._download_error = None

        try:
            self._printer.commands(
                [f"M6400 {filename}"],
                tags={"m6400download"},
            )
        except RuntimeError:
            with self._download_lock:
                self._download_status = "failed"
                self._download_error = "Failed to queue M6400 command"
            raise

    def get_download_base64(self):
        """
        Return the accumulated base64 text once an M6400 transfer completes.
        """
        with self._download_lock:
            if self._download_status != "complete":
                return None
            return self._download_buffer.getvalue()

    def get_download_state(self):
        """
        Return a snapshot suitable for a future API or UI layer.
        """
        with self._download_lock:
            return {
                "filename": self._download_filename,
                "size": self._download_size,
                "force": self._download_force,
                "status": self._download_status,
                "error": self._download_error,
            }

    def process_received_line(self, _comm_instance, line, *_args, **_kwargs):
        """
        Collect M6400 responses regardless of who requested the command.

        The firmware's begin record starts collection. Only a matching pending
        request may authorize overwriting a local file; external transfers use
        the default no-overwrite policy. Preserve normal serial processing.
        """
        begin = _B64_BEGIN.match(line)
        data = _B64_DATA.match(line)

        with self._download_lock:
            if begin or line.strip() == "B64_END":
                self._send_notice(
                    f"Received {line.strip()} (transfer state: {self._download_status})"
                )
            if begin and self._download_status not in ("receiving", "saving"):
                force = (
                    self._download_status == "waiting"
                    and self._download_filename == begin.group("filename")
                    and self._download_force
                )
                self._download_buffer = io.StringIO()
                self._download_filename = begin.group("filename")
                self._download_size = int(begin.group("size"))
                self._download_force = force
                self._download_status = "receiving"
                self._download_error = None
            elif data and self._download_status == "receiving":
                self._download_buffer.write(data.group("data"))
            elif line.strip() == "B64_END" and self._download_status == "receiving":
                self._download_status = "saving"
                filename = self._download_filename
                expected_size = self._download_size
                force = self._download_force
                encoded_data = self._download_buffer.getvalue()
                threading.Thread(
                    target=self._save_download,
                    args=(filename, expected_size, force, encoded_data),
                    name="M6400Download-save",
                    daemon=True,
                ).start()
            elif line.strip() == "B64_FAILURE" \
                    and self._download_status in ("waiting", "receiving"):
                self._download_status = "failed"
                self._download_error = "Firmware reported an M6400 transfer failure"
            elif line.startswith("Error:") and self._download_status == "waiting":
                self._download_status = "failed"
                self._download_error = line.strip()

        # A received hook must return the line to avoid aborting normal
        # communication processing.
        return line

    def _send_notice(self, message, level="info"):
        """Display transfer diagnostics in connected OctoPrint clients."""
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "download_notice", "message": message, "level": level},
        )

    def _save_download(self, filename, expected_size, force, encoded_data):
        """
        Decode and write a completed transfer directly to the uploads folder.
        """
        try:
            contents = base64.b64decode(encoded_data.encode("ascii"), validate=True)
            if len(contents) != expected_size:
                raise ValueError(
                    f"received {len(contents)} bytes, expected {expected_size}"
                )
            saved_path = self._write_download(filename, contents, force)
        except (binascii.Error, UnicodeError, ValueError, OSError) as error:
            self._logger.error("Could not save downloaded file %r: %s", filename, error)
            with self._download_lock:
                self._download_status = "failed"
                self._download_error = f"Could not save downloaded file: {error}"
            self._send_notice(f"Could not save {filename}: {error}", level="error")
        else:
            with self._download_lock:
                self._download_status = "complete"
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "download_complete", "path": filename, "file": saved_path},
            )

    def _download_path(self, filename):
        """Resolve an SD path inside uploads, rejecting traversal and symlinks."""
        root = os.path.realpath(self._settings.global_get_basefolder("uploads"))
        parts = filename.lstrip("/").split("/")
        if any(part in ("", ".", "..") for part in parts) or "\\" in filename:
            raise ValueError("invalid download path")
        path = root
        for part in parts:
            path = os.path.join(path, part)
            if os.path.islink(path):
                raise ValueError("download path cannot contain symlinks")
        if os.path.commonpath((root, os.path.realpath(path))) != root:
            raise ValueError("download path must stay inside uploads")
        return path

    def _write_download(self, filename, contents, force):
        """Publish a complete file without exposing partial writes."""
        path = self._download_path(filename)
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".m6400-", delete=False) as output:
                temporary = output.name
                output.write(contents)
            if force:
                os.replace(temporary, path)
            else:
                # Linking fails atomically if another writer created the destination.
                os.link(temporary, path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        return filename.lstrip("/")

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return {"debugging": False}

    ##~~ TemplatePlugin mixin

    def get_template_configs(self):
        return [{"type": "settings", "custom_bindings": False}]

    ##~~ AssetPlugin mixin

    def get_assets(self):
        return {
            "js": ["js/M6400Download.js"],
            "css": ["css/M6400Download.css"],
            "less": ["less/M6400Download.less"],
        }

    ##~~ Softwareupdate hook

    def get_update_information(self):
        """Describe this plugin to OctoPrint's software update system."""
        return {
            "M6400Download": {
                "displayName": "M6400Download Plugin",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "kaedenn",
                "repo": "OctoPrint-M6400Download",
                "current": self._plugin_version,
                "pip": (
                    "https://github.com/kaedenn/OctoPrint-M6400Download/"
                    "archive/{target_version}.zip"
                ),
            }
        }


__plugin_name__ = "M6400Download Plugin"
__plugin_pythoncompat__ = ">=3,<4"
__plugin_implementation__ = None
__plugin_hooks__ = None


def __plugin_load__():
    global __plugin_implementation__  # pylint: disable=global-statement
    __plugin_implementation__ = M6400DownloadPlugin()

    global __plugin_hooks__  # pylint: disable=global-statement
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.received": (
            __plugin_implementation__.process_received_line
        ),
        "octoprint.plugin.softwareupdate.check_config": (
            __plugin_implementation__.get_update_information
        ),
    }

# vim: set ts=4 sts=4 sw=4:
