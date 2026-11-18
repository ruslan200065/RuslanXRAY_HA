# Xray's official upstream image, current pinned version.
FROM ghcr.io/xtls/xray-core:26.10.31 AS xray
FROM ghcr.io/home-assistant/base:3.24
RUN apk add --no-cache python3 py3-flask py3-waitress iproute2 nftables curl jq dnsmasq hostapd iw dbus
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY --from=xray /usr/local/share/xray/ /usr/local/share/xray/
ENV XRAY_LOCATION_ASSET=/usr/local/share/xray
COPY app /app
# Reject Python syntax errors during Docker build, before HA can start the add-on.
RUN python3 -m compileall -q /app
RUN mkdir -p /data/ruslan-gateway && chmod +x /app/run.sh
WORKDIR /app
CMD ["/app/run.sh"]
