# llm-harness

Мини-агентный харнесс в духе pi: один долгоживущий Python-процесс, без UI.
Всё управление — по gRPC, всё, что происходит — ширится по SSE.
Локальная модель: Qwen3.8-27B на mtplx (OpenAI-совместимый сервер, `127.0.0.1:8000`).

```
[клиент: твой будущий UI / скрипт]
   |  gRPC :9000            ^  SSE :9001 /events
   v                       |
+------------------------------------------------------+
|                harness-процесс (asyncio)             |
|  +-------------+   +---------------------------+     |
|  | gRPC-сервис |   | agent loop (ядро)          |    |
|  | Ask/Stop/   |-->| история + tools + LLM      |    |
|  | Resume/     |   | + компакт на границе      |    |
|  | SetSettings |   +-----------+---------------+    |
|  +------+------+         |  stream (httpx)          |
+-------------------------|---------------------------+
                           v
                mtplx http://127.0.0.1:8000/v1
                (Qwen3.8-27B, контекст сервера 204 800)
```

## Компоненты

| Файл | Назначение |
|---|---|
| `proto/harness.proto` | gRPC-контракт управления |
| `docs/API.md` | Документация API для клиента (gRPC + SSE, с примерами) |
| `docs/MTPLX.md` | Маппинг запросов на локальный mtplx (как у pi) + границы контекста |
| `docs/TOOLS.md` | Семантика инструментов (read/write/edit/bash), edit — точное совпадение |
| `docs/COMPACT.md` | Авто- и ручной компакт на реальной границе токенов |
| `docs/FLUTTER.md` | Архитектура Flutter-приложения + моста на VPS (план фаз 7–10) |
| `requirements.txt` | Зависимости (grpcio, grpcio-tools, uvicorn, httpx) |
| `llm_harness/persistence.py` | JSONL-персист сессии + restore при старте |
| `llm_harness/envinfo.py` | Снапшот окружения (установленные инструменты, запущенные сервисы, PATH) → системный промпт |
| `tools/test_tools.py` | Юнит-тесты инструментов и компакта (без сети) |
| `tools/test_retry.py` | Юнит-тесты 409-ретрая стрима (mock-транспорт) |
| `tools/test_persist.py` | Юнит-тесты персиста: replay, снапшот, flush |
| `tools/ask_client.py` | gRPC smoke-клиент (ask + SSE-прослушка) |
| `tools/test_mgmt.py` | Management-тест: SetSettings/Stop/Resume/Compact/Status |
| `scripts/build_apk.py` | Сборка релиза: `app-<N>.apk` в `releases/` (см. «Релиз и версия») |
| `releases/app-<N>.apk` | Опубликованные релизы; сервер раздаёт последний (max N) |
| `app/lib/updater.dart` | Самобот приложения: manifest → сравнение → скачать → системный установщик |

## Что есть в ядре (MVP)

1. **Agent loop** — как в pi: `prompt → LLM(stream) → tool calls → исполнение → LLM → …`
   до тех пор, пока модель не отвечает без инструментов.
2. **Инструменты**: `read(path, offset?, limit?)`, `write(path, content)`,
   `edit(path, oldText, newText)` (uniqueness обязателен), `bash(command, timeout?)`.
3. **gRPC**: `Ask`, `Stop`, `Resume`, `SetSettings`, `Compact`, `Status`.
4. **SSE**: полный поток событий рана (текст/размышление/инструменты/компа́кт/usage).
5. **Компакт на границе токенов**: считаем **реальный** `usage.prompt_tokens`
   из стрима (`stream_options.include_usage`), а не оценку. Триггер — когда
   реальное потребление доходит до выставленной границы.
6. **Настройки на лету**: `max_context_tokens`, `max_output_tokens`, thinking (on/off + effort).
7. **Язык промптов**: системный промпт, описания инструментов, все вставляемые
   инструкции и compact-запросы — **только на английском** (независимо от языка
   диалога пользователя с агентом).
8. **Персист сессии**: append-only JSONL в `<cwd>/.llm-harness/session.jsonl`.
   История, сообщения, ран (в т.ч. «running»-маркер), снапшоты после компакта и
   настройки пишутся фоновым флешером (~0.5 с). При старте сессия восстанавливается:
   kill в середине рана → состояние `stopped` (можно Resume), завершённый ран → `idle`.
9. **Контекст окружения**: при старте харнесс снимает снапшот машины
   (`llm_harness/envinfo.py`): установленные инструменты с версиями, слушающие
   сервисы, топ процессов, фактический PATH bash-тула (всегда включает
   `/opt/homebrew/bin`). Снапшот вшивается в системный промпт, поэтому агент
   знает, что и где стоит, без допросов.

## Маппинг на mtplx (кратко, подробно — в `docs/MTPLX.md`)

Берётся один-в-один из текущего маппинга pi (`~/.pi/agent/models.json`):

- `POST /v1/chat/completions`, `stream: true`, `stream_options: {include_usage: true}`
- `max_tokens` (не `max_completion_tokens`), роли `system/user/assistant/tool` (нет `developer`)
- thinking: `enable_thinking: bool` + `reasoning_effort: "low|medium|xhigh"` (сервер понимает ровно эти три уровня)
- ответ: `reasoning_content` в дельтах стрима — это размышления; `tool_calls` — стандартные OpenAI
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
| 7–10 | `GetMessages` RPC, мост на VPS, Flutter-приложение, полировка | ⬜ план в [`docs/FLUTTER.md`](docs/FLUTTER.md) |

## Запуск

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m llm_harness --cwd /path/to/workspace
# gRPC :9000, SSE :9001 (флаги: --grpc-port, --sse-port)
# сессия персистируется в /path/to/workspace/.llm-harness/session.jsonl
# и восстанавливается при следующем запуске с тем же --cwd
```

```bash
# клиент
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Status -d '{}'
grpcurl -plaintext -d '{"prompt":"исправь баг в main.py"}' 127.0.0.1:9000 harness.v1.Harness/Ask
curl -N http://127.0.0.1:9001/events
```

## Правила разработки приложения

После **каждой** доработки в `app/` прогонять `flutter analyze`
(SDK в homebrew, в дефолтном PATH его нет):

```bash
/opt/homebrew/bin/flutter analyze
```

Сборка APK — только при чистом analyze.

## Релиз и версия

Релиз приложения — **обычный счётчик N (1, 2, 3, …)**: без semantic-versioning, без crypto-версионирования. Одна и та же цифра:

1. вшивается в APK при сборке: `--dart-define=APP_VERSION=<N>` (`app/lib/config.dart`);
2. шьётся в имя файла: `releases/app-<N>.apk`;
3. читается сервером из имени — `llm_harness/update.py::manifest()` возвращает max N как «последний релиз».

### Сборка

## Релиз и версия

```bash
python3 scripts/build_apk.py             # N = max(существующих) + 1
python3 scripts/build_apk.py --version 5 # явно
python3 scripts/build_apk.py --force     # перезаписать app-<N>.apk
python3 scripts/build_apk.py --no-build  # только скопировать готовый app-release.apk
```

Скрипт запускает `flutter build apk --release` с defines из `secrets/defs.json`:
`SERVER_URL`, `AUTH_TOKEN`, `UPDATE_BASE_URL` + `APP_VERSION=N`, затем копирует
APK в `releases/app-<N>.apk`. **Не собирать «голым» `flutter build apk`** —
без defines получится APK с пустым токеном/URL и `APP_VERSION=0` (бессмысленный).

`pubspec.yaml: version: 1.0.0+<N>` — это Android **versionCode**, отдельная от
счётчика N величина: чтобы новый APK ставился поверх установленного на телефоне,
versionCode должен быть выше versionCode установленного APK. Повышать вместе с N.

### Раздача и установка (без S3)

Всё по существующему туннелю: SSE-сервер на маке отдаёт

- `GET /update/manifest` → `{version, filename, size, sha256}` последнего релиза
  (sha256 — локальный хэш цельности, кэшируется в процессе по name+mtime+size,
  поэтому новый файл в `releases/` подхватывается **без рестарта сервера**);
- `GET /update/app-<N>.apk` → сам APK.

Мост на VPS пришивает Bearer-токен на обратном пути (телефон → VPS → мак),
поэтому с телефона это выглядит как `https://llm.iq-factura.com/update/...`.
`version: 0` в манифесте = «ничего не опубликовано».

Приложение: в Настройках «Проверить обновление» → `Updater` (`app/lib/updater.dart`)
сравнивает N из манифеста со своим зашитым `APP_VERSION`; если больше — скачивает
APK (прогресс + проверка sha256, при несовпадении файл удаляется) и открывает
системный установщик.

### Текущие релизы

| N | Файл | Что |
|---|---|---|
| 1 | `app-1.apk` (2026-10-01) | первая собранная сборка |
| 2 | `app-2.apk` (2026-10-02) | рабочая: bridge-мост, настройки, самобот |
| 3 | `app-3.apk` (2026-10-02) | лимит 217k токенов; модель `qwen3.8-27b` → `qwen3.8-35b`; thinking `xhigh` → `max` |

Телефон, у которого установленный APK ниже 3, увидит обновление по проверке в Настройках.