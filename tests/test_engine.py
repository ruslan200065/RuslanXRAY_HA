import subprocess,sys,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'xray_gateway'/'app'))
import engine
class EngineTest(unittest.TestCase):
    def test_full_profile_preservation(self):
        src={'outbounds':[{'tag':'a','protocol':'vmess'}],'routing':{'balancers':[{'tag':'b','selector':['a']}], 'rules':[]},'inbounds':[{'protocol':'socks','listen':'0.0.0.0','port':1000}]}
        out=engine.build_profile(src,12345)
        self.assertEqual(out['routing']['balancers'],src['routing']['balancers'])
        self.assertEqual(out['routing']['rules'][0]['balancerTag'],'b')
        self.assertEqual(out['outbounds'],src['outbounds'])
        self.assertEqual(src['inbounds'][0]['listen'],'0.0.0.0')
        self.assertEqual(out['inbounds'][0]['listen'],'127.0.0.1')
        self.assertEqual(out['inbounds'][-1]['protocol'],'dokodemo-door')
    def test_no_open_forward(self):
        r=engine.nft_rules('eth1',12345,True)
        self.assertIn('iifname "eth1" drop',r)
        self.assertIn('meta nfproto ipv6 drop',r)
        self.assertNotIn('masquerade',r)
    def test_reject_same_interface(self):
        with patch.object(engine,'interfaces',return_value=[{'name':'eth0','is_default':True,'addresses':['10.0.0.2/24']}]):
            with self.assertRaises(ValueError):engine.validate_settings({'upstream':'eth0','downstream':'eth0','port':12345})
    def test_reject_upstream_as_downstream(self):
        with patch.object(engine,'interfaces',return_value=[{'name':'eth0','is_default':True,'addresses':['10.0.0.2/24']},{'name':'eth1','is_default':False,'addresses':['192.168.50.1/24']} ]):
            with self.assertRaises(ValueError):engine.validate_settings({'upstream':'eth1','downstream':'eth0','port':12345})
    def test_reject_invalid_profile(self):
        with self.assertRaises(ValueError):engine.build_profile({'inbounds':[]},12345)

    def test_failed_route_add_leaves_existing_routes_untouched(self):
        success = subprocess.CompletedProcess([], 0)
        missing_table = subprocess.CompletedProcess([], 1)
        collision = subprocess.CalledProcessError(2, ['ip', 'route', 'add'])
        with patch.object(engine, 'validate_settings', return_value=('eth0', 'eth1', 12345)):
            with patch.object(engine, 'run', side_effect=[success, missing_table, collision]) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    engine.apply_rules({})
        self.assertEqual(run.call_count, 3)

    def test_local_lan_access_allows_only_selected_subnets(self):
        settings = {**engine.defaults(), 'upstream': 'eth0', 'downstream': 'eth1'}
        paths = engine.local_lan_paths(settings, [{'name': 'eth0', 'addresses': ['192.168.0.89/24']}])
        rules = engine.nft_rules('eth1', 12345, lan_paths=paths)
        self.assertIn('iifname "eth1" oifname "eth0" ip saddr 192.168.50.0/24 ip daddr 192.168.0.0/24 accept', rules)
        self.assertIn('iifname "eth0" oifname "eth1" ip saddr 192.168.0.0/24 ip daddr 192.168.50.0/24 accept', rules)
        self.assertIn('iifname "eth1" drop', rules)
        self.assertIn('tproxy ip to :12345', rules)
        self.assertNotIn('oifname "eth0"', engine.nft_rules('eth1', 12345))

if __name__=='__main__':unittest.main()
