"""An intentionally conservative, reversible IPv4 Xray TProxy adapter."""
import copy, ipaddress, json, os, re, subprocess, tempfile
import network
from pathlib import Path
DATA = Path(os.environ.get("GATEWAY_DATA", "/data/ruslan-gateway"))
STATE = DATA / "gateway.json"
PROFILE = DATA / "profile.json"
EFFECTIVE = DATA / "effective.json"
NFT_NAME = "ruslan_xray_gateway"
MARK = "0x233"
TABLE = "203"
PREF = "20300"
SAFE_NETS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4"]
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")

def run(args, check=True, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True, timeout=15, check=check)

def interfaces():
    result = run(["ip", "-j", "-d", "link", "show"])
    links = json.loads(result.stdout)
    ips = {x['ifname']: x for x in json.loads(run(["ip", "-j", "address", "show"]).stdout)}
    default = run(["ip", "-j", "route", "show", "default"], check=False)
    upstream = {r.get('dev') for r in json.loads(default.stdout or '[]')} if default.returncode == 0 else set()
    out=[]
    for link in links:
        name=link['ifname']
        if name == 'lo' or name.startswith(('veth','docker','hassio','br-','tun','wg')): continue
        addresses=[x.get('local')+'/'+str(x.get('prefixlen')) for x in ips.get(name,{}).get('addr_info',[]) if x.get('family')=='inet']
        out.append({'name':name, 'mac':link.get('address',''), 'status':link.get('operstate','unknown'),
                    'wifi': (Path('/sys/class/net')/name/'wireless').exists() or (Path('/sys/class/net')/name/'phy80211').exists(),
                    'kind': link.get('linkinfo',{}).get('info_kind') or link.get('link_type',''),
                    'addresses':addresses, 'is_default':name in upstream})
    return out

def defaults():
    return {'upstream':'','downstream':'','intercept':False,'port':12345,'block_ipv6':True,
            'lan_access':False,'client_service':'manual','gateway_cidr':'192.168.50.1/24',
            'dhcp_start':'192.168.50.100','dhcp_end':'192.168.50.200',
            'wifi_ssid':'RuslanXRAY','wifi_password':'','wifi_band':'2.4','wifi_channel':6,
            'wifi_allow_dfs':False,
            'wifi_country':'','wifi_apply_regdomain':False,'wifi_11n':True,
            'route_mode':'force','target':''}

def settings():
    if not STATE.exists():return defaults()
    return {**defaults(), **json.loads(STATE.read_text())}

def save_settings(v):
    DATA.mkdir(parents=True,exist_ok=True)
    temp = STATE.with_suffix('.tmp');temp.write_text(json.dumps(v,indent=2));os.chmod(temp,0o600);temp.replace(STATE)

def validate_settings(v,require_address=True):
    li=interfaces(); known={i['name']:i for i in li}
    u=v.get('upstream',''); d=v.get('downstream','')
    if not INTERFACE_RE.fullmatch(u) or not INTERFACE_RE.fullmatch(d): raise ValueError('Выберите существующие физические интерфейсы')
    if u not in known or d not in known: raise ValueError('Интерфейс исчез или не найден')
    if u==d:raise ValueError('Один интерфейс нельзя использовать как вход и выход без отдельного VLAN/виртуального интерфейса')
    if known[d]['is_default']: raise ValueError('Выходной интерфейс является текущим default route HAOS. Использовать его опасно')
    if not known[u]['is_default']: raise ValueError('WAN не является default route HAOS. В этой версии такая схема ещё не поддерживается')
    mode, _, subnet = network.validate_client_settings(v,li)
    if v.get('lan_access'):
        local_lan_paths(v, li)
    if require_address and mode == 'manual' and str(subnet) not in known[d]['addresses']:
        raise ValueError('В ручном режиме назначьте адрес шлюза из gateway_cidr в HAOS')
    port=int(v.get('port',12345))
    if not 1024<=port<=65535: raise ValueError('TProxy порт должен быть в диапазоне 1024–65535')
    return u,d,port

def build_profile(raw, port, route_mode="force", target=""):
    """Native Xray profiles remain untouched on disk. Effective profile is isolated."""
    """Preserves routing/outbound structure; updates only required local inbounds."""
    if not isinstance(raw,dict):raise ValueError('Ожидается полный JSON-конфиг Xray')
    cfg=copy.deepcopy(raw)
    if not cfg.get('outbounds') or not isinstance(cfg['outbounds'],list):raise ValueError('В профиле нет outbounds')
    inbounds=cfg.setdefault('inbounds',[])
    if not isinstance(inbounds,list):raise ValueError('inbounds должен быть массивом')
    if any(isinstance(x,dict) and x.get('tag')=='rgw-tproxy' for x in inbounds):raise ValueError('Тег rgw-tproxy зарезервирован')
    # No imported inbound may unexpectedly listen on the host's LAN/WAN.
    for inbound in inbounds:
        if not isinstance(inbound,dict): raise ValueError('Некорректный элемент inbounds')
        inbound['listen']='127.0.0.1'
    inbounds.append({'tag':'rgw-tproxy','listen':'0.0.0.0','port':port,'protocol':'dokodemo-door',
          'settings':{'network':'tcp,udp','followRedirect':True},
          'streamSettings':{'sockopt':{'tproxy':'tproxy'}},
          'sniffing':{'enabled':True,'routeOnly':True,'destOverride':['http','tls','quic']}})
    if route_mode not in ('force','native'):
        raise ValueError('Недопустимый режим маршрутизации')
    if route_mode == 'force':
        balancers=[x.get('tag') for x in cfg.get('routing',{}).get('balancers',[]) if isinstance(x,dict)]
        proxies=[x.get('tag') for x in cfg['outbounds'] if isinstance(x,dict) and x.get('protocol') not in ('freedom','blackhole','dns') and x.get('tag')]
        if target:
            if target.startswith('balancer:') and target[9:] in balancers:
                rule={'type':'field','inboundTag':['rgw-tproxy'],'balancerTag':target[9:]}
            elif target in proxies:
                rule={'type':'field','inboundTag':['rgw-tproxy'],'outboundTag':target}
            else:
                raise ValueError('Выбранный VPN-выход отсутствует в профиле')
        elif balancers:
            rule={'type':'field','inboundTag':['rgw-tproxy'],'balancerTag':balancers[0]}
        elif proxies:
            rule={'type':'field','inboundTag':['rgw-tproxy'],'outboundTag':proxies[0]}
        else:
            raise ValueError('Не найден VPN outbound. Нельзя включать шлюз с direct-only профилем')
        cfg['routing']=cfg.get('routing') or {}
        cfg['routing']['rules']=[rule]+cfg['routing'].get('rules',[])
    return cfg

def verify_profile(cfg):
    DATA.mkdir(parents=True,exist_ok=True)
    fd, path = tempfile.mkstemp(prefix='verify-', suffix='.json', dir=DATA)
    temp = Path(path)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(cfg, stream, ensure_ascii=False)
        result=run(['/usr/local/bin/xray','run','-test','-c',str(temp)],check=False)
        if result.returncode:raise ValueError((result.stderr or result.stdout or 'Ошибка Xray')[-2500:])
    finally:temp.unlink(missing_ok=True)

def local_lan_paths(settings, interfaces):
    wan = settings['upstream']
    downstream = settings['downstream']
    source = next((item for item in interfaces if item['name'] == wan), {})
    private_blocks = [ipaddress.IPv4Network(block) for block in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    networks = {ipaddress.IPv4Interface(value).network for value in source.get('addresses', [])
                if any(ipaddress.IPv4Interface(value).network.subnet_of(block) for block in private_blocks)}
    if len(networks) != 1:
        raise ValueError('Для доступа к локальной сети на WAN нужна одна частная IPv4-подсеть.')
    return wan, downstream, next(iter(networks)), ipaddress.IPv4Interface(settings['gateway_cidr']).network


def nft_rules(d, port, block_ipv6=True, lan_paths=None):
    # Do not install a NAT or general forward rule. All public IPv4 TCP/UDP is captured
    # at PREROUTING. A forward hook blocks all unmatched client traffic (fail closed).
    safe=', '.join(SAFE_NETS)
    ipv6 = f'\n  chain forward6 {{ type filter hook forward priority -15; policy accept; iifname "{d}" meta nfproto ipv6 drop; }}' if block_ipv6 else '' 
    local_forward = ''
    if lan_paths:
        wan, downstream, main, clients = lan_paths
        local_forward = (
            f'iifname "{downstream}" oifname "{wan}" ip saddr {clients} ip daddr {main} accept\n'
            f'    iifname "{wan}" oifname "{downstream}" ip saddr {main} ip daddr {clients} accept\n'
            f'    oifname "{downstream}" drop\n    '
        )
    return f"""table inet {NFT_NAME} {{
  chain intercept {{
    type filter hook prerouting priority mangle; policy accept;
    iifname "{d}" ip daddr {{ {safe} }} return
    iifname "{d}" meta nfproto ipv4 meta l4proto {{ tcp, udp }} meta mark set {MARK} tproxy ip to :{port} accept
  }}
  chain input_lock {{
    type filter hook input priority -15; policy accept;
    iifname != "{d}" meta l4proto {{ tcp, udp }} th dport {port} drop
  }}
  chain lockdown {{
    type filter hook forward priority -15; policy accept;
    {local_forward}iifname "{d}" drop
  }}{ipv6}
}}
"""

def route_commands():
    return [f'ip -4 route add local default dev lo table {TABLE}',f'ip -4 rule add pref {PREF} fwmark {MARK} lookup {TABLE}']

def apply_rules(v):
    u,d,port=validate_settings(v)
    lan_paths = local_lan_paths(v, interfaces()) if v.get('lan_access') else None
    if lan_paths and Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() != '1':
        raise ValueError('HAOS отключил IPv4-пересылку. Доступ между подсетями недоступен.')
    rules=nft_rules(d,port,bool(v.get('block_ipv6',True)),lan_paths)
    # Never replace shared firewall tables. Fail before changes on invalid nft syntax.
    test=run(['nft','-c','-f','-'],check=False,input=rules)
    if test.returncode:raise ValueError('Ошибка nft: '+test.stderr)
    # Intentionally no automatic cleanup of another process's tables on collision.
    existing=run(['nft','list','table','inet',NFT_NAME],check=False)
    if existing.returncode==0:raise ValueError('Таблица уже существует; выключите gateway через панель')
    route = ['local','default','dev','lo','table',TABLE]
    rule = ['pref',PREF,'fwmark',MARK,'lookup',TABLE]
    run(['ip','-4','route','add',*route])
    try:
        run(['ip','-4','rule','add',*rule])
        try:
            run(['nft','-f','-'],input=rules)
        except Exception:
            run(['ip','-4','rule','del',*rule],check=False)
            raise
    except Exception:
        run(['ip','-4','route','del',*route],check=False)
        raise

def remove_rules():
    run(['nft','delete','table','inet',NFT_NAME],check=False)
    run(['ip','-4','rule','del','pref',PREF,'fwmark',MARK,'lookup',TABLE],check=False)
    run(['ip','-4','route','del','local','default','dev','lo','table',TABLE],check=False)

def rules_present():return run(['nft','list','table','inet',NFT_NAME],check=False).returncode==0
