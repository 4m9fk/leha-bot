"""Ежедневная подборка: открытка «С добрым утром», карточка-инфографика (погода, курсы, главное сегодня),
новости, афиша, кино, комплимент и мем дня.

Все тексты заранее пишет routine в Claude Code по routine.md (из подписки, без API): общую часть
и личные приветствие, «для тебя» и комплимент для каждого получателя из state.json, открытку и мем.
Результат — digest.json в ветке claude/shared; workflow подкладывает его рядом со скриптом.
Этот скрипт только добавляет погоду (Open-Meteo) и курсы (НБРБ), рисует карточку и рассылает.
Если digest.json за сегодня нет — уходит приветствие, погода и курсы.

Переменные окружения:
  TELEGRAM_BOT_TOKEN  токен от @BotFather
  INVITE_CODE         секретный код для ссылки-приглашения t.me/<бот>?start=<код>
  TELEGRAM_CHAT_ID    (необязательно) chat_id, который подписан сразу, без приглашения
  MODE                (необязательно) когда и как рассылать; ставит workflow:
                        force    — разослать сейчас (ручной запуск, по умолчанию)
                        sync     — только обработать входящие и выйти: ночной запуск перед routine,
                                   чтобы она видела свежий state.json
                        morning  — 08:00: разослать, только если выпуск за сегодня уже готов
                        push     — routine запушила выпуск: разослать, если уже после 08:00 и сегодня не слали
                        deadline — 10:00: если сегодня ещё не слали — разослать что есть
  DRY_RUN=1           (необязательно) напечатать подборку и сохранить card.png, а не отправлять

Получатели, имена и интересы хранятся в state.json (workflow коммитит его обратно в репозиторий).
Подписаться: открыть ссылку-приглашение и нажать Start. Команды: /name Имя, /interests кино, спорт, /zodiac Лев.
"""

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state.json"
DIGEST_FILE = ROOT / "digest.json"  # пишет routine, см. routine.md
CARD_FILE = ROOT / "card.png"
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
PHOTO_MAX_BYTES = 10_000_000  # лимит Telegram на фото
TG_LIMIT = 4000  # у Telegram лимит 4096 символов на сообщение
DRY_RUN = bool(os.getenv("DRY_RUN"))
MODE = os.getenv("MODE", "force")
SYNC_ONLY = MODE == "sync"
SEND_AFTER_HOUR = 8  # раньше этого часа по Минску push-запуск не рассылает; совпадает с cron «0 5» в workflow
INVITE_CODE = os.getenv("INVITE_CODE", "").strip()
RAIN_PROBABILITY = 40  # %, с какой вероятности осадков считать час дождливым
CURRENCIES = ("USD", "EUR", "RUB")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
ZODIAC = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
          "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]
DEFAULT_ZODIAC = "Дева"  # для тех, кто не выбрал знак; routine берёт его из config.json
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]

WMO = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
       45: "туман", 48: "изморозь", 51: "слабая морось", 53: "морось", 55: "сильная морось",
       61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
       71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
       80: "ливень", 81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад",
       95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом"}


def today(cfg: dict) -> date:
    return datetime.now(ZoneInfo(cfg["timezone"])).date()


# ---------- состояние ----------

def load_state() -> dict:
    """state.json: {"users": {chat_id: {"name", "awaiting_name", "interests", "zodiac"}},
    "delivered": "YYYY-MM-DD" — когда последний раз ушла рассылка (чтобы не слать дважды)}."""
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

def tg(method: str, files: dict | None = None, **params) -> dict:
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"
    if files:  # загрузка файла — multipart
        r = requests.post(url, data=params, files=files, timeout=120)
    else:
        r = requests.post(url, json=params, timeout=params.get("timeout", 0) + 30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send(chat_id: str, text: str, html_mode: bool = False) -> None:
    """html_mode — текст в Telegram-HTML (<b>, <i>, <a>, <blockquote expandable>). Если Telegram не разобрал
    разметку, кусок уходит простым текстом без тегов, а не теряется."""
    if DRY_RUN:
        print(text, "\n" + "-" * 40)
        return
    # лимит Telegram считается по тексту после разбора разметки — теги и адреса ссылок не в счёт
    for part in split_message(text, measure=(lambda t: len(strip_html(t))) if html_mode else len):
        # ранний синхронизирующий запуск идёт ночью — отвечаем без звука
        params = dict(chat_id=chat_id, disable_web_page_preview=True, disable_notification=SYNC_ONLY)
        if not html_mode:
            tg("sendMessage", text=part, **params)
            continue
        try:
            tg("sendMessage", text=part, parse_mode="HTML", **params)
        except RuntimeError as e:
            if "can't parse entities" not in str(e):
                raise
            print(f"HTML не разобрался, шлём без разметки: {e}", file=sys.stderr)
            tg("sendMessage", text=strip_html(part), **params)


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_photo(chat_id: str, path: Path, caption: str = "") -> None:
    if DRY_RUN:
        print(f"[фото: {path}] {caption}")
        return
    with path.open("rb") as f:
        tg("sendPhoto", files={"photo": f}, chat_id=chat_id, caption=caption[:1024])


def split_message(text: str, limit: int = TG_LIMIT, measure=len) -> list[str]:
    """Режет по разделам (абзацам через пустую строку), чтобы не разрывать HTML-теги;
    раздел длиннее лимита — по строкам, а строку длиннее лимита — как есть кусками.
    measure — как считать длину (для HTML — видимый текст без тегов)."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        pieces = [block] if measure(block) <= limit else block.splitlines()
        for piece in pieces:
            while measure(piece) > limit:
                parts.append(piece[:limit]); piece = piece[limit:]
            sep = "\n\n" if piece is block else "\n"
            if current and measure(current + sep + piece) > limit:
                parts.append(current); current = ""
            current = f"{current}{sep}{piece}" if current else piece
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
    send(chat_id, "Привет! 👋 Я утренний бот: открытка, погода, новости, афиша и мем дня "
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
        elif text.startswith("/interests"):
            new = text[len("/interests"):].strip()
            if new:
                user["interests"] = new[:300]
                send(chat_id, f"Запомнил интересы: {user['interests']} 🎯\n"
                              "В подборке появится раздел «Для тебя». Сбросить — /interests без текста.")
            else:
                user.pop("interests", None)
                send(chat_id, "Интересы сброшены.")
        elif text.startswith("/zodiac"):
            arg = text[len("/zodiac"):].strip().lower()
            sign = next((z for z in ZODIAC if z.lower() == arg), None)
            if sign:
                user["zodiac"] = sign
                send(chat_id, f"Запомнил: {sign} 🔮 Звёзды уже в курсе. Гороскоп — со следующего утра.")
            elif not arg:
                user.pop("zodiac", None)
                send(chat_id, f"Знак сброшен — гороскоп снова для «{DEFAULT_ZODIAC}».")
            else:
                send(chat_id, "Не знаю такого знака 🤔 Напиши, например: /zodiac Рыбы\n" + ", ".join(ZODIAC))
        elif text.startswith("/"):
            continue  # /start и прочие команды
        elif user.get("awaiting_name") and not user.get("name"):
            user["name"] = text[:50]
            user["awaiting_name"] = False
            send(chat_id, f"Приятно познакомиться, {user['name']}! 🤗 "
                          "Теперь каждое утро буду присылать тебе подборку.\n"
                          "Сменить имя — /name и новое имя. Расскажи, что тебе интересно, — "
                          "например, /interests кино, концерты, спорт — и я буду подбирать это отдельно.\n"
                          f"Гороскоп пока для знака «{DEFAULT_ZODIAC}» — свой знак: /zodiac Лев.")


BOT_COMMANDS = [  # меню «/» в Telegram; держать в синхроне с handle_messages
    {"command": "name", "description": "Сменить имя: /name Новое имя"},
    {"command": "interests", "description": "Интересы для «Для тебя»: /interests кино, концерты"},
    {"command": "zodiac", "description": "Знак зодиака для гороскопа: /zodiac Лев"},
]


def ensure_names(state: dict) -> None:
    try:  # идемпотентно: меню всегда совпадает с кодом
        tg("setMyCommands", commands=BOT_COMMANDS)
    except Exception as e:
        print(f"Меню команд не обновилось: {e}", file=sys.stderr)
    users, asked = state["users"], set()
    messages, offset = fetch_messages(None)
    handle_messages(messages, state, asked)
    for chat_id, user in users.items():
        if not user.get("name") and not user.get("awaiting_name"):
            ask_name(chat_id, user, asked)

    # ждём ответа только от тех, у кого спросили имя в этом запуске (и не в ночной синхронизации);
    # ответ на старый вопрос подхватится при следующем запуске
    deadline = time.time() + (0 if SYNC_ONLY else CONFIG["name_wait_minutes"] * 60)
    while any(not users[c].get("name") for c in asked) and time.time() < deadline:
        messages, offset = fetch_messages(offset, timeout=50)
        handle_messages(messages, state, asked)

    if offset:  # подтверждаем обработанные сообщения, чтобы не читать их повторно
        tg("getUpdates", offset=offset, timeout=0)
    save_state(state)


# ---------- погода (Open-Meteo) и курсы (НБРБ), без ключей ----------

def get_hourly(cfg: dict) -> dict | None:
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
            "latitude": cfg["latitude"], "longitude": cfg["longitude"], "timezone": cfg["timezone"],
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
            "forecast_days": 1})
        r.raise_for_status()
        return r.json()["hourly"]
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Погоду получить не удалось: {e}", file=sys.stderr)
        return None


def rain_windows(h: dict) -> list[str]:
    """Интервалы с 6 до 24 часов, когда вероятны осадки, например «14:00–17:00 дождь (до 80%)»."""
    rainy = [i for i in range(6, 24)
             if h["precipitation_probability"][i] >= RAIN_PROBABILITY or h["precipitation"][i] >= 0.1]
    windows = []
    for i in rainy:
        if windows and windows[-1][-1] == i - 1:
            windows[-1].append(i)
        else:
            windows.append([i])
    result = []
    for w in windows:
        peak = max(w, key=lambda i: h["precipitation_probability"][i])
        result.append(f"{w[0]:02d}:00–{w[-1] + 1:02d}:00 {WMO.get(h['weather_code'][peak], 'осадки')} "
                      f"(до {max(h['precipitation_probability'][i] for i in w)}%)")
    return result


def weather_section(h: dict) -> str:
    t = h["temperature_2m"]
    return "\n".join(["🌤 Погода", f"Утром {t[8]:.0f}°C, днём {t[13]:.0f}°C, вечером {t[18]:.0f}°C"]
                     + ([f"☔ {w}" for w in rain_windows(h)] or ["Без осадков"]))


def get_rates() -> list[dict]:
    """Официальные курсы НБРБ на сегодня и изменение ко вчера: [{code, scale, rate, delta}]."""
    def fetch(ondate: str | None = None) -> dict:
        params = {"periodicity": 0, **({"ondate": ondate} if ondate else {})}
        r = requests.get("https://api.nbrb.by/exrates/rates", params=params, timeout=20)
        r.raise_for_status()
        return {x["Cur_Abbreviation"]: x for x in r.json()}
    try:
        now = fetch()
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Курсы получить не удалось: {e}", file=sys.stderr)
        return []
    try:
        some_day = datetime.fromisoformat(next(iter(now.values()))["Date"]).date()
        before = fetch((some_day - timedelta(days=1)).isoformat())
    except (requests.RequestException, KeyError, ValueError, StopIteration):
        before = {}
    return [{"code": c, "scale": now[c]["Cur_Scale"], "rate": now[c]["Cur_OfficialRate"],
             "delta": now[c]["Cur_OfficialRate"] - before[c]["Cur_OfficialRate"] if c in before else None}
            for c in CURRENCIES if c in now]


def rates_section(rates: list[dict]) -> str:
    def fmt(r: dict) -> str:
        arrow = "" if not r["delta"] else (" ▲" if r["delta"] > 0 else " ▼")
        return f"{'' if r['scale'] == 1 else r['scale']} {r['code']}".strip() + f" {r['rate']:.4f}{arrow}"
    return "💱 Курсы НБРБ, BYN\n" + " · ".join(fmt(r) for r in rates)


# ---------- карточка-инфографика ----------

# цвета — эталонная палитра dataviz (светлая тема); температура и дождь — два отдельных графика
# с общей осью часов, а не один с двумя осями Y. Значки — из DejaVu Sans (☀☁☔❄⚡★), цветных эмодзи
# matplotlib не рисует.
INK, INK_2, GRID = "#0b0b0b", "#52514e", "#e6e5e0"
TEMP_COLOR, RAIN_COLOR = "#eb6834", "#2a78d6"
ACCENTS = ["#eb6834", "#2a78d6", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7"]  # категориальная палитра по порядку
BG_TOP, BG_BOTTOM, PANEL = "#fff3d6", "#fde2ec", "#ffffffcc"


def weather_icon(h: dict) -> tuple[str, str]:
    """Значок и цвет для «погоды дня» по часам 8–21."""
    codes = [h["weather_code"][i] for i in range(8, 22)]
    if any(c >= 95 for c in codes):
        return "⚡", "#eda100"
    if any(c in (71, 73, 75, 77, 85, 86) for c in codes):
        return "❄", RAIN_COLOR
    if rain_windows(h):
        return "☔", RAIN_COLOR
    if sum(c >= 3 for c in codes) > len(codes) / 2:
        return "☁", "#8b8a86"
    return "☀", "#eda100"


def verdicts(h: dict) -> list[str]:
    """Весёлые вердикты дня: зонт и одежда."""
    t = h["temperature_2m"]
    lo, hi = min(t[8:22]), max(t[8:22])
    umbrella = "☂ Зонт: бери!" if rain_windows(h) else "☂ Зонт: пусть отдыхает"
    if lo < 0:
        wear = "Надеть: всё, что есть"
    elif lo < 8:
        wear = "Надеть: куртку и шапку"
    elif lo < 14:
        wear = "Надеть: куртку"
    elif hi < 20:
        wear = "Надеть: худи"
    elif hi < 26:
        wear = "Надеть: футболку + кофту"
    else:
        wear = "Надеть: минимум, SPF — максимум"
    return [umbrella, wear]


def render_card(cfg: dict, h: dict | None, rates: list[dict], highlights: list[dict], tagline: str,
                path: Path) -> bool:
    """Рисует PNG 1080×1350: приветствие, погода с вердиктами, температура и дождь по часам, курсы,
    главное сегодня и девиз дня."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import FancyBboxPatch

    plt.rcParams.update({"font.family": "DejaVu Sans"})  # встроен в matplotlib, есть кириллица
    d = today(cfg)
    fig = plt.figure(figsize=(7.2, 9), dpi=150)
    bg = fig.add_axes([0, 0, 1, 1], zorder=-10)
    bg.imshow([[0], [1]], cmap=LinearSegmentedColormap.from_list("bg", [BG_TOP, BG_BOTTOM]),
              aspect="auto", extent=(0, 1, 0, 1), interpolation="bicubic")
    bg.axis("off")

    def panel(x, y, w, hgt, color=PANEL, radius=0.018):
        # zorder ниже осей: подложки не должны перекрывать графики
        fig.patches.append(FancyBboxPatch((x, y), w, hgt, boxstyle=f"round,pad=0,rounding_size={radius}",
                                          transform=fig.transFigure, facecolor=color, edgecolor="none",
                                          zorder=-5))

    fig.text(0.07, 0.955, f"Доброе утро, {cfg['city']}!", fontsize=22, fontweight="bold", color=INK, va="top")
    fig.text(0.07, 0.912, f"{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1]}",
             fontsize=12, color=INK_2, va="top")

    if h:
        hours = list(range(6, 24))
        temps = [h["temperature_2m"][i] for i in hours]
        rain = [h["precipitation_probability"][i] for i in hours]
        lo, hi = min(temps), max(temps)
        icon, icon_color = weather_icon(h)
        panel(0.05, 0.395, 0.9, 0.475)
        fig.text(0.09, 0.855, icon, fontsize=58, color=icon_color, va="top")
        fig.text(0.29, 0.852, f"{lo:.0f}° … {hi:.0f}°", fontsize=32, fontweight="bold", color=INK, va="top")
        windows = rain_windows(h)
        fig.text(0.29, 0.793, shorten("Осадки: " + "; ".join(windows), 48) if windows else "Без осадков",
                 fontsize=10, color=INK_2, va="top")
        for k, text in enumerate(verdicts(h)):  # плашки-вердикты
            x = 0.09 + k * 0.42
            panel(x, 0.715, 0.4, 0.045, color=ACCENTS[1 + k] + "26")
            fig.text(x + 0.02, 0.7375, text, fontsize=11, fontweight="bold", color=INK, va="center")

        ax_t = fig.add_axes([0.12, 0.535, 0.78, 0.13])
        ax_r = fig.add_axes([0.12, 0.43, 0.78, 0.06], sharex=ax_t)
        ax_t.plot(hours, temps, color=TEMP_COLOR, linewidth=2.2)
        ax_t.fill_between(hours, temps, lo - 2, color=TEMP_COLOR, alpha=0.15, linewidth=0)
        for i in (temps.index(lo), temps.index(hi)):  # подписываем только минимум и максимум
            ax_t.annotate(f"{temps[i]:.0f}°", (hours[i], temps[i]), textcoords="offset points",
                          xytext=(0, 7), ha="center", fontsize=10, fontweight="bold", color=INK)
        ax_t.set_ylim(lo - 2, hi + 3)
        ax_t.set_title("Температура, °C", loc="left", fontsize=9, color=INK_2, pad=3)
        ax_r.bar(hours, rain, width=0.7, color=RAIN_COLOR)
        ax_r.set_ylim(0, 100)
        ax_r.set_yticks([0, 50, 100])
        ax_r.set_title("Вероятность дождя, %", loc="left", fontsize=9, color=INK_2, pad=3)
        ax_r.set_xticks(range(6, 24, 3), [f"{x}:00" for x in range(6, 24, 3)])
        for ax in (ax_t, ax_r):
            ax.set_facecolor("none")
            ax.tick_params(colors=INK_2, labelsize=8, length=0)
            ax.grid(axis="y", color=GRID, linewidth=0.8)
            ax.set_axisbelow(True)
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            ax.spines["bottom"].set_color(GRID)
        plt.setp(ax_t.get_xticklabels(), visible=False)

    if rates:  # плашки с курсами, у каждой своя полоска-акцент
        fig.text(0.07, 0.372, "Курсы НБРБ, BYN", fontsize=10, color=INK_2, va="top")
        w = 0.9 / len(rates)
        for k, r in enumerate(rates):
            x = 0.05 + k * w
            panel(x, 0.265, w - 0.02, 0.085)
            panel(x + 0.018, 0.338, w - 0.056, 0.006, color=ACCENTS[k], radius=0.003)
            label = r["code"] if r["scale"] == 1 else f"{r['scale']} {r['code']}"
            fig.text(x + 0.025, 0.33, label, fontsize=10, color=INK_2, va="top")
            arrow = "" if not r["delta"] else (" ▲" if r["delta"] > 0 else " ▼")
            fig.text(x + 0.025, 0.303, f"{r['rate']:.4f}{arrow}", fontsize=15, fontweight="bold",
                     color=INK, va="top")

    if highlights:  # главное сегодня — из digest.json, с цветными номерами
        fig.text(0.07, 0.24, "Сегодня стоит", fontsize=10, color=INK_2, va="top")
        y = 0.212
        for k, item in enumerate(highlights[:3]):
            fig.text(0.075, y - 0.001, "●", fontsize=22, color=ACCENTS[k], va="top")
            fig.text(0.0885, y - 0.0115, str(k + 1), fontsize=10, fontweight="bold", color="white",
                     va="top", ha="center")
            fig.text(0.13, y, shorten(str(item.get("title", "")), 36), fontsize=12, fontweight="bold",
                     color=INK, va="top")
            meta = " · ".join(str(item[key]) for key in ("when", "where") if item.get(key))
            fig.text(0.13, y - 0.027, shorten(meta, 56), fontsize=9.5, color=INK_2, va="top")
            y -= 0.058

    if tagline:  # девиз дня от routine
        fig.text(0.5, 0.018, f"★ {shorten(tagline, 60)} ★", fontsize=11, fontstyle="italic",
                 color=ACCENTS[5], ha="center", va="bottom")

    fig.savefig(path)
    plt.close(fig)
    return True


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---------- картинки от routine: открытка и мем ----------

# ключ в digest.json → (базовое имя файла в ветке claude/shared, подпись в Telegram)
PICTURES = {"postcard": ("postcard", ""), "meme": ("meme", "😂 Мем дня")}


def get_picture(digest: dict, key: str) -> tuple[Path, str] | None:
    """Картинка от routine: файл <base>.jpg/.png из ветки или (если она не смогла скачать) загрузка по url."""
    base, title = PICTURES[key]
    info = digest.get(key) or {}
    caption = "\n".join(c for c in (title, info.get("caption", "")) if c)
    for path in (ROOT / f"{base}.jpg", ROOT / f"{base}.png"):
        if path.exists() and 0 < path.stat().st_size <= PHOTO_MAX_BYTES:
            return path, caption
    if not str(info.get("url", "")).startswith("http"):
        return None
    try:
        r = requests.get(info["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; digest-bot/1.0)"})
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"{key}: картинка не скачалась: {e}", file=sys.stderr)
        return None
    ext = PHOTO_TYPES.get(r.headers.get("content-type", "").split(";")[0])
    if not ext or len(r.content) > PHOTO_MAX_BYTES:
        print(f"{key}: картинка не подошла: {r.headers.get('content-type')}, {len(r.content)} байт", file=sys.stderr)
        return None
    path = ROOT / f"{base}{ext}"
    path.write_bytes(r.content)
    return path, caption


# ---------- подборка ----------

def load_digest(cfg: dict) -> dict | None:
    """digest.json от routine, если он собран сегодня."""
    if not DIGEST_FILE.exists():
        return None
    try:
        digest = json.loads(DIGEST_FILE.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"digest.json битый: {e}", file=sys.stderr)
        return None
    if digest.get("date") != today(cfg).isoformat():
        print(f"digest.json не за сегодня (дата «{digest.get('date')}») — шлём приветствие, погоду и курсы")
        return None
    return digest


def main() -> None:
    state = load_state()
    if DRY_RUN:
        state["users"] = state["users"] or {"dry-run": {"name": "Друг"}}
    else:
        ensure_names(state)
    if SYNC_ONLY:
        print(f"Синхронизация: получателей {len(state['users'])}")
        return
    if not state["users"]:
        print("Получателей нет: открой ссылку-приглашение и нажми Start.")
        return

    digest = load_digest(CONFIG)
    if not should_send(state, digest):
        return
    digest = digest or {}
    hourly, rates = get_hourly(CONFIG), get_rates()
    try:
        card = render_card(CONFIG, hourly, rates, digest.get("highlights") or [],
                           str(digest.get("card_tagline") or ""), CARD_FILE)
    except Exception as e:  # карточка — бонус, без неё подборка всё равно уходит
        print(f"Карточку нарисовать не удалось: {e}", file=sys.stderr)
        card = False
    # погода и курсы живут на карточке; текстом — только если карточки нет
    common = [] if card else [html.escape(weather_section(hourly)) if hourly else "",
                              html.escape(rates_section(rates)) if rates else ""]
    common.append(digest.get("shared", ""))

    postcard, meme = get_picture(digest, "postcard"), get_picture(digest, "meme")

    failed_ids = []
    for chat_id, user in state["users"].items():
        name = user.get("name") or "друг"
        personal = (digest.get("personal") or {}).get(chat_id)
        if personal:
            parts = [personal.get("intro"), personal.get("for_you")]
            outro = personal.get("outro")
        else:  # новый получатель или нет digest.json — общий вариант
            parts = [f"Доброе утро, {html.escape(name)}! ☀️"]
            outro = (digest.get("default") or {}).get("outro")
        text = "\n\n".join(p.strip() for p in (*parts, *common, outro) if p and p.strip())
        if postcard:
            try:  # открытка — бонус, как и мем: её сбой не считается сбоем подборки
                send_photo(chat_id, *postcard)
            except Exception as e:
                print(f"Открытка для {chat_id} не отправлена: {e}", file=sys.stderr)
        try:
            if card:
                send_photo(chat_id, CARD_FILE)
            send(chat_id, text, html_mode=True)
        except Exception as e:
            failed_ids.append(chat_id)
            print(f"Подборка для {chat_id} не отправлена: {e}", file=sys.stderr)
            continue
        if meme:
            try:  # мем — бонус: его сбой не считается сбоем подборки
                send_photo(chat_id, *meme)
            except Exception as e:
                print(f"Мем для {chat_id} не отправлен: {e}", file=sys.stderr)
    if not DRY_RUN and len(failed_ids) < len(state["users"]):  # хоть кому-то ушло — сегодня больше не шлём
        state["delivered"] = today(CONFIG).isoformat()
        save_state(state)
    if failed_ids:
        sys.exit(1)


def should_send(state: dict, digest: dict | None) -> bool:
    """Решает по MODE, рассылать ли сейчас; объясняет решение в логе."""
    if MODE == "force" or DRY_RUN:
        return True
    today_s = today(CONFIG).isoformat()
    if state.get("delivered") == today_s:
        # allow_resend — только для пуша routine (тесты перезапуском); 08:00 и 10:00 дублей не шлют
        if not (CONFIG.get("allow_resend") and MODE == "push"):
            print(f"[{MODE}] сегодня уже рассылали — пропускаем")
            return False
        print(f"[{MODE}] сегодня уже рассылали, но allow_resend включён — шлём ещё раз")
    if MODE == "morning" and not digest:
        print("[morning] выпуска за сегодня ещё нет — ждём пуш от routine, крайний срок 10:00")
        return False
    if MODE == "push":
        if not digest:
            print("[push] в ветке нет выпуска за сегодня — пропускаем")
            return False
        if datetime.now(ZoneInfo(CONFIG["timezone"])).hour < SEND_AFTER_HOUR:
            print(f"[push] выпуск готов, но ещё нет {SEND_AFTER_HOUR}:00 — разошлёт утренний запуск")
            return False
    return True


if __name__ == "__main__":
    main()
