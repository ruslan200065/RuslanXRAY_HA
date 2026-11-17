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
            result['devices'] = [descriptor_report(dev) for dev in devices]
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
        ]
        if not matches:
            raise ValueError('Requested USB interface is not a vendor-specific WLAN candidate')
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
