import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'xray_gateway' / 'app'))

import engine
import latency


class LatencyConfigTest(unittest.TestCase):
    def test_isolates_listener_and_uses_gateway_routing(self):
        profile = {
            'inbounds': [{'tag': 'original', 'protocol': 'http', 'port': 8080}],
            'outbounds': [{'tag': 'vpn', 'protocol': 'vless'}],
            'metrics': {'listen': '127.0.0.1:9090'},
            'log': {'access': '/data/access.log'},
        }
        cfg = latency.probe_config(profile, engine.defaults(), 49152, 'test-password')

        self.assertEqual(len(cfg['inbounds']), 1)
        inbound = cfg['inbounds'][0]
        self.assertEqual(inbound['listen'], '127.0.0.1')
        self.assertEqual(inbound['port'], 49152)
        self.assertEqual(inbound['settings']['auth'], 'password')
        self.assertEqual(cfg['routing']['rules'][0]['inboundTag'], [inbound['tag']])
        self.assertEqual(cfg['routing']['rules'][0]['outboundTag'], 'vpn')
        self.assertNotIn('metrics', cfg)
        self.assertEqual(cfg['log'], {'loglevel': 'none'})
        self.assertEqual(profile['inbounds'][0]['tag'], 'original')
