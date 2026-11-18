#!/bin/sh
set -eu
if [ "${RGW_BRIDGE_LAB:-}" != 1 ] || [ ! -f /.dockerenv ]; then
  echo "Run only in the isolated Docker test container described in tools/README.md." >&2
  exit 1
fi
unshare -n sleep 120 &
r=$!
unshare -n sleep 120 &
c=$!
trap 'kill "$r" "$c" 2>/dev/null || true' EXIT
sleep 0.3
ip link add brtest type bridge
ip link add rlan type veth peer name rpeer
ip link add clan type veth peer name cpeer
ip link set rpeer netns "$r"
ip link set cpeer netns "$c"
ip link set rlan master brtest
ip link set clan master brtest
ip link set rlan up
ip link set clan up
ip link set brtest up
ip addr add 192.168.80.2/24 dev brtest
ip route replace 198.18.0.1/32 via 192.168.80.1
rn() { nsenter -t "$r" -n "$@"; }
cn() { nsenter -t "$c" -n "$@"; }
rn ip link set lo up
rn ip link set rpeer up
rn ip addr add 192.168.80.1/24 dev rpeer
rn ip addr add 198.18.0.1/32 dev lo
cn ip link set lo up
cn ip link set cpeer up
rn dnsmasq --keep-in-foreground --port=0 --bind-interfaces --interface=rpeer \
  --dhcp-range=192.168.80.100,192.168.80.110,255.255.255.0,10m --dhcp-option=option:router,192.168.80.1 \
  --dhcp-leasefile=/tmp/router-leases --log-facility=- >/tmp/router.log 2>&1 &
rn python3 -m http.server 8080 --bind 0.0.0.0 >/tmp/http.log 2>&1 &
cn python3 -m http.server 8081 --bind 0.0.0.0 >/tmp/client-http.log 2>&1 &
mac=$(cat /sys/class/net/brtest/address)
cat >/tmp/bridge.nft <<NFT
table netdev rgw_bridge_lab {
  chain divert {
    type filter hook ingress device "clan" priority -500; policy accept;
    ether type ip ip daddr { 192.168.80.0/24, 0.0.0.0/8, 224.0.0.0/4, 240.0.0.0/4 } return
    ether type arp return
    ether type ip meta l4proto tcp ether daddr set $mac meta pkttype set host meta mark set 0x233 counter accept
    counter drop
  }
}
table ip rgw_proxy_lab {
  chain intercept {
    type nat hook prerouting priority dstnat; policy accept;
    iifname "brtest" meta mark 0x233 tcp dport 1-65535 counter redirect to :12345
  }
  chain protect {
    type filter hook forward priority 0; policy accept;
    iifname "brtest" meta mark 0x233 counter drop
  }
}
NFT
nft -c -f /tmp/bridge.nft
nft -f /tmp/bridge.nft
python3 - <<'PY'
import engine,json
from pathlib import Path
profile = engine.build_profile({'outbounds':[{'tag':'lab','protocol':'freedom'}]},12345,'native')
profile['inbounds'][-1]['streamSettings']['sockopt']['tproxy']='redirect'
Path('/tmp/xray.json').write_text(json.dumps(profile))
PY
xray run -c /tmp/xray.json >/tmp/xray.log 2>&1 &
x=$!
cn udhcpc -q -n -t 4 -T 2 -i cpeer >/tmp/client.log 2>&1 || { cat /tmp/client.log /tmp/router.log; exit 1; }
ip=$(cn ip -j -4 addr show cpeer | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["addr_info"][0]["local"])')
echo "Lease $ip"
cn curl -fsS --max-time 3 -o /dev/null http://192.168.80.1:8080/
rn curl -fsS --max-time 3 -o /dev/null "http://$ip:8081/"
echo "LAN both directions OK"
cn curl -fsS --max-time 4 -o /dev/null http://198.18.0.1:8080/ || { nft list ruleset; cat /tmp/xray.log; exit 1; }
kill "$x"
wait "$x" || true
if cn curl -fsS --max-time 2 -o /dev/null http://198.18.0.1:8080/ 2>/dev/null; then
  echo 'Client bypassed Xray'; exit 1
fi
cn curl -fsS --max-time 3 -o /dev/null http://192.168.80.1:8080/
echo "External traffic intercepted; no direct bypass when Xray stops"
