"""Ежедневная подборка: шутка, погода, новости и мероприятия города, комплимент.

Переменные окружения:
  ANTHROPIC_API_KEY   ключ с console.anthropic.com
  TELEGRAM_BOT_TOKEN  токен от @BotFather
  INVITE_CODE         секретный код для ссылки-приглашения t.me/<бот>?start=<код>
  TELEGRAM_CHAT_ID    (необязательно) chat_id, который подписан сразу, без приглашения
  DRY_RUN=1           (необязательно) напечатать подборку, а не отправлять

Получатели и их имена хранятся в state.json (workflow коммитит его обратно в репозиторий).
Подписаться: открыть ссылку-приглашение и нажать Start — бот спросит имя при следующем запуске.
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
INVITE_CODE = os.getenv("INVITE_CODE", "").strip()
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

WMO = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
       45: "туман", 48: "изморозь", 51: "слабая морось", 53: "морось", 55: "сильная морось",
       61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
       71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
       80: "ливень", 81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад",
       95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом"}


# ---------- состояние ----------

def load_state() -> dict:
    """state.json: {"users": {chat_id: {"name": ..., "awaiting_name": ...}}}."""
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    users = state.setdefault("users", {})
    owner = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if owner and owner not in users:
        users[owner] = {}
    if "name" in state:  # старый формат с одним получателем
        if owner:
            users[owner].setdefault("name", state["name"])
        del state["name"]
    state.pop("awaiting_name", None)
    return state


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


def send(chat_id: str, text: str) -> None:
    if DRY_RUN:
        print(text, "\n" + "-" * 40)
        return
    for part in split_message(text):
        tg("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)


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


def fetch_messages(offset: int | None, timeout: int = 0) -> tuple[list[tuple[str, str]], int | None]:
    """Возвращает пары (chat_id, текст) из личных чатов с ботом и следующий offset."""
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    updates = tg("getUpdates", **params)
    messages = [(str(m["chat"]["id"]), m["text"].strip()) for u in updates
                if (m := u.get("message")) and m["chat"]["type"] == "private" and m.get("text")]
    new_offset = updates[-1]["update_id"] + 1 if updates else offset
    return messages, new_offset


def ask_name(chat_id: str, user: dict, asked: set[str]) -> None:
    send(chat_id, "Привет! 👋 Я утренний бот: шутка, погода, новости и афиша "
                  f"города {CONFIG['city']}. Как тебя зовут?")
    user["awaiting_name"] = True
    asked.add(chat_id)


def handle_messages(messages: list[tuple[str, str]], state: dict, asked: set[str]) -> None:
    users = state["users"]
    for chat_id, text in messages:
        user = users.get(chat_id)
        if user is None:  # подписаться можно только по ссылке-приглашению
            if INVITE_CODE and text.split() == ["/start", INVITE_CODE]:
                users[chat_id] = user = {}
                ask_name(chat_id, user, asked)
            continue
        if text.startswith("/name"):
            new = text[len("/name"):].strip()
            if new:
                user["name"] = new[:50]
                user["awaiting_name"] = False
                send(chat_id, f"Запомнил: теперь ты для меня {user['name']} 😊")
            else:
                user.pop("name", None)
        elif text.startswith("/"):
            continue  # /start и прочие команды
        elif user.get("awaiting_name") and not user.get("name"):
            user["name"] = text[:50]
            user["awaiting_name"] = False
            send(chat_id, f"Приятно познакомиться, {user['name']}! 🤗 "
                          f"Теперь каждое утро буду присылать тебе подборку. "
                          f"Если захочешь сменить имя — напиши /name и новое имя.")


def ensure_names(state: dict) -> None:
    if DRY_RUN:
        state["users"] = {"dry-run": {"name": "Друг"}}
        return
    users, asked = state["users"], set()
    messages, offset = fetch_messages(None)
    handle_messages(messages, state, asked)
    for chat_id, user in users.items():
        if not user.get("name") and not user.get("awaiting_name"):
            ask_name(chat_id, user, asked)

    # ждём ответа только от тех, у кого спросили имя в этом запуске;
    # ответ на старый вопрос подхватится при следующем запуске
    deadline = time.time() + CONFIG["name_wait_minutes"] * 60
    while any(not users[c].get("name") for c in asked) and time.time() < deadline:
        messages, offset = fetch_messages(offset, timeout=50)
        handle_messages(messages, state, asked)

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
    ensure_names(state)
    if not state["users"]:
        print("Получателей нет: открой ссылку-приглашение и нажми Start.")
        return
    failed = False
    for chat_id, user in state["users"].items():
        try:
            send(chat_id, generate_digest(CONFIG, user.get("name") or "друг"))
        except Exception as e:
            if DRY_RUN:
                raise
            failed = True
            print(f"Подборка для {chat_id} не отправлена: {e}", file=sys.stderr)
            try:
                send(chat_id, f"⚠️ Не удалось собрать подборку: {e}")
            except Exception:
                pass  # например, пользователь заблокировал бота
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
