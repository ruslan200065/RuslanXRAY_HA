import base64, json, sys, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
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

    def test_hwid_survives_reloads_and_is_unique_per_installation(self):
        with tempfile.TemporaryDirectory() as root:
            saved = Path(root) / 'one' / 'hwid'
            with patch.object(subscriptions, 'HWID', saved):
                first = subscriptions.hwid()
                self.assertEqual(uuid.UUID(first).version, 4)
                self.assertEqual(saved.read_text().strip(), first)
                self.assertEqual(subscriptions.hwid(), first)
            with patch.object(subscriptions, 'HWID', Path(root) / 'two' / 'hwid'):
                self.assertNotEqual(subscriptions.hwid(), first)

    def test_subscription_always_sends_saved_hwid(self):
        with tempfile.TemporaryDirectory() as root:
            saved = Path(root) / 'hwid'
            original = str(uuid.uuid4()).upper()
            saved.write_text(original + '\n', encoding='ascii')
            with patch.object(subscriptions, 'HWID', saved):
                for remnawave in (True, False):
                    cfg = {'url': 'https://example.org/sub', 'user_agent': 'test', 'remnawave': remnawave}
                    with patch.object(subscriptions, 'config', return_value=cfg):
                        with patch.object(subscriptions, 'urlopen', side_effect=URLError('test')) as opener:
                            with self.assertRaises(URLError):
                                subscriptions.update_catalog()
                            request = opener.call_args.args[0]
                            headers = {key.lower(): value for key, value in request.header_items()}
                            self.assertEqual(headers['x-hwid'], original)

    def test_hwid_repairs_corrupt_file(self):
        with tempfile.TemporaryDirectory() as root:
            saved = Path(root) / 'hwid'
            saved.write_bytes(b'corrupt\xff')
            with patch.object(subscriptions, 'HWID', saved):
                repaired = subscriptions.hwid()
                self.assertEqual(uuid.UUID(repaired).version, 4)
                self.assertEqual(subscriptions.hwid(), repaired)

    def test_refresh_keeps_selected_profile_when_order_changes(self):
        first = {'remarks': 'First', 'outbounds': [{'protocol': 'vless'}]}
        second = {**first, 'remarks': 'Second'}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.multiple(subscriptions, CATALOG=root/'legacy', CATALOG_SNAPSHOT=root/'catalog.json',
                                SELECTED=root/'selected.json', CONFIG=root/'config.json'):
                with patch.object(subscriptions.engine, 'PROFILE', root/'active.json'):
                    subscriptions.save_json(subscriptions.CATALOG/'0.json', first)
                    subscriptions.save_json(subscriptions.CATALOG/'1.json', second)
                    subscriptions.save_json(subscriptions.SELECTED, {'id': 0})
                    subscriptions.save_json(subscriptions.engine.PROFILE, first)
                    self.assertFalse(subscriptions.replace_catalog([second, first], 0)['selection_lost'])
                    self.assertEqual(subscriptions.list_catalog()['selected'], 1)
                    self.assertEqual(subscriptions.load(subscriptions.engine.PROFILE, None), first)
                    with self.assertRaisesRegex(ValueError, 'Каталог изменился'):
                        subscriptions.get_profile(0, subscriptions.profile_fingerprint(first))

    def test_matching_uses_unique_provider_id_for_updates(self):
        old = {'id': 'unique', 'remarks': 'Paris', 'outbounds': [{'protocol': 'vless'}]}
        replacement = {**old, 'remarks': 'Paris renewed'}
        self.assertEqual(subscriptions.matching_profile(old, {0: old}, [replacement]), 0)
        self.assertIsNone(subscriptions.matching_profile(old, {0: old}, [replacement, replacement]))

    def test_fetch_skips_invalid_profiles(self):
        from unittest.mock import MagicMock
        good = {'outbounds': [{'tag': 'vpn', 'protocol': 'vless'}]}
        bad = {'outbounds': [{'tag': 'fail', 'protocol': 'vless'}]}
        response = MagicMock()
        response.geturl.return_value = 'https://example.org/sub'
        response.read.return_value = json.dumps([None, bad, good]).encode()
        cfg = {'url': 'https://example.org/sub', 'user_agent': 'test', 'remnawave': False}
        with patch.object(subscriptions, 'hwid', return_value='id'), \
             patch.object(subscriptions, 'urlopen') as opener, \
             patch.object(subscriptions.engine, 'verify_profile', side_effect=[ValueError('bad'), None]):
            opener.return_value.__enter__.return_value = response
            self.assertEqual(subscriptions.fetch_catalog(cfg, subscriptions.engine.defaults()), ([good], 2))

if __name__=='__main__':unittest.main()
