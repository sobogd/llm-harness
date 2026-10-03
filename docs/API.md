# API: как коннектиться к llm-harness

Два транспорта, один процесс:

| Транспорт | Порт | Назначение |
|---|---|---|
| gRPC (plaintext, loopback) | `9000` | Команды: Ask / Stop / Resume / SetSettings / Compact / Status / GetMessages |
| SSE (HTTP GET) | `9001` `/events` | Поступательные события всего, что происходит |

Контракт gRPC — в [`../proto/harness.proto`](../proto/harness.proto).
Принцип связи: gRPC — «рука» (команды), SSE — «глаза» (наблюдение).
Синхронных ответов с контентом нет: `Ask` сразу возвращает `run_id`,
весь контент потока приходит событиями с этим `run_id`.

---

## 1. SSE: `GET http://127.0.0.1:9001/events`

Стандартный `text/event-stream`. Каждый event:

```
id: 42
event: text_delta
data: {"run_id":"r_01","seq":42,"delta":"Смотрю ",...}
```

- `id` — глобальный monotonically растущий `seq` по всем событиям процесса.
- `data` — JSON; у каждого события `seq` (совпадает с `id`) и `run_id`
  (для событий вне рана — пустая строка).
- Переподключение: `GET /events?since=<seq>` — сервер досылает события
  с этого `seq` из ring-буфера (держит последние 10 000).

### События

Все события несут `seq` (глобальный счётчик, совпадает с `id`), `run_id` (пусто для событий вне рана) и `ts` (unix ms). Поле `id` дублирует `seq`.

| event | data (помимо seq, run_id, ts) | Когда |
|---|---|---|
| `session_loaded` | `{"path":"<jsonl>","messages":N,"state":"idle\|stopped"}` | При старте сервера, если в workspace найдена сессия (см. §3) |
| `run_started` | `{"session_id":...}` | Ask принят, ран начался |
| `turn_started` | `{"turn":N}` | Новый LLM-запрос в рамках рана |
| `thinking_delta` | `{"turn":N,"text":"..."}` | Куски `reasoning_content` (размышления) |
| `text_delta` | `{"turn":N,"text":"..."}` | Куски видимого ответа модели |
| `tool_start` | `{"turn":N,"tool":"read","tool_call_id":"c1","args_preview":"..."}` | Модель вызвала инструмент (первые 500 символов аргументов) |
| `tool_end` | `{"turn":N,"tool":"read","tool_call_id":"c1","ok":true,"result_preview":"..."}` | Результат инструмента (первые 500 символов) |
| `usage` | `{"turn":N,"usage":{"prompt_tokens":...,"completion_tokens":...,"total_tokens":...,"completion_tokens_details":{"reasoning_tokens":...}}}` | После каждого LLM-запроса (usage из стрима) |
| `turn_finished` | `{"turn":N,"reason":"stop\|tool_calls\|length\|retry\|error\|stopped"}` | `stop` — ответ без инструментов; `tool_calls` — инструменты исполнены, идём дальше; `length` — обрезано max_output_tokens, продолжим; `retry` — CONTEXT_OVERFLOW спасён вынужденным компактом; `error` — ошибка; `stopped` — ран остановлен |
| `compact_started` | `{"reason":"auto\|manual","prompt_tokens":...,"boundary":...}` | Запуск компакта |
| `compact_done` | `{"ok":true,"reason":"...","tokens_before":...,"tokens_after":...,"messages_before":...,"messages_after":...,"summary_preview":"..."}` | Компакт завершён; `ok:false` — сбой (напр. отказ движка по памяти) |
| `queued` | `{"queue_depth":N}` | Ask вошёл в очередь (ран уже идёт) |
| `run_stopped` | `{"turn":N}` | Stop принят |
| `run_resumed` | `{"turn":...}` | Resume принят (либо drain очереди в новый run_id) |
| `run_done` | `{"state":"done\|stopped\|error","turn":N,"error":"..."}` | Ран закончен любым исходом |
| `error` | `{"code":"LLM_ERROR\|CONTEXT_OVERFLOW\|INTERNAL","message":"...","turn":N,"detail":"..."}` | Ошибка; ран останавливается |
| `settings_changed` | `{"settings":{...}}` | После SetSettings |
| `heartbeat` | `{}` | Каждые 15 c, если тишина (для proxy/LB-таймаутов) |

### Пример сессии (схлопнуто)

```
event: run_started      {"run_id":"r_01","session_id":"s_01"}
event: turn_started     {"turn":1}
event: thinking_delta   {"turn":1,"text":"Нужно прочитать main.py"}
event: text_delta       {"turn":1,"text":"Смотрю файл. "}
event: tool_start       {"turn":1,"tool":"read","tool_call_id":"c1","args_preview":"{\"path\": \"main.py\"}"}
event: tool_end         {"turn":1,"tool":"read","tool_call_id":"c1","ok":true,"result_preview":"def main():\n    ..."}
event: usage            {"turn":1,"usage":{"prompt_tokens":1502,...}}
event: turn_finished    {"turn":1,"reason":"tool_calls"}
event: turn_started     {"turn":2}
event: text_delta       {"turn":2,"text":"В строке 12 опечатка, "}
event: tool_start       {"turn":2,"tool":"edit","tool_call_id":"c2","args_preview":"..."}
event: tool_end         {"turn":2,"tool":"edit","tool_call_id":"c2","ok":true,"result_preview":"OK, replaced 1 occurrence"}
event: usage            {"turn":2,"usage":{"prompt_tokens":1980,...}}
event: turn_finished    {"turn":2,"reason":"tool_calls"}
event: turn_started     {"turn":3}
event: text_delta       {"turn":3,"text":"Готово, баг исправлен."}
event: usage            {"turn":3,"usage":{"prompt_tokens":2611,...}}
event: turn_finished    {"turn":3,"reason":"stop"}
event: run_done         {"run_id":"r_01","state":"done","turn":3,"error":""}
```

---

## 2. gRPC-команды

### `Ask` — задача агенту

```bash
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Ask \
  -d '{"prompt":"Найди баг в main.py и исправь"}'
# → {"run_id":"r_01","session_id":"s_01","state":"running"}
```

Если ран уже идёт — `state:"queued"`: сообщение выполняется после
окончания текущего assistant-turn (steering, как в pi).

### `Stop` / `Resume`

```bash
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Stop    -d '{}'
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Resume  -d '{}'
# → {"run_id":"r_01","state":"stopped","turn":4,"last_error":""}
```

Stop рвёт LLM-стрим. Всё, что модель уже сгенерировала (текст/размышление),
сохраняется в истории; недоисполненный tool call не исполняется и не записывается
(иначе история осталась бы с «висящим» tool_call без tool-результата, что сделало
бы следующий запрос невалидным). После Resume модель видит текст и решает заново.

### `SetSettings`

```bash
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/SetSettings -d '{
  "max_context_tokens": 60000,
  "max_output_tokens": 8192,
  "thinking_enabled": true,
  "thinking_effort": "xhigh"
}'
# → возвращает полные текущие эффективные настройки
```

Неноль-поле = изменить, нулевое = не трогать. Вступают в силу с
следующего LLM-запроса. Ограничения: `max_context_tokens ≤ 204800`
(контекст сервера), `max_context_tokens + max_output_tokens ≤ 202752`.

### `Compact` — ручной компакт

```bash
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Compact \
  -d '{"keep_last_messages":8}'
# → {"ok":true,"tokens_before":58112,"tokens_after":6204,...}
```

Если ран идёт — компакт выполняется после конца текущего turn'а
(SSE: `compact_started` с `"reason":"manual"`).

### `Status`

```bash
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Status -d '{}'
```

Возвращает: эффективные настройки, состояние рана, id сессии,
`history_messages` / `persisted_messages` (кол-во сообщений в истории),
`prompt_tokens_last` (реальные, из usage), глубину очереди Ask,
`history_loaded` (true, если сессия восстановлена из JSONL при старте)
i `loaded_from` (путь к файлу сессии; пусто — свежая сессия).

### `GetMessages` — история диалога

```bash
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/GetMessages \
  -d '{"last": 0}'   # 0 = вся история; N = последние N сообщений
```

Возвращает историю из RAM (`repeated Message` + `model` из текущих
настроек) — то, что ушло в последний LLM-запрос: system-промпт, user/assistant,
tool-результаты. Каждое `Message`: `role` (system|user|assistant|tool),
`content`, `tool_calls[]` (assistant: id/name/arguments), `tool_call_id` и
`name` (tool-результат: какой вызов и какой инструмент — denormalized для UI),
`reasoning_content` (thinking, может быть пустым).

Снапшот на момент вызова; live-изменения продолжают приходить по SSE.
Для клиента это «восстановление экрана» после перезапуска: ring-буфер SSE
покрывает переподключение живого процесса, `GetMessages` — перезапуск клиента.

## 3. Персист сессии

Сессия живёт в append-only файле `<cwd>/.llm-harness/session.jsonl`
(одна строка-запись на JSON; файл можно читать вручную). Записи:

| type | Что фиксирует |
|---|---|
| `meta` | session_id, moment создания |
| `message` | каждое сообщение истории (system/user/assistant/tool), по мере появления |
| `history` | снапшот всей истории после компакта (заменяет предыдущие сообщения при replay) |
| `run` | ран: `running` при старте (маркер!), финальный `done\|stopped\|error` + turn |
| `settings` | SetSettings |

Писать — фоновый flusher каждые ~0.5 с (плюс принудительный flush в конце рана).

### Поведение при рестарте/kill

- **Завершённый ран** (последняя `run`-запись `done`/`error`) → сервер стартует в `idle`,
  история восстановлена, новый `Ask` продолжает сессию. Событие `session_loaded`.
- **Kill в середине рана** (есть `run`-запись `running` без финала) → сервер стартует в
  `stopped` с `last_error:"interrupted by server restart"`; `Resume` продолжает именно этот
  ран, `Ask` отменяет его и запускает новый. «Висящие» tool_calls (вызваны, но не получившие
  результата до kill) отбрасываются при восстановлении.
- Тонкость MVP: при жёстком kill последние ~0.5 с сообщений могут не успеть
  дописаться (flush-период); финальная `run`-запись — надёжный маркер завершения рана.

## 4. Жизненный цикл рана

```
idle --Ask--> running <--> stopped
                 |  \          |
                 |   \-- error (возврат к idle по следующему Ask)
                 v
               done --Ask--> running (новая цель, та же сессия)
```

- **running**: loop крутится, пока модель отвечает с tool calls.
- **stopped**: после Stop; `Resume` продолжает, `Ask` отменяет остановку и
  запускает новый ран.
- **done**: модель дала ответ без инструментов; Ask — новая цель в той же сессии.
- **error**: ран упал (LLM-ошибка / CONTEXT_OVERFLOW без спасения). `Resume` из
  error не действует — только новый `Ask` (та же сессия, история сохранена).

Сессия (история) переживает раны; компакты и остановки — события внутри
сессии, не между ними.

## 5. Ошибки

| code | Значение | Что делать |
|---|---|---|
| `LLM_ERROR` | Сервер модели упал/ответил ошибкой (см. `~/.mtplx/logs`) | Дождаться mtplx (у него есть watchdog), `Resume` |
| `CONTEXT_OVERFLOW` | usage > окна даже после компакта | Снизить `max_output_tokens` или `Compact` с большим keep, `Resume` |
| `TOOL_ERROR` | (не является кодом события `error`) Инструменты не «падают» рана: ошибка инструмента возвращается модели как tool result, в SSE это `tool_end` с `ok:false` | Ничего делать не нужно — модель увидит ошибку и поступит по ситуации |
| `INVALID_SETTINGS` | SetSettings вне лимитов | gRPC RPC-ошибка, ничего не меняется |

## 6. Быстрый smoke-тест клиента

```bash
# 1. подписаться
curl -N http://127.0.0.1:9001/events &
# 2. статус
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Status -d '{}'
# 3. задача
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Ask \
  -d '{"prompt":"Создай hello.py, печатающий hello, и запусти его"}'
# 4. остановка/продолжение при желании
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Stop   -d '{}'
grpcurl -plaintext 127.0.0.1:9000 harness.v1.Harness/Resume -d '{}'
```
