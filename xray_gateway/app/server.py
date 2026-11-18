import json, os, subprocess, threading, time, signal
from functools import wraps
from urllib.error import HTTPError, URLError
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException
import engine
import network
import subscriptions
import latency
app=Flask(__name__,static_folder=None)
app.config['MAX_CONTENT_LENGTH']=2*1024*1024
LOCK=threading.RLock()
PING_LOCK=threading.Lock()
process=None
LAST_ERROR=''
REFRESH = {'state': 'idle'}
services=network.ClientServices()

def reply_error(e,code=400):return jsonify({'error':str(e)}),code

def serialized(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        with LOCK:
            return view(*args, **kwargs)
    return wrapped

def xray_running():
    return process is not None and process.poll() is None

def gateway_started():
    return process is not None or bool(services.daemons)

def stop_xray(timeout=5):
    global process
    if xray_running():
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    process=None

@app.errorhandler(Exception)
def error_handler(e):
    if isinstance(e,HTTPException):return reply_error(e.description,e.code)
    if isinstance(e, (ValueError, TypeError)):
        return reply_error(e)
    app.logger.exception('API failure')
    return reply_error('Внутренняя ошибка. Подробности в журнале приложения.',500)

@app.before_request
def ingress_only():
    # HA ingress requests originate through the Supervisor gateway.
    remote=request.remote_addr or ''
    if remote != '172.30.32.2':
        return reply_error('Доступ только через HA Ingress',403)

APP_DIR = Path(__file__).resolve().parent

@app.get('/')
def index():
    return send_from_directory(APP_DIR, 'index.html')

@app.get('/panel.css')
def panel_css():
    return send_from_directory(APP_DIR, 'panel.css')

@app.get('/panel.js')
def panel_js():
    return send_from_directory(APP_DIR, 'panel.js')
@app.get('/api/status')
@serialized
def status():
    settings = engine.settings().copy()
    settings['wifi_password_saved'] = bool(settings.pop('wifi_password'))
    interfaces = engine.interfaces()
    rules = engine.rules_present()
    started = gateway_started()
    healthy = xray_running() and rules and services.alive()
    ap = services.ap_health() if healthy else None
    phase, message = network.client_network_health(settings, interfaces) if healthy else ('failed', '')
    if phase == 'ready' and ap:
        phase, message = ap['phase'], ap.get('message', '')
    dhcp = None
    if healthy and settings['client_service'] in ('dhcp', 'wifi_ap'):
        dhcp = {'leases': network.dhcp_lease_count(settings)}
    return jsonify({'interfaces': interfaces, 'settings': settings,
       'profile_present': engine.PROFILE.is_file(), 'running': healthy and phase == 'ready',
       'needs_stop': started or rules, 'rules_present': rules,
       'starting': healthy and phase == 'starting',
       'wifi_status': message if phase == 'starting' else '',
       'dhcp': dhcp,
       'error': LAST_ERROR or (message if phase == 'failed' else '')})

@app.post('/api/settings')
@serialized
def update_settings():
    if gateway_started():return reply_error('Сначала остановите шлюз')
    v=request.get_json(force=True)
    if not isinstance(v,dict):return reply_error('Неверный формат')
    v={**engine.defaults(),**{k:v[k] for k in engine.defaults() if k in v}}
    if not v['wifi_password']:
        v['wifi_password'] = engine.settings()['wifi_password']
    engine.validate_settings(v,require_address=False)
    engine.save_settings(v)
    return jsonify({'ok':True})

@app.post('/api/profile')
@serialized
def upload_profile():
    global LAST_ERROR
    if gateway_started():return reply_error('Остановите шлюз перед обновлением профиля')
    obj=request.get_json(force=True)
    if not isinstance(obj,dict) or 'profile' not in obj:return reply_error('Ожидается объект profile')
    cfg=obj['profile']
    settings=engine.settings()
    effective=engine.build_profile(cfg,int(settings['port']),settings['route_mode'],settings['target'])
    engine.verify_profile(effective)
    engine.DATA.mkdir(parents=True,exist_ok=True)
    tmp=engine.PROFILE.with_suffix('.tmp');tmp.write_text(json.dumps(cfg,ensure_ascii=False,indent=2));os.chmod(tmp,0o600);tmp.replace(engine.PROFILE)
    subscriptions.SELECTED.unlink(missing_ok=True)
    LAST_ERROR=''
    return jsonify({'ok':True,'outbounds':len(cfg['outbounds'])})

@app.get('/api/subscription')
@serialized
def subscription_info():
    return jsonify(subscriptions.list_catalog())

@app.post('/api/subscription')
@serialized
def subscription_save():
    if gateway_started():return reply_error('Сначала остановите шлюз')
    obj = request.get_json()
    if not isinstance(obj, dict):
        return reply_error('Ожидается объект настроек подписки')
    return jsonify(subscriptions.save_config(obj))

@app.post('/api/subscription/refresh')
@serialized
def subscription_refresh():
    if gateway_started():return reply_error('Сначала остановите шлюз')
    global REFRESH
    if REFRESH['state'] == 'running':
        return jsonify(REFRESH), 202
    config = subscriptions.config()
    if not config['url']:
        return reply_error('Сначала сохраните URL подписки')
    REFRESH = {'state': 'running'}
    threading.Thread(target=refresh_subscription, args=(config, engine.settings()), daemon=True).start()
    return jsonify(REFRESH), 202


@app.get('/api/subscription/refresh')
@serialized
def subscription_refresh_status():
    return jsonify(REFRESH)


def refresh_subscription(config, settings):
    global REFRESH
    try:
        profiles, skipped = subscriptions.fetch_catalog(config, settings)
        with LOCK:
            if gateway_started() or config != subscriptions.config() or settings != engine.settings():
                raise ValueError('Настройки изменились во время обновления. Запустите обновление снова.')
            REFRESH = {'state': 'done', **subscriptions.replace_catalog(profiles, skipped)}
            return
    except HTTPError as error:
        message = f'Сервер подписки вернул HTTP {error.code}.'
    except (URLError, TimeoutError):
        message = 'Сервер подписки недоступен. Прежний каталог сохранён.'
    except ValueError as error:
        message = str(error)
    except Exception:
        app.logger.error('Could not refresh subscription')
        message = 'Не удалось обновить подписку. Проверьте журнал приложения.'
    with LOCK:
        REFRESH = {'state': 'error', 'error': message}

@app.post('/api/subscription/select')
@serialized
def subscription_select():
    if gateway_started():return reply_error('Сначала остановите шлюз')
    obj = request.get_json()
    if not isinstance(obj, dict):
        return reply_error('Ожидается объект с ID профиля')
    return jsonify(subscriptions.select_profile(obj.get('id'), obj.get('revision')))

@app.post('/api/ping')
def ping():
    obj = request.get_json()
    if not isinstance(obj, dict):
        return reply_error('Ожидается объект настроек проверки')
    if not PING_LOCK.acquire(blocking=False):
        return reply_error('Проверка уже идёт', 409)
    try:
        with LOCK:
            profile_id = obj.get('id')
            if profile_id is None:
                if not engine.PROFILE.is_file():
                    return reply_error('Сначала выберите профиль')
                profile = subscriptions.load(engine.PROFILE, None)
            else:
                profile = subscriptions.get_profile(profile_id, obj.get('revision'))
            settings = engine.settings()
        return jsonify({'milliseconds': latency.measure(profile, settings)})
    finally:
        PING_LOCK.release()

@app.get('/api/preview')
@serialized
def preview():
    settings=engine.settings();u,d,port=engine.validate_settings(settings)
    if not engine.PROFILE.is_file():return reply_error('Сначала загрузите JSON-профиль')
    cfg=engine.build_profile(json.loads(engine.PROFILE.read_text()),port,settings['route_mode'],settings['target'])
    engine.verify_profile(cfg)
    return jsonify({'nft':engine.nft_rules(d,port,bool(settings['block_ipv6']),
                                engine.local_lan_paths(settings, engine.interfaces()) if settings.get('lan_access') else None),
                    'dhcp': network.dnsmasq_config(settings) if settings['client_service']!='manual' else '',
                    'wifi': network.hostapd_config(settings).replace(settings['wifi_password'],'***') if settings['client_service']=='wifi_ap' else '',
                    'routes':engine.route_commands(),'note':'Адрес клиентского интерфейса назначается временно. NetworkManager и default route HAOS не изменяются. При включённом wifi_apply_regdomain iw reg set изменит глобальный Wi-Fi регион хоста; при остановке будет попытка восстановить предыдущий регион.'})

@app.post('/api/start')
def start():
    global process,LAST_ERROR
    with LOCK:
        if gateway_started():return reply_error('Сначала остановите шлюз')
        network.restore_downstream(recover=True)
        if engine.rules_present():return reply_error('Обнаружены оставшиеся firewall-правила; нажмите «Остановить»')
        if not engine.PROFILE.is_file():return reply_error('Загрузите конфигурацию Xray')
        v=engine.settings();u,d,port=engine.validate_settings(v)
        cfg=engine.build_profile(json.loads(engine.PROFILE.read_text()),port,v['route_mode'],v['target'])
        engine.verify_profile(cfg)
        tmp=engine.EFFECTIVE.with_suffix('.tmp');tmp.write_text(json.dumps(cfg,ensure_ascii=False,indent=2));os.chmod(tmp,0o600);tmp.replace(engine.EFFECTIVE)
        rules_applied = False
        try:
            logpath = engine.DATA / 'xray.log'
            if logpath.exists() and logpath.stat().st_size > 1024*1024:
                logpath.write_bytes(logpath.read_bytes()[-65536:])
            with open(logpath,'ab') as log:
                process=subprocess.Popen(['/usr/local/bin/xray','run','-c',str(engine.EFFECTIVE)],stdout=log,stderr=subprocess.STDOUT)
            time.sleep(.6)
            if process.poll() is not None:
                message=logpath.read_text(errors='replace')[-2500:]
                process=None
                app.logger.error('Xray stopped during startup: %s', message)
                raise ValueError('Xray не запустился. Проверьте профиль и журнал приложения.')
            engine.apply_rules(v)
            rules_applied = True
            services.start(v,engine.interfaces())
            LAST_ERROR=''
        except Exception as e:
            try:
                stop_xray()
                services.stop()
            finally:
                if rules_applied:
                    engine.remove_rules()
            LAST_ERROR=str(e)
            return reply_error(e)
        return jsonify({'ok':True})

@app.post('/api/stop')
def stop():
    global LAST_ERROR
    with LOCK:
        try:
            stop_xray()
            services.stop()
        finally:
            engine.remove_rules()
        LAST_ERROR=''
    return jsonify({'ok':True})

@app.post('/api/reset')
@serialized
def reset():
    if gateway_started():return reply_error('Сначала остановите шлюз')
    engine.remove_rules()
    return jsonify({'ok':True})

@app.get('/api/wifi-capabilities')
def wifi_capabilities():
    dev=request.args.get('device',engine.settings().get('downstream',''))
    selected=next((i for i in engine.interfaces() if i['name']==dev and i['wifi']),None)
    if not selected:return reply_error('Выберите Wi-Fi интерфейс в настройках сети')
    return jsonify(network.radio_capabilities(dev))

def shutdown(*_):
    try:
        stop_xray(timeout=4)
        services.stop()
    finally:
        if engine.rules_present():
            engine.remove_rules()
    raise SystemExit(0)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,shutdown)
    from waitress import serve

    try:
        network.restore_downstream(recover=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        app.logger.exception('Не удалось восстановить управление выделенным портом')

    serve(app, host='0.0.0.0', port=int(os.environ.get('INGRESS_PORT', '8099')),
          max_request_body_size=app.config['MAX_CONTENT_LENGTH'])
