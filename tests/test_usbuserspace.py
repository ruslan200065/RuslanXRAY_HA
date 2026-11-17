import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'xray_gateway' / 'app'))
import usbuserspace


class DescriptorTest(unittest.TestCase):
    def test_single_wlan_endpoint(self):
        ep = SimpleNamespace(bEndpointAddress=0x81, bmAttributes=2, wMaxPacketSize=512)
        itf = MagicMock(bInterfaceNumber=0, bAlternateSetting=0, bInterfaceClass=255,
                        bInterfaceSubClass=255, bInterfaceProtocol=255)
        itf.__iter__.return_value = iter([ep])
        cfg = MagicMock(bConfigurationValue=1)
        cfg.__iter__.return_value = iter([itf])
        device = MagicMock(idVendor=0x0bda, idProduct=0xb851, bus=1, address=10,
                           bcdUSB=0x0200, bcdDevice=0)
        device.__iter__.return_value = iter([cfg])
        report = usbuserspace.descriptor_report(device)
        self.assertEqual(report['vendor_id'], '0bda')
        self.assertEqual(report['configurations'][0]['interfaces'][0]['endpoints'][0]['direction'], 'in')

    def test_rejects_wrong_usb_device(self):
        with self.assertRaises(ValueError):
            usbuserspace.RTL8851BUTransport(SimpleNamespace(idVendor=0x0bda,idProduct=0x1234))

    def test_transport_unclaimed_refuses_io(self):
        device = SimpleNamespace(idVendor=0x0bda, idProduct=0xb851)
        transport = usbuserspace.RTL8851BUTransport(device)
        with self.assertRaises(RuntimeError):
            transport.bulk_read(0x81, 64)
        with self.assertRaises(RuntimeError):
            transport.bulk_write(0x01, b'test')

    def test_writes_validate_direction(self):
        dev = MagicMock(idVendor=0x0bda, idProduct=0xb851)
        t = usbuserspace.RTL8851BUTransport(dev)
        t.claimed = True
        with self.assertRaises(ValueError):
            t.bulk_write(0x81,b'x')
        with self.assertRaises(ValueError):
            t.bulk_read(0x01,10)
        dev.write.assert_not_called()
        dev.read.assert_not_called()

    def test_usb_mount_required(self):
        with patch.object(usbuserspace.Path, 'is_dir', return_value=False):
            status = usbuserspace.probe()
        self.assertEqual(status['status'], 'usb_not_mounted')
        self.assertFalse(status['ap_ready'])

if __name__ == '__main__':
    unittest.main()
