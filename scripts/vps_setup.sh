#!/usr/bin/env bash
set -euo pipefail

# One-time migration: systemd -> pm2 + bare-IP nginx vhost. Idempotent.
# Staged code must already be at /root/llm-bridge-sync (scp -r from Mac).
SRC=/opt/llm-bridge
ACTIVE_FILE=/root/.llm-bridge-active
OLD_CFG=${1:-/root/.llm-bridge.json}

# 1. stop systemd (the single planned stop)
if systemctl list-unit-files 2>/dev/null | grep -q '^llm-bridge\.service'; then
  systemctl stop llm-bridge || true
  systemctl disable llm-bridge || true
fi

# 2. venv (never overwritten by code sync)
if [ ! -x "$SRC/.venv/bin/python" ]; then
  python3 -m venv "$SRC/.venv"
  "$SRC/.venv/bin/pip" install --upgrade pip >/dev/null
  "$SRC/.venv/bin/pip" install -r "$SRC/requirements.txt"
fi

# 3. sync staged code (never touch .venv)
if [ -d /root/llm-bridge-sync ]; then
  rsync -a --delete --exclude .venv /root/llm-bridge-sync/ "$SRC/"
  rm -rf /root/llm-bridge-sync
fi

# 4. per-port config for 18830 (token/grpc/sse inherited from live config)
python3 - "$OLD_CFG" > /root/.llm-bridge-18830.json <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
c["port"] = 18830
print(json.dumps(c))
PY
chmod 600 /root/.llm-bridge-18830.json

# 5. pm2
if ! pm2 describe llm-bridge-18830 >/dev/null 2>&1; then
  pm2 start "$SRC/.venv/bin/python" --name llm-bridge-18830 -- "$SRC/bridge/server.py" /root/.llm-bridge-18830.json
fi
pm2 save
pm2 startup systemd -u root --hp /root || true

# 6. bare-IP vhost (SSE-safe proxy settings)
cat > /etc/nginx/sites-available/llm-bridge-ip.conf <<'NGX'
server {
    listen 80;
    server_name 46.225.143.221;

    location / {
        proxy_pass http://127.0.0.1:18830;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        chunked_transfer_encoding on;
    }
}
NGX
ln -sf /etc/nginx/sites-available/llm-bridge-ip.conf /etc/nginx/sites-enabled/llm-bridge-ip.conf

# 7. active state + reload
echo 18830 > "$ACTIVE_FILE"
nginx -t
nginx -s reload

echo VPS_SETUP_OK