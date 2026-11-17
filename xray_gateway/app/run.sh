#!/command/with-contenv bashio
set -e
export INGRESS_PORT="$(bashio::app.ingress_port)"
exec python3 /app/server.py
