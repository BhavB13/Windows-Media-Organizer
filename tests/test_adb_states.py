"""Reading `adb devices -l` and telling the user what each state means.

All output here is recorded text fed through the parser or a faked subprocess;
no phone is needed.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import adb_bridge
import engine
from adb_bridge import ADB_STATE_HELP, describe_device_state, parse_devices_output
from duplicate_transfer_manager.services.support_services import ADB_CONNECTION_HELP, DeviceService

STARTING_SERVER = """\
* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
R58M12ABCDE            device usb:1-1 product:beyond1lteeea model:SM_G973F device:beyond1 transport_id:3

"""

EVERY_STATE = """\
List of devices attached
AAA unauthorized usb:1-1 transport_id:1
BBB offline transport_id:2
CCC no permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com/tools/device.html] usb:1-2 transport_id:3
DDD recovery product:x model:Y transport_id:4
EEE sideload transport_id:5
FFF bootloader transport_id:6
GGG rescue transport_id:7
HHH host transport_id:8
III authorizing transport_id:9
JJJ device product:p model:Pixel_8 device:shiba transport_id:10
"""


class ParsingTests(unittest.TestCase):
    def test_daemon_banner_lines_are_not_devices(self):
        devices = parse_devices_output(STARTING_SERVER)
        self.assertEqual([d["serial"] for d in devices], ["R58M12ABCDE"])
        self.assertEqual(devices[0]["model"], "SM G973F")
        self.assertEqual(devices[0]["transport_id"], "3")

    def test_every_state_is_read_including_the_two_word_one(self):
        states = {d["serial"]: d["status"] for d in parse_devices_output(EVERY_STATE)}
        self.assertEqual(
            states,
            {
                "AAA": "unauthorized", "BBB": "offline", "CCC": "no permissions", "DDD": "recovery",
                "EEE": "sideload", "FFF": "bootloader", "GGG": "rescue", "HHH": "host",
                "III": "authorizing", "JJJ": "device",
            },
        )

    def test_adb_explanation_text_is_not_taken_for_fields(self):
        entry = next(d for d in parse_devices_output(EVERY_STATE) if d["serial"] == "CCC")
        self.assertEqual(entry["usb"], "1-2")
        self.assertNotIn("see", entry)
        self.assertEqual(entry["model"], "CCC")

    def test_empty_truncated_and_windows_line_endings(self):
        self.assertEqual(parse_devices_output(""), [])
        self.assertEqual(parse_devices_output("List of devices attached\r\n\r\n"), [])
        devices = parse_devices_output("List of devices attached\r\nXYZ\r\nABC device\r\n")
        self.assertEqual([(d["serial"], d["status"]) for d in devices], [("XYZ", "unknown"), ("ABC", "device")])

    def test_list_devices_uses_the_parser(self):
        with mock.patch.object(adb_bridge.ADBBridge, "start_server"), \
                mock.patch.object(adb_bridge.subprocess, "check_output", return_value=STARTING_SERVER.encode()):
            devices = adb_bridge.ADBBridge.list_devices()
        self.assertEqual([(d["serial"], d["status"], d["model"]) for d in devices], [("R58M12ABCDE", "device", "SM G973F")])


class AdviceTests(unittest.TestCase):
    def test_a_ready_device_needs_no_advice(self):
        self.assertEqual(describe_device_state("device"), "")

    def test_each_state_gets_its_own_advice(self):
        advice = {state: describe_device_state(state) for state in ADB_STATE_HELP if state}
        self.assertEqual(len(set(advice.values())), len(advice), "two states share one message")
        self.assertIn("Allow USB debugging", advice["unauthorized"])
        self.assertIn("driver", advice["no permissions"])
        self.assertIn("recovery", advice["recovery"])
        self.assertNotIn("Allow USB debugging", advice["offline"])

    def test_unknown_states_get_a_general_instruction(self):
        self.assertIn("\"weird\"", describe_device_state("weird"))

    def test_the_old_name_still_reaches_the_same_table(self):
        self.assertIs(ADB_CONNECTION_HELP, ADB_STATE_HELP)
        self.assertIn("driver", DeviceService.connection_help([{"status": "no permissions"}]))

    def test_transfer_validation_names_the_real_problem(self):
        settings = SimpleNamespace(
            source_root="/sdcard/DCIM", dest_root=".", output_root="", source_is_adb=True,
            adb_serial="R58", transfer_mode="copy",
        )
        with mock.patch.object(engine.ADBBridge, "get_device_state", return_value="recovery"):
            message = engine.validate_transfer_paths(settings)
        self.assertIn("recovery mode", message)
        self.assertNotIn("Authorize USB debugging", message)


if __name__ == "__main__":
    unittest.main()
