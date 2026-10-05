# llm-harness

Автономный agent loop (на `qwen3.8-35b` в MTPLX, 48GB Mac) + инструменты
(`read`/`write`/`edit`/`bash`/`web_search`/`browser_fetch` + subagents),
персист сессий (JSONL), авто-компакт контекста, SSE-журнал событий,
gRPC-контракт.

## Клиенты

- **cloudlyru** — вкладка «Харнесс» в разделе «Проекты»: pi-bridge
  (127.0.0.1:18820) на этом же маке говорит с harness по gRPC :9000 и
  ретранслирует SSE :9001; туннель VPS→мак :18820, `src/projects` на VPS,
  Flutter-приложение.
- `tools/ask_client.py` — CLI-клиент для отладки (gRPC + SSE, локально).

## Компоненты

| Файл | Роль |
|---|---|
| `llm_harness/llm.py` | LLM-клиент: MTPLX `/v1/chat/completions`, стриминг, reasoning-карта |
| `llm_harness/harness.py` | Ядро: agent loop, history, KV/session, settings, runs |
| `llm_harness/tools.py` | Инструменты (read/write/edit/bash/web_search/browser_fetch) |
| `llm_harness/subagents.py` | Subagents (деlegates) |
| `llm_harness/persistence.py` | JSONL-персист: session + archive, replay, restore |
| `llm_harness/compaction.py` | Авто-компакт (token-граница) + ручной (gRPC) |
| `llm_harness/events.py` | Event bus: seq, ring-буфер, SSE-формат, `?since=` |
| `llm_harness/grpc_server.py` | gRPC :9000 — команда/ответ (контракт в `proto/harness.proto`) |
| `llm_harness/sse_server.py` | SSE :9001 `/events` — стрим всех событий |
| `llm_harness/config.py` | Settings (model, base_url, лимиты, sampling, thinking) |
| `proto/harness.proto` | gRPC-контракт (Ask/Stop/Resume/SetSettings/Compact/Status/GetMessages/NewSession/ListSessions/LoadSession/DeleteSession/RenameSession) |
| `tools/ask_client.py` | CLI-клиент (gRPC + SSE) |
| `tools/sessions_probe.py` | CLI-утилита для проверки сессий |
| `run-harness.sh` | Запуск daemon (launchd: `com.agent.harness`) |

## Документация

- [`docs/API.md`](docs/API.md) — полный API-контракт (gRPC + SSE, все события).
- [`docs/COMPACT.md`](docs/COMPACT.md) — компакция: алгоритм, границы, поведение.
- [`docs/TOOLS.md`](docs/TOOLS.md) — инструменты: описание, параметры, лимиты.
- [`docs/MTPLX.md`](docs/MTPLX.md) — карта reasoning API MTPLX.

## Модель и лимиты

- `qwen3.8-35b`, база `https://46.225.143.221:8000/v1`
- лимиты: контекст сервера **204 800** (модель 262 144, лимит machine-bound по RAM),
  серверного жёсткого максимума вывода нет (pi ставит 34 000 самом)
- хедер `x-mtplx-client: llm-harness`

## План по фазам

| Фаза | Что | Статус |
|---|---|---|
| 1 | LLM-клиент: стриминг `/v1/chat/completions` по маппингу MTPLX.md, `reasoning_content` → SSE `thinking_delta` | ✅ live-верифицировано 2026-09-30 (thinking, tool-calls, round-trip) |
| 2 | Agent loop + инструменты (`read`/`write`/`edit`/`bash`), стрим tool-событий | ✅ E2E: агент сам создаёт и запускает код, SSE показывает весь ран |
| 3 | gRPC-сервис: `Ask/Stop/Resume/Status/SetSettings/Compact` | ✅ incl. queued Ask, Stop/Resume жизненный цикл верифицирован |
| 4 | SSE-сервер (uvicorn, порт 9001): все события, seq + `?since=` для перезахвата | ✅ ring-буфер 10 000, heartbeat 15 c, `?since=` |
| 5 | Компакт: авто по `usage.prompt_tokens`, ручной `Compact`, CONTEXT_OVERFLOW recovery | ✅ авто-триггер на реальной границе; ручной — через gRPC; retry после вынужденного компакта |
| 6 | Персист: сессии в JSONL, restore при старте | ✅ E2E: fresh→done, restart→restore (контекст пережил рестарт), kill mid-run→stopped→Resume→done |
| 7 | Управление сессиями: `NewSession/ListSessions/LoadSession/DeleteSession/RenameSession` | ✅ |

## Запуск

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m llm_harness --cwd /path/to/workspace
# gRPC :9000, SSE :9001 (флаги: --port-grpc, --port-sse)
# сессия персистируется в /path/to/workspace/.llm-harness/session.jsonl
# и восстанавливается при следующем запуске с тем же --cwd
```

```bash
# клиент
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Status -d '{}'
grpcurl -plaintext -d '{"prompt":"исправь баг в main.py"}' 127.0.0.1:9000 harness.v1.Harness/Ask
curl -N http://127.0.0.1:9001/events
```

Daemon: `launchctl load ~/Library/LaunchAgents/com.agent.harness.plist`
(скрипт `run-harness.sh`, loopback-only, `--cwd /Users/sobogd/work/llm-harness`).

## Регенерация gRPC-кода

```bash
.venv/bin/python -m grpc_tools.protoc -Iproto \
  --python_out=/tmp/pbgen --grpc_python_out=/tmp/pbgen proto/harness.proto
# /tmp/pbgen/harness_pb2*.py → llm_harness/llm_harness_pb2*.py
# (импорт в _grpc: `from . import llm_harness_pb2`)
```
