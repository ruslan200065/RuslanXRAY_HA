import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'xray_gateway'/'app'))
import network
DEVICES=[{'name':'eth0','is_default':True,'addresses':['192.168.0.100/24'],'wifi':False},
         {'name':'eth1','is_default':False,'addresses':[],'wifi':False},
         {'name':'wlan0','is_default':False,'addresses':[],'wifi':True}]
BASE={'downstream':'eth1','gateway_cidr':'192.168.50.1/24','client_service':'dhcp',
      'dhcp_start':'192.168.50.100','dhcp_end':'192.168.50.200'}
class NetworkTest(unittest.TestCase):
    def test_valid_dhcp(self):
        self.assertEqual(network.validate_client_settings(BASE,DEVICES)[0],'dhcp')
        self.assertIn('dhcp-range=192.168.50.100,192.168.50.200,255.255.255.0',network.dnsmasq_config(BASE))
    def test_prevent_overlap(self):
        v={**BASE,'gateway_cidr':'192.168.0.1/24'}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
    def test_prevent_dhcp_gateway_collision(self):
        v={**BASE,'gateway_cidr':'192.168.50.100/24'}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
    def test_wifi_requires_wireless(self):
        v={**BASE,'client_service':'wifi_ap','wifi_ssid':'Test','wifi_password':'12345678'}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
        v['downstream']='wlan0'
        self.assertEqual(network.validate_client_settings(v,DEVICES)[0],'wifi_ap')
        self.assertIn('ssid2=54657374',network.hostapd_config(v))
    def test_reject_wifi_injection(self):
        v={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
           'wifi_ssid':'Unsafe\nbridge=eth0','wifi_password':'12345678'}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
    def test_dhcp_mask_from_network(self):
        v={**BASE,'gateway_cidr':'192.168.50.1/25','dhcp_start':'192.168.50.10','dhcp_end':'192.168.50.100'}
        self.assertIn('255.255.255.128',network.dnsmasq_config(v))
if __name__=='__main__':unittest.main()
