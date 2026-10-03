# Мост llm-bridge (VPS)

`llm.iq-factura.com` — единственный публичный вход в харнесс. Всё дальше — loopback:

```
телефон (Flutter)
   │ HTTPS + SSE, Bearer-токен (из настроек приложения)
nginx (VPS) llm.iq-factura.com → 127.0.0.1:18830
   │
мост  /opt/llm-bridge/bridge/server.py (aiohttp, systemd llm-bridge)
   │ gRPC 127.0.0.1:19000 + SSE 127.0.0.1:19001 (reverse-SSH туннель мака)
мак  llm-harness 127.0.0.1:9000/9001 → mtplx :8000
```

Мост — тонкий адаптер HTTP(S)/SSE → gRPC: `/ask` `/stop` `/resume` `/status`
`/get-messages` `/settings` `/compact` (JSON, ответы в форме protobuf-JSON) и
`GET /events` (SSE-фан-аут событий харнесса). Токен один, из `~/.llm-bridge.json`
(mode 600) — то же значение `LLM_HARNESS_TOKEN` из `~/work/.env`, что введено
в настройках приложения. SSE-клиент на
маке живёт постоянно (реконнект с бэкоффом), последние 256 фреймов — ring-буфер
для реконнекта клиентов; после перезапуска моста клиент восстанавливает экран
через `get-messages`.

## Развёртывание (VPS)

```bash
# код и зависимости
mkdir -p /opt/llm-bridge
rsync -a llm_harness/ bridge/ user@vps:/opt/llm-bridge/
ssh user@vps 'python3 -m venv /opt/llm-bridge/.venv && /opt/llm-bridge/.venv/bin/pip install -r ... '
```

Конфиг `/root/.llm-bridge.json` (600):
`{"token": "<LLM_HARNESS_TOKEN из ~/work/.env>", "grpc": "127.0.0.1:19000",
  "sse": "http://127.0.0.1:19001/events", "port": 18830}`

Системд: `cp bridge/llm-bridge.service /etc/systemd/system/ && systemctl daemon-reload
&& systemctl enable --now llm-bridge`

Нгинкс: `cp bridge/nginx-llm-iq-factura.conf /etc/nginx/sites-available/ &&
ln -s ../../sites-available/llm.iq-factura.conf /etc/nginx/sites-enabled/ &&
nginx -t && certbot --nginx -d llm.iq-factura.com && systemctl reload nginx`
(DNS A-запись `llm.iq-factura.com` → 46.225.143.221 уже есть в Cloudflare.)

## Мак

- `cloudlyru/agents/mac/run-harness.sh` + `com.agent.harness.plist` — вечный
  харнесс (launchd, cwd `/Users/sobogd/work/llm-harness`).
- `run-tunnel.sh` — два новых `-R`: `127.0.0.1:19000:127.0.0.1:9000` (gRPC) и
  `127.0.0.1:19001:127.0.0.1:9001` (SSE); оба порта добавлены в grep-очистку
  stale-слушателей.

## Живой формат ответов

`/status` (текущий proto, без контекст-метрик — их добавит Phase 8+):

```json
{"settings":{"maxContextTokens":60000,"maxOutputTokens":8192,
 "thinkingEnabled":true,"thinkingEffort":"medium"},
 "run":{},"sessionId":"5de687b9-...","historyMessages":1,
 "startedAtUnixMs":"1790799790428","historyLoaded":true,
 "loadedFrom":".../session.jsonl","persistedMessages":1}
```

`startedAtUnixMs` — строка (protobuf int64), `run:{}` — idle,
отсутствующий `thinkingEnabled` — false.

Если upstream-SSE рвётся, мост пингует gRPC Status и шлёт текущим подписчикам
синтетический `event: bridge` c `{"type":"bridge","kind":"harness_down|up"}`
(`reason`: `read_error` / `exception` / `timeout`). В ring-буфер эти события не
попадают. Приложение показывает баннер «агент на Mac офлайн».

`/settings` — PUT-like: `{thinking_enabled, thinking_effort,
max_context_tokens, max_output_tokens}` (отсутствующий ключ = не менять);
`/compact` — `{keep_last_messages}`, ответ `{tokensBefore, tokensAfter}`.

## Приложение (app/)

Flutter-приложение `harness.iqfactura.agent`; URL зашивается в APK, токен в APK
не лежит — вводится в настройках (shared_preferences), единый источник —
`LLM_HARNESS_TOKEN` в `~/work/.env`:

```bash
cd app && JAVA_HOME=/opt/homebrew/opt/openjdk@21 \
  flutter build apk --debug --dart-define-from-file=../secrets/defs.json
# → build/app/outputs/flutter-apk/app-debug.apk
adb install build/app/outputs/flutter-apk/app-debug.apk
```

UI/политики SSE описаны в `docs/FLUTTER.md` (фаза 9). `lastSeq` хранится в
shared_preferences; `session_loaded` — полный ресинк.
