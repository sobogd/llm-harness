# Flutter-приложение + мост: архитектура и план

Приложение на Flutter/Dart (Android, **один пользователь, установка через ADB**)
управляет llm-harness через мост на VPS. Мост — по мотивам
`cloudlyru/agents/pi-bridge` (тонкий HTTP-адаптер: не думает, не хранит историю).

## 0. Зафиксированные решения

| Параметр | Значение |
|---|---|
| Домен (TLS) | `harness.iq-factura.com` (Caddy + Let's Encrypt на VPS → loopback-порт моста) |
| Workspace | **один**: harness с cwd `/Users/sobogd/work/llm-harness` |
| Название приложения | `harness.iq-factura.agent` (и applicationId) |
| Токен | **один общий секрет, вводится в настройках приложения** (см. §5) |
| Установка | `adb install` (или `flutter run`), в стор не публикуется |
| Экран ввода токена/сервера | адрес — константа сборки; токен — поле на экране настроек (shared_preferences) |

## 1. Где что живёт

| Компонент | Где | Почему |
|---|---|---|
| llm-harness (gRPC :9000, SSE :9001) | Мак | mtplx (локальная модель) живёт на маке; harness ходит на `127.0.0.1:8000` |
| reverse-SSH-туннель мак→VPS | существующий `cloudlyru/agents/mac/run-tunnel.sh` | паттерн опробован cloudlyru; порт на loopback **обоих** концов |
| TLS-прокси + мост (loopback :18830) | VPS | единственная точка, видная телефону |
| Flutter-приложение | Android (ADB) | UI: чат с агентом, инструменты, usage, настройки |

```
[Android: harness.iq-factura.agent, ADB-установка]
   │  HTTPS + SSE (harness.iq-factura.com), Authorization: Bearer <зашитый токен>
[VPS: Caddy (Let's Encrypt) → мост :18830 (loopback)]
   │  reverse-SSH туннель мака → 127.0.0.1:19000 / :19001
[Мак: llm-harness  gRPC :9000  SSE :9001, cwd=/Users/sobogd/work/llm-harness]   (loopback)
   └── mtplx :8000
```

Никто наружу не смотрит, кроме домена VPS (TLS + зашитый токен). ГRPC/SSE
harness'a закрыты на loopback мака.

## 2. Что добавить в харнесс (фаза 7)

UI-клиенту не хватает одного: **получить историю сообщений** для экрана
(после перезапуска приложения, при входе в сессию). Ring-буфер SSE покрывает
переподключение *живого* процесса, но не *перезапуск* приложения.

Новый RPC:

```proto
rpc GetMessages(GetMessagesRequest) returns (GetMessagesReply);
message GetMessagesRequest { int32 last = 1; }   // 0 = все
message GetMessagesReply {
  repeated Message m = 1;   // { role, content, tool_calls[], tool_call_id,
                            //   name, reasoning_content }
  string model = 2;
}
```

Читается из RAM-истории (персист уже есть — история жива). Всё остальное
(API.md §1–§6) мосту достаточно: команды + SSE-события.

## 3. Мост (Python, aiohttp, на VPS)

**Назначение**: тонкий адаптер HTTP/SSE → (gRPC, SSE). Не думает, не хранит,
не знает про модели. История — в harness (session.jsonl на маке), мост её не читает.

### Конфиг `~/.llm-bridge.json` (mode 600)

```json
{
  "port": 18830,
  "token": "<тот же секрет, что зашит в APK>",
  "harness": {"name": "harness", "grpc": "127.0.0.1:19000", "sse": "127.0.0.1:19001",
              "cwd": "/Users/sobogd/work/llm-harness"}
}
```

Один workspace (решено). Allowlist из конфига — телефон не может сам выбрать
адрес (никакого SSRF). Масштабирование на несколько workspace-ов возможно
формой конфига, но не строится.

### Ручки

| Ручка | Что делает |
|---|---|
| `GET /health` | жив ли мост, жив ли harness (пинг по gRPC Status) |
| `GET /harness` | статус workspace: имя, cwd, состояние рана, turn, hist, prompt_tokens_last, loaded |
| `GET /harness/status` | полный Status harness'a |
| `POST /harness/ask` `{prompt}` | → gRPC Ask → `{run_id, state}` |
| `POST /harness/stop` / `resume` / `compact` `{keep}` / `settings` | прокидка соответствующего gRPC |
| `GET /harness/messages?last=N` | GetMessages (RPC из фазы 7) |
| `GET /harness/events?since=<seq>` | **SSE-прокидка**: мост держит соединение с SSE harness'a и прозрачно ретранслирует события; heartbeat 15 c дублируется, если harness молчит |

Маршруты `/harness/*` без параметра id — workspace один; при масштабировании
вернём id, это не ломает API.

Аутентификация: `Authorization: Bearer <токен>` на **всех** ручках (включая
/events — header ставится на GET). Проверка — сравнение с токеном **приложения**
(единственный общий секрет; мост хранит его копию в конфиге). Неверный/отсутствующий
→ `401` без различения причин. TLS снаружи (Caddy); мост — loopback.
Логирование: какая ручка, seq-диапазон; токен в логах никогда.

### Переподключение (критично для мобайла)

1. Поток умер (NAT/proxy рвёт молча) → приложение видит: `ping`-heartbeat
   не пришёл N секунд → переподключение с `?since=<последний seq>`.
2. Мост на VPS умер/перезапустился → мост переподключается к SSE harness'a
   с тем же `since` (ring-буфер 10 000) и досылает.
3. Harness-процесс умер → мост держит SSE-ответ открытым и шлёт своё событие
   `bridge` `{kind:"harness_down"}`; приложение показывает баннер «агент
   недоступен» и пингует `/health`. (Автостарт harness'a — launchd на маке;
   мосту не надо его поднимать.)
4. Туннель рванул → то же: `harness_down` → пинг.

Это проще pi-bridge (`snapshot`/`ping`): у нас есть `seq` + `?since=`,
снимок «накопленный текст» не нужен.

## 4. Приложение (Flutter, Android)

Один пользователь, без аккаунтов, **без Setup-экрана**: адрес сервера
(`https://harness.iq-factura.com`) и токен — константы приложения.
Установка — ADB (`flutter run` / `adb install app-release.apk`).

### Константы сборки

```dart
// lib/config.dart — значения задаются при сборке:
//   flutter build apk --dart-define-from-file=secrets/defs.json
// secrets/defs.json НЕ входит в git (.gitignore)
const String serverUrl = String.fromEnvironment('SERVER_URL',
    defaultValue: 'https://harness.iq-factura.com');
const String authToken = String.fromEnvironment('AUTH_TOKEN');
```

Секрет живёт только в `secrets/defs.json` рядом с проектом (gitignore) и в
бинарнике; в репозиторий не попадает. Ротация = новое значение в defs.json,
пересборка APK, та же строка в конфиге моста.

### Экраны

| Экран | Содержимое |
|---|---|
| **Chat** (главный, открывается сразу) | переписка: сообщения user/assistant; assistant — блоки (текст, tool-карточки в порядке появления — как `blocks` в pi-bridge), свёрнутый thinking (раскрывается), usage под ответом (prompt/completion/reasoning токены); шапка: состояние (running/stopped/done/error), turn, контекст (окно + % занято); FAB «остановить», при stopped — «продолжить» |
| **Settings** (шестерёнка в шапке) | thinking on/off + effort (low/medium/xhigh), max_context_tokens, max_output_tokens (валидация лимитов 204800/202752), ручной Compact |

Workspace один — отдельного списка workspace нет; при масштабировании список
возвращается перед Chat.

### Состояние и данные

- `RunView` (сообщения + live-блоки текущего рана), `AgentState`
  (idle/running/stopped/done/error из Status), `ConnectionState`
  (ok / server_down / harness_down / bad_token).
- Источники: REST-снапшоты (`/harness/status`, `/messages`) + SSE-поток.
  SSE — единственный источник live-изменений; REST — только при входе в
  экран и по таймауту пингов.
- Локально: `shared_preferences` (последний seq). Историю сообщений в телефон
  не пишем (источник правды — session.jsonl на маке; при слабом канале
  показываем кеш кадра, не правду).
- `bad_token` (401) — отдельный экран: «APK устарел относительно сервера,
  соберите новую сборку» (ротация токена).

### Клиент SSE в Dart

`http` (или `dio`) со стриминговым ответом + разбор `text/event-stream`
(`id:`/`event:`/`data:`). Требования: читать построчно без буферизации всего
тела; по `id` вести `lastSeq`; таймер «нет события 45 c →
reconnect(since=lastSeq)». Пакет `eventsource` — кандидат; если не подойдёт —
~100 строк своего парсера.

### Уведомления

MVP: без push. Приложение живёт только открытым; фон — только auto-reconnect
SSE (Android Doze это уронит — ок для MVP, пометить в README приложения).
Опционально потом: FCM-напоминалка «ран завершён» через мост — не планировать
серьёзно.

## 5. Безопасность

- **Один токен — зашит в приложение.** Распределение APK — только ADB
  (свой телефон, свой бинарник), поэтому зашитый токен принимается: тот, у
  кого есть токен вне приложения, всё равно не имеет моего APK, и наоборот.
  Токен не вводится, не хранится в secure storage, не показывается — это
  константа сборки.
- **Принятый риск**: из APK токен извлекается (`strings` на dex, decompile).
  Защита против этого — не криптография, а модель «моя железяка, мой канал»:
  TLS с валидным сертификатом (Let's Encrypt), токен без значения вне пары
  «APK + сервер». Если модель изменится (APK уйдёт в стор) — токен выносим
  на серверную выдачу и пересобираем.
- **Мост — просто мост**: хранит копию токена только для сравнения
  (`~/.llm-bridge.json`, mode 600); логирует prefix 8 hex + `…`, сам токен — никогда.
- **TLS**: Caddy на VPS, Let's Encrypt для `harness.iq-factura.com` →
  `127.0.0.1:18830`. Приложение ходит только по HTTPS.
- **Mac-сторона**: harness на loopback; туннель — существующий reverse-SSH
  (механизм cloudlyru); порты 19000/19001 добавить в `run-tunnel.sh` + в
  очистку залипших слушателей (паттерн README pi-bridge).

## 6. Порядок работ и критерии

| Фаза | Что | Критерий |
|---|---|---|
| **7** | `GetMessages` RPC в харнессе + регресс | gRPC-вызов возвращает историю в том же виде, что RAM; E2E не сломан |
| **8** | Мост на VPS + Caddy + туннель: конфиг, токен, `/health`, `/harness*`, прокидка ask/stop/resume/compact/settings/messages/events, `harness_down`, systemd | `curl -H "Authorization: Bearer …" https://harness.iq-factura.com/harness/status`; полный цикл ask→stop→resume; `curl -N /harness/events` показывает SSE; без токена 401; убийство harness'a → `harness_down` → автопоявление после restart |
| **9** | Flutter-скелет: Chat (SSE-клиент, reconnect по `since`), Settings, экраны состояния (`server_down`/`harness_down`/`bad_token`), константы сборки через `--dart-define-from-file` | С телефона (ADB): спросить, остановить/продолжить, увидеть tool-карточки и thinking; убить Wi-Fi → переподключение → поток догнан по `since` |
| **10** | Полировка: usage-метрики в UI, компакт-кнопка, обработка error-состояний, иконка/название `harness.iq-factura.agent`, release-сборка APK | Ручной pass по чек-листу сценариев |

Оценки: фаза 7 — день; 8 — 2–3 дня; 9 — 3–5 дней; 10 — 1–2 дня.

## 7. Чего намеренно НЕТ (MVP-отсечения)

- Push-уведомления (фон Android) — фаза 10+, по ощущению.
- Мультиаккаунты/несколько токенов — один зашитый токен, один пользователь.
- Setup-экран и secure storage — токен и адрес зашиты в бинарник (ADB-модель).
- Список workspace / мульти-агенты — один workspace; форма конфига моста
  это допускает, UI строится при масштабировании.
- Управление файлами workspace'а (браузер) — агент сам читает/пишет; UI не надо.
- Выбор модели — модель одна (mtplx); когда появятся другие — появится
  `GET /harness/models` у моста (у харнесса уже есть поле `model` в настройках,
  SetSettings умеет менять `model`).