# RTL8851BU userspace AP — план разработки

**Цель:** USB Realtek `0bda:b851` через libusb в дополнении RuslanXRAY HA, без собственного модуля `rtw89_8851bu.ko` в ядре.

## Существующая база (v0.3.9)

- `usb: true` монтирует `/dev/bus/usb` в дополнение; Python PyUSB / libusb.
- `app/usbuserspace.py`: поиск устройства, стандартные USB-дескрипторы, конфигурации, endpoints, транспорт `RTL8851BUTransport` с явным захватом только vendor-specific интерфейса, без автоматического отсоединения драйвера.
- `GET /api/usb-wifi/userspace` и интерфейс «USB Wi-Fi»: только чтение дескрипторов. **Точка доступа не реализована.**

## Этапы, обязательные для действующего Wi-Fi AP

1. **USB transport:** выделить и проверить WLAN USB-интерфейс `0bda:b851`, endpoint'ы и безопасный claim/release. Не захватывать Bluetooth-интерфейс `e0:01:01` и не вмешиваться в встроенный Intel Wi-Fi.
2. **Инициализация RTL8851BU:** портировать аппаратную часть `rtw89` для USB: регистры, vendor control requests, eFuse/MAC адрес, power sequence, USB TX/RX очереди и сообщения прошивки. Основной первоисточник — Linux `drivers/net/wireless/realtek/rtw89/{usb.c,rtw8851b.c,rtw8851bu.c}`. Лицензии исходников сохранить.
3. **Firmware:** загрузка и проверка совместимой `rtw89/rtw8851b_fw.bin` с соблюдением лицензии linux-firmware, handshakes и обработки ошибок. Не отправлять команды firmware в реальный USB-адаптер, пока последовательности не подтверждены исходниками.
4. **802.11 AP:** управление каналом и регуляторным доменом, beacon, (re)association, WPA2 handshake и ключи, TX/RX 802.11 кадров. Для обычного `hostapd -Dnl80211` требуется kernel Wi-Fi интерфейс. Вариант `userspace-wifi hwsim` требует `mac80211_hwsim`, то есть модуль ядра. Для **полностью kernel-module-free AP** потребуется интеграция с userspace 802.11 AP stack (или собственный backend для hostapd), а не один лишь libusb.
5. **IPv4 gateway:** userspace 802.11 ↔ Ethernet/TAP интерфейс, DHCP, Xray TProxy, fail-closed, изоляция клиента, тесты roaming/disconnect.
6. **Стабильность:** тесты с конкретным устройством (USB 2/3, 5 ГГц, WPA2, перезапуск, переподключение), регуляторные ограничения, безопасный teardown.

## Приоритеты и ограничения

- Никаких фиктивных WLAN интерфейсов или обещаний «готово» до подтверждённой работы AP.
- Стадия 1 только читает **стандартные USB-дескрипторы**. Пробная радиопередача и запись vendor-регистров по HTTP недоступны.
- `userspace-wifi` от Kismet поддерживает семейства `rtw88` и `mt76` и в настоящее время **не поддерживает `rtw89` / RTL8851BU**. Поддержка monitor mode не равна поддержке AP.
- USB всё равно использует обычный USB стек Linux и хост-контроллер; под «без драйвера ядра» понимается отсутствие собственного **Wi-Fi** kernel driver.

## Исходники для портирования

- https://github.com/kismetwireless/userspace-wifi
- https://github.com/morrownr/rtw89
- https://lwn.net/Articles/1027994/
- https://wireless.docs.kernel.org/en/latest/en/users/drivers/mac80211_hwsim.html
- https://github.com/pyusb/pyusb
