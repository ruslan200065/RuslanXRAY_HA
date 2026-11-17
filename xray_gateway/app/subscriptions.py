"""Native Xray JSON subscriptions with Remnawave /json, User-Agent and HWID."""
import base64, json, os, re, shutil, tempfile, uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
import engine
ROOT=engine.DATA/'subscriptions'
CONFIG=ROOT/'config.json'
CATALOG=ROOT/'profiles'
SELECTED=ROOT/'selected.json'
HWID=ROOT/'hwid'

def save_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.new-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

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
    save_json(CONFIG,{'url':url,'user_agent':agent,'remnawave':bool(data.get('remnawave',True))})
    return {'ok':True}

def hwid():
    if HWID.exists():
        value=HWID.read_text(encoding='ascii').strip()
        if re.fullmatch(r'[a-fA-F0-9-]{36}',value):return value
    ROOT.mkdir(parents=True,exist_ok=True)
    value=str(uuid.uuid4())
    try:fd=os.open(HWID,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:return HWID.read_text(encoding='ascii').strip()
    with os.fdopen(fd,'w',encoding='ascii') as f:f.write(value+'\n')
    return value

def parse_catalog(payload):
    if len(payload)>6*1024*1024:raise ValueError('Подписка слишком большая')
    value=payload.decode('utf-8-sig').strip()
    try:data=json.loads(value)
    except json.JSONDecodeError:
        try:data=json.loads(base64.b64decode(''.join(value.split()),validate=True).decode('utf-8-sig'))
        except (ValueError,UnicodeError) as e:raise ValueError('Нужен полный Xray JSON либо массив JSON') from e
    if isinstance(data,dict) and isinstance(data.get('configs'),list):data=data['configs']
    if isinstance(data,dict):data=[data]
    if not isinstance(data,list) or not 1<=len(data)<=128:raise ValueError('Каталог должен содержать от 1 до 128 профилей')
    if any(not isinstance(p,dict) or not isinstance(p.get('outbounds'),list) or not p['outbounds'] for p in data):
        raise ValueError('Подписка содержит неполный Xray JSON')
    return data

def profile_name(raw,i):
    meta=raw.get('meta') if isinstance(raw.get('meta'),dict) else {}
    return str(raw.get('remarks') or meta.get('serverDescription') or f'Профиль {i+1}')[:150]

def update_catalog():
    c=config()
    if not c['url']:raise ValueError('Сначала сохраните URL подписки')
    req=Request(native_url(c['url'],c['remnawave']),headers={'User-Agent':c['user_agent'],'X-HWID':hwid(),'Accept':'application/json, text/plain, */*'})
    with urlopen(req,timeout=20) as response:
        if urlsplit(response.geturl()).scheme not in ('http','https'):raise ValueError('Недопустимый редирект')
        body=response.read(6*1024*1024+1)
    profiles=parse_catalog(body)
    ROOT.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.profiles-',dir=ROOT))
    old=ROOT/'.profiles-old'
    try:
        settings=engine.settings()
        for i,raw in enumerate(profiles):
            effective=engine.build_profile(raw,int(settings['port']),settings['route_mode'],settings['target'])
            engine.verify_profile(effective)
            save_json(stage/f'{i}.json',raw)
        if old.exists():shutil.rmtree(old)
        if CATALOG.exists():os.replace(CATALOG,old)
        try:os.replace(stage,CATALOG)
        except Exception:
            if old.exists():os.replace(old,CATALOG)
            raise
        if old.exists():shutil.rmtree(old)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {'ok':True,'count':len(profiles)}

def list_catalog():
    profiles=[]
    if CATALOG.exists():
        for path in sorted(CATALOG.glob('*.json'),key=lambda p:int(p.stem)):
            raw=load(path,{})
            profiles.append({'id':int(path.stem),'name':profile_name(raw,int(path.stem)),
                             'outbounds':len(raw.get('outbounds',[])),
                             'balancers':len(raw.get('routing',{}).get('balancers',[]))})
    selected=load(SELECTED,{}).get('id')
    if selected is not None and (not (CATALOG/f'{selected}.json').exists() or load(CATALOG/f'{selected}.json',{})!=load(engine.PROFILE,{})):
        selected=None
    c=config()
    return {'profiles':profiles,'selected':selected,'configured':bool(c['url']),
            'user_agent':c['user_agent'],'remnawave':c['remnawave']}

def select_profile(profile_id):
    if type(profile_id) is not int or not 0<=profile_id<128:raise ValueError('Неверный ID')
    path=CATALOG/f'{profile_id}.json'
    if not path.exists():raise ValueError('Профиль отсутствует')
    raw=load(path,{})
    v=engine.settings()
    engine.verify_profile(engine.build_profile(raw,int(v['port']),v['route_mode'],v['target']))
    save_json(engine.PROFILE,raw)
    save_json(SELECTED,{'id':profile_id})
    return {'ok':True,'name':profile_name(raw,profile_id)}
