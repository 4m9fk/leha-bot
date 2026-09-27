# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Telegram-бот без сервера и без Claude API: утренняя подборка по Витебску (кринжовая открытка «С добрым утром», карточка-инфографика с погодой и курсами, «для тебя», этот день (праздники и история), 5 поводов выпить, новости Витебска, хорошие новости дня, мир (политика и технологии), куда сходить вечером, рецепт дня без мяса (рыба и яйца можно), алкогольный напиток дня, план на выходные по пятницам, новое место по понедельникам, афиша, кино, комплимент, мем дня). Работа разделена между двумя планировщиками:

1. **Routine в Claude Code** (облако, по подписке, ~06:07 по Минску) выполняет `routine.md`: читает `config.json` и `state.json`, веб-поиском собирает общую часть, пишет личные тексты каждому получателю, находит открытку и мем и force-push'ит `digest.json`, `history.json` (архив прошлых выпусков против повторов, routine ведёт его сама) и `postcard.*`/`meme.*`, если смогла скачать, в ветку `claude/shared`. Модель и расписание routine задаются в её настройках на claude.ai/code/routines, не в репозитории.
2. **GitHub Actions** (`.github/workflows/digest.yml`) забирает файлы из `claude/shared`, добавляет погоду (Open-Meteo) и курсы (НБРБ), рисует `card.png` и шлёт. Когда слать, решает `should_send()` по env `MODE`, который workflow выводит из события:
   - `sync` (cron 05:30) — только обработать входящие и закоммитить `state.json`, чтобы routine видела свежие имена/интересы/знаки/подписки;
   - `morning` (cron 08:00) — слать, только если выпуск за сегодня уже готов;
   - `push` (пуш routine в `claude/shared`) — слать, если уже после `SEND_AFTER_HOUR` и сегодня ещё не слали: страховка от опоздания routine;
   - `deadline` (cron 10:00) — если сегодня ещё не слали, слать что есть (урезанную подборку);
   - `force` (ручной запуск, по умолчанию) — слать всегда.
   Отметка «сегодня уже слали» — `state["delivered"]`. `config.json["allow_resend"] = true` отключает эту проверку только для `push` (временно включён для тестов перезапуском routine — вернуть в `false`). Push-запуск идёт из ветки `claude/shared`, поэтому checkout явно берёт основную ветку, и `state.json` коммитится туда.

История решения: общая часть через API с `web_search` стоила ~$1.6 за подборку (685K input tokens за 7 поисков), поэтому вся генерация текста перенесена в routine. Документация и тексты бота — на русском.

## Команды

```bash
pip install -r requirements.txt
git fetch origin claude/shared && git show FETCH_HEAD:digest.json > digest.json   # как в workflow
DRY_RUN=1 python digest.py   # напечатать подборку и сохранить card.png, ничего не отправлять
python digest.py             # реальная отправка (нужны TELEGRAM_BOT_TOKEN, INVITE_CODE)
```

Тестов, линтера и сборки нет. `DRY_RUN=1` не трогает Telegram и не требует токенов; получатели берутся из локального `state.json` (пустой — один фиктивный «Друг»), исключения не глушатся. После правок карточки смотри на сам `card.png` — вёрстка там на абсолютных координатах `fig.text`, наложения видно только глазами. `digest.json`, `history.json`, `card.png`, `postcard.*`, `meme.*` в `.gitignore` на master. Локальный `python3` на macOS может быть 3.9 — код требует 3.10+ (`int | None`, walrus в comprehension).

## Архитектура `digest.py`

Поток `main()`: `load_state()` → `ensure_names()` (в `SYNC_ONLY` на этом выход) → `load_digest()` (только если `date` — сегодня) → `get_hourly()`, `get_rates()` → `render_card()`, `get_picture()` → для каждого получателя: открытка, карточка, текст `intro, for_you, shared, outro`, мем. Погода и курсы — только на карточке; текстовыми разделами (`weather_section`, `rates_section`) они идут лишь если карточку нарисовать не удалось. Получатель без записи в `digest["personal"]` (подписался после routine) или день без `digest.json` — «Доброе утро, <имя>!» + `default.outro`. Открытка, карточка и мем — бонусы: их сбой только логируется.

- **Контракт с routine** — формат `digest.json` описан в `routine.md` (`date`, `shared`, `highlights` для карточки, `personal[chat_id] = {intro, for_you, outro}`, `default.outro`, `card_tagline`, `postcard = {url, source}`, `meme = {caption, url, source}`). Меняешь поля — меняй обе стороны.
- **Состояние** — `state.json`: `{"users": {chat_id: {"name", "awaiting_name", "interests"}}}`. Workflow коммитит его после каждого запуска (явно только его). `load_state()` добавляет `TELEGRAM_CHAT_ID` (если задан) как получателя без приглашения и мигрирует старый формат с одним `name` на верхнем уровне.
- **Подписка** — только по deep link `t.me/<бот>?start=<INVITE_CODE>`: бот получает `/start <код>` и добавляет chat_id. Незнакомые чаты и `/start` с неверным кодом молча игнорируются. Пустой `INVITE_CODE` отключает подписку.
- **Входящие (`ensure_names`)** — `getUpdates` (long polling, без вебхуков). `/name`, `/interests`, ответ на вопрос об имени. Меню команд в Telegram задаётся `setMyCommands` из `BOT_COMMANDS` в начале каждого запуска — новую команду добавляй и туда. Ждёт ответа до `name_wait_minutes` только от спрошенных в этом запуске; в `SYNC_ONLY` не ждёт и отвечает с `disable_notification`. В конце offset подтверждается отдельным `getUpdates`.
- **Погода** — готовый раздел без модели: температура в 08/13/18 и интервалы осадков (`rain_windows()`: час дождливый при вероятности ≥ `RAIN_PROBABILITY` или осадках ≥ 0.1 мм).
- **Курсы** — `api.nbrb.by/exrates/rates?periodicity=0` (USD, EUR, 100 RUB) и тот же запрос с `ondate` на день раньше для стрелки ▲/▼.
- **Карточка** — matplotlib (Agg), 1080×1350, шрифт DejaVu Sans (встроен, с кириллицей; цветных эмодзи нет — только монохромные значки ☀☁☂☔❄⚡★▲▼●, их и красим). Градиентный фон, подложки `panel()` с `zorder=-5` (иначе перекрывают графики), значок погоды `weather_icon()`, вердикты «зонт/что надеть» `verdicts()`, девиз `digest["card_tagline"]` внизу. Температура и вероятность дождя — два графика с общей осью X, не dual-axis. Цвета — эталонная палитра dataviz.
- **Открытка и мем** — `get_picture(digest, key)` по таблице `PICTURES`: сначала файл `<base>.jpg`/`.png` от routine (пустит ли её облако к хостингам картинок — не гарантировано), иначе скачивает `<key>.url` сам (только JPEG/PNG ≤ 10 МБ). Файлы шлются multipart'ом через `tg(..., files=...)`.
- **Вывод** — подборка в Telegram-HTML (`send(..., html_mode=True)`, `parse_mode="HTML"`: `<b>`, `<i>`, `<a>`, `<blockquote expandable>`). Свои куски (приветствие-фоллбэк, погода/курсы без карточки) экранируются `html.escape`. Если Telegram отвечает `can't parse entities`, кусок уходит без разметки (`strip_html`). `split_message()` режет по разделам (`\n\n`), чтобы не рвать теги, — поэтому routine пишет разделы без пустых строк внутри. Ответы бота на команды и подписи к фото — простой текст.

## Деплой

- Секреты GitHub Actions: `TELEGRAM_BOT_TOKEN`, `INVITE_CODE`, необязательный `TELEGRAM_CHAT_ID`. `ANTHROPIC_API_KEY` больше не используется.
- Режим запуска (`MODE`) выводится в workflow из события и cron-выражения. Меняешь cron — меняй выражение и в `on.schedule`, и в вычислении `MODE`; час утренней рассылки ещё и в `SEND_AFTER_HOUR`. Пуш через `GITHUB_TOKEN` workflow не запускает (коммиты `state.json` в master этим не зацикливаются), а пуш routine через GitHub App — запускает.
- Routine: у запусков по расписанию сдвиг до ~30 минут, поэтому она стоит между синхронизацией (05:30) и рассылкой (08:00). Правки `config.json`/`routine.md` она подхватывает при следующем запуске (каждый раз клонирует репозиторий заново).
