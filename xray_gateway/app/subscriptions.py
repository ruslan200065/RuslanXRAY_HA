"""Native Xray JSON subscriptions with Remnawave /json, User-Agent and HWID."""
import base64, json, os, re, tempfile, threading, uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from subprocess import TimeoutExpired
from hashlib import sha256
import engine
ROOT=engine.DATA/'subscriptions'
CONFIG=ROOT/'config.json'
CATALOG=ROOT/'profiles'
CATALOG_SNAPSHOT=ROOT/'catalog.json'
SELECTED=ROOT/'selected.json'
HWID=ROOT/'hwid'
HWID_LOCK = threading.Lock()

def save_text(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix='.new-', dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def save_json(path, obj):
    save_text(path, json.dumps(obj, ensure_ascii=False, indent=2))

def load(path,default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def config():
    return {**{'url':'','user_agent':'RuslanXRAY-HA/0.3','remnawave':True},**load(CONFIG,{})}

def validate_url(url):
    if not isinstance(url,str) or not url or len(url)>4096 or re.search(r'[\x00-\x20\x7f#]',url):
        raise ValueError('Некорректный URL подписки')
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise ValueError('Требуется HTTP(S)-ссылка без логина и пароля')
    return url

def native_url(url,remnawave=True):
    p=urlsplit(validate_url(url))
    if not remnawave:return url
    path=p.path.rstrip('/')
    if not path.endswith(('/json','/v2ray-json')):path+='/json'
    return urlunsplit((p.scheme,p.netloc,path,p.query,''))

def save_config(data):
    url=validate_url(data.get('url'))
    agent=data.get('user_agent','RuslanXRAY-HA/0.3')
    if not isinstance(agent,str) or not 1<=len(agent)<=200 or re.search(r'[\r\n\x00-\x1f\x7f]',agent):
        raise ValueError('Некорректный User-Agent')
    selection = load(SELECTED, {})
    if selection.get('id') is not None and 'source' not in selection:
        save_json(SELECTED, {**selection, 'source': config()['url']})
    save_json(CONFIG,{'url':url,'user_agent':agent,'remnawave':bool(data.get('remnawave',True))})
    return {'ok':True}

def hwid():
    """Keep one UUID for the lifetime of the app's persistent data."""
    with HWID_LOCK:
        try:
            value = HWID.read_text(encoding='ascii').strip()
            if str(uuid.UUID(value)) == value.lower():
                return value
        except (FileNotFoundError, ValueError):
            pass
        value = str(uuid.uuid4())
        save_text(HWID, value + '\n')
        return value

def parse_catalog(payload, *, allow_invalid=False):
    if len(payload)>6*1024*1024:raise ValueError('Подписка слишком большая')
    value=payload.decode('utf-8-sig').strip()
    try:data=json.loads(value)
    except json.JSONDecodeError:
        try:data=json.loads(base64.b64decode(''.join(value.split()),validate=True).decode('utf-8-sig'))
        except (ValueError,UnicodeError) as e:raise ValueError('Нужен полный Xray JSON либо массив JSON') from e
    if isinstance(data,dict) and isinstance(data.get('configs'),list):data=data['configs']
    if isinstance(data,dict):data=[data]
    if not isinstance(data,list) or not 1<=len(data)<=128:raise ValueError('Каталог должен содержать от 1 до 128 профилей')
    if not allow_invalid and any(not valid_profile(p) for p in data):
        raise ValueError('Подписка содержит неполный Xray JSON')
    return data

def profile_name(raw,i):
    meta=raw.get('meta') if isinstance(raw.get('meta'),dict) else {}
    return str(raw.get('remarks') or meta.get('serverDescription') or f'Профиль {i+1}')[:150]

def valid_profile(raw):
    return isinstance(raw, dict) and isinstance(raw.get('outbounds'), list) and bool(raw['outbounds'])


def fetch_catalog(c, settings):
    if not c['url']:
        raise ValueError('Сначала сохраните URL подписки')
    req = Request(native_url(c['url'], c['remnawave']), headers={
        'User-Agent': c['user_agent'], 'X-HWID': hwid(), 'Accept': 'application/json, text/plain, */*',
    })
    with urlopen(req, timeout=20) as response:
        if urlsplit(response.geturl()).scheme not in ('http', 'https'):
            raise ValueError('Недопустимый редирект')
        incoming = parse_catalog(response.read(6 * 1024 * 1024 + 1), allow_invalid=True)
    profiles = []
    for raw in incoming:
        if not valid_profile(raw):
            continue
        try:
            effective = engine.build_profile(raw, int(settings['port']), settings['route_mode'], settings['target'])
            engine.verify_profile(effective)
        except (ValueError, TypeError, AttributeError, TimeoutExpired):
            continue
        profiles.append(raw)
    if not profiles:
        raise ValueError('В подписке нет рабочих профилей. Прежний каталог сохранён.')
    return profiles, len(incoming) - len(profiles)


def catalog_profiles():
    snapshot = load(CATALOG_SNAPSHOT, None)
    if snapshot is not None:
        return dict(enumerate(snapshot))
    return {int(path.stem): load(path, {}) for path in
            sorted(CATALOG.glob('*.json'), key=lambda path: int(path.stem))}


def profile_identity(raw):
    # Only use a provider's explicit identifier across endpoint changes.
    identifier = raw.get('id')
    if type(identifier) in (str, int) and identifier != '':
        return ('id', identifier)
    name = raw.get('remarks')
    endpoints = []
    for outbound in raw.get('outbounds', []):
        if not isinstance(outbound, dict):
            continue
        options = outbound.get('settings', {})
        if not isinstance(options, dict):
            continue
        for key in ('vnext', 'servers'):
            servers = options.get(key)
            if not isinstance(servers, list):
                continue
            for server in servers:
                if isinstance(server, dict) and server.get('address') and server.get('port'):
                    endpoints.append((outbound.get('protocol'), server['address'], server['port']))
    return ('endpoint', name, endpoints) if name and endpoints else None


def matching_profile(active, old, new):
    exact = next((i for i, raw in enumerate(new) if raw == active), None)
    if exact is not None:
        return exact
    identity = profile_identity(active)
    if identity is None or sum(profile_identity(raw) == identity for raw in old.values()) != 1:
        return None
    matches = [i for i, raw in enumerate(new) if profile_identity(raw) == identity]
    return matches[0] if len(matches) == 1 else None


def replace_catalog(profiles, skipped):
    old = catalog_profiles()
    selection = load(SELECTED, {})
    active = load(engine.PROFILE, None)
    was_selected = active is not None and selection.get('id') is not None and active in old.values()
    same_source = selection.get('source', config()['url']) == config()['url']
    selected = matching_profile(active, old, profiles) if was_selected and same_source else None
    save_json(CATALOG_SNAPSHOT, profiles)
    if selected is not None:
        save_json(engine.PROFILE, profiles[selected])
        save_json(SELECTED, {'id': selected, 'source': config()['url']})
    elif was_selected:
        SELECTED.unlink(missing_ok=True)
    return {'ok': True, 'count': len(profiles), 'skipped': skipped,
            'selection_lost': was_selected and selected is None}


def update_catalog():
    return replace_catalog(*fetch_catalog(config(), engine.settings()))


def profile_fingerprint(raw):
    payload = json.dumps(raw, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return sha256(payload).hexdigest()


def list_catalog():
    entries = catalog_profiles()
    selection = load(SELECTED, {})
    selected = selection.get('id')
    if selected is not None:
        active = load(engine.PROFILE, None)
        if selected not in entries or entries[selected] != active:
            selected = next((i for i, raw in entries.items() if raw == active), None)
    profiles = [
        {'id': i, 'name': profile_name(raw, i), 'revision': profile_fingerprint(raw), 'outbounds': len(raw.get('outbounds', [])),
         'balancers': len((raw.get('routing') or {}).get('balancers', []))}
        for i, raw in entries.items()
    ]
    c = config()
    return {'profiles': profiles, 'selected': selected, 'configured': bool(c['url']),
            'user_agent': c['user_agent'], 'remnawave': c['remnawave']}


def get_profile(profile_id, revision=None):
    if type(profile_id) is not int or not 0 <= profile_id < 128:
        raise ValueError('Неверный ID')
    raw = catalog_profiles().get(profile_id)
    if raw is None:
        raise ValueError('Профиль отсутствует')
    if revision is not None and revision != profile_fingerprint(raw):
        raise ValueError('Каталог изменился. Обновите список профилей.')
    return raw


def select_profile(profile_id, revision=None):
    raw = get_profile(profile_id, revision)
    settings = engine.settings()
    engine.verify_profile(engine.build_profile(raw, int(settings['port']), settings['route_mode'], settings['target']))
    save_json(engine.PROFILE, raw)
    save_json(SELECTED, {'id': profile_id, 'source': config()['url']})
    return {'ok': True, 'name': profile_name(raw, profile_id)}
