import sys
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'xray_gateway' / 'app'))
import rtw8851b_firmware as fw


def image(header_version=1, section_count=2):
    words = [0] * 12
    words[1] = (5 << 24) | (41 << 16) | (29 << 8) | 0
    words[2] = 0x12345678
    words[3] = header_version << 24
    words[6] = section_count << 8
    return struct.pack("<12I", *words) + b"\x00" * 256


class FirmwareTest(unittest.TestCase):
    def test_header_version_and_section(self):
        info = fw.parse_header(image())
        self.assertEqual(info["header_version"], 1)
        self.assertEqual(info["section_count"], 2)
        self.assertEqual(info["firmware_version"], "0.29.41.5")

    def test_reject_short_file(self):
        with self.assertRaises(ValueError):
            fw.parse_header(b"\x00" * 8)

    def test_reject_invalid_section_count(self):
        with self.assertRaises(ValueError):
            fw.parse_header(image(section_count=0))

    def test_inventory_real_file(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "rtw8851b_fw.bin"
            path.write_bytes(image())
            with patch.object(fw, "FIRMWARE_DIRS", (Path(d),)):
                result = fw.inventory()
        self.assertTrue(result["firmware_available"])
        self.assertFalse(result["firmware_uploaded_to_device"])
        self.assertEqual(len(result["firmware_candidates"][0]["sha256"]), 64)

    def test_no_firmware(self):
        with TemporaryDirectory() as d:
            with patch.object(fw, "FIRMWARE_DIRS", (Path(d),)):
                result = fw.inventory()
        self.assertFalse(result["firmware_available"])

if __name__ == "__main__":
    unittest.main()
