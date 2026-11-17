import unittest
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'xray_gateway'/'app'))
import usbwifi

class USBDriverTest(unittest.TestCase):
    def test_not_connected(self):
        with patch.object(usbwifi, 'discover', return_value=[]), patch.object(
                usbwifi, 'execute', return_value={'returncode':1,'stdout':'','stderr':'not found'}):
            s=usbwifi.state()
        self.assertEqual(s['status'],'not_connected')
    def test_module_missing(self):
        with patch.object(usbwifi,'discover',return_value=[{'interfaces':[]}]), patch.object(
                usbwifi,'execute',return_value={'returncode':1,'stdout':'','stderr':'not found'}):
            s=usbwifi.state()
        self.assertEqual(s['status'],'module_missing')
    def test_module_ready(self):
        with patch.object(usbwifi,'discover',return_value=[{'interfaces':[]}]), patch.object(
                usbwifi,'execute',return_value={'returncode':0,'stdout':'/lib/modules/foo.ko\n','stderr':''}):
            s=usbwifi.state()
        self.assertEqual(s['status'],'module_ready')
    def test_activate_no_device(self):
        with patch.object(usbwifi,'state',return_value={'devices':[],'status':'not_connected'}),self.assertRaises(ValueError):
            usbwifi.activate()
    def test_activate_missing_module(self):
        old={'devices':[{'interfaces':[]}], 'status':'module_missing','module_available':False,'module_loaded':False,'message':'No module'}
        with patch.object(usbwifi,'state',return_value=old),self.assertRaisesRegex(ValueError,'No module'):
            usbwifi.activate()
    def test_activate_safe_fixed_module(self):
        old={'devices':[{'interfaces':[]}], 'status':'module_ready','module_available':True,'module_loaded':False}
        after={'devices':[{'interfaces':['wlx123']}], 'status':'ready'}
        with patch.object(usbwifi,'state',side_effect=[old,after]),patch.object(
                usbwifi,'execute',return_value={'returncode':0,'stdout':'','stderr':''}) as run,patch.object(usbwifi.time,'sleep'):
            result=usbwifi.activate()
        run.assert_called_once_with(['modprobe','rtw89_8851bu'],timeout=20)
        self.assertEqual(result['status'],'ready')

if __name__ == '__main__':
    unittest.main()
