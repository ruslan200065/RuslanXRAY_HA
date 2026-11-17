import base64, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'xray_gateway'/'app'))
import subscriptions

class TestSubscriptions(unittest.TestCase):
    def test_remnawave_path(self):
        self.assertEqual(subscriptions.native_url('https://example.org/sub/123?key=x'),'https://example.org/sub/123/json?key=x')
        self.assertEqual(subscriptions.native_url('https://example.org/sub/123/json?key=x'),'https://example.org/sub/123/json?key=x')
        self.assertEqual(subscriptions.native_url('https://example.org/sub/123',False),'https://example.org/sub/123')
    def test_bad_urls(self):
        for u in ('file:///etc/passwd','https://example.org/a\nInjected: yes','https://user:pass@example.org/a','https://example.org/a#fragment'):
            with self.subTest(u=u),self.assertRaises(ValueError):subscriptions.validate_url(u)
    def test_json_and_base64(self):
        profiles=[{'outbounds':[{'tag':'proxy','protocol':'vless'}],'remarks':'Node 1'}]
        raw=json.dumps(profiles).encode()
        self.assertEqual(subscriptions.parse_catalog(raw),profiles)
        self.assertEqual(subscriptions.parse_catalog(base64.b64encode(raw)),profiles)
    def test_reject_invalid(self):
        for payload in (b'{}',b'[]',b'{"outbounds":[]}'):
            with self.subTest(payload=payload),self.assertRaises(ValueError):subscriptions.parse_catalog(payload)

if __name__=='__main__':unittest.main()
