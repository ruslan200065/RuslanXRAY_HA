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
    def test_phy_fractional_frequency_parser(self):
        output = 'Band 2:\\n\\tFrequencies:\\n\\t* 5180.0 MHz [36] (23.0 dBm)\\n\\t* 5200 MHz [40] (no IR)\\n'
        parsed=network.parse_phy_channels(output)
        self.assertEqual(parsed[0]['frequency_mhz'],5180.0)
        self.assertEqual(parsed[0]['channel'],36)
        self.assertFalse(parsed[0]['no_ir'])
        self.assertTrue(parsed[1]['no_ir'])
    def test_phy_disabled_and_dfs_flags(self):
        parsed=network.parse_phy_channels(' * 2412 MHz [1] (disabled)\\n * 5260 MHz [52] (radar detection)\\n')
        self.assertTrue(parsed[0]['disabled'])
        self.assertTrue(parsed[1]['radar'])
    def test_5ghz_ap_configuration(self):
        v={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
           'wifi_ssid':'Test5','wifi_password':'12345678','wifi_band':'5','wifi_channel':36}
        self.assertEqual(network.validate_client_settings(v,DEVICES)[0],'wifi_ap')
        result=network.hostapd_config(v)
        self.assertIn('hw_mode=a',result)
        self.assertIn('channel=36',result)
    def test_reject_incompatible_band_channel(self):
        v={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
           'wifi_ssid':'Test5','wifi_password':'12345678','wifi_band':'5','wifi_channel':6}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
    def test_reject_wifi_injection(self):
        v={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
           'wifi_ssid':'Unsafe\nbridge=eth0','wifi_password':'12345678'}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
    def test_dhcp_mask_from_network(self):
        v={**BASE,'gateway_cidr':'192.168.50.1/25','dhcp_start':'192.168.50.10','dhcp_end':'192.168.50.100'}
        self.assertIn('255.255.255.128',network.dnsmasq_config(v))
    def test_global_regulatory_country(self):
        self.assertEqual(network.global_country('global\ncountry 00: DFS-UNSET\nphy#0 (self-managed)\ncountry US: DFS-FCC'), '00')
        self.assertEqual(network.global_country('global\ncountry DE: DFS-ETSI\n'), 'DE')
        self.assertIsNone(network.global_country('phy#0 (self-managed)\ncountry US: DFS-FCC'))

    def test_country_settings_and_ht_config(self):
        cfg={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
             'wifi_ssid':'Test','wifi_password':'12345678',
             'wifi_country':'DE','wifi_apply_regdomain':True}
        self.assertEqual(network.validate_client_settings(cfg,DEVICES)[0], 'wifi_ap')
        hostapd=network.hostapd_config(cfg)
        self.assertIn('country_code=DE',hostapd)
        self.assertIn('ieee80211d=1',hostapd)
        self.assertIn('ieee80211n=1',hostapd)
        cfg['wifi_country']='00'
        with self.assertRaises(ValueError):network.validate_client_settings(cfg,DEVICES)
        cfg['wifi_country']=''
        with self.assertRaises(ValueError):network.validate_client_settings(cfg,DEVICES)

    def test_country_change_opt_in_only(self):
        cfg={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
             'wifi_ssid':'Test','wifi_password':'12345678','wifi_country':'DE'}
        self.assertNotIn('country_code',network.hostapd_config(cfg))
        cfg['wifi_11n']=False
        self.assertNotIn('ieee80211n=1',network.hostapd_config(cfg))

    def test_regulatory_apply_no_mutation_when_same_country(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        calls=[]
        def fake_run(args,check=True,timeout=12):
            calls.append(args)
            return SimpleNamespace(returncode=0,stdout='global\ncountry DE: DFS-ETSI',stderr='')
        with patch.object(network,'run',side_effect=fake_run):
            self.assertIsNone(network.apply_regdomain('DE'))
        self.assertEqual(calls,[['iw','reg','get']])

    def test_regulatory_apply_and_restore_token(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        calls=[]
        def fake_run(args,check=True,timeout=12):
            calls.append(args)
            return SimpleNamespace(returncode=0,stdout='global\ncountry 00: DFS-UNSET',stderr='')
        with patch.object(network,'run',side_effect=fake_run),patch.object(network.time,'sleep'):
            self.assertEqual(network.apply_regdomain('FI'),('FI','00'))
        self.assertEqual(calls,[['iw','reg','get'],['iw','reg','set','FI']])

if __name__=='__main__':unittest.main()
