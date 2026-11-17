"""Measure HTTPS response time through an isolated instance of the chosen profile."""
import json
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import engine

CHECK_URL = 'https://www.gstatic.com/generate_204'
XRAY = '/usr/local/bin/xray'


def probe_config(profile, settings, port, password):
    cfg = engine.build_profile(profile, settings['port'], settings['route_mode'], settings['target'])
    inbound = cfg['inbounds'][-1]
    cfg['inbounds'] = [{
        'tag': inbound['tag'],
        'protocol': 'socks',
        'listen': '127.0.0.1',
        'port': port,
        'settings': {'auth': 'password', 'accounts': [{'user': 'probe', 'pass': password}], 'udp': False},
        'sniffing': inbound['sniffing'],
    }]
    for key in ('api', 'metrics', 'stats'):
        cfg.pop(key, None)
    cfg['log'] = {'loglevel': 'none'}
    return cfg


def wait_for_proxy(probe, address):
    deadline = time.monotonic() + 3
    while probe.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(address, timeout=.1):
                return
        except OSError:
            time.sleep(.05)
    raise ValueError('Не удалось запустить проверку для этого профиля')


def measure(profile, settings):
    password = secrets.token_hex(24)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        address = listener.getsockname()
    with tempfile.TemporaryDirectory(prefix='rgw-ping-') as folder:
        config_path = Path(folder) / 'config.json'
        config_path.write_text(json.dumps(probe_config(profile, settings, address[1], password)), encoding='utf-8')
        with subprocess.Popen(
            [XRAY, 'run', '-c', str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as probe:
            try:
                wait_for_proxy(probe, address)
                result = subprocess.run(
                    ['curl', '-q', '--silent', '--noproxy', '',
                     '--proxy', f'socks5h://127.0.0.1:{address[1]}',
                     '--proxy-user', f'probe:{password}',
                     '--max-time', '8', '--output', '/dev/null',
                     '--write-out', '%{http_code} %{time_starttransfer}', CHECK_URL],
                    capture_output=True, text=True, timeout=9, check=False,
                )
                if result.returncode == 28:
                    raise ValueError('Время ожидания истекло')
                code, _, seconds = result.stdout.strip().partition(' ')
                if result.returncode or code != '204':
                    raise ValueError('Профиль не получил ответ от сервера проверки')
                return round(float(seconds) * 1000)
            except subprocess.TimeoutExpired as error:
                raise ValueError('Время ожидания истекло') from error
            finally:
                if probe.poll() is None:
                    probe.terminate()
                    try:
                        probe.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        probe.kill()
