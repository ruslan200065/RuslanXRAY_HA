import importlib.util,sys,unittest
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
if __name__=='__main__':unittest.main()
