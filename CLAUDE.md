# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Telegram-бот без сервера и без Claude API: утренняя подборка по Витебску (карточка-инфографика, шутка, «для тебя», погода, курсы, новости, быт, афиша, кино, комплимент, мем дня). Работа разделена между двумя планировщиками:

1. **Routine в Claude Code** (облако, по подписке, ~06:07 по Минску) выполняет `routine.md`: читает `config.json` и `state.json`, веб-поиском собирает общую часть, пишет личные тексты каждому получателю, находит мем и force-push'ит `digest.json` (+ `meme.jpg`/`meme.png`, если смогла скачать) в ветку `claude/shared`. Модель и расписание routine задаются в её настройках на claude.ai/code/routines, не в репозитории.
2. **GitHub Actions** (`.github/workflows/digest.yml`) — два cron: в 05:30 `SYNC_ONLY=true` (только обработать входящие и закоммитить `state.json`, чтобы routine видела свежие имена/интересы/подписки), в 08:00 — рассылка: забирает файлы из `claude/shared`, добавляет погоду (Open-Meteo) и курсы (НБРБ), рисует `card.png` и шлёт.

История решения: общая часть через API с `web_search` стоила ~$1.6 за подборку (685K input tokens за 7 поисков), поэтому вся генерация текста перенесена в routine. Документация и тексты бота — на русском.

## Команды

```bash
pip install -r requirements.txt
git fetch origin claude/shared && git show FETCH_HEAD:digest.json > digest.json   # как в workflow
DRY_RUN=1 python digest.py   # напечатать подборку и сохранить card.png, ничего не отправлять
python digest.py             # реальная отправка (нужны TELEGRAM_BOT_TOKEN, INVITE_CODE)
```

Тестов, линтера и сборки нет. `DRY_RUN=1` не трогает Telegram и не требует токенов; получатели берутся из локального `state.json` (пустой — один фиктивный «Друг»), исключения не глушатся. После правок карточки смотри на сам `card.png` — вёрстка там на абсолютных координатах `fig.text`, наложения видно только глазами. `digest.json`, `card.png`, `meme.*` в `.gitignore` на master. Локальный `python3` на macOS может быть 3.9 — код требует 3.10+ (`int | None`, walrus в comprehension).

## Архитектура `digest.py`

Поток `main()`: `load_state()` → `ensure_names()` (в `SYNC_ONLY` на этом выход) → `load_digest()` (только если `date` — сегодня) → `get_hourly()`, `get_rates()` → `render_card()`, `get_meme()` → для каждого получателя: карточка, текст `intro, for_you, погода, курсы, shared, outro`, мем. Получатель без записи в `digest["personal"]` (подписался после routine) или день без `digest.json` — «Доброе утро, <имя>!» + `default.outro`. Карточка и мем — бонусы: их сбой только логируется.

- **Контракт с routine** — формат `digest.json` описан в `routine.md` (`date`, `shared`, `highlights` для карточки, `personal[chat_id] = {intro, for_you, outro}`, `default.outro`, `meme = {caption, url, source}`). Меняешь поля — меняй обе стороны.
- **Состояние** — `state.json`: `{"users": {chat_id: {"name", "awaiting_name", "interests"}}}`. Workflow коммитит его после каждого запуска (явно только его). `load_state()` добавляет `TELEGRAM_CHAT_ID` (если задан) как получателя без приглашения и мигрирует старый формат с одним `name` на верхнем уровне.
- **Подписка** — только по deep link `t.me/<бот>?start=<INVITE_CODE>`: бот получает `/start <код>` и добавляет chat_id. Незнакомые чаты и `/start` с неверным кодом молча игнорируются. Пустой `INVITE_CODE` отключает подписку.
- **Входящие (`ensure_names`)** — `getUpdates` (long polling, без вебхуков). `/name`, `/interests`, ответ на вопрос об имени. Ждёт ответа до `name_wait_minutes` только от спрошенных в этом запуске; в `SYNC_ONLY` не ждёт и отвечает с `disable_notification`. В конце offset подтверждается отдельным `getUpdates`.
- **Погода** — готовый раздел без модели: температура в 08/13/18 и интервалы осадков (`rain_windows()`: час дождливый при вероятности ≥ `RAIN_PROBABILITY` или осадках ≥ 0.1 мм).
- **Курсы** — `api.nbrb.by/exrates/rates?periodicity=0` (USD, EUR, 100 RUB) и тот же запрос с `ondate` на день раньше для стрелки ▲/▼.
- **Карточка** — matplotlib (Agg), 1080×1350, шрифт DejaVu Sans (встроен, с кириллицей; эмодзи в нём нет — в карточке их не использовать). Температура и вероятность дождя — два графика с общей осью X, не dual-axis. Цвета — эталонная палитра dataviz.
- **Мем** — `get_meme()`: сначала файл `meme.jpg`/`meme.png` от routine (пустит ли её облако к хостингам картинок — не гарантировано), иначе скачивает `meme.url` сам (только JPEG/PNG ≤ 10 МБ). Файлы шлются multipart'ом через `tg(..., files=...)`.
- **Вывод** — обычный текст без Markdown (`parse_mode` не задаётся). `split_message()` режет по строкам под `TG_LIMIT = 4000`.

## Деплой

- Секреты GitHub Actions: `TELEGRAM_BOT_TOKEN`, `INVITE_CODE`, необязательный `TELEGRAM_CHAT_ID`. `ANTHROPIC_API_KEY` больше не используется.
- Режим запуска определяется в workflow по `github.event.schedule == '30 2 * * *'` → `SYNC_ONLY`. Меняешь это cron-выражение — меняй его во всех местах workflow.
- Routine: у запусков по расписанию сдвиг до ~30 минут, поэтому она стоит между синхронизацией (05:30) и рассылкой (08:00). Правки `config.json`/`routine.md` она подхватывает при следующем запуске (каждый раз клонирует репозиторий заново).
