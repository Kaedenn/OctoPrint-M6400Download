# coding=utf-8
from __future__ import absolute_import

"""
Transport support for Marlin's M6400 base64 SD-card download command.
"""

import base64
import io
import re
import threading

import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.filemanager.destinations import FileDestinations
from octoprint.filemanager.util import StreamWrapper


_B64_BEGIN = re.compile(r"^B64_BEGIN\s+(?P<filename>\S+)\s+(?P<size>\d+)\s*$")
_B64_DATA = re.compile(r"^B64_DATA\s+(?P<data>[A-Za-z0-9+/=]+)\s*$")


class M6400DownloadPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    """
    Receive M6400 responses without blocking OctoPrint's serial read loop.
    """

    def __init__(self):
        self._download_lock = threading.RLock()
        self._download_buffer = io.StringIO()
        self._download_filename = None
        self._download_size = None
        self._download_force = False
        self._download_status = "idle"
        self._download_error = None

    ##~~ Download transport

    def get_api_commands(self):
        return {"download": ["filename"]}

    def is_api_protected(self):
        """
        Require an authenticated OctoPrint user for download requests.
        """
        return True

    @Permissions.FILES_DOWNLOAD.require(403)
    def on_api_command(self, command, data):
        if command == "download":
            try:
                self.request_download(data["filename"], force=data.get("force", False))
            except FileExistsError:
                return {"error": "file_exists"}, 409

    def request_download(self, filename, force=False):
        """
        Queue ``M6400 <filename>`` and begin collecting its base64 response.

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
            if not force and self._file_manager.file_exists(FileDestinations.LOCAL, filename):
                raise FileExistsError("a local file named '{0}' already exists".format(filename))

            self._download_buffer = io.StringIO()
            self._download_filename = filename
            self._download_size = None
            self._download_force = force
            self._download_status = "waiting"
            self._download_error = None

        try:
            self._printer.commands(["M6400 {0}".format(filename)], tags={"m6400download"})
        except Exception:
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

    def process_received_line(self, comm_instance, line, *args, **kwargs):
        """
        Collect M6400 protocol lines and always preserve serial processing.
        """
        begin = _B64_BEGIN.match(line)
        data = _B64_DATA.match(line)

        with self._download_lock:
            if begin and self._download_status == "waiting":
                self._download_filename = begin.group("filename")
                self._download_size = int(begin.group("size"))
                self._download_status = "receiving"
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
            elif line.strip() == "B64_FAILURE" and self._download_status in ("waiting", "receiving"):
                self._download_status = "failed"
                self._download_error = "Firmware reported an M6400 transfer failure"
            elif line.startswith("Error:") and self._download_status == "waiting":
                self._download_status = "failed"
                self._download_error = line.strip()

        # A received hook must return the line to avoid aborting normal
        # communication processing.
        return line

    def _save_download(self, filename, expected_size, force, encoded_data):
        """
        Decode and add a completed transfer to OctoPrint's local storage.
        """
        try:
            contents = base64.b64decode(encoded_data.encode("ascii"), validate=True)
            if len(contents) != expected_size:
                raise ValueError(
                    "received {0} bytes, expected {1}".format(len(contents), expected_size)
                )
            file_object = StreamWrapper(filename, io.BytesIO(contents))
            saved_path = self._file_manager.add_file(
                FileDestinations.LOCAL,
                filename,
                file_object,
                allow_overwrite=force,
            )
        except Exception as error:
            with self._download_lock:
                self._download_status = "failed"
                self._download_error = "Could not save downloaded file: {0}".format(error)
        else:
            with self._download_lock:
                self._download_status = "complete"
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "download_complete", "path": filename, "file": saved_path},
            )

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
        return {
            "M6400Download": {
                "displayName": "M6400Download Plugin",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "kaedenn",
                "repo": "OctoPrint-M6400Download",
                "current": self._plugin_version,
                "pip": "https://github.com/kaedenn/OctoPrint-M6400Download/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "M6400Download Plugin"
__plugin_pythoncompat__ = ">=3,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = M6400DownloadPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.process_received_line,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }

# vim: set ts=4 sts=4 sw=4:
