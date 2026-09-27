import base64
import sys
import tempfile
from pathlib import Path
import time
import types
import unittest
from unittest.mock import Mock, patch


plugin_module = types.ModuleType("octoprint.plugin")


class _SettingsPlugin(object):
    pass


class _AssetPlugin(object):
    pass


class _TemplatePlugin(object):
    pass


class _SimpleApiPlugin(object):
    pass


plugin_module.SettingsPlugin = _SettingsPlugin
plugin_module.AssetPlugin = _AssetPlugin
plugin_module.TemplatePlugin = _TemplatePlugin
plugin_module.SimpleApiPlugin = _SimpleApiPlugin
octoprint_module = types.ModuleType("octoprint")
octoprint_module.plugin = plugin_module
access_module = types.ModuleType("octoprint.access")
permissions_module = types.ModuleType("octoprint.access.permissions")


class _FilesDownloadPermission(object):
    def require(self, status_code):
        def decorate(function):
            return function
        return decorate


class _Permissions(object):
    FILES_DOWNLOAD = _FilesDownloadPermission()


permissions_module.Permissions = _Permissions
access_module.permissions = permissions_module
octoprint_module.access = access_module
sys.modules.setdefault("octoprint", octoprint_module)
sys.modules.setdefault("octoprint.plugin", plugin_module)
sys.modules.setdefault("octoprint.access", access_module)
sys.modules.setdefault("octoprint.access.permissions", permissions_module)

from octoprint_M6400Download import M6400DownloadPlugin


class FakePrinter(object):
    def __init__(self):
        self.calls = []

    def commands(self, commands, tags=None):
        self.calls.append((commands, tags))


class FailingPrinter(object):
    def __init__(self, error):
        self.error = error

    def commands(self, commands, tags=None):
        raise self.error


class FakePluginManager(object):
    def __init__(self):
        self.messages = []
        self.notices = []

    def send_plugin_message(self, identifier, payload):
        if payload["type"] == "download_notice":
            self.notices.append((identifier, payload))
        else:
            self.messages.append((identifier, payload))


class M6400TransportTest(unittest.TestCase):
    def setUp(self):
        self.plugin = M6400DownloadPlugin()
        self.printer = FakePrinter()
        uploads = tempfile.TemporaryDirectory()
        self.addCleanup(uploads.cleanup)
        self.uploads = Path(uploads.name)
        self.plugin._settings = Mock()
        self.plugin._settings.global_get_basefolder.return_value = uploads.name
        self.plugin._printer = self.printer
        self.plugin._plugin_manager = FakePluginManager()
        self.plugin._identifier = "M6400Download"
        self.plugin._logger = Mock()

    def test_terminal_response_from_log(self):
        filename = "ender3_status.gcode"
        contents = b"M105\r\nM114\r\nM27\r\nM31\r\n"
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.setUp()
                if existing:
                    (self.uploads / filename).write_bytes(b"existing contents")
                for line in (
                    "B64_BEGIN ender3_status.gcode 22\r\n",
                    "B64_DATA TTEwNQ0KTTExNA0KTTI3DQpNMzENCg==\r\n",
                    "B64_END\r\n",
                    "ok\r\n",
                ):
                    self.assertEqual(line, self.plugin.process_received_line(None, line))
                self._wait_for_save()
                if existing:
                    self.assertEqual("failed", self.plugin.get_download_state()["status"])
                    self.assertEqual(b"existing contents", (self.uploads / filename).read_bytes())
                    self.plugin._logger.error.assert_called_once()
                else:
                    self.assertEqual("complete", self.plugin.get_download_state()["status"])
                    self.assertEqual(contents, (self.uploads / filename).read_bytes())
                    self.plugin._logger.error.assert_not_called()

    def _wait_for_save(self):
        deadline = time.time() + 1
        while self.plugin.get_download_state()["status"] == "saving" and time.time() < deadline:
            time.sleep(0.01)
        self.assertNotEqual("saving", self.plugin.get_download_state()["status"])

    def _wait_for_message(self):
        deadline = time.time() + 1
        while not self.plugin._plugin_manager.messages and time.time() < deadline:
            time.sleep(0.01)

    def test_collects_base64_response(self):
        self.plugin.request_download("cube.gcode")
        self.assertEqual([(["M6400 cube.gcode"], {"m6400download"})], self.printer.calls)

        encoded = base64.b64encode(b"G1 X1\n").decode("ascii")
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 6")
        self.plugin.process_received_line(None, "B64_DATA " + encoded[:4])
        self.plugin.process_received_line(None, "B64_DATA " + encoded[4:])
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()
        self._wait_for_message()

        self.assertEqual(encoded, self.plugin.get_download_base64())
        self.assertEqual("complete", self.plugin.get_download_state()["status"])
        self.assertEqual(b"G1 X1\n", (self.uploads / "cube.gcode").read_bytes())
        self.assertEqual(
            [("M6400Download", {"type": "download_complete", "path": "cube.gcode", "file": "cube.gcode"})],
            self.plugin._plugin_manager.messages,
        )

    def test_preserves_serial_line_and_records_firmware_failure(self):
        self.plugin.request_download("cube.gcode")
        self.assertEqual("B64_FAILURE", self.plugin.process_received_line(None, "B64_FAILURE"))
        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertIsNone(self.plugin.get_download_base64())
        self.assertEqual([], self.plugin._plugin_manager.messages)

    def test_external_transfers_start_without_a_plugin_request(self):
        for filename in ("first.gcode", "second.gcode"):
            for line in (f"B64_BEGIN {filename} 3", "B64_DATA bmV3", "B64_END"):
                self.assertEqual(line, self.plugin.process_received_line(None, line))
            self._wait_for_save()
            self.assertEqual("complete", self.plugin.get_download_state()["status"])
            self.assertEqual(b"new", (self.uploads / filename).read_bytes())
            self.assertEqual("bmV3", self.plugin.get_download_base64())
        self.assertEqual([], self.printer.calls)

    def test_external_transfer_recovers_after_firmware_failure(self):
        self.plugin.process_received_line(None, "B64_BEGIN old.gcode 6")
        self.plugin.process_received_line(None, "B64_DATA b2xk")
        self.plugin.process_received_line(None, "B64_FAILURE")
        self.plugin.process_received_line(None, "B64_BEGIN new.gcode 3")
        self.plugin.process_received_line(None, "B64_DATA bmV3")
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()
        self.assertEqual("complete", self.plugin.get_download_state()["status"])
        self.assertIsNone(self.plugin.get_download_state()["error"])
        self.assertEqual({"new.gcode": b"new"}, {p.name: p.read_bytes() for p in self.uploads.iterdir()})

    def test_external_transfer_does_not_inherit_overwrite_permission(self):
        self.plugin.request_download("requested.gcode", force=True)
        (self.uploads / "external.gcode").write_bytes(b"old")
        self.plugin.process_received_line(None, "B64_BEGIN external.gcode 3")
        self.assertFalse(self.plugin.get_download_state()["force"])
        self.plugin.process_received_line(None, "B64_DATA bmV3")
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()
        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertEqual(b"old", (self.uploads / "external.gcode").read_bytes())

    def test_completed_force_request_does_not_authorize_later_transfer(self):
        self.plugin.request_download("cube.gcode", force=True)
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 0")
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()
        self.assertEqual("complete", self.plugin.get_download_state()["status"])
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 3")
        self.assertFalse(self.plugin.get_download_state()["force"])
        self.plugin.process_received_line(None, "B64_DATA bmV3")
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()
        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertEqual(b"", (self.uploads / "cube.gcode").read_bytes())

    def test_completion_message_uses_saved_file_path(self):
        self.plugin.request_download("LONGNA~1.GCO")
        self.plugin.process_received_line(None, "B64_BEGIN LONGNA~1.GCO 3")
        self.plugin.process_received_line(None, "B64_DATA bmV3")
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_message()

        self.assertEqual(
            [("M6400Download", {
                "type": "download_complete",
                "path": "LONGNA~1.GCO",
                "file": "LONGNA~1.GCO",
            })],
            self.plugin._plugin_manager.messages,
        )

    def test_rejects_ambiguous_filenames(self):
        with self.assertRaises(ValueError):
            self.plugin.request_download("cube file.gcode")

    def test_queue_runtime_error_marks_download_failed(self):
        self.plugin._printer = FailingPrinter(RuntimeError("printer unavailable"))

        with self.assertRaises(RuntimeError):
            self.plugin.request_download("cube.gcode")

        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertEqual(
            "Failed to queue M6400 command",
            self.plugin.get_download_state()["error"],
        )

    def test_queue_programming_error_is_not_handled(self):
        self.plugin._printer = FailingPrinter(TypeError("bad printer call"))

        with self.assertRaises(TypeError):
            self.plugin.request_download("cube.gcode")

        self.assertEqual("waiting", self.plugin.get_download_state()["status"])

    def test_storage_error_marks_download_failed(self):
        self.plugin._write_download = Mock(side_effect=OSError("disk full"))

        self.plugin._save_download("cube.gcode", 3, False, "bmV3")

        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertEqual(
            "Could not save downloaded file: disk full",
            self.plugin.get_download_state()["error"],
        )
        self.assertEqual([], self.plugin._plugin_manager.messages)
        self.assertEqual(
            {"type": "download_notice", "level": "error",
             "message": "Could not save cube.gcode: disk full"},
            self.plugin._plugin_manager.notices[-1][1],
        )

    def test_protocol_markers_display_notices_even_when_ignored(self):
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 3\r\n")
        self.plugin.process_received_line(None, "B64_BEGIN other.gcode 3\r\n")
        self.plugin.process_received_line(None, "B64_FAILURE")
        self.plugin.process_received_line(None, "B64_END\r\n")
        self.assertEqual(
            ["Received B64_BEGIN cube.gcode 3 (transfer state: idle)",
             "Received B64_BEGIN other.gcode 3 (transfer state: receiving)",
             "Received B64_END (transfer state: failed)"],
            [payload["message"] for _, payload in self.plugin._plugin_manager.notices],
        )

    def test_save_programming_error_is_not_handled(self):
        self.plugin._write_download = Mock(side_effect=TypeError("bad storage call"))

        with self.assertRaises(TypeError):
            self.plugin._save_download("cube.gcode", 3, False, "bmV3")

    def test_api_download_command_queues_transfer(self):
        self.plugin.on_api_command("download", {"filename": "cube.gcode"})
        self.assertEqual([(["M6400 cube.gcode"], {"m6400download"})], self.printer.calls)

    def test_api_existing_file_returns_conflict_and_accepts_force(self):
        (self.uploads / "cube.gcode").write_bytes(b"old")
        self.assertEqual(
            ({"error": "file_exists"}, 409),
            self.plugin.on_api_command("download", {"filename": "cube.gcode"}),
        )
        self.assertEqual([], self.printer.calls)

        self.plugin.on_api_command("download", {"filename": "cube.gcode", "force": True})
        self.assertEqual([(["M6400 cube.gcode"], {"m6400download"})], self.printer.calls)
        self.assertTrue(self.plugin.get_download_state()["force"])

    def test_debugging_defaults_to_false(self):
        self.assertEqual({"debugging": False}, self.plugin.get_settings_defaults())

    def test_arbitrary_extensions_are_saved_directly(self):
        contents = b'{"version": 1}\n'
        encoded = base64.b64encode(contents).decode("ascii")
        for filename in ("marlin_config.json", "README", "folder/settings.bin"):
            self.plugin.process_received_line(None, f"B64_BEGIN {filename} {len(contents)}")
            self.plugin.process_received_line(None, "B64_DATA " + encoded)
            self.plugin.process_received_line(None, "B64_END")
            self._wait_for_save()
            self.assertEqual("complete", self.plugin.get_download_state()["status"])
            self.assertEqual(contents, (self.uploads / filename).read_bytes())

    def test_rejects_traversal_and_symlinks(self):
        for filename in ("../escape.json", "folder/../../escape", "folder\\escape"):
            with self.subTest(filename=filename):
                self.plugin._save_download(filename, 3, True, "bmV3")
                self.assertEqual("failed", self.plugin.get_download_state()["status"])
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "target.json"
            target.write_bytes(b"original")
            (self.uploads / "link.json").symlink_to(target)
            (self.uploads / "folder").symlink_to(outside, target_is_directory=True)
            for filename in ("link.json", "folder/target.json"):
                self.plugin._save_download(filename, 3, True, "bmV3")
                self.assertEqual("failed", self.plugin.get_download_state()["status"])
            self.assertEqual(b"original", target.read_bytes())

    def test_failed_publish_preserves_existing_file_and_cleans_temporary(self):
        target = self.uploads / "config.json"
        target.write_bytes(b"original")
        with patch("octoprint_M6400Download.os.replace", side_effect=OSError("disk error")):
            self.plugin._save_download("config.json", 3, True, "bmV3")
        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertEqual(b"original", target.read_bytes())
        self.assertEqual([target], list(self.uploads.iterdir()))

    def test_size_mismatch_does_not_replace_existing_file(self):
        target = self.uploads / "config.json"
        target.write_bytes(b"original")
        self.plugin._save_download("config.json", 4, True, "bmV3")
        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertEqual(b"original", target.read_bytes())

    def test_refuses_existing_file_unless_forced(self):
        (self.uploads / "cube.gcode").write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            self.plugin.request_download("cube.gcode")

        self.plugin.request_download("cube.gcode", force=True)
        encoded = base64.b64encode(b"new").decode("ascii")
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 3")
        self.plugin.process_received_line(None, "B64_DATA " + encoded)
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()

        self.assertEqual(b"new", (self.uploads / "cube.gcode").read_bytes())


if __name__ == "__main__":
    unittest.main()
