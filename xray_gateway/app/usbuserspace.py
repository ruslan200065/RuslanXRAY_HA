"""Userspace libusb transport bootstrap for Realtek RTL8851BU (0bda:b851).

Stage 1: read-only USB descriptors and endpoint discovery.  Does not claim a USB
interface, issue vendor commands, load firmware, or implement an 802.11 AP.
This module intentionally doesn't touch the Bluetooth interface.
"""
from pathlib import Path

VID, PID = 0x0BDA, 0xB851

USB_DIR_IN = 0x80
USB_TRANSFER = {0: 'control', 1: 'isochronous', 2: 'bulk', 3: 'interrupt'}


def descriptor_report(device):
    """Interpret cached standard USB descriptors without claiming the device."""
    configs = []
    for config in device:
        entries = []
        for interface in config:
            endpoints = [{
                'address': int(ep.bEndpointAddress),
                'direction': 'in' if int(ep.bEndpointAddress) & USB_DIR_IN else 'out',
                'type': USB_TRANSFER.get(int(ep.bmAttributes) & 0x03, 'unknown'),
                'max_packet_size': int(ep.wMaxPacketSize),
            } for ep in interface]
            entries.append({
                'number': int(interface.bInterfaceNumber),
                'alternate_setting': int(interface.bAlternateSetting),
                'class': f'{int(interface.bInterfaceClass):02x}',
                'subclass': f'{int(interface.bInterfaceSubClass):02x}',
                'protocol': f'{int(interface.bInterfaceProtocol):02x}',
                'endpoints': endpoints,
            })
        configs.append({'value': int(config.bConfigurationValue), 'interfaces': entries})
    return {
        'vendor_id': f'{int(device.idVendor):04x}',
        'product_id': f'{int(device.idProduct):04x}',
        'bus': int(device.bus or 0),
        'address': int(device.address or 0),
        'usb_version_bcd': f'{int(device.bcdUSB):04x}',
        'device_version_bcd': f'{int(device.bcdDevice):04x}',
        'configurations': configs,
    }


def wlan_candidates(report):
    """Interpret the descriptors the same way as Linux rtw89_usb_parse.

    The RTL8851BU USB driver is bound only to class ff/subclass ff/protocol ff;
    Bluetooth uses separate e0 interfaces and MUST NOT be claimed.
    This is topology validation, not a 802.11 driver or a firmware handshake.
    """
    candidates = []
    for config in report['configurations']:
        for interface in config['interfaces']:
            if (interface['alternate_setting'] != 0 or
                (interface['class'], interface['subclass'], interface['protocol']) !=
                    ('ff', 'ff', 'ff')):
                continue
            bulk_in = [x['address'] for x in interface['endpoints']
                       if x['type'] == 'bulk' and x['direction'] == 'in']
            bulk_out = [x['address'] for x in interface['endpoints']
                        if x['type'] == 'bulk' and x['direction'] == 'out']
            candidates.append({
                'interface_number': interface['number'],
                'configuration': config['value'],
                'bulk_in': bulk_in,
                'bulk_out': bulk_out,
                'valid_rtw89_topology': len(bulk_in) == 1 and len(bulk_out) >= 1,
                'rx_endpoint': bulk_in[0] if len(bulk_in) == 1 else None,
                'tx_endpoint_count': len(bulk_out),
            })
    return candidates


def claim_test():
    """Explicit, short-lived claim/release of RTL8851BU WLAN ONLY.

    Invoked only on user button press. NO firmware/vendor request,
    bulk transfer, kernel-driver detaching or configuration change.
    """
    try:
        import usb.core
        import usb.util
        from usb.backend import libusb1
    except ImportError as exc:
        raise ValueError('PyUSB/libusb недоступен: ' + str(exc))
    backend = libusb1.get_backend()
    if backend is None:
        raise ValueError('libusb backend недоступен')
    devices = list(usb.core.find(find_all=True, idVendor=VID,
                                 idProduct=PID, backend=backend) or [])
    if len(devices) != 1:
        raise ValueError('Для безопасного теста нужен ровно один RTL8851BU: найдено ' +
                         str(len(devices)))
    device = devices[0]
    report = descriptor_report(device)
    matches = [c for c in wlan_candidates(report) if c['valid_rtw89_topology']]
    if len(matches) != 1:
        raise ValueError('Не найден единственный интерфейс ff:ff:ff с одним Bulk IN '
                         'и минимум одним Bulk OUT. Захват отменён.')
    candidate = matches[0]
    number = candidate['interface_number']
    if number in (0, 1):
        raise ValueError('Отказ: обнаружен Bluetooth-интерфейс, захват запрещён')
    transport = RTL8851BUTransport(device)
    try:
        transport.open_wlan(number)
        return {
            'status': 'claim_success',
            'usb_id': '0bda:b851',
            'interface_number': number,
            'bulk_in': candidate['bulk_in'],
            'bulk_out': candidate['bulk_out'],
            'claimed_and_released': True,
            'bluetooth_untouched': True,
            'ap_ready': False,
            'message': ('Wi-Fi USB-интерфейс #'+str(number)+' успешно захвачен и '
                        'освобождён через libusb. Bluetooth интерфейсы не тронуты. '
                        'Радио/прошивка/AP пока не инициализируются.'),
        }
    except (OSError, RuntimeError, usb.core.USBError) as exc:
        raise ValueError('Захват Wi-Fi USB интерфейса #'+str(number)+
                         ' не удался: '+str(exc))
    finally:
        transport.close()
        usb.util.dispose_resources(device)


def probe():
    """Locate 0bda:b851 via libusb. Never use set_configuration/claim_interface."""
    result = {
        'stage': 'usb_descriptor_probe',
        'driver_ready': False,
        'ap_ready': False,
        'device_id': '0bda:b851',
        'access': 'read_only_descriptors',
        'devices': [],
        'message': '',
    }
    if not Path('/dev/bus/usb').is_dir():
        result['status'] = 'usb_not_mounted'
        result['message'] = 'Нет /dev/bus/usb. Для прямого доступа включи usb: true в дополнении HAOS.'
        return result
    try:
        import usb.core
        from usb.backend import libusb1
    except ImportError as exc:
        result['status'] = 'library_missing'
        result['message'] = 'В контейнере отсутствует PyUSB/libusb: ' + str(exc)
        return result
    backend = libusb1.get_backend()
    if backend is None:
        result['status'] = 'backend_missing'
        result['message'] = 'libusb-1.0 не найден в контейнере'
        return result
    try:
        devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID,
                                     backend=backend) or [])
        if not devices:
            result['status'] = 'device_missing'
            result['message'] = 'Realtek 0bda:b851 не обнаружен через libusb'
        else:
            result['devices'] = [dict(report, wlan_candidates=wlan_candidates(report)) for report in (descriptor_report(dev) for dev in devices)]
            result['status'] = 'usb_transport_available'
            result['message'] = (
                'Прямой доступ libusb работает: USB-дескрипторы прочитаны. '
                'Драйвер RTL8851BU в userspace, прошивка и точка доступа пока не реализованы.'
            )
    except (OSError, ValueError, usb.core.USBError) as exc:
        result['status'] = 'usb_access_error'
        result['message'] = 'Ошибка чтения USB-дескрипторов: ' + str(exc)
    return result


class RTL8851BUTransport:
    """Restricted userspace USB transport foundation, NOT a Wi-Fi driver.

    Constructor validates VID/PID; opening the WLAN interface is explicit.
    No other USB interfaces (including Bluetooth) will be detached or claimed.
    """

    def __init__(self, device):
        if (int(device.idVendor), int(device.idProduct)) != (VID, PID):
            raise ValueError('Unexpected USB hardware ID')
        self.device = device
        self.interface_number = None
        self.claimed = False

    def open_wlan(self, interface_number):
        if self.claimed:
            raise RuntimeError('WLAN interface already claimed')
        import usb.util
        number = int(interface_number)
        matches = [
            itf for cfg in self.device for itf in cfg
            if int(itf.bInterfaceNumber) == number
            and int(itf.bInterfaceClass) == 0xFF
            and int(itf.bInterfaceSubClass) == 0xFF
            and int(itf.bInterfaceProtocol) == 0xFF
            and int(itf.bAlternateSetting) == 0
        ]
        if not matches:
            raise ValueError('Requested USB interface is not a vendor-specific WLAN candidate')
        if number in (0, 1):
            raise ValueError('Bluetooth interfaces cannot be claimed')
        if self.device.is_kernel_driver_active(number):
            raise RuntimeError('USB interface occupied by a kernel driver; refusing to detach automatically')
        usb.util.claim_interface(self.device, number)
        self.interface_number = number
        self.claimed = True

    def close(self):
        if self.claimed:
            import usb.util
            try:
                usb.util.release_interface(self.device, self.interface_number)
            finally:
                self.claimed = False
                self.interface_number = None

    def _require_open(self):
        if not self.claimed:
            raise RuntimeError('WLAN USB interface is not claimed')


    def read_sys_pw_ctrl(self, timeout=500):
        """Read RTL8851B power register using upstream rtw89 USB vendor protocol.

        This performs only one READ of address 0x0004, never vendor WRITE.
        """
        self._require_open()
        address = 0x0004
        raw = self.device.ctrl_transfer(0xC0, 0x05,
                                        address & 0xffff,
                                        (address >> 16) & 0xff,
                                        4, timeout=timeout)
        data = bytes(raw)
        if len(data) != 4:
            raise IOError('Expected 4 bytes, got ' + str(len(data)))
        return {
            'register': 'R_AX_SYS_PW_CTRL',
            'address': '0x0004',
            'value': '0x%08x' % int.from_bytes(data, 'little'),
            'raw_le': data.hex(),
            'operation': 'vendor control read 0xc0/0x05',
        }

    def bulk_read(self, endpoint, length, timeout=1000):
        self._require_open()
        if not (0x80 <= endpoint <= 0x8F):
            raise ValueError('Not a USB IN endpoint')
        return bytes(self.device.read(endpoint, length, timeout=timeout))

    def bulk_write(self, endpoint, payload, timeout=1000):
        self._require_open()
        if not (0x01 <= endpoint <= 0x0F):
            raise ValueError('Not a USB OUT endpoint')
        return self.device.write(endpoint, payload, timeout=timeout)


def read_chip_power_register():
    """Optional USB control register read, claiming only WLAN interface 2.

    No reset, no firmware upload, no vendor writes, no radio TX.
    """
    try:
        import usb.core
        import usb.util
        from usb.backend import libusb1
    except ImportError as exc:
        raise ValueError('PyUSB/libusb unavailable: ' + str(exc))
    backend = libusb1.get_backend()
    if backend is None:
        raise ValueError('libusb backend unavailable')
    devices = list(usb.core.find(find_all=True, idVendor=VID,
                                 idProduct=PID, backend=backend) or [])
    if len(devices) != 1:
        raise ValueError('Expected one RTL8851BU, found ' + str(len(devices)))
    device = devices[0]
    report = descriptor_report(device)
    matches = [x for x in wlan_candidates(report) if x['valid_rtw89_topology']]
    if len(matches) != 1 or matches[0]['interface_number'] != 2:
        raise ValueError('WLAN interface 2 topology mismatch')
    transport = RTL8851BUTransport(device)
    try:
        transport.open_wlan(2)
        register = transport.read_sys_pw_ctrl()
        return {
            'status': 'register_read_success',
            'stage': 'rtl8851bu_vendor_read',
            'usb_id': '0bda:b851',
            'interface_number': 2,
            'register': register,
            'ap_ready': False,
            'message': ('RTL8851BU answered a read-only power register request. '
                        'No firmware or AP initialization was attempted.'),
        }
    except (OSError, ValueError, RuntimeError, usb.core.USBError) as exc:
        raise ValueError('RTL8851BU register read failed: ' + str(exc))
    finally:
        transport.close()
        usb.util.dispose_resources(device)
