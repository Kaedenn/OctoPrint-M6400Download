import base64
import sys
import time
import types
import unittest


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
filemanager_module = types.ModuleType("octoprint.filemanager")
destinations_module = types.ModuleType("octoprint.filemanager.destinations")
util_module = types.ModuleType("octoprint.filemanager.util")


class _FileDestinations(object):
    LOCAL = "local"


class _StreamWrapper(object):
    def __init__(self, filename, stream):
        self.filename = filename
        self._stream = stream

    def stream(self):
        return self._stream


destinations_module.FileDestinations = _FileDestinations
util_module.StreamWrapper = _StreamWrapper
filemanager_module.destinations = destinations_module
filemanager_module.util = util_module
octoprint_module.filemanager = filemanager_module
sys.modules.setdefault("octoprint", octoprint_module)
sys.modules.setdefault("octoprint.plugin", plugin_module)
sys.modules.setdefault("octoprint.access", access_module)
sys.modules.setdefault("octoprint.access.permissions", permissions_module)
sys.modules.setdefault("octoprint.filemanager", filemanager_module)
sys.modules.setdefault("octoprint.filemanager.destinations", destinations_module)
sys.modules.setdefault("octoprint.filemanager.util", util_module)

from octoprint_M6400Download import M6400DownloadPlugin


class FakePrinter(object):
    def __init__(self):
        self.calls = []

    def commands(self, commands, tags=None):
        self.calls.append((commands, tags))


class FakeFileManager(object):
    def __init__(self):
        self.files = {}

    def file_exists(self, destination, filename):
        return filename in self.files

    def add_file(self, destination, filename, file_object, allow_overwrite=False):
        if filename in self.files and not allow_overwrite:
            raise FileExistsError(filename)
        self.files[filename] = file_object.stream().read()
        return filename


class M6400TransportTest(unittest.TestCase):
    def setUp(self):
        self.plugin = M6400DownloadPlugin()
        self.printer = FakePrinter()
        self.file_manager = FakeFileManager()
        self.plugin._printer = self.printer
        self.plugin._file_manager = self.file_manager

    def _wait_for_save(self):
        deadline = time.time() + 1
        while self.plugin.get_download_state()["status"] == "saving" and time.time() < deadline:
            time.sleep(0.01)
        self.assertNotEqual("saving", self.plugin.get_download_state()["status"])

    def test_collects_base64_response(self):
        self.plugin.request_download("cube.gcode")
        self.assertEqual([(["M6400 cube.gcode"], {"m6400download"})], self.printer.calls)

        encoded = base64.b64encode(b"G1 X1\n").decode("ascii")
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 6")
        self.plugin.process_received_line(None, "B64_DATA " + encoded[:4])
        self.plugin.process_received_line(None, "B64_DATA " + encoded[4:])
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()

        self.assertEqual(encoded, self.plugin.get_download_base64())
        self.assertEqual("complete", self.plugin.get_download_state()["status"])
        self.assertEqual(b"G1 X1\n", self.file_manager.files["cube.gcode"])

    def test_preserves_serial_line_and_records_firmware_failure(self):
        self.plugin.request_download("cube.gcode")
        self.assertEqual("B64_FAILURE", self.plugin.process_received_line(None, "B64_FAILURE"))
        self.assertEqual("failed", self.plugin.get_download_state()["status"])
        self.assertIsNone(self.plugin.get_download_base64())

    def test_rejects_ambiguous_filenames(self):
        with self.assertRaises(ValueError):
            self.plugin.request_download("cube file.gcode")

    def test_api_download_command_queues_transfer(self):
        self.plugin.on_api_command("download", {"filename": "cube.gcode"})
        self.assertEqual([(["M6400 cube.gcode"], {"m6400download"})], self.printer.calls)

    def test_debugging_defaults_to_false(self):
        self.assertEqual({"debugging": False}, self.plugin.get_settings_defaults())

    def test_refuses_existing_file_unless_forced(self):
        self.file_manager.files["cube.gcode"] = b"old"
        with self.assertRaises(FileExistsError):
            self.plugin.request_download("cube.gcode")

        self.plugin.request_download("cube.gcode", force=True)
        encoded = base64.b64encode(b"new").decode("ascii")
        self.plugin.process_received_line(None, "B64_BEGIN cube.gcode 3")
        self.plugin.process_received_line(None, "B64_DATA " + encoded)
        self.plugin.process_received_line(None, "B64_END")
        self._wait_for_save()

        self.assertEqual(b"new", self.file_manager.files["cube.gcode"])


if __name__ == "__main__":
    unittest.main()
