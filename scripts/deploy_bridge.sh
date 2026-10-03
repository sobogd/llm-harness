#!/usr/bin/env bash
set -euo pipefail

# Zero-downtime blue/green deploy of llm-bridge (pm2, ports 18830/18831).
# Code must already be synced into /opt/llm-bridge (never touch its .venv).
# Env: BRIDGE_TOKEN (required), SMOKE_ASK=1 (optional real /ask test on canary).
SRC=/opt/llm-bridge
ACTIVE_FILE=/root/.llm-bridge-active
GRPC=${BRIDGE_GRPC:-127.0.0.1:19000}
SSE=${BRIDGE_SSE:-http://127.0.0.1:19001/events}

ACTIVE=$(cat "$ACTIVE_FILE" 2>/dev/null || echo 18830)
NEW=$((18830 + 18831 - ACTIVE))
OLD=$ACTIVE
echo "deploy: old=$OLD new=$NEW"

canary_cleanup() { pm2 delete "llm-bridge-$NEW" 2>/dev/null || true; }

python3 - "$BRIDGE_TOKEN" "$NEW" "$GRPC" "$SSE" > "/root/.llm-bridge-$NEW.json" <<'PY'
import json, sys
tok, port, grpc, sse = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
print(json.dumps({"token": tok, "grpc": grpc, "sse": sse, "port": port}))
PY
chmod 600 "/root/.llm-bridge-$NEW.json"

pm2 delete "llm-bridge-$NEW" 2>/dev/null || true
pm2 start "$SRC/.venv/bin/python" --name "llm-bridge-$NEW" -- "$SRC/bridge/server.py" "/root/.llm-bridge-$NEW.json"

ok=""
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$NEW/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ -z "$ok" ]; then
  echo "canary $NEW never became healthy; keeping old $OLD"
  canary_cleanup
  exit 1
fi

old_h=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$OLD/health")
new_h=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$NEW/health")
old_s=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $BRIDGE_TOKEN" "http://127.0.0.1:$OLD/status")
new_s=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $BRIDGE_TOKEN" "http://127.0.0.1:$NEW/status")
echo "equivalence: health old=$old_h new=$new_h; status old=$old_s new=$new_s"
if [ "$old_h" != "200" ] || [ "$old_h" != "$new_h" ] || [ "$old_s" != "$new_s" ]; then
  echo "equivalence FAILED; keeping old $OLD"
  canary_cleanup
  exit 1
fi

if [ "${SMOKE_ASK:-0}" = "1" ]; then
  code=$(curl -s -o /tmp/llm-ask-smoke.json -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $BRIDGE_TOKEN" -H 'Content-Type: application/json' \
    -d '{"prompt":"ping"}' "http://127.0.0.1:$NEW/ask")
  case "$code" in
    200|201|202) echo "smoke /ask: $code (pass)" ;;
    502) echo "smoke /ask: 502, harness down (pass with warning)" ;;
    *) echo "smoke /ask failed: $code"; cat /tmp/llm-ask-smoke.json 2>/dev/null || true; canary_cleanup; exit 1 ;;
  esac
fi

for f in /etc/nginx/sites-available/llm-bridge-ip.conf /etc/nginx/sites-available/llm.iq-factura.conf; do
  if [ -f "$f" ]; then
    sed -i -E "s|proxy_pass http://127\.0\.0\.1:[0-9]+;|proxy_pass http://127.0.0.1:$NEW;|" "$f"
  fi
done
nginx -t
nginx -s reload

echo "$NEW" > "$ACTIVE_FILE"
pm2 delete "llm-bridge-$OLD" 2>/dev/null || true
pm2 save

echo "DEPLOY_OK active=$NEW old=$OLD"