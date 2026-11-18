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
# Linux UNIX socket paths are limited. The control socket is ephemeral.
AP_CONTROL = Path('/run/ruslan-xray-ap')
AP_START_TIMEOUT = 10
AP_DFS_TIMEOUT = 660
# Expanded primary 20-MHz channels; actual availability comes from iw phy/regdomain.
WIFI_CHANNELS = {
    '2.4': tuple(range(1, 14)),
    '5': (36, 40, 44, 48, 52, 56, 60, 64,
          100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
          149, 153, 157, 161, 165),
}
DFS_CHANNELS = frozenset((52, 56, 60, 64, 100, 104, 108, 112,
                          116, 120, 124, 128, 132, 136, 140, 144))



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
        if not isinstance(password, str) or not re.fullmatch(r'[ -~]{8,63}', password):
            raise ValueError('Пароль Wi-Fi: 8–63 ASCII-символа')
        band = str(cfg.get('wifi_band', '2.4'))
        if band not in WIFI_CHANNELS:
            raise ValueError('Диапазон Wi-Fi должен быть 2.4 или 5 ГГц')
        channel = int(cfg.get('wifi_channel', 6))
        if channel not in WIFI_CHANNELS[band]:
            raise ValueError(f'Канал {channel} недопустим для диапазона {band} ГГц')
        if band == '5' and channel in DFS_CHANNELS:
            if not cfg.get('wifi_allow_dfs', False):
                raise ValueError('Каналы DFS требуют отдельного разрешения')
            if not cfg.get('wifi_apply_regdomain', False):
                raise ValueError('Для DFS нужен регион Wi-Fi, применяемый при запуске')
        country = str(cfg.get('wifi_country', '')).strip().upper()
        if country and (not re.fullmatch(r'[A-Z]{2}', country) or country in ('XX', 'ZZ', 'AA')):
            raise ValueError('Укажите реальный код страны из двух латинских букв: RU, DE, FI и т. п.')
        if cfg.get('wifi_apply_regdomain', False) and not country:
            raise ValueError('Укажите страну перед изменением Wi-Fi регуляторного домена')
        # The channel must still be permitted by the physical driver.
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


def parse_phy_channels(output):
    """Parse iw phy output, including fractional MHz and regulatory flags."""
    result = []
    for line in output.splitlines():
        found = re.search(r'(\d+(?:\.\d+)?)\s*MHz\s*\[\s*(\d+)\s*\]', line, re.I)
        if not found:
            continue
        flags = line.lower()
        result.append({
            'channel': int(found.group(2)),
            'frequency_mhz': float(found.group(1)),
            'disabled': bool(re.search(r'\bdisabled\b', flags)),
            'no_ir': bool(re.search(r'\bno[\s_-]?ir\b|\bpassive[- ]scan(?:ning)?\b', flags)),
            'radar': 'radar detection' in flags,
        })
    return result


def radio_capabilities(dev):
    """Return actual AP-capable frequencies, not guessed supported channels."""
    info = run(['iw', 'dev', dev, 'info'], check=False)
    match = re.search(r'\bwiphy\s+(\d+)', info.stdout)
    if info.returncode != 0 or not match:
        raise ValueError('Не удалось определить Wi-Fi PHY '+dev+': '+(info.stderr or info.stdout)[-350:])
    phy = run(['iw', 'phy', 'phy'+match.group(1), 'info'], check=False)
    if phy.returncode != 0:
        raise ValueError('Не удалось прочитать Wi-Fi PHY: '+(phy.stderr or phy.stdout)[-350:])
    supports_ap = bool(re.search(r'^\s*\*\s+AP\s*$', phy.stdout, re.M))
    channels = parse_phy_channels(phy.stdout)
    regulatory = run(['iw', 'reg', 'get'], check=False)
    regulatory_output = regulatory.stdout
    country_entries = re.findall(r'(?m)^\s*country\s+([A-Za-z0-9]{2}):', regulatory_output)
    phy_number = match.group(1)
    driver_link = Path('/sys/class/net') / dev / 'device' / 'driver'
    driver = driver_link.resolve().name if driver_link.is_symlink() else ''
    self_managed = bool(re.search(rf'(?m)^phy#{phy_number}[ \t]+\(self-managed\)', regulatory_output))
    return {'device': dev, 'phy': 'phy'+phy_number, 'supports_ap': supports_ap,
            'driver': driver, 'self_managed': self_managed,
            'global_country': global_country(regulatory_output),
            'channels': channels, 'reg_domain': regulatory_output[:16000],
            'reg_countries': country_entries,
            'reg_error': regulatory.stderr[-500:] if regulatory.returncode else ''}


def global_country(reg_output):
    """Return only the global country from iw reg get, not a PHY override."""
    match = re.search(r'(?m)^global\s*\n\s*country\s+([A-Z]{2}|00):', reg_output)
    return match.group(1) if match else None


def apply_regdomain(country):
    """Temporarily request a real country; hardware regulatory limits still apply."""
    country = str(country).strip().upper()
    if not re.fullmatch(r'[A-Z]{2}', country) or country in ('XX', 'ZZ', 'AA'):
        raise ValueError('Недопустимый ISO-код страны Wi-Fi')
    current = run(['iw', 'reg', 'get'], check=False)
    previous = global_country(current.stdout) if current.returncode == 0 else None
    if previous is None:
        raise ValueError('Не удалось прочитать исходный глобальный Wi-Fi регион — изменение отменено')
    if previous == country:
        return None
    result = run(['iw', 'reg', 'set', country], check=False)
    if result.returncode:
        raise ValueError('Не удалось применить Wi-Fi регион '+country+': '+(result.stderr or result.stdout)[-400:])
    return (country, previous)


def await_country(country):
    for attempt in range(12):
        result = run(['iw', 'reg', 'get'], check=False, timeout=2)
        if result.returncode == 0 and global_country(result.stdout) == country:
            return
        if attempt < 11:
            time.sleep(.25)
    raise ValueError(f'Ядро не подтвердило Wi-Fi регион {country}.')


def verify_ap_radio(dev, band, channel, allow_dfs=False):
    """Check that the driver permits AP startup and return whether DFS is required."""
    details = radio_capabilities(dev)
    if not details['supports_ap']:
        raise ValueError('Адаптер '+dev+' не поддерживает режим точки доступа (AP)')
    freq = 5000 + 5 * channel if band == '5' else 2407 + 5 * channel
    entries = [x for x in details['channels']
               if x['channel'] == channel and abs(x['frequency_mhz'] - freq) < 0.5]
    if not entries:
        raise ValueError(f'Канал {channel} ({freq} МГц) отсутствует у адаптера {dev}. '
                         'Выберите другой канал в настройках сети.')
    entry = entries[0]
    radar = entry['radar'] or (band == '5' and channel in DFS_CHANNELS)
    if entry['disabled']:
        raise ValueError(f'Канал {channel} отключён драйвером {dev}. Выберите другой канал.')
    if entry['no_ir'] and (not entry['radar'] or not allow_dfs or details.get('self_managed')):
        driver = details.get('driver') or dev
        hint = (' Адаптер сам управляет регионом. Смена страны в HAOS не снимает запрет.'
                if details.get('self_managed') else '')
        raise ValueError(f'Канал {channel} ({freq} МГц) помечен no-IR. '
                         f'{driver} запрещает начать передачу на этом канале.{hint} '
                         'Выберите разрешённый канал или внешнюю точку доступа.')
    if radar and not allow_dfs:
        raise ValueError(f'Для канала {channel} требуется DFS. Выберите разрешённый канал без DFS '
                         'или настройте DFS и фактическую страну.')
    return radar


def hostapd_config(cfg, *, driver_requires_dfs=False):
    """20-MHz WPA2 AP; DFS opt-in, CAC handled by hostapd/driver."""
    dev=cfg['downstream']
    ssid=cfg['wifi_ssid']
    password=cfg['wifi_password']
    channel=int(cfg.get('wifi_channel',6))
    band=str(cfg.get('wifi_band','2.4'))
    if band not in WIFI_CHANNELS or channel not in WIFI_CHANNELS[band]:
        raise ValueError('Некорректный диапазон или канал Wi-Fi')
    dfs = driver_requires_dfs or (band == '5' and channel in DFS_CHANNELS)
    if dfs and (not cfg.get('wifi_allow_dfs', False) or not cfg.get('wifi_apply_regdomain', False)):
        raise ValueError('Для DFS требуется разрешение DFS и применение реальной страны Wi-Fi')
    hw_mode='a' if band == '5' else 'g'
    country = str(cfg.get('wifi_country', '')).strip().upper()
    change_country = bool(cfg.get('wifi_apply_regdomain', False))
    if change_country and (not re.fullmatch(r'[A-Z]{2}',country) or country in ('XX','ZZ','AA')):
        raise ValueError('Некорректная страна для hostapd')
    country_lines = f'country_code={country}\nieee80211d=1\n' if change_country else ''
    ht_lines = 'ieee80211n=1\n' if cfg.get('wifi_11n', True) else ''
    dfs_lines = 'ieee80211h=1\n' if dfs else ''
    return (f'interface={dev}\ndriver=nl80211\nctrl_interface={AP_CONTROL}\nssid2={ssid.encode().hex()}\n'
            f'hw_mode={hw_mode}\nchannel={channel}\nwmm_enabled=1\n{country_lines}{ht_lines}{dfs_lines}'
            f'auth_algs=1\nwpa=2\nwpa_passphrase={password}\nwpa_key_mgmt=WPA-PSK\n'
            f'rsn_pairwise=CCMP\n')


def dnsmasq_config(cfg):
    # DNS supplied via DHCP only; never binds host port 53.
    gw=ipaddress.ip_interface(cfg['gateway_cidr']).ip
    mask=ipaddress.ip_interface(cfg['gateway_cidr']).network.netmask
    return (f'interface={cfg["downstream"]}\nbind-interfaces\n'
            f'port=0\ndhcp-authoritative\ndhcp-leasefile={RUNTIME / "dnsmasq.leases"}\n'
            'log-facility=-\n'
            f'dhcp-range={cfg["dhcp_start"]},{cfg["dhcp_end"]},{mask},12h\n'
            f'dhcp-option=option:router,{gw}\ndhcp-option=option:dns-server,1.1.1.1,9.9.9.9\n'
            'log-dhcp\n')


def client_network_health(cfg, interfaces):
    device = cfg['downstream']
    link = next((item for item in interfaces if item['name'] == device), None)
    if link is None:
        return 'failed', f'Клиентский интерфейс {device} отсутствует.'
    address = str(ipaddress.ip_interface(cfg['gateway_cidr']))
    if address not in link['addresses']:
        return 'failed', (f'На {device} нет адреса {address}. Остановите шлюз и проверьте '
                          'настройки IPv4 выделенного порта в HAOS.')
    if cfg['client_service'] == 'dhcp' and link['status'].upper() in ('DOWN', 'LOWERLAYERDOWN', 'DORMANT'):
        return 'starting', f'Нет линка на {device}. Проверьте кабель и порт роутера.'
    return 'ready', ''


def dhcp_lease_count(cfg):
    try:
        leases = (RUNTIME / 'dnsmasq.leases').read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return 0
    start = ipaddress.IPv4Address(cfg['dhcp_start'])
    end = ipaddress.IPv4Address(cfg['dhcp_end'])
    now = int(time.time())
    count = 0
    for line in leases.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            expiry = int(fields[0])
            address = ipaddress.IPv4Address(fields[2])
        except ValueError:
            continue
        if (expiry == 0 or expiry > now) and start <= address <= end:
            count += 1
    return count


def hostapd_state(device):
    try:
        reply = run(['hostapd_cli', '-p', str(AP_CONTROL), '-i', device, 'status'],
                    check=False, timeout=2)
    except subprocess.TimeoutExpired:
        return ''
    for line in reply.stdout.splitlines():
        if reply.returncode == 0 and line.startswith('state='):
            return line.removeprefix('state=').strip()
    return ''


NM_BUS = Path('/run/dbus/system_bus_socket')
NM_NAME = 'org.freedesktop.NetworkManager'
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')


def nm_call(path, method, *args):
    return run(['dbus-send', f'--bus=unix:path={NM_BUS}', '--print-reply',
                '--reply-timeout=2500', f'--dest={NM_NAME}', path, method, *args],
               timeout=4).stdout


def nm_device(device):
    response = nm_call('/org/freedesktop/NetworkManager', f'{NM_NAME}.GetDeviceByIpIface',
                       f'string:{device}')
    match = re.search(r'object path "(/org/freedesktop/NetworkManager/Devices/\d+)"', response)
    if not match:
        raise ValueError(f'NetworkManager не нашёл интерфейс {device}.')
    return match.group(1)


def nm_managed(path):
    response = nm_call(path, 'org.freedesktop.DBus.Properties.Get',
                       f'string:{NM_NAME}.Device', 'string:Managed')
    match = re.search(r'boolean (true|false)', response)
    if not match:
        raise ValueError('NetworkManager не сообщил состояние порта.')
    return match.group(1) == 'true'


def nm_set_managed(path, value):
    flag = 'true' if value else 'false'
    nm_call(path, 'org.freedesktop.DBus.Properties.Set',
            f'string:{NM_NAME}.Device', 'string:Managed', f'variant:boolean:{flag}')


def interface_link(device):
    return json.loads(run(['ip', '-j', 'link', 'show', 'dev', device]).stdout)[0]


def default_route_device(device):
    for family in ('-4', '-6'):
        routes = json.loads(run(['ip', '-j', family, 'route', 'show', 'default']).stdout)
        if any(r.get('dev') == device or any(hop.get('dev') == device for hop in r.get('nexthops', []))
               for r in routes):
            return True
    return False


def release_downstream(device, address, had_address):
    if not NM_BUS.is_socket():
        if os.getenv('SUPERVISOR_TOKEN'):
            raise ValueError('Нет доступа к системной шине HAOS для управления клиентским портом.')
        return
    if default_route_device(device):
        raise ValueError(f'{device} используется маршрутом по умолчанию HAOS.')
    try:
        path = nm_device(device)
        if not nm_managed(path):
            return
        link = interface_link(device)
        record = {'device': device, 'boot_id': BOOT_ID.read_text().strip(),
                  'ifindex': link['ifindex'], 'mac': link.get('address'),
                  'address': '' if had_address else str(address)}
        marker = RUNTIME / 'networkmanager.json'
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(record), encoding='utf-8')
        temporary.replace(marker)
        nm_set_managed(path, False)
        if nm_managed(path):
            raise ValueError(f'NetworkManager не освободил {device}.')
    except (subprocess.SubprocessError, OSError) as error:
        raise ValueError(f'Не удалось передать {device} от NetworkManager приложению.') from error


def restore_downstream(*, recover=False):
    marker = RUNTIME / 'networkmanager.json'
    try:
        record = json.loads(marker.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return
    device = record['device']
    if record['boot_id'] != BOOT_ID.read_text().strip() or not (Path('/sys/class/net') / device).exists():
        marker.unlink()
        return
    link = interface_link(device)
    if link['ifindex'] != record['ifindex'] or link.get('address') != record['mac']:
        marker.unlink()
        return
    if default_route_device(device):
        raise ValueError(f'Восстановление NetworkManager отложено: {device} теперь используется как WAN.')
    path = nm_device(device)
    if not nm_managed(path):
        if recover and record['address']:
            run(['ip', '-4', 'address', 'del', record['address'], 'dev', device], check=False)
        nm_set_managed(path, True)
        if not nm_managed(path):
            raise ValueError(f'Не удалось вернуть {device} под управление NetworkManager.')
    marker.unlink()


def interface_addresses(device):
    links = json.loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', device]).stdout)
    return {f"{item['local']}/{item['prefixlen']}" for link in links for item in link.get('addr_info', [])}



class ClientServices:
    def __init__(self):
        self.daemons=[]
        self.owned_ip=None
        self.owned_link=None
        self.owned_regdomain=None
        self.ap_device = None
        self.ap_state = ''
        self.ap_deadline = 0

    def start(self, cfg, interfaces):
        mode,dev,addr=validate_client_settings(cfg,interfaces)
        if mode == 'manual':
            if not any(addr == ipaddress.ip_interface(x) for x in next(i for i in interfaces if i['name']==dev)['addresses']):
                raise ValueError('В ручном режиме назначьте IP шлюза в HAOS')
            return
        RUNTIME.mkdir(parents=True,exist_ok=True)
        os.chmod(RUNTIME,0o700)
        try:
            # Both a dedicated Ethernet DHCP port and a Wi-Fi AP must be
            # administratively UP. HAOS may leave unused downstream ports DOWN.
            # Restore the original admin state only if we changed it.
            dfs = False
            if mode == 'wifi_ap':
                association = run(['iw', 'dev', dev, 'link'], check=False)
                if association.stdout.lstrip().startswith('Connected to '):
                    raise ValueError(f'{dev} подключён к Wi-Fi как клиент. '
                                     'Выберите выделенный адаптер или отключите его подключение к этой сети.')
                if cfg.get('wifi_apply_regdomain', False):
                    self.owned_regdomain = apply_regdomain(cfg.get('wifi_country', ''))
                    if self.owned_regdomain:
                        await_country(self.owned_regdomain[0])
                dfs = verify_ap_radio(
                    dev, str(cfg.get('wifi_band', '2.4')), int(cfg.get('wifi_channel', 6)),
                    allow_dfs=bool(cfg.get('wifi_allow_dfs') and cfg.get('wifi_apply_regdomain')),
                )
            before = interface_addresses(dev)
            release_downstream(dev, addr, str(addr) in before)
            link = interface_link(dev)
            if 'UP' not in link.get('flags', []):
                try:
                    run(['ip', 'link', 'set', 'dev', dev, 'up'])
                except subprocess.CalledProcessError as exc:
                    raise ValueError('Не удалось включить клиентский интерфейс '+dev+
                        ': '+(exc.stderr or str(exc))+
                        '. Проверьте драйвер и NetworkManager.')
                self.owned_link=dev
            if str(addr) not in interface_addresses(dev):
                run(['ip','-4','address','add',str(addr),'dev',dev])
                self.owned_ip=(str(addr),dev)
            if mode == 'wifi_ap':
                AP_CONTROL.mkdir(mode=0o700, parents=True, exist_ok=True)
                config = RUNTIME / 'hostapd.conf'
                config.write_text(hostapd_config(cfg, driver_requires_dfs=dfs), encoding='utf-8')
                os.chmod(config, 0o600)
                self._launch(['hostapd', str(config)], 'hostapd')
                self.ap_device = dev
                self.ap_deadline = time.monotonic() + AP_START_TIMEOUT
            (RUNTIME/'dnsmasq.conf').write_text(dnsmasq_config(cfg))
            self._launch(['dnsmasq','--keep-in-foreground','--conf-file='+str(RUNTIME/'dnsmasq.conf'),
                         '--pid-file='+str(RUNTIME/'dnsmasq.pid')], 'dnsmasq')
        except Exception:
            self.stop()
            raise

    def _launch(self,cmd,label):
        log=open(RUNTIME/(label+'.log'),'wb')
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

    def ap_health(self):
        if self.ap_device is None:
            return None
        if not self.alive():
            return {'phase': 'failed', 'message': 'Остановился hostapd или DHCP. Проверьте журнал приложения.'}
        state = hostapd_state(self.ap_device)
        now = time.monotonic()
        if state == 'ENABLED':
            self.ap_state = state
            return {'phase': 'ready'}
        if state != self.ap_state:
            if state == 'DFS':
                self.ap_deadline = now + AP_DFS_TIMEOUT
            elif self.ap_state in ('DFS', 'ENABLED'):
                self.ap_deadline = now if state == 'DISABLED' else now + AP_START_TIMEOUT
            self.ap_state = state
        if now >= self.ap_deadline:
            return {'phase': 'failed', 'message': 'Точка доступа не начала раздачу. '
                    'Остановите шлюз и проверьте канал и журнал приложения.'}
        return {'phase': 'starting', 'message': 'Проверка канала на радары (DFS)…' if state == 'DFS'
                else 'Ожидаем готовности точки доступа…'}

    def stop(self):
        self.ap_device = None
        self.ap_state = ''
        self.ap_deadline = 0
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
        try:
            restore_downstream()
        finally:
            self.restore_regdomain()

    def restore_regdomain(self):
        if self.owned_regdomain:
            expected, previous = self.owned_regdomain
            self.owned_regdomain = None
            current = run(['iw', 'reg', 'get'], check=False)
            # Do not overwrite a newer change performed by another administrator.
            if current.returncode == 0 and global_country(current.stdout) == expected:
                result = run(['iw', 'reg', 'set', previous], check=False)
                if result.returncode:
                    RUNTIME.mkdir(parents=True,exist_ok=True)
                    (RUNTIME/'regdomain_restore.log').write_text(
                        'Регион '+previous+' не восстановлен: '+(result.stderr or result.stdout)[-500:])
