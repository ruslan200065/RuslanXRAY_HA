# Стенд прозрачного моста

Сценарий создаёт имитацию основного роутера и клиента в отдельных сетевых пространствах внутри временного контейнера. Роутер выдаёт DHCP-адрес, клиент и роутер обмениваются IPv4-трафиком через мост. Запрос к тестовому внешнему IPv4-адресу проходит через Xray. После остановки Xray внешний запрос должен перестать работать, а локальный — по-прежнему проходить.

Из корня репозитория выполните команды в PowerShell:

```powershell
docker build -t ruslan-bridge-lab xray_gateway
$lab = (Resolve-Path tools/transparent-bridge-lab.sh).Path
docker run --rm --cap-add NET_ADMIN --cap-add SYS_ADMIN --security-opt seccomp=unconfined -e RGW_BRIDGE_LAB=1 --mount "type=bind,source=$lab,target=/tmp/lab.sh,readonly" --entrypoint sh ruslan-bridge-lab /tmp/lab.sh
```

Запускайте только во временном контейнере с отдельным сетевым пространством. Подключать сетевое пространство хоста к контейнеру для этого теста нельзя.

Стенд проверяет внешний TCP и локальный IPv4. Внешний UDP и IPv6 блокируются на клиентском порту. Интеграция с NetworkManager на HAOS, перенос адреса управления и автоматический откат не входят в стенд.
