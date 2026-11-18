import sys,unittest
from pathlib import Path
from unittest.mock import Mock, patch
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
        output = 'Band 2:\n\tFrequencies:\n\t* 5180.0 MHz [36] (23.0 dBm)\n\t* 5200 MHz [40] (no IR)\n'
        parsed=network.parse_phy_channels(output)
        self.assertEqual(parsed[0]['frequency_mhz'],5180.0)
        self.assertEqual(parsed[0]['channel'],36)
        self.assertFalse(parsed[0]['no_ir'])
        self.assertTrue(parsed[1]['no_ir'])
    def test_phy_disabled_and_dfs_flags(self):
        parsed=network.parse_phy_channels(' * 2412 MHz [1] (disabled)\n * 5260 MHz [52] (radar detection)\n')
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


    def test_expanded_channel_ranges(self):
        self.assertEqual(network.WIFI_CHANNELS['2.4'], tuple(range(1,14)))
        self.assertIn(149, network.WIFI_CHANNELS['5'])
        self.assertIn(165, network.WIFI_CHANNELS['5'])
        self.assertIn(100, network.DFS_CHANNELS)
    def test_upper_5ghz_channel(self):
        v={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
           'wifi_ssid':'Test','wifi_password':'12345678','wifi_band':'5','wifi_channel':149}
        self.assertEqual(network.validate_client_settings(v,DEVICES)[0], 'wifi_ap')
        self.assertIn('channel=149',network.hostapd_config(v))
        self.assertNotIn('ieee80211h=1',network.hostapd_config(v))
    def test_dfs_requires_explicit_opt_in_and_country(self):
        v={**BASE,'downstream':'wlan0','client_service':'wifi_ap',
           'wifi_ssid':'Test','wifi_password':'12345678','wifi_band':'5','wifi_channel':100}
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
        v['wifi_allow_dfs']=True
        with self.assertRaises(ValueError):network.validate_client_settings(v,DEVICES)
        v.update(wifi_apply_regdomain=True,wifi_country='FI')
        self.assertEqual(network.validate_client_settings(v,DEVICES)[0], 'wifi_ap')
        self.assertIn('ieee80211h=1',network.hostapd_config(v))
    def test_regdomain_flag_on_dfs(self):
        from unittest.mock import patch
        phy={'supports_ap':True,'phy':'phy0','channels':[{'channel':100,
             'frequency_mhz':5500.0,'disabled':False,'no_ir':True,'radar':True}]}
        with patch.object(network,'radio_capabilities',return_value=phy):
            with self.assertRaises(ValueError):network.verify_ap_radio('wlan0','5',100)
            self.assertTrue(network.verify_ap_radio('wlan0','5',100,allow_dfs=True))
            with self.assertRaises(ValueError):network.verify_ap_radio('wlan0','5',36,allow_dfs=True)
    def test_passive_scan_spelling(self):
        channel = network.parse_phy_channels(' * 5180 MHz [36] (passive-scan)')[0]
        self.assertTrue(channel['no_ir'])

    def test_no_ir_cannot_be_ignored_on_self_managed_radio(self):
        radio = {'supports_ap': True, 'phy': 'phy0', 'driver': 'iwlwifi',
                 'self_managed': True, 'channels': [
                     {'channel': 100, 'frequency_mhz': 5500, 'disabled': False,
                      'no_ir': True, 'radar': True},
                     {'channel': 36, 'frequency_mhz': 5180, 'disabled': False,
                      'no_ir': False, 'radar': False},
                 ]}
        with patch.object(network, 'radio_capabilities', return_value=radio):
            with self.assertRaisesRegex(ValueError, 'no-IR'):
                network.verify_ap_radio('wlan0', '5', 100, allow_dfs=True)
            self.assertFalse(network.verify_ap_radio('wlan0', '5', 36, allow_dfs=True))

    def test_driver_requires_dfs_even_when_channel_is_not_in_default_dfs_list(self):
        radio = {'supports_ap': True, 'phy': 'phy0', 'channels': [
            {'channel': 149, 'frequency_mhz': 5745, 'disabled': False,
             'no_ir': False, 'radar': True},
        ]}
        with patch.object(network, 'radio_capabilities', return_value=radio):
            with self.assertRaisesRegex(ValueError, 'DFS'):
                network.verify_ap_radio('wlan0', '5', 149)
            self.assertTrue(network.verify_ap_radio('wlan0', '5', 149, allow_dfs=True))

    def test_ap_health_requires_enabled_state(self):
        services = network.ClientServices()
        services.ap_device = 'wlan0'
        services.ap_deadline = 10
        services.daemons = [('hostapd', Mock(poll=Mock(return_value=None)))]
        with patch.object(network, 'hostapd_state', side_effect=['HT_SCAN', 'DFS', 'DFS', 'ENABLED', 'DISABLED']):
            with patch.object(network.time, 'monotonic', side_effect=[1, 2, 64, 65, 66]):
                self.assertEqual(services.ap_health()['phase'], 'starting')
                self.assertIn('DFS', services.ap_health()['message'])
                self.assertEqual(services.ap_health()['phase'], 'starting')
                self.assertEqual(services.ap_health()['phase'], 'ready')
                self.assertEqual(services.ap_health()['phase'], 'failed')

    def test_live_hostapd_without_control_response_times_out(self):
        services = network.ClientServices()
        services.ap_device = 'wlan0'
        services.ap_deadline = 10
        with patch.object(network, 'hostapd_state', return_value=''):
            with patch.object(network.time, 'monotonic', return_value=11):
                self.assertEqual(services.ap_health()['phase'], 'failed')

    def test_ethernet_gateway_loses_address_and_carrier(self):
        interface = {**DEVICES[1], 'status': 'UP'}
        phase, error = network.client_network_health(BASE, [interface])
        self.assertEqual(phase, 'failed')
        self.assertIn('192.168.50.1/24', error)
        interface.update(addresses=['192.168.50.1/24'], status='DOWN')
        self.assertEqual(network.client_network_health(BASE, [interface])[0], 'starting')
        interface['status'] = 'UP'
        self.assertEqual(network.client_network_health(BASE, [interface])[0], 'ready')

    def test_networkmanager_never_releases_default_route_port(self):
        with patch.object(network, 'NM_BUS') as bus, patch.object(network, 'nm_device') as lookup:
            bus.is_socket.return_value = True
            with patch.object(network, 'default_route_device', return_value=True):
                with self.assertRaisesRegex(ValueError, 'WAN|маршрутом'):
                    network.release_downstream('eth0', '192.168.50.1/24', False)
            lookup.assert_not_called()

if __name__=='__main__':unittest.main()
