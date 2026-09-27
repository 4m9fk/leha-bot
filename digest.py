"""Ежедневная подборка: шутка, погода, новости и мероприятия города, комплимент.

Переменные окружения:
  ANTHROPIC_API_KEY   ключ с console.anthropic.com
  TELEGRAM_BOT_TOKEN  токен от @BotFather
  TELEGRAM_CHAT_ID    твой chat_id (см. README)
  DRY_RUN=1           (необязательно) напечатать подборку, а не отправлять

Имя хранится в state.json (workflow коммитит его обратно в репозиторий).
Сменить имя: написать боту «/name Новое имя» — применится при следующем запуске.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state.json"
TG_LIMIT = 4000  # у Telegram лимит 4096 символов на сообщение
DRY_RUN = bool(os.getenv("DRY_RUN"))
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

WMO = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
       45: "туман", 48: "изморозь", 51: "слабая морось", 53: "морось", 55: "сильная морось",
       61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
       71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
       80: "ливень", 81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад",
       95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом"}


# ---------- состояние ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- Telegram ----------

def tg(method: str, **params) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params,
                      timeout=params.get("timeout", 0) + 30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send(text: str) -> None:
    if DRY_RUN:
        print(text, "\n" + "-" * 40)
        return
    for part in split_message(text):
        tg("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=part,
           disable_web_page_preview=True)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            parts.append(line[:limit]); line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current); current = ""
        current += line
    if current.strip():
        parts.append(current)
    return parts


def fetch_my_messages(offset: int | None, timeout: int = 0) -> tuple[list[str], int | None]:
    """Возвращает тексты сообщений из моего чата и следующий offset."""
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    updates = tg("getUpdates", **params)
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
    texts = [u["message"]["text"].strip() for u in updates
             if "message" in u and str(u["message"]["chat"]["id"]) == chat_id
             and u["message"].get("text")]
    new_offset = updates[-1]["update_id"] + 1 if updates else offset
    return texts, new_offset


def handle_messages(texts: list[str], state: dict) -> None:
    for text in texts:
        if text.startswith("/name"):
            new = text[len("/name"):].strip()
            if new:
                state["name"] = new[:50]
                send(f"Запомнил: теперь ты для меня {state['name']} 😊")
            else:
                state.pop("name", None)
        elif text.startswith("/"):
            continue  # /start и прочие команды
        elif state.get("awaiting_name") and not state.get("name"):
            state["name"] = text[:50]
            state["awaiting_name"] = False
            send(f"Приятно познакомиться, {state['name']}! 🤗 "
                 f"Теперь каждое утро буду присылать тебе подборку. "
                 f"Если захочешь сменить имя — напиши /name и новое имя.")


def ensure_name(state: dict) -> None:
    if DRY_RUN:
        state.setdefault("name", "Друг")
        return
    texts, offset = fetch_my_messages(None)
    handle_messages(texts, state)

    if not state.get("name"):
        send("Привет! 👋 Я твой утренний бот: шутка, погода, новости и афиша "
             f"города {CONFIG['city']}. Как тебя зовут?")
        state["awaiting_name"] = True
        deadline = time.time() + CONFIG["name_wait_minutes"] * 60
        while not state.get("name") and time.time() < deadline:
            texts, offset = fetch_my_messages(offset, timeout=50)
            handle_messages(texts, state)

    if offset:  # подтверждаем обработанные сообщения, чтобы не читать их повторно
        tg("getUpdates", offset=offset, timeout=0)
    save_state(state)


# ---------- погода (Open-Meteo, без ключа) ----------

def get_weather(cfg: dict) -> str:
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
            "latitude": cfg["latitude"], "longitude": cfg["longitude"], "timezone": cfg["timezone"],
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m",
            "daily": "temperature_2m_min,temperature_2m_max,precipitation_sum,weather_code,sunrise,sunset",
            "forecast_days": 1, "wind_speed_unit": "ms"})
        r.raise_for_status()
        d = r.json()
        h, day = d["hourly"], d["daily"]
        lines = [f"Итог дня: {WMO.get(day['weather_code'][0], '?')}, "
                 f"от {day['temperature_2m_min'][0]:.0f} до {day['temperature_2m_max'][0]:.0f}°C, "
                 f"осадки {day['precipitation_sum'][0]} мм, "
                 f"восход {day['sunrise'][0][-5:]}, закат {day['sunset'][0][-5:]}."]
        for i in (8, 13, 18, 21):
            lines.append(f"{i:02d}:00 — {h['temperature_2m'][i]:.0f}°C "
                         f"(ощущается {h['apparent_temperature'][i]:.0f}), "
                         f"{WMO.get(h['weather_code'][i], '?')}, "
                         f"вероятность осадков {h['precipitation_probability'][i]}%, "
                         f"ветер {h['wind_speed_10m'][i]:.0f} м/с")
        return "\n".join(lines)
    except Exception as e:
        return f"(данные о погоде получить не удалось: {e} — найди прогноз через веб-поиск)"


# ---------- подборка ----------

def build_prompt(cfg: dict, name: str, weather: str) -> str:
    today = datetime.now(ZoneInfo(cfg["timezone"]))
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(cfg["sections"], 1))
    return f"""Ты — тёплый и остроумный утренний бот. Сегодня {today:%d.%m.%Y} ({WEEKDAYS[today.weekday()]}).
Получателя зовут {name}. Начни с приветствия по имени и дальше пиши подборку на языке: {cfg['language']}.

Разделы строго в таком порядке:
{sections}

Данные о погоде в городе {cfg['city']} (Open-Meteo):
{weather}

Правила:
- Для новостей и мероприятий обязательно используй веб-поиск (местные сайты, афиши, соцсети города).
  Опирайся только на найденное, ничего не выдумывай. Если мероприятий мало — лучше меньше, но настоящие.
- Каждая новость: 1–2 предложения сути + ссылка на источник.
- Формат — простой текст для Telegram, БЕЗ Markdown (никаких *, #, **). Заголовки разделов с эмодзи,
  пункты через «•», ссылки — просто URL на отдельной строке.
- Если по разделу ничего стоящего не нашлось — так и напиши одной строкой.
- Род в комплименте и обращениях определи по имени; если непонятно — используй нейтральные формулировки.
- Выведи только саму подборку, без комментариев о поиске."""


def generate_digest(cfg: dict, name: str) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": build_prompt(cfg, name, get_weather(cfg))}]
    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": cfg["max_searches"],
        "user_location": {"type": "approximate", "city": cfg["city"],
                          "country": cfg["country"], "timezone": cfg["timezone"]},
    }]
    for _ in range(5):  # длинный поиск может прийти с stop_reason=pause_turn — продолжаем
        resp = client.messages.create(model=cfg["model"], max_tokens=8000,
                                      messages=messages, tools=tools)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"Пустой ответ от Claude (stop_reason={resp.stop_reason})")
    return text


def main() -> None:
    state = load_state()
    ensure_name(state)
    try:
        send(generate_digest(CONFIG, state.get("name") or "друг"))
    except Exception as e:
        if DRY_RUN:
            raise
        send(f"⚠️ Не удалось собрать подборку: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
