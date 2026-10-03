# Маппинг на mtplx (локальная модель)

Целевой backend harness'а — локальный mtplx-сервер (launchd, порт 8000,
только loopback), OpenAI-совместимый. Маппинг копируется 1:1 из текущего
настроя pi: [`~/.pi/agent/models.json`](~/.pi/agent/models.json), секция
provider `mtplx` (compat: `thinkingFormat: "qwen"`, `maxTokensField: "max_tokens"`,
`supportsDeveloperRole: false`, `supportsReasoningEffort: true`).

## Подключение

```
base_url : http://127.0.0.1:8000/v1
endpoint : POST /chat/completions   (API: openai-completions)
auth     : Authorization: Bearer mtplx-local
headers  : x-mtplx-client: llm-harness        # mtplx логгирует по клиенту
             x-mtplx-session-id: <uuid>         # один стабильный id на клиент;
                                               # все запросы пинаются в одну
                                               # сессию движка, session bank не
                                               # растёт на запрос (как у pi)
model    : mtplx-qwen38-27b-optimized-speed
```

Сервер поднят с `--reasoning auto --reasoning-effort medium --reasoning-parser qwen3
--preserve-thinking auto --tool-prompt-mode hybrid --default-temperature 1.0
--default-top-p 0.95 --default-top-k 20` (см. `cloudlyru/agents/mac/run-mtplx-serve.sh`).
Sampling-параметры по умолчанию сервер ужеставляет сам — присылаем их только
если клиент явно поменял в SetSettings.

## Лимиты модели (проверено по `/health` + `models.json` pi)

| Параметр | Значение | Примечание |
|---|---|---|
| контекст сервера | **204 800** | `context_window` в `/health`; модель поддерживает 262 144, лимит 204.8K — machine-bound (48 GiB RAM, KV 64 KiB/токен). 64000 в models.json pi — **самоналоженный** лимит pi, не сервера |
| max output tokens | нет жёсткого серверного (`max_response_tokens: null`) | pi ставит 34000 самом; реальное ограничение — память под KV и время декода |
| reasoning levels | **`low`, `medium`, `xhigh`** | `reasoning.effort_levels` из `/health`, дефолт `medium` |
| reasoning parser | `qwen3`, preserve_when_enabled | `--preserve-thinking auto`: `reasoning_content` assistant-сообщений пересылать обратно |
| input | text, image (vision вкл.) | images пока не используем (MVP — текст) |
| cost | 0 | локальная модель |

Вывод: модель действительно держит **больше**, чем выставит harness.
Граница компакта — свободная настройка где угодно в `(0 … 204800)`;
дефолт harness'а: `max_context_tokens = 60000` (как у pi), можно поднять до
~150 000+. Правило harness'а: `B + O ≤ W − H`, где `W = 204800`,
`H = 2048` (запас на KV-план/округление).

## Тело запроса (шаблон)

```jsonc
POST /v1/chat/completions
{
  "model": "mtplx-qwen38-27b-optimized-speed",
  "stream": true,
  "stream_options": { "include_usage": true },   // ОБЯЗАТЕЛЬНО: реальные токены для компакта
  "max_tokens": 8192,                            // = Settings.max_output_tokens
  "enable_thinking": true,                       // = Settings.thinking_enabled
  "reasoning_effort": "high",                    // = Settings.thinking_effort (только если не "" и не minimal)
  "tools": [ ... ],                              // OpenAI function calling, см. TOOLS.md
  "tool_choice": "auto",
  "messages": [
    { "role": "system",    "content": "<system prompt>" },
    { "role": "user",      "content": "..." },
    { "role": "assistant", "content": null,
      "reasoning_content": "<мысли, сохраняем в history>",
      "tool_calls": [ { "id":"call_1", "type":"function",
                        "function":{"name":"read","arguments":"{\"path\":\"main.py\"}"} } ] },
    { "role": "tool", "tool_call_id": "call_1", "content": "<результат>" }
  ]
}
```

Ключевые отличия от «классического» OpenAI (из pi compat):

1. **`max_tokens`**, а не `max_completion_tokens`.
2. **Нет роли `developer`** — только `system` (pi: `supportsDeveloperRole: false`).
3. Thinking — не `thinking.budget_tokens` (Anthropic) и не `reasoning`-объект
   (OpenAI o-серии), а плоские поля **`enable_thinking` + `reasoning_effort`**
   (формат qwen). Бюджета в токенах API не даёт — только уровень усилия.
4. Роли в историях: `system, user, assistant, tool` (ровно этот набор,
   подтверждено request-log mtplx).
5. `reasoning_content` assistant-сообщений **сохраняется и пересылается**
   в следующих запросах (`--preserve-thinking auto` на сервере это ожидает).

## Маппинг thinking-настроек

| harness `thinking_enabled` | harness `thinking_effort` | `enable_thinking` | `reasoning_effort` в запросе |
|---|---|---|---|
| false | — (игнорируется) | `false` | не присылаем |
| true | `""` (дефолт) | `true` | не присылаем → серверный `medium` |
| true | `minimal` | `false` | не присылаем |
| true | `low / medium / xhigh` | `true` | то же значение |

Сервер принимает ровно `low`, `medium`, `xhigh` (`reasoning.effort_levels` из
`/health`); любые другие значения (например `high`) harness'у
превращать в `medium` и предупреждать через `settings_changed`. Это
совпадает с `thinkingLevelMap` из models.json pi: `{"minimal": null, "xhigh": "xhigh"}` —
`minimal` эквивалентен выключенному thinking, остальные уровни прокидываются
как есть. Это ровно поведение `thinkingFormat: "qwen"` из pi
(`params.enable_thinking = !!effort; params.reasoning_effort = effort`).

## Формат ответа (стрим)

SSE от mtplx, `data:` — JSON `chat.completion.chunk`:

```jsonc
// кусок размышления (до видимого текста):
{"choices":[{"delta":{"role":"assistant","reasoning_content":"Анализирую баг...","content":null}}]}
// кусок ответа:
{"choices":[{"delta":{"content":"В строке 12...","reasoning_content":null}}]}
// вызов инструмента (собирается по chunk'ам, как в OpenAI — arguments прилетает строкой по кускам):
{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"edit","arguments":"{\"path\":..."}}]}}]}
// конец:
{"choices":[{"finish_reason":"stop","delta":{}}]}          // либо "tool_calls", либо "length"
// usage — в последнем чанке (потому что stream_options.include_usage):
{"choices":[],"usage":{"prompt_tokens":41233,"completion_tokens":812,"cached_tokens":40000}}
```

Харнесс собирает chunk'и: `reasoning_content` → SSE `thinking_delta`,
`content` → `text_delta`, `tool_calls` → буфер (arguments — JSON, парсится
только после `finish_reason`), `usage` → событие `usage` + учёт границы.

### `finish_reason`

| значение | Что делать |
|---|---|
| `tool_calls` | исполнить вызовы → следующий turn |
| `stop` | ран завершён |
| `length` | выход уперся в `max_tokens`: вернуть модели сообщение «ответ обрезан, продолжи с места остановки» или (если это compact-запрос) повторить — политика в COMPACT.md |

## Проверка маппинга (выполнена 2026-09-30, живым curl'ом на 127.0.0.1:8000)

1. thinking: `enable_thinking:true, reasoning_effort:"low"` → 40 дельт
   `reasoning_content`, затем `content`, `finish_reason:"stop"`, финальный чанк
   с `usage` ✓
2. tool call: `finish_reason:"tool_calls"`, id `call_*`, `function.name` в первой
   дельте, `function.arguments` — конкатенация дельт (валидный JSON) ✓
3. round-trip: assistant-сообщение с `reasoning_content` + `tool_calls` +
   `role:"tool"` с `tool_call_id` — сервер принял, модель ответила по
   tool-результату ✓

Ошибки при проверке: **`Content-Type: application/json` обязателен** — без него
pydantic валится с `"Input should be a valid dictionary or object to extract
fields from"` (curl `-d` без явного хедера шлёт form-urlencoded). В httpx
(Python) всегда ставить явно.

Проверочный curl:

```bash
curl -sN http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer mtplx-local' -H 'x-mtplx-client: llm-harness' \
  -d '{
    "model":"mtplx-qwen38-27b-optimized-speed","stream":true,
    "stream_options":{"include_usage":true},
    "max_tokens":2048,"enable_thinking":true,"reasoning_effort":"medium",
    "tools":[{"type":"function","function":{"name":"read",
      "description":"Read a file. offset/limit are 1-based line numbers.",
      "parameters":{"type":"object","properties":{"path":{"type":"string"},
        "offset":{"type":"integer"},"limit":{"type":"integer"}},"required":["path"]}}}],
    "messages":[{"role":"system","content":"Ты кодинг-агент."},
                {"role":"user","content":"Вызови read для main.py, если есть"}]
  }'
```

Дополнительные факты из проверок:
- `usage.completion_tokens_details.reasoning_tokens` — thinking-токены вынесены
  отдельно (в `completion_tokens` включены: 36 + 28 = 64 ✓).
- `usage.prompt_tokens_details.cached_tokens` — токены из KV-кэша.
- Request log `~/.mtplx/logs/request-log-8000.jsonl`: `request_enable_thinking`,
  `request_reasoning_effort`, `request_max_tokens`, `effective_max_tokens`,
  `context_len`, `cached_tokens`, тайминги — главный инструмент отладки.
- Сервер busy-tolerant: если все сессии заняты, новые запросы стоят в очереди
  (у нас harness — единственный «большой» клиент, pi уже отключён).
