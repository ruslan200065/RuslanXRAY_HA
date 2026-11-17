"""Opt-in client network services. Never edits NetworkManager's persistent configuration."""
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path

NET_RE = re.compile(r'^[a-zA-Z0-9_.:-]{1,15}$')
DATA = Path(os.environ.get('GATEWAY_DATA', '/data/ruslan-gateway'))
RUNTIME = DATA / 'runtime'
WIFI_CHANNELS = {'2.4': (1, 6, 11), '5': (36, 40, 44, 48)}



def run(argv, *, check=True, timeout=12):
    return subprocess.run(argv, capture_output=True, text=True, check=check, timeout=timeout)


def validate_client_settings(cfg, interfaces, current_routes=None):
    """Static address is added temporarily only if interface is otherwise unnumbered."""
    mode = cfg.get('client_service', 'manual')
    if mode not in ('manual', 'dhcp', 'wifi_ap'):
        raise ValueError('Неизвестный режим клиентского интерфейса')
    dev = cfg.get('downstream', '')
    known = {x['name']: x for x in interfaces}
    if dev not in known:
        raise ValueError('Не найден клиентский интерфейс')
    if known[dev]['is_default']:
        raise ValueError('Нельзя управлять WAN/default интерфейсом как клиентским')
    network = ipaddress.ip_interface(cfg.get('gateway_cidr', '192.168.50.1/24'))
    if network.version != 4 or network.network.prefixlen < 16 or network.network.prefixlen > 29:
        raise ValueError('Разрешена только IPv4-сеть с маской /16…/29')
    if network.ip == network.network.network_address or network.ip == network.network.broadcast_address:
        raise ValueError('Адрес шлюза не должен быть адресом сети или broadcast')
    for it in interfaces:
        for addr in it.get('addresses', []):
            existing = ipaddress.ip_interface(addr)
            if existing.version != 4:
                continue
            if it['name'] != dev and existing.network.overlaps(network.network):
                raise ValueError('Выбранная подсеть пересекается с сетью другого интерфейса: ' + it['name'])
            if it['name'] == dev and existing != network:
                raise ValueError('У интерфейса уже другой IP. Используйте совпадающий адрес или очистите его в HAOS')
    if current_routes:
        for route in current_routes:
            if route.get('dev') != dev and route.get('dst') not in (None, 'default'):
                try:
                    if ipaddress.ip_network(route['dst'], strict=False).overlaps(network.network):
                        raise ValueError('Подсеть пересекается с маршрутом через ' + str(route.get('dev')))
                except ValueError as exc:
                    if 'Подсеть пересекается' in str(exc):
                        raise
    if mode == 'wifi_ap':
        if not known[dev].get('wifi', False):
            raise ValueError('Для точки доступа нужен настоящий Wi-Fi-интерфейс')
        ssid = cfg.get('wifi_ssid', 'RuslanXRAY')
        password = cfg.get('wifi_password', '')
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32 or '\n' in ssid or '\r' in ssid:
            raise ValueError('SSID должен содержать 1–32 байта без переводов строки')
        if not isinstance(password, str) or not 8 <= len(password) <= 63 or not password.isascii() or '\n' in password:
            raise ValueError('Пароль Wi-Fi: 8–63 ASCII-символа')
        band = str(cfg.get('wifi_band', '2.4'))
        if band not in WIFI_CHANNELS:
            raise ValueError('Диапазон Wi-Fi должен быть 2.4 или 5 ГГц')
        channel = int(cfg.get('wifi_channel', 6))
        if channel not in WIFI_CHANNELS[band]:
            raise ValueError(f'Канал {channel} недопустим для диапазона {band} ГГц')
        # An adapter being Wi-Fi does not imply that it supports AP mode on 5 GHz.
        # Check the selected PHY before starting, not during settings validation.
    if mode in ('dhcp', 'wifi_ap'):
        start = ipaddress.ip_address(cfg.get('dhcp_start', '192.168.50.100'))
        end = ipaddress.ip_address(cfg.get('dhcp_end', '192.168.50.200'))
        if (start not in network.network or end not in network.network or int(start)>int(end)
                or network.ip in (start, end) or int(start) <= int(network.ip) <= int(end)):
            raise ValueError('Диапазон DHCP должен находиться внутри подсети и не содержать IP шлюза')
        for ip in (start, end):
            if ip in (network.network.network_address, network.network.broadcast_address):
                raise ValueError('DHCP диапазон содержит адрес сети/broadcast')
    return mode, dev, network


def verify_ap_radio(dev, band, channel):
    """Fail clearly if selected radio/PHY cannot operate as an AP on this channel."""
    info = run(['iw', 'dev', dev, 'info'], check=False)
    match = re.search(r'\\bwiphy\\s+(\\d+)', info.stdout)
    if info.returncode != 0 or not match:
        raise ValueError('Не удалось определить Wi-Fi PHY для '+dev+': '+info.stderr[-300:])
    phy = run(['iw', 'phy', 'phy'+match.group(1), 'info'], check=False)
    if phy.returncode != 0:
        raise ValueError('Не удалось получить возможности Wi-Fi адаптера: '+phy.stderr[-300:])
    if re.search(r'^\\s*\\* AP\\s*
    """2.4 / 5 GHz WPA2 CCMP; restrict 5 GHz to non-DFS channels."""
    dev=cfg['downstream']
    ssid=cfg['wifi_ssid']
    password=cfg['wifi_password']
    channel=int(cfg.get('wifi_channel',6))
    band=str(cfg.get('wifi_band','2.4'))
    if band not in WIFI_CHANNELS or channel not in WIFI_CHANNELS[band]:
        raise ValueError('Некорректный диапазон или канал Wi-Fi')
    hw_mode='a' if band == '5' else 'g'
    # hex SSID supports UTF-8 and avoids newline/escape injection
    return (f'interface={dev}\ndriver=nl80211\nssid2={ssid.encode().hex()}\n'
            f'hw_mode={hw_mode}\nchannel={channel}\nwmm_enabled=1\n'
            f'auth_algs=1\nwpa=2\nwpa_passphrase={password}\nwpa_key_mgmt=WPA-PSK\n'
            f'rsn_pairwise=CCMP\n')


def dnsmasq_config(cfg):
    # DNS supplied via DHCP only; never binds host port 53.
    gw=ipaddress.ip_interface(cfg['gateway_cidr']).ip
    mask=ipaddress.ip_interface(cfg['gateway_cidr']).network.netmask
    return (f'interface={cfg["downstream"]}\nbind-interfaces\n'
            'port=0\ndhcp-authoritative\n'
            f'dhcp-range={cfg["dhcp_start"]},{cfg["dhcp_end"]},{mask},12h\n'
            f'dhcp-option=option:router,{gw}\ndhcp-option=option:dns-server,1.1.1.1,9.9.9.9\n'
            'log-dhcp\n')


class ClientServices:
    def __init__(self):
        self.daemons=[]
        self.owned_ip=None
        self.owned_link=None

    def start(self, cfg, interfaces):
        mode,dev,addr=validate_client_settings(cfg,interfaces)
        if mode == 'manual':
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                raise ValueError('В ручном режиме назначьте IP шлюза в HAOS')
            return
        RUNTIME.mkdir(parents=True,exist_ok=True)
        os.chmod(RUNTIME,0o700)
        try:
            # A Wi-Fi adapter can be administratively DOWN in HAOS. Bring up only
            # the selected, dedicated client radio and restore its original state.
            if mode == 'wifi_ap':
                verify_ap_radio(dev, str(cfg.get('wifi_band','2.4')), int(cfg.get('wifi_channel',6)))
                link = json.loads(run(['ip', '-j', 'link', 'show', 'dev', dev]).stdout)[0]
                if 'UP' not in link.get('flags', []):
                    try:
                        run(['ip', 'link', 'set', 'dev', dev, 'up'])
                    except subprocess.CalledProcessError as exc:
                        raise ValueError('Не удалось включить Wi-Fi '+dev+
                            ': '+(exc.stderr or str(exc))+
                            '. Проверьте rfkill, драйвер и NetworkManager.')
                    self.owned_link=dev
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                run(['ip','-4','address','add',str(addr),'dev',dev])
                self.owned_ip=(str(addr),dev)
            if mode == 'wifi_ap':
                (RUNTIME/'hostapd.conf').write_text(hostapd_config(cfg))
                os.chmod(RUNTIME/'hostapd.conf',0o600)
                self._launch(['hostapd',str(RUNTIME/'hostapd.conf')], 'hostapd')
            (RUNTIME/'dnsmasq.conf').write_text(dnsmasq_config(cfg))
            self._launch(['dnsmasq','--keep-in-foreground','--conf-file='+str(RUNTIME/'dnsmasq.conf'),
                         '--pid-file='+str(RUNTIME/'dnsmasq.pid')], 'dnsmasq')
        except Exception:
            self.stop()
            raise

    def _launch(self,cmd,label):
        log=open(RUNTIME/(label+'.log'),'ab')
        try:
            p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
        finally:
            log.close()
        self.daemons.append((label,p))
        time.sleep(.35)
        if p.poll() is not None:
            raise ValueError(label+' не запустился: '+(RUNTIME/(label+'.log')).read_text(errors='replace')[-1200:])

    def alive(self):
        return all(p.poll() is None for _,p in self.daemons)

    def stop(self):
        for _,p in reversed(self.daemons):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        self.daemons.clear()
        if self.owned_ip:
            cidr,dev=self.owned_ip
            run(['ip','-4','address','del',cidr,'dev',dev],check=False)
            self.owned_ip=None
        if self.owned_link:
            run(['ip','link','set','dev',self.owned_link,'down'],check=False)
            self.owned_link=None
, phy.stdout, flags=re.M) is None:
        raise ValueError('Wi-Fi адаптер не поддерживает режим точки доступа (AP)')
    freq = 5000 + 5*channel if band == '5' else 2407 + 5*channel
    pattern = rf'^.*?\\[\\s*{channel}\\s*\\].*
    """2.4 / 5 GHz WPA2 CCMP; restrict 5 GHz to non-DFS channels."""
    dev=cfg['downstream']
    ssid=cfg['wifi_ssid']
    password=cfg['wifi_password']
    channel=int(cfg.get('wifi_channel',6))
    band=str(cfg.get('wifi_band','2.4'))
    if band not in WIFI_CHANNELS or channel not in WIFI_CHANNELS[band]:
        raise ValueError('Некорректный диапазон или канал Wi-Fi')
    hw_mode='a' if band == '5' else 'g'
    # hex SSID supports UTF-8 and avoids newline/escape injection
    return (f'interface={dev}\ndriver=nl80211\nssid2={ssid.encode().hex()}\n'
            f'hw_mode={hw_mode}\nchannel={channel}\nwmm_enabled=1\n'
            f'auth_algs=1\nwpa=2\nwpa_passphrase={password}\nwpa_key_mgmt=WPA-PSK\n'
            f'rsn_pairwise=CCMP\n')


def dnsmasq_config(cfg):
    # DNS supplied via DHCP only; never binds host port 53.
    gw=ipaddress.ip_interface(cfg['gateway_cidr']).ip
    mask=ipaddress.ip_interface(cfg['gateway_cidr']).network.netmask
    return (f'interface={cfg["downstream"]}\nbind-interfaces\n'
            'port=0\ndhcp-authoritative\n'
            f'dhcp-range={cfg["dhcp_start"]},{cfg["dhcp_end"]},{mask},12h\n'
            f'dhcp-option=option:router,{gw}\ndhcp-option=option:dns-server,1.1.1.1,9.9.9.9\n'
            'log-dhcp\n')


class ClientServices:
    def __init__(self):
        self.daemons=[]
        self.owned_ip=None
        self.owned_link=None

    def start(self, cfg, interfaces):
        mode,dev,addr=validate_client_settings(cfg,interfaces)
        if mode == 'manual':
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                raise ValueError('В ручном режиме назначьте IP шлюза в HAOS')
            return
        RUNTIME.mkdir(parents=True,exist_ok=True)
        os.chmod(RUNTIME,0o700)
        try:
            # A Wi-Fi adapter can be administratively DOWN in HAOS. Bring up only
            # the selected, dedicated client radio and restore its original state.
            if mode == 'wifi_ap':
                link = json.loads(run(['ip', '-j', 'link', 'show', 'dev', dev]).stdout)[0]
                if 'UP' not in link.get('flags', []):
                    try:
                        run(['ip', 'link', 'set', 'dev', dev, 'up'])
                    except subprocess.CalledProcessError as exc:
                        raise ValueError('Не удалось включить Wi-Fi '+dev+
                            ': '+(exc.stderr or str(exc))+
                            '. Проверьте rfkill, драйвер и NetworkManager.')
                    self.owned_link=dev
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                run(['ip','-4','address','add',str(addr),'dev',dev])
                self.owned_ip=(str(addr),dev)
            if mode == 'wifi_ap':
                (RUNTIME/'hostapd.conf').write_text(hostapd_config(cfg))
                os.chmod(RUNTIME/'hostapd.conf',0o600)
                self._launch(['hostapd',str(RUNTIME/'hostapd.conf')], 'hostapd')
            (RUNTIME/'dnsmasq.conf').write_text(dnsmasq_config(cfg))
            self._launch(['dnsmasq','--keep-in-foreground','--conf-file='+str(RUNTIME/'dnsmasq.conf'),
                         '--pid-file='+str(RUNTIME/'dnsmasq.pid')], 'dnsmasq')
        except Exception:
            self.stop()
            raise

    def _launch(self,cmd,label):
        log=open(RUNTIME/(label+'.log'),'ab')
        try:
            p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
        finally:
            log.close()
        self.daemons.append((label,p))
        time.sleep(.35)
        if p.poll() is not None:
            raise ValueError(label+' не запустился: '+(RUNTIME/(label+'.log')).read_text(errors='replace')[-1200:])

    def alive(self):
        return all(p.poll() is None for _,p in self.daemons)

    def stop(self):
        for _,p in reversed(self.daemons):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        self.daemons.clear()
        if self.owned_ip:
            cidr,dev=self.owned_ip
            run(['ip','-4','address','del',cidr,'dev',dev],check=False)
            self.owned_ip=None
        if self.owned_link:
            run(['ip','link','set','dev',self.owned_link,'down'],check=False)
            self.owned_link=None

    entries = [line.lower() for line in phy.stdout.splitlines() if re.match(pattern, line) and f'{freq} mhz' in line.lower()]
    if not entries:
        raise ValueError(f'Адаптер не поддерживает канал {channel} диапазона {band} ГГц')
    if any(('[disabled]' in line or '(disabled)' in line or 'no ir' in line or 'no-ir' in line) for line in entries):
        raise ValueError(f'Канал {channel} запрещён для AP текущим Wi-Fi regulatory domain. Настройте страну в HAOS/драйвере.')
    return True


def hostapd_config(cfg):
    """2.4 / 5 GHz WPA2 CCMP; restrict 5 GHz to non-DFS channels."""
    dev=cfg['downstream']
    ssid=cfg['wifi_ssid']
    password=cfg['wifi_password']
    channel=int(cfg.get('wifi_channel',6))
    band=str(cfg.get('wifi_band','2.4'))
    if band not in WIFI_CHANNELS or channel not in WIFI_CHANNELS[band]:
        raise ValueError('Некорректный диапазон или канал Wi-Fi')
    hw_mode='a' if band == '5' else 'g'
    # hex SSID supports UTF-8 and avoids newline/escape injection
    return (f'interface={dev}\ndriver=nl80211\nssid2={ssid.encode().hex()}\n'
            f'hw_mode={hw_mode}\nchannel={channel}\nwmm_enabled=1\n'
            f'auth_algs=1\nwpa=2\nwpa_passphrase={password}\nwpa_key_mgmt=WPA-PSK\n'
            f'rsn_pairwise=CCMP\n')


def dnsmasq_config(cfg):
    # DNS supplied via DHCP only; never binds host port 53.
    gw=ipaddress.ip_interface(cfg['gateway_cidr']).ip
    mask=ipaddress.ip_interface(cfg['gateway_cidr']).network.netmask
    return (f'interface={cfg["downstream"]}\nbind-interfaces\n'
            'port=0\ndhcp-authoritative\n'
            f'dhcp-range={cfg["dhcp_start"]},{cfg["dhcp_end"]},{mask},12h\n'
            f'dhcp-option=option:router,{gw}\ndhcp-option=option:dns-server,1.1.1.1,9.9.9.9\n'
            'log-dhcp\n')


class ClientServices:
    def __init__(self):
        self.daemons=[]
        self.owned_ip=None
        self.owned_link=None

    def start(self, cfg, interfaces):
        mode,dev,addr=validate_client_settings(cfg,interfaces)
        if mode == 'manual':
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                raise ValueError('В ручном режиме назначьте IP шлюза в HAOS')
            return
        RUNTIME.mkdir(parents=True,exist_ok=True)
        os.chmod(RUNTIME,0o700)
        try:
            # A Wi-Fi adapter can be administratively DOWN in HAOS. Bring up only
            # the selected, dedicated client radio and restore its original state.
            if mode == 'wifi_ap':
                link = json.loads(run(['ip', '-j', 'link', 'show', 'dev', dev]).stdout)[0]
                if 'UP' not in link.get('flags', []):
                    try:
                        run(['ip', 'link', 'set', 'dev', dev, 'up'])
                    except subprocess.CalledProcessError as exc:
                        raise ValueError('Не удалось включить Wi-Fi '+dev+
                            ': '+(exc.stderr or str(exc))+
                            '. Проверьте rfkill, драйвер и NetworkManager.')
                    self.owned_link=dev
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                run(['ip','-4','address','add',str(addr),'dev',dev])
                self.owned_ip=(str(addr),dev)
            if mode == 'wifi_ap':
                (RUNTIME/'hostapd.conf').write_text(hostapd_config(cfg))
                os.chmod(RUNTIME/'hostapd.conf',0o600)
                self._launch(['hostapd',str(RUNTIME/'hostapd.conf')], 'hostapd')
            (RUNTIME/'dnsmasq.conf').write_text(dnsmasq_config(cfg))
            self._launch(['dnsmasq','--keep-in-foreground','--conf-file='+str(RUNTIME/'dnsmasq.conf'),
                         '--pid-file='+str(RUNTIME/'dnsmasq.pid')], 'dnsmasq')
        except Exception:
            self.stop()
            raise

    def _launch(self,cmd,label):
        log=open(RUNTIME/(label+'.log'),'ab')
        try:
            p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
        finally:
            log.close()
        self.daemons.append((label,p))
        time.sleep(.35)
        if p.poll() is not None:
            raise ValueError(label+' не запустился: '+(RUNTIME/(label+'.log')).read_text(errors='replace')[-1200:])

    def alive(self):
        return all(p.poll() is None for _,p in self.daemons)

    def stop(self):
        for _,p in reversed(self.daemons):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        self.daemons.clear()
        if self.owned_ip:
            cidr,dev=self.owned_ip
            run(['ip','-4','address','del',cidr,'dev',dev],check=False)
            self.owned_ip=None
        if self.owned_link:
            run(['ip','link','set','dev',self.owned_link,'down'],check=False)
            self.owned_link=None
