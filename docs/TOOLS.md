# Инструменты (tool calling)

Стандартный OpenAI function calling: `tools[]` с JSON Schema, `tool_choice: "auto"`.
На сервере `--tool-prompt-mode hybrid` — инструменты и в системный промпт, и
как structured; ничего special присылать не нужно (проверено request-log:
`request_tool_names: ['read','bash','edit','write',...]`).

Семантика — по pi (он с этим набором и ходит в mtplx).

## `read`

```json
{"name":"read",
 "description":"Read file contents. Returns numbered lines. Use offset/limit for large files.",
 "parameters":{"type":"object","properties":{
   "path":{"type":"string","description":"File path, absolute or relative to cwd"},
   "offset":{"type":"integer","description":"1-based line number to start from"},
   "limit":{"type":"integer","description":"Max lines to return"}
 },"required":["path"]}}
```

- Возвращает строки **с номерами** (`  12│ def main():`) — без номеров
  модель не может адресовать строки для `edit`.
- `offset`/`limit` — чтение определённых строк (требование №1).
- По умолчанию: первые 2000 строк или 50KB, что меньше; при обрезке —
  пометка в конце `«обрезано, продолжай с offset=N»`.
- Ошибки: файл не найден / не читается → `ok:false` с сообщением.

## `write`

```json
{"name":"write",
 "description":"Create or overwrite a file with full content.",
 "parameters":{"type":"object","properties":{
   "path":{"type":"string"},"content":{"type":"string"}
 },"required":["path","content"]}}
```

Полная перезапись (или создание; родительские каталоги создаются).

## `edit` — ТОЧНОЕ совпадение

```json
{"name":"edit",
 "description":"Exact text replacement in a file. oldText must match the file content EXACTLY (whitespace, indentation) and must be unique.",
 "parameters":{"type":"object","properties":{
   "path":{"type":"string"},
   "oldText":{"type":"string","description":"Exact text to find, copied verbatim from read output (strip line numbers first)"},
   "newText":{"type":"string","description":"Replacement text"}
 },"required":["path","oldText","newText"]}}
```

Правила (как в pi, fuzzy-поиска НЕТ):

1. `oldText` должен совпасть с файлом **побайтово** (пробелы, табы, переносы).
2. Совпадение должно быть **единственным** в файле:
   - 0 совпадений → ошибка «no match, вот ближайшие фрагменты» (модель перечитает файл);
   - N>1 совпадений → ошибка «not unique, N matches at lines …, уточни oldText».
3. `oldText != newText` (no-op — ошибка).
4. Успех → `«OK, replaced 1 occurrence in <path>»`.

Модель получает текст из `read` с номерами строк `  12│ …`; в `oldText`
номера не включать — это явно прописывается в description обоих инструментов.
Перед сравнением у обоих сторон приводим `\r\n → \n` (один-единственный
нормализуемый шаг, без fuzzy).

## `bash`

```json
{"name":"bash",
 "description":"Run a shell command in the working directory. Use for builds, tests, git, ls, grep, etc.",
 "parameters":{"type":"object","properties":{
   "command":{"type":"string"},
   "timeout":{"type":"integer","description":"Seconds, default 120"}
 },"required":["command"]}}
```

- Вывод (stdout+stderr) обрезается до последних 2000 строк / 50KB.
- `Stop` рвёт LLM-стрим; уже запущенная команда доживает свой timeout
  (дефолт 120 c) — это осознанное упрощение MVP.

## `web_search`

```json
{"name":"web_search",
 "description":"Search the web (Brave, fallback Google Custom Search) and return ranked results with titles, URLs and snippets.",
 "parameters":{"type":"object","properties":{
   "query":{"type":"string"},
   "count":{"type":"integer","description":"1-10, default 8"}
 },"required":["query"]}}
```

- Каскад: **Brave** (`BRAVE_SEARCH_JSON_API_KEY`, квота 2000/мес) →
  **Google Custom Search JSON** (`GOOGLE_SEARCH_JSON_API_KEY` + `GOOGLE_CSE_ENGINE_ID`,
  квота 100/день). Ключи читаются из `~/work/.env` (переопределяется
  `LLM_HARNESS_ENV`), в ответ и логи не попадают.
- 429 от Brave (квота) → прозрачный переход на Google; 429 от Google →
  честная ошибка «квоты кончились», не молчание.
- Результат: нумерованный список `title [engine] / url / snippet`, дедупликация
  по URL.

## `browser_fetch`

```json
{"name":"browser_fetch",
 "description":"Open a URL in the user's real logged-in Chrome (CDP attach, fresh tab, closed after), wait for load, return rendered page text.",
 "parameters":{"type":"object","properties":{
   "url":{"type":"string"},
   "wait":{"type":"number","description":"Seconds, default 10, max 30"},
   "js":{"type":"string","description":"Optional JS expression instead of title+innerText"}
 },"required":["url"]}}
```

- Реализация: прямой Chrome DevTools Protocol поверх websocket
  (без зависимостей): `PUT /json/new?url=…` → создаётся новая вкладка →
  `Runtime.evaluate` до `document.readyState == "complete"` (+1.5 c на
  JS-рендер) → читаем результат → вкладка закрывается (`DELETE /json/{id}`).
- Требования: Chrome запущен с флагами отладки (порт 9222, только loopback):
  `open -na "Google Chrome" --args --user-data-dir="$HOME/.chrome-cdp" --remote-debugging-port=9222`
  (обёртка: команда `chrome-debug`, установлена в `/opt/homebrew/bin`).
  Для Chrome ≥ 136 `--user-data-dir` обязателен: с дефолтным профилем
  порт не поднимается. Это выделенный профиль `~/.chrome-cdp` — в него
  один раз логинятся (Google-аккаунт, reddit, x.com), сессии сохраняются.
- Автозапуск при входе в систему: LaunchAgent
  `~/Library/LaunchAgents/com.sobogd.chrome-debug.plist` →
  `~/bin/chrome-debug-login` (запускает Chrome со флагами и сворачивает окна).
- Флаг действует на процесс: после полного выхода Chrome запускать
  только через `chrome-debug` (или дождаться следующего входа в систему).
- Назначение: JS-тяжёлые и login-walled страницы (reddit, x.com и т.п.),
  которые `web_search`/curl прочитать не могут.
- То же самое работает для Chrome на Android через
  `adb forward tcp:9222 localabstract:chrome_devtools_remote`.
- Safari-вариант (AppleScript) не используется: в Safari 27 на macOS 26
  AppleScript-мост сломан — элементные доступы `window 1` / `document 1` /
  `front document` дают -10008, `close window` молча не работает,
  `make new window` падает с -10000.
- Результат обрезается до 50KB.
- Отладка без харнесса: `python -m llm_harness.browserfetch <url> [wait] [js]`.

## Общие правила исполнения

- Инструменты исполняются **последовательно** (MVP), результаты кладутся в
  историю `role: tool, tool_call_id: …` и пересылаются в следующем запросе.
- Результат инструмента обрезается до 50 000 символов с пометкой.
- Ошибка инструмента — не ошибка рана: модель получает текст ошибки и сама
  чинится (иногда по 2–3 попытки — нормально).
- Рабочая директория — cwd harness'а (параметр `--cwd`); относительные пути
  разрешаются от неё.
