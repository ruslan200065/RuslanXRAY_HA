import json, os, subprocess, threading, time, signal
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException
import engine
import network
import subscriptions
import usbuserspace
import rtw8851b_firmware
app=Flask(__name__,static_folder=None)
app.config['MAX_CONTENT_LENGTH']=2*1024*1024
LOCK=threading.RLock()
process=None
LAST_ERROR=''
services=network.ClientServices()

def reply_error(e,code=400):return jsonify({'error':str(e)}),code

@app.errorhandler(Exception)
def error_handler(e):
    if isinstance(e,HTTPException):return reply_error(e.description,e.code)
    app.logger.exception('API failure')
    return reply_error(e,500)

@app.before_request
def ingress_only():
    # HA ingress requests originate through the Supervisor gateway.
    remote=request.remote_addr or ''
    if remote != '172.30.32.2':
        return reply_error('Доступ только через HA Ingress',403)

@app.get('/')
def index():return send_from_directory('/app','index.html')
@app.get('/api/status')
def status():
    global process
    return jsonify({'interfaces':engine.interfaces(),'settings':engine.settings(),
       'profile_present':engine.PROFILE.is_file(), 'running':bool(process and process.poll() is None),
       'rules_present':engine.rules_present(),'services_running':services.alive(),
       'error':LAST_ERROR})

@app.post('/api/settings')
def update_settings():
    if process and process.poll() is None:return reply_error('Сначала остановите шлюз')
    v=request.get_json(force=True)
    if not isinstance(v,dict):return reply_error('Неверный формат')
    v={**engine.defaults(),**{k:v[k] for k in engine.defaults() if k in v}}
    engine.validate_settings(v,require_address=False)
    engine.save_settings(v)
    return jsonify({'ok':True,'settings':v})

@app.post('/api/profile')
def upload_profile():
    global LAST_ERROR
    if process and process.poll() is None:return reply_error('Остановите шлюз перед обновлением профиля')
    obj=request.get_json(force=True)
    if not isinstance(obj,dict) or 'profile' not in obj:return reply_error('Ожидается объект profile')
    cfg=obj['profile']
    settings=engine.settings()
    effective=engine.build_profile(cfg,int(settings['port']),settings['route_mode'],settings['target'])
    engine.verify_profile(effective)
    engine.DATA.mkdir(parents=True,exist_ok=True)
    tmp=engine.PROFILE.with_suffix('.tmp');tmp.write_text(json.dumps(cfg,ensure_ascii=False,indent=2));os.chmod(tmp,0o600);tmp.replace(engine.PROFILE)
    LAST_ERROR=''
    return jsonify({'ok':True,'outbounds':len(cfg['outbounds'])})

@app.get('/api/subscription')
def subscription_info():
    return jsonify(subscriptions.list_catalog())

@app.post('/api/subscription')
def subscription_save():
    if process and process.poll() is None:return reply_error('Сначала остановите шлюз')
    with LOCK:
        return jsonify(subscriptions.save_config(request.get_json(force=True)))

@app.post('/api/subscription/refresh')
def subscription_refresh():
    if process and process.poll() is None:return reply_error('Сначала остановите шлюз')
    with LOCK:
        return jsonify(subscriptions.update_catalog())

@app.post('/api/subscription/select')
def subscription_select():
    if process and process.poll() is None:return reply_error('Сначала остановите шлюз')
    with LOCK:
        return jsonify(subscriptions.select_profile(request.get_json(force=True).get('id')))

@app.get('/api/preview')
def preview():
    settings=engine.settings();u,d,port=engine.validate_settings(settings)
    if not engine.PROFILE.is_file():return reply_error('Сначала загрузите JSON-профиль')
    cfg=engine.build_profile(json.loads(engine.PROFILE.read_text()),port,settings['route_mode'],settings['target'])
    engine.verify_profile(cfg)
    return jsonify({'nft':engine.nft_rules(d,port,bool(settings['block_ipv6'])),
                    'dhcp': network.dnsmasq_config(settings) if settings['client_service']!='manual' else '',
                    'wifi': network.hostapd_config(settings).replace(settings['wifi_password'],'***') if settings['client_service']=='wifi_ap' else '',
                    'routes':engine.route_commands(),'note':'Адрес клиентского интерфейса назначается временно. NetworkManager и default route HAOS не изменяются. При включённом wifi_apply_regdomain iw reg set изменит глобальный Wi-Fi регион хоста; при остановке будет попытка восстановить предыдущий регион.'})

@app.post('/api/start')
def start():
    global process,LAST_ERROR
    with LOCK:
        if process and process.poll() is None:return reply_error('Уже запущено')
        if engine.rules_present():return reply_error('Обнаружены оставшиеся firewall-правила; нажмите Сбросить правила')
        if not engine.PROFILE.is_file():return reply_error('Загрузите конфигурацию Xray')
        v=engine.settings();u,d,port=engine.validate_settings(v)
        cfg=engine.build_profile(json.loads(engine.PROFILE.read_text()),port,v['route_mode'],v['target'])
        engine.verify_profile(cfg)
        tmp=engine.EFFECTIVE.with_suffix('.tmp');tmp.write_text(json.dumps(cfg,ensure_ascii=False,indent=2));os.chmod(tmp,0o600);tmp.replace(engine.EFFECTIVE)
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
                raise ValueError('Xray остановился: '+message)
            engine.apply_rules(v)
            services.start(v,engine.interfaces())
            LAST_ERROR=''
        except Exception as e:
            if process and process.poll() is None:process.terminate();process.wait(timeout=5)
            process=None
            services.stop()
            engine.remove_rules()
            LAST_ERROR=str(e)
            return reply_error(e)
        return jsonify({'ok':True})

@app.post('/api/stop')
def stop():
    global process
    with LOCK:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        process=None
        services.stop()
        engine.remove_rules()
    return jsonify({'ok':True})

@app.post('/api/reset')
def reset():
    if process and process.poll() is None:return reply_error('Сначала остановите шлюз')
    engine.remove_rules()
    return jsonify({'ok':True})

@app.get('/api/usb-wifi/firmware')
def usb_wifi_firmware():
    # Metadata only. The image is never flashed or sent to the hardware here.
    return jsonify(rtw8851b_firmware.inventory())

@app.get('/api/usb-wifi/userspace')
def usb_userspace_status():
    return jsonify(usbuserspace.probe())

@app.post('/api/usb-wifi/userspace/claim-test')
def usb_userspace_claim_test():
    # Explicit action: claims and releases only one validated vendor WLAN USB
    # interface. Never sends firmware commands or detaches a host driver.
    with LOCK:
        return jsonify(usbuserspace.claim_test())

@app.post('/api/usb-wifi/userspace/read-power')
def usb_userspace_read_power():
    # Fixed read-only R_AX_SYS_PW_CTRL at 0x0004; user-triggered only.
    with LOCK:
        return jsonify(usbuserspace.read_chip_power_register())

@app.post('/api/usb-wifi/userspace/snapshot')
def usb_userspace_register_snapshot():
    # Fixed allowlist of read-only chip registers; no client-supplied address.
    with LOCK:
        return jsonify(usbuserspace.snapshot_chip_registers())

@app.get('/api/wifi-capabilities')
def wifi_capabilities():
    dev=request.args.get('device',engine.settings().get('downstream',''))
    selected=next((i for i in engine.interfaces() if i['name']==dev and i['wifi']),None)
    if not selected:return reply_error('Выберите Wi-Fi интерфейс в настройках сети')
    return jsonify(network.radio_capabilities(dev))

@app.get('/api/diagnostics')
def diagnostics():
    wifi={}
    for item in engine.interfaces():
        if item['wifi']:
            try: wifi[item['name']]=network.radio_capabilities(item['name'])
            except Exception as exc: wifi[item['name']]={'error':str(exc)}
    return jsonify({'interfaces':engine.interfaces(),'wifi':wifi,'usb_userspace':usbuserspace.probe(),'usb_firmware':rtw8851b_firmware.inventory(),
       'routes':engine.run(['ip','-4','route','show'],check=False).stdout[-8000:],
       'rules':engine.run(['nft','list','table','inet',engine.NFT_NAME],check=False).stdout[-8000:],
       'service_logs': {n:(network.RUNTIME/(n+'.log')).read_text(errors='replace')[-3000:] if (network.RUNTIME/(n+'.log')).exists() else '' for n in ('hostapd','dnsmasq')},
       'xray_log':(engine.DATA/'xray.log').read_text(errors='replace')[-4000:] if (engine.DATA/'xray.log').exists() else '',
       'regdomain_restore':(network.RUNTIME/'regdomain_restore.log').read_text(errors='replace')[-500:] if (network.RUNTIME/'regdomain_restore.log').exists() else ''})

def shutdown(*_):
    global process
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=4)
        except subprocess.TimeoutExpired: process.kill();process.wait()
    services.stop()
    engine.remove_rules()
    raise SystemExit(0)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,shutdown)
    app.run(host='0.0.0.0',port=int(os.environ.get('INGRESS_PORT','8099')),threaded=True)
