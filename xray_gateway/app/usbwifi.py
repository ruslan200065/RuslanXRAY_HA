"""Safe, opt-in loading of the host's native RTL8851BU Wi-Fi driver."""
import platform
import subprocess
import time
from pathlib import Path

USB_ROOT = Path('/sys/bus/usb/devices')
MODULE = 'rtw89_8851bu'
VENDOR = '0bda'
PRODUCT = 'b851'


def execute(args, timeout=12):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return {'returncode': proc.returncode, 'stdout': proc.stdout[-1500:], 'stderr': proc.stderr[-1500:]}
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {'returncode': -1, 'stdout': '', 'stderr': str(exc)}


def _read_text(path):
    try:
        return path.read_text().strip().lower()
    except OSError:
        return ''


def discover():
    """Only enumerate USB devices matching the known RTL8851BU ID."""
    result = []
    if not USB_ROOT.exists():
        return result
    for dev in USB_ROOT.iterdir():
        if (_read_text(dev / 'idVendor'), _read_text(dev / 'idProduct')) != (VENDOR, PRODUCT):
            continue
        wifi_interfaces = set()
        bound_drivers = set()
        for iface in USB_ROOT.glob(dev.name + ':*'):
            driver = iface / 'driver'
            if driver.exists():
                try:
                    bound_drivers.add(driver.resolve().name)
                except OSError:
                    pass
            net = iface / 'net'
            if net.is_dir():
                wifi_interfaces.update(child.name for child in net.iterdir())
        result.append({'sysfs_device': dev.name, 'usb_id': VENDOR + ':' + PRODUCT,
                       'interfaces': sorted(wifi_interfaces), 'drivers': sorted(bound_drivers)})
    return result


def state():
    devices = discover()
    kernel = platform.uname().release
    available = execute(['modinfo', '-n', MODULE])
    module_file = available['stdout'].strip() if available['returncode'] == 0 else ''
    loaded = (Path('/sys/module') / MODULE).is_dir()
    if not devices:
        condition = 'not_connected'
        message = 'USB адаптер Realtek 0bda:b851 не найден'
    elif any(d['interfaces'] for d in devices):
        condition = 'ready'
        message = 'Wi-Fi интерфейс USB адаптера найден. Выбери его в разделе Сеть.'
    elif loaded:
        condition = 'loaded_without_interface'
        message = 'Модуль загружен, но Wi-Fi интерфейс не создан. Проверь прошивку и сообщения ядра хоста.'
    elif module_file:
        condition = 'module_ready'
        message = 'Модуль драйвера доступен в ядре HAOS. Можно загрузить его кнопкой ниже.'
    else:
        condition = 'module_missing'
        message = ('Для ядра HAOS ' + kernel + ' нет модуля ' + MODULE +
                   '. Его нельзя установить обычным apk или pip внутри дополнения; '
                   'нужен модуль, собранный под это ядро, или HAOS с поддержкой драйвера.')
    return {'device': 'Realtek RTL8851BU', 'usb_id': VENDOR + ':' + PRODUCT,
            'module': MODULE, 'kernel': kernel, 'module_file': module_file,
            'module_available': bool(module_file), 'module_loaded': loaded,
            'devices': devices, 'status': condition, 'message': message,
            'modinfo_error': available['stderr'][-500:] if not module_file else ''}


def activate():
    """Never accept a module name from the client or fetch/execute external code."""
    current = state()
    if not current['devices']:
        raise ValueError('Сначала подключи USB адаптер Realtek 0bda:b851')
    if current['status'] == 'ready':
        return current
    if not current['module_available'] and not current['module_loaded']:
        raise ValueError(current['message'])
    result = execute(['modprobe', MODULE], timeout=20)
    if result['returncode'] != 0:
        raise ValueError('Не удалось загрузить модуль ядра ' + MODULE + ': ' +
                         (result['stderr'] or result['stdout'])[-1000:])
    time.sleep(1)
    return state()
