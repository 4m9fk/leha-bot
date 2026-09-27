"""Мини-выпуск новостей: озвучка (edge-tts) + новостные кадры (matplotlib) → вертикальный mp4 (ffmpeg).

Сценарий — digest.json → video.segments, его пишет routine (см. routine.md). Сюжет о погоде
добавляется здесь из данных Open-Meteo. ffmpeg берётся из пакета imageio-ffmpeg, чтобы одинаково
работать локально и в Actions. Любая ошибка — повод просто не отправлять ролик (решает вызывающий).
"""

import asyncio
import re
import subprocess
import textwrap
from pathlib import Path

VOICE = "ru-RU-SvetlanaNeural"  # ru-RU-DmitryNeural на 27.09.2026 не отдаёт звук
RATE = "+8%"
W, H, FPS = 1080, 1920, 25
PAUSE = 0.35  # секунд тишины после каждого сюжета
BG_TOP, BG_BOTTOM = "#0b1a3a", "#1d3170"
RED, INK, INK_2 = "#e34948", "#ffffff", "#c9d2ea"
ACCENTS = ["#eda100", "#1baf7a", "#2a78d6", "#e87ba4", "#eb6834", "#9085e9"]
TICKER_H = 96  # высота бегущей строки внизу, px


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(*args: str) -> None:
    r = subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg: {r.stderr[-500:]}")


def media_duration(path: Path) -> float:
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise RuntimeError(f"не удалось узнать длительность {path}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


async def tts(text: str, out: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(out))


def render_slide(seg: dict, idx: int, total: int, header: str, pictures: dict[str, Path], out: Path) -> None:
    """Кадр 1080×1920 в стиле новостной студии; нижние TICKER_H px оставлены под бегущую строку."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import FancyBboxPatch

    plt.rcParams.update({"font.family": "DejaVu Sans"})
    fig = plt.figure(figsize=(W / 150, H / 150), dpi=150)
    bg = fig.add_axes([0, 0, 1, 1], zorder=-10)
    bg.imshow([[0], [1]], cmap=LinearSegmentedColormap.from_list("bg", [BG_TOP, BG_BOTTOM]),
              aspect="auto", extent=(0, 1, 0, 1), interpolation="bicubic")
    bg.axis("off")

    def box(x, y, w, h, color, radius=0.01):
        fig.patches.append(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}",
                                          transform=fig.transFigure, facecolor=color, edgecolor="none",
                                          zorder=-5))

    # шапка: LIVE + название + прогресс сюжетов
    box(0.06, 0.935, 0.13, 0.03, RED, radius=0.006)
    fig.text(0.125, 0.95, "● LIVE", fontsize=15, fontweight="bold", color=INK, ha="center", va="center")
    fig.text(0.22, 0.95, header, fontsize=17, fontweight="bold", color=INK, va="center")
    for k in range(total):
        box(0.06 + k * (0.88 / total), 0.915, 0.88 / total - 0.008, 0.005,
            INK if k <= idx else "#ffffff33", radius=0.002)

    accent = ACCENTS[idx % len(ACCENTS)]
    picture = pictures.get(seg.get("image") or "")
    if picture and picture.exists():  # сюжет с картинкой: картинка сверху, текст ниже
        img = mpimg.imread(str(picture))
        ih, iw = img.shape[:2]
        box_w, box_h = 0.88, 0.40
        scale = min(box_w * W / iw, box_h * H / ih)
        pw, ph = iw * scale / W, ih * scale / H
        ax = fig.add_axes([0.5 - pw / 2, 0.5 + (box_h - ph) / 2, pw, ph])
        ax.imshow(img)
        ax.axis("off")
        text_top, title_size, wrap = 0.465, 30, 22
    else:  # только текст: крупный полупрозрачный номер сюжета сверху, текст ближе к центру
        fig.text(0.04, 0.9, f"{idx + 1:02d}", fontsize=190, fontweight="bold", color=accent, alpha=0.18,
                 va="top")
        text_top, title_size, wrap = 0.57, 40, 17

    rubric = str(seg.get("rubric", "")).upper()
    label = fig.text(0.078, text_top + 0.014, rubric, fontsize=16, fontweight="bold", color="#0b1a3a",
                     va="center")
    bb = label.get_window_extent(fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    box(0.06, text_top - 0.004, bb.width + 0.036, 0.036, accent, radius=0.006)  # плашка по ширине текста

    y = text_top - 0.035
    for line in textwrap.wrap(str(seg.get("title", "")), wrap)[:5]:
        fig.text(0.06, y, line, fontsize=title_size, fontweight="bold", color=INK, va="top")
        y -= title_size / 1000 * 1.45
    y -= 0.015
    for line in textwrap.wrap(str(seg.get("lead", "")), 36)[:5]:
        fig.text(0.06, y, line, fontsize=19, color=INK_2, va="top")
        y -= 0.03

    fig.savefig(out)
    plt.close(fig)


def weather_segment(h: dict, rain_windows) -> dict:
    t = h["temperature_2m"]
    lo, hi = round(min(t[8:22])), round(max(t[8:22]))
    rain = rain_windows(h)
    rain_voice = ("Осадки: " + "; ".join(rain) + ", так что зонт — берём.") if rain else "Осадков не ждём, зонт отдыхает."
    return {"rubric": "Погода", "title": f"От {lo} до {hi} градусов", "lead": "Подробности — на карточке дня",
            "voice": f"Теперь о погоде. Сегодня в Витебске от {lo} до {hi} градусов. {rain_voice}",
            "image": "card"}


def make_video(segments: list[dict], header: str, ticker: str, pictures: dict[str, Path],
               workdir: Path, out: Path) -> Path:
    """Собирает ролик. segments: [{rubric, title, lead, voice, image?}], image ∈ ключи pictures."""
    workdir.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, seg in enumerate(segments):
        audio, slide, clip = workdir / f"a{i}.mp3", workdir / f"s{i}.png", workdir / f"c{i}.mp4"
        asyncio.run(tts(str(seg["voice"]), audio))
        render_slide(seg, i, len(segments), header, pictures, slide)
        dur = media_duration(audio) + PAUSE
        frames = int(dur * FPS)
        # медленный наезд камеры на кадр; звук добиваем тишиной до длины кадра
        run_ffmpeg("-loop", "1", "-framerate", str(FPS), "-i", str(slide), "-i", str(audio),
                   "-filter_complex",
                   f"[0:v]scale={W * 2}:{H * 2},zoompan=z='1+0.05*on/{frames}':d=1"
                   f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},format=yuv420p[v];"
                   f"[1:a]apad,atrim=0:{dur:.2f}[a]",
                   "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k",
                   "-ar", "44100", str(clip))
        clips.append(clip)

    concat_list = workdir / "list.txt"
    concat_list.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    joined = workdir / "joined.mp4"
    run_ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(joined))

    # сплошная бегущая строка поверх всего ролика: текст из файла, чтобы не экранировать кавычки и двоеточия
    from matplotlib import font_manager
    font = font_manager.findfont("DejaVu Sans:bold")
    ticker_file = workdir / "ticker.txt"
    ticker_file.write_text(ticker, encoding="utf-8")
    label_w = 250
    vf = (f"drawbox=x=0:y=ih-{TICKER_H}:w=iw:h={TICKER_H}:color=0x07102a@1:t=fill,"
          f"drawtext=fontfile='{font}':textfile='{ticker_file}':fontcolor=white:fontsize=40"
          f":x={label_w}+(w-{label_w})-mod(t*170\\,w-{label_w}+tw):y=h-{TICKER_H}+({TICKER_H}-text_h)/2,"
          f"drawbox=x=0:y=ih-{TICKER_H}:w={label_w}:h={TICKER_H}:color=0xe34948@1:t=fill,"
          f"drawtext=fontfile='{font}':text='ГЛАВНОЕ':fontcolor=white:fontsize=38"
          f":x=({label_w}-text_w)/2:y=h-{TICKER_H}+({TICKER_H}-text_h)/2")
    run_ffmpeg("-i", str(joined), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
               "-c:a", "copy", "-movflags", "+faststart", str(out))
    return out
