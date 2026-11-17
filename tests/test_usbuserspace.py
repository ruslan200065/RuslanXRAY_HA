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


    def test_actual_rtl8851bu_endpoint_map(self):
        """Based on the physical 0bda:b851 descriptor supplied by the user."""
        report={
            'configurations':[{
                'value':1,
                'interfaces':[
                    {'number':0,'alternate_setting':0,'class':'e0',
                     'subclass':'01','protocol':'01','endpoints':[
                        {'address':129,'direction':'in','type':'interrupt'},
                        {'address':2,'direction':'out','type':'bulk'}]},
                    {'number':1,'alternate_setting':0,'class':'e0',
                     'subclass':'01','protocol':'01','endpoints':[
                        {'address':3,'direction':'out','type':'isochronous'}]},
                    {'number':2,'alternate_setting':0,'class':'ff',
                     'subclass':'ff','protocol':'ff','endpoints':[
                        {'address':132,'direction':'in','type':'bulk'},
                        *[{'address':n,'direction':'out','type':'bulk'}
                          for n in (5,6,7,9,10,11,12)]},
                ],
            }],
        }
        found=usbuserspace.wlan_candidates(report)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['interface_number'],2)
        self.assertEqual(found[0]['bulk_in'],[0x84])
        self.assertEqual(found[0]['bulk_out'],[5,6,7,9,10,11,12])
        self.assertTrue(found[0]['valid_rtw89_topology'])

    def test_bluetooth_endpoints_not_eligible(self):
        report={'configurations':[{'value':1,'interfaces':[{
            'number':0,'alternate_setting':0,'class':'e0','subclass':'01',
            'protocol':'01','endpoints':[
                {'address':0x82,'direction':'in','type':'bulk'},
                {'address':0x02,'direction':'out','type':'bulk'}]}]}]}
        self.assertEqual(usbuserspace.wlan_candidates(report), [])

    def test_invalid_vendor_topology_not_allowed(self):
        report={'configurations':[{'value':1,'interfaces':[{
            'number':2,'alternate_setting':0,'class':'ff','subclass':'ff',
            'protocol':'ff','endpoints':[
                {'address':5,'direction':'out','type':'bulk'}]}]}]}
        result=usbuserspace.wlan_candidates(report)
        self.assertEqual(len(result),1)
        self.assertFalse(result[0]['valid_rtw89_topology'])


    def test_read_power_register_uses_upstream_rtw89_usb_protocol(self):
        device = MagicMock(idVendor=0x0bda,idProduct=0xb851)
        device.ctrl_transfer.return_value = bytes((0x78,0x56,0x34,0x12))
        transport=usbuserspace.RTL8851BUTransport(device)
        transport.claimed=True
        result=transport.read_sys_pw_ctrl()
        device.ctrl_transfer.assert_called_once_with(0xC0,0x05,0x0004,0,4,timeout=500)
        self.assertEqual(result['value'],'0x12345678')
        self.assertEqual(result['raw_le'],'78563412')

    def test_register_read_denied_until_claimed(self):
        device = MagicMock(idVendor=0x0bda,idProduct=0xb851)
        transport=usbuserspace.RTL8851BUTransport(device)
        with self.assertRaises(RuntimeError):
            transport.read_sys_pw_ctrl()
        device.ctrl_transfer.assert_not_called()

    def test_register_read_rejects_short_response(self):
        device = MagicMock(idVendor=0x0bda,idProduct=0xb851)
        device.ctrl_transfer.return_value = bytes((0x12,0x34))
        transport=usbuserspace.RTL8851BUTransport(device)
        transport.claimed=True
        with self.assertRaises(IOError):
            transport.read_sys_pw_ctrl()


    def test_decode_power_flags_from_real_device(self):
        flags=usbuserspace.decode_power_flags(0x50030082)
        self.assertTrue(flags['sys_power_ready'])
        self.assertTrue(flags['wlan_power_enabled'])
        self.assertTrue(flags['power_swr_enabled'])
        self.assertFalse(flags['mac_on_requested'])
        self.assertFalse(flags['mac_off_requested'])
        self.assertFalse(flags['wlan_suspend_enabled'])

    def test_fixed_allowlisted_snapshot_registers(self):
        self.assertEqual(set(usbuserspace.RTL8851BU_SNAPSHOT_REGISTERS),
                         {0x0004,0x0008,0x1060,0x1068,0x1078})
        device=MagicMock(idVendor=0x0bda,idProduct=0xb851)
        transport=usbuserspace.RTL8851BUTransport(device)
        transport.claimed=True
        device.ctrl_transfer.return_value = b'\x82\x00\x03\x50'
        value=transport.read_register(0x0004)
        self.assertEqual(value['value'],'0x50030082')
        device.ctrl_transfer.assert_called_once_with(0xc0,0x05,0x0004,0,4,timeout=500)
        with self.assertRaises(ValueError):
            transport.read_register(0x8400)


    def test_rtw89_write32_encoding_only_with_claim(self):
        device=MagicMock(idVendor=0x0bda,idProduct=0xb851)
        device.ctrl_transfer.return_value=4
        transport=usbuserspace.RTL8851BUTransport(device)
        with self.assertRaises(RuntimeError):
            transport.write_register32(0x0004, 0x50030082)
        device.ctrl_transfer.assert_not_called()
        transport.claimed=True
        count=transport.write_register32(0x1078, 0x12345678)
        self.assertEqual(count,4)
        device.ctrl_transfer.assert_called_once_with(
            0x40, 0x05, 0x1078, 0, b'\x78\x56\x34\x12', timeout=500)

    def test_rtw89_write32_validates_range(self):
        device=MagicMock(idVendor=0x0bda,idProduct=0xb851)
        transport=usbuserspace.RTL8851BUTransport(device)
        transport.claimed=True
        with self.assertRaises(ValueError):
            transport.write_register32(0x1000000, 0)
        with self.assertRaises(ValueError):
            transport.write_register32(0x0004, 0x100000000)
        device.ctrl_transfer.assert_not_called()

if __name__ == '__main__':
    unittest.main()
