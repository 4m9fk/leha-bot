# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Telegram-бот без сервера: GitHub Actions раз в день запускает `digest.py`, который собирает утреннюю подборку по Витебску (шутка, погода, новости, афиша, комплимент) через Claude API с веб-поиском и отправляет её в Telegram. Весь код — один файл `digest.py`, поведение настраивается через `config.json`. Документация и тексты бота — на русском.

## Команды

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
DRY_RUN=1 python digest.py   # напечатать подборку в stdout, ничего не отправлять
python digest.py             # реальная отправка в Telegram
```

Тестов, линтера и сборки нет. `DRY_RUN=1` — основной способ проверить изменения: он не трогает Telegram (для него `TELEGRAM_*` не нужны), подставляет имя «Друг» и пробрасывает исключения вместо отправки сообщения об ошибке. Для DRY_RUN всё равно нужен `ANTHROPIC_API_KEY`, и каждый запуск стоит денег (веб-поиск, ≈ $0.05–0.15).

## Архитектура `digest.py`

Поток `main()`: `load_state()` → `ensure_name()` → `generate_digest()` → `send()`.

- **Состояние** — `state.json` в корне репозитория (`name`, `awaiting_name`). Workflow должен коммитить его обратно в репозиторий после запуска, иначе имя теряется между запусками.
- **Знакомство (`ensure_name`)** — читает входящие через `getUpdates` (long polling, без вебхуков). Если имени нет — спрашивает и ждёт ответа до `name_wait_minutes`. Команда `/name <имя>` меняет имя; она обрабатывается только в начале следующего запуска (Telegram хранит апдейты 24 часа). В конце offset подтверждается отдельным вызовом `getUpdates`, чтобы сообщения не обрабатывались повторно.
- **Погода** — Open-Meteo (без ключа), почасовые точки 08/13/18/21 плюс дневной итог, коды погоды переводятся через словарь `WMO`. При ошибке в промпт уходит строка с просьбой найти прогноз через веб-поиск, а не исключение.
- **Генерация** — один промпт из `build_prompt()`; разделы берутся из `config.json["sections"]` в заданном порядке (добавить или изменить раздел = править конфиг, а не код). Используется серверный инструмент `web_search_20250305` с `user_location` и лимитом `max_searches`. Цикл до 5 итераций продолжает запрос при `stop_reason == "pause_turn"`.
- **Вывод** — обычный текст без Markdown (так требует промпт; `parse_mode` не задаётся). `split_message()` режет по строкам под лимит `TG_LIMIT = 4000`.
- **Ошибки** — при сбое генерации пользователю уходит «⚠️ Не удалось собрать подборку…», и процесс завершается с кодом 1.

## Деплой

- Секреты GitHub Actions: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Workflow `.github/workflows/digest.yml`: запуск по `cron` (в UTC; `0 5 * * *` = 08:00 по Минску) и вручную через `workflow_dispatch`. После скрипта шаг с `if: always()` коммитит изменившийся `state.json`, поэтому нужен `permissions: contents: write`. `timeout-minutes` должен покрывать `name_wait_minutes` плюс генерацию.
- Модель задаётся в `config.json` (`claude-sonnet-5` по умолчанию, `claude-haiku-4-5` — дешевле). Если API ругается на модель или `web_search_20250305`, сверь актуальные названия с документацией Anthropic.
