#!/usr/bin/env python3
"""
Clipper - saca clips verticales (9:16) de videos largos.

Corre en tu PC: detecta los momentos con mas energia de audio, te deja
revisarlos y corta los clips con ffmpeg. Gratis y sin marca de agua.

Que hace:
  - Gráfica de volumen con los momentos marcados; los mueves, quitas o añades
  - Formatos: fondo borroso, recorte de lados o pantalla dividida
  - Zoom, vibración, varios textos, subtítulos automáticos y logo
  - Cola de varios videos, descarga por link, cancelar en cualquier momento
  - Usa la tarjeta de video para cortar más rápido (si hay)

Requisitos: Python 3.8+ y ffmpeg (el .exe ya los trae).

Uso:
    python clipper_app.py            # abre la ventana de Clipper
    python clipper_app.py --lan      # modo servidor (iPhone en la misma wifi)
"""

import argparse
import array
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request
import uuid
import wave
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# El .exe se crea sin consola: en ese caso no hay stdout/stderr y print() fallaria
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW


def resource_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent

SAMPLE_RATE = 2000
IDLE_LIMIT = 120          # segundos sin que la ventana responda antes de cerrar el programa
MAX_UPLOAD = 4 * 1024 ** 3
JOB_MAX_AGE_SEC = 24 * 3600
UPDATE_API = "https://api.github.com/repos/Y4ll/Clipper/releases/tags/latest"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"  # instalador oficial de Microsoft
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

TOKEN = secrets.token_urlsafe(16)   # solo la ventana de Clipper lo conoce
WORK = Path(tempfile.gettempdir()) / "clipper_app_jobs"
DATA_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "Clipper"
SETTINGS_FILE = DATA_DIR / "settings.json"
ASSET_DIR = DATA_DIR / "assets"

FFMPEG = "ffmpeg"
LAN_MODE = False
LAST_PING = [0.0]
JOBS = {}                 # id -> datos que ve la pagina
RT = {}                   # id -> {cancel: Event, proc: Popen}
ENCODER = {"name": None, "ready": False}
UPDATE = {"state": "idle", "available": False, "version": "", "url": ""}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

SETTING_KEYS = {"n", "len", "preset", "mode", "zoom", "shake", "texts", "text_pos",
                "text_color", "text_size", "subs", "sub_lang", "logo_pos", "fps", "gpu",
                "review", "autosave", "out_dir", "quality", "smart", "intro_seen"}
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def read_build():
    f = resource_path("build.txt")
    try:
        return f.read_text(encoding="utf-8").strip()[:12] if f.is_file() else "dev"
    except OSError:
        return "dev"


BUILD = read_build()


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ procesos
def rt(job_id):
    return RT.setdefault(job_id, {"cancel": threading.Event(), "procs": set(), "lock": threading.Lock()})


def run_proc(cmd, cwd=None, job_id=None, on_line=None):
    """Ejecuta un programa, se puede cancelar desde fuera. Devuelve el final de su salida."""
    state = rt(job_id) if job_id else None
    if state and state["cancel"].is_set():
        raise Cancelled()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=cwd, creationflags=NO_WINDOW)
    if state:
        with state["lock"]:
            state["procs"].add(p)
    tail = deque(maxlen=25)
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            tail.append(line)
            if on_line:
                on_line(line)
        p.wait()
    finally:
        if state:
            with state["lock"]:
                state["procs"].discard(p)
    if state and state["cancel"].is_set():
        raise Cancelled()
    if p.returncode != 0:
        raise RuntimeError("\n".join(tail)[-700:])
    return "\n".join(tail)


def run_ffmpeg(cmd, cwd=None, job_id=None, length=None, on_progress=None):
    if cmd and cmd[0] == "ffmpeg":
        cmd = [FFMPEG] + list(cmd[1:])

    def on_line(line):
        if on_progress and length and (line.startswith("out_time_us=")
                                       or line.startswith("out_time_ms=")):
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                return
            on_progress(min(1.0, max(0.0, us / 1e6 / length)))

    return run_proc(cmd, cwd, job_id, on_line)


def fmt_time(sec):
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------------------------------------------ analisis audio
def probe_duration(video):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=NO_WINDOW, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode(errors="ignore"))
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def energy_per_second(wav_path):
    out = []
    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        while True:   # un segundo cada vez: no carga horas de audio en memoria
            frames = w.readframes(rate)
            if len(frames) < rate * 2:
                break
            block = array.array("h")
            block.frombytes(frames)
            if sys.byteorder == "big":
                block.byteswap()
            out.append(math.sqrt(sum(x * x for x in block) / rate))
    return out


def pick_moments(energy, duration, length, count):
    """Elige tramos con mucha energia Y mucha variacion (la musica constante pierde)."""
    total = int(max(len(energy), duration))
    if total <= 0:
        return []
    length = max(3, int(length))
    if total <= length:
        return [{"start": 0, "len": max(1, total), "score": 1.0}]
    count = max(1, int(count))
    if not energy or max(energy) == 0:  # sin audio: repartir a partes iguales
        step = (total - length) / max(1, count - 1) if count > 1 else 0
        starts = sorted({int(i * step) for i in range(count)})
        return [{"start": s, "len": length, "score": 0.0} for s in starts]
    energy = list(energy) + [0.0] * (total - len(energy))
    ref = sorted(energy)[int(0.95 * (len(energy) - 1))] or max(energy) or 1.0
    e = [min(x / ref, 1.5) for x in energy]
    p1, p2 = [0.0], [0.0]
    for x in e:
        p1.append(p1[-1] + x)
        p2.append(p2[-1] + x * x)
    # picos: lo mas alto de cada tramo (un grito o explosion cuenta aunque dure poco)
    peaks = [0.0] * (total - length + 1)
    dq = deque()
    for i, x in enumerate(e):
        while dq and e[dq[-1]] <= x:
            dq.pop()
        dq.append(i)
        start = i - length + 1
        if start >= 0:
            while dq[0] < start:
                dq.popleft()
            peaks[start] = e[dq[0]]
    cands = []
    for s in range(0, total - length + 1):
        mean = (p1[s + length] - p1[s]) / length
        var = max(0.0, (p2[s + length] - p2[s]) / length - mean * mean)
        cands.append((mean + math.sqrt(var) + 0.3 * peaks[s], s))
    cands.sort(reverse=True)
    chosen = []
    for score, s in cands:
        if all(abs(s - c["start"]) >= length for c in chosen):
            chosen.append({"start": s, "len": length, "score": round(score, 3)})
        if len(chosen) == count:
            break
    return sorted(chosen, key=lambda m: m["start"])


def waveform(energy, bins=600):
    if not energy:
        return [], 1
    ref = sorted(energy)[int(0.99 * (len(energy) - 1))] or max(energy) or 1.0
    size = max(1, math.ceil(len(energy) / bins))
    out = [int(min(1.0, max(energy[i:i + size]) / ref) * 100)
           for i in range(0, len(energy), size)]
    return out, size


# ------------------------------------------------------------------ fuentes
def find_font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def ff_escape(path):
    # Escapa una ruta para usarla dentro de un filtro de ffmpeg (Windows incluido)
    return str(path).replace("\\", "/").replace(":", "\\:")


# ------------------------------------------------------ tarjeta de video (GPU)
def detect_encoder():
    """Prueba h264_nvenc, h264_qsv y h264_amf; el primero que funcione se usa."""
    for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=320x320:d=0.5", "-c:v", enc, "-f", "null", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=NO_WINDOW, timeout=30)
            if r.returncode == 0:
                ENCODER["name"] = enc
                break
        except Exception:  # noqa: BLE001
            continue
    ENCODER["ready"] = True


def video_args(enc, quality="fast"):
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4" if quality == "best" else "p2",
                "-b:v", "8M", "-maxrate", "12M"]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", "8M"]
    if enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-b:v", "8M"]
    if quality == "best":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "0"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "0"]


# ---------------------------------------------------------------- subtitulos
def find_whisper():
    d = APP_DIR / "whisper"
    for name in ("whisper-cli.exe", "whisper-cli"):
        exe = d / name
        if exe.is_file():
            models = sorted(d.glob("ggml-*.bin"))
            if models:
                return exe, models[0]
    return None


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for k, ln in enumerate(lines):
            m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", ln.strip())
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
            b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
            body = " ".join(lines[k + 1:]).strip()
            body = re.sub(r"[\[\(][^\]\)]*[\]\)]", "", body).strip()   # [MUSICA], (aplausos)
            if body and b > a:
                cues.append((a, b, body))
            break
    return cues


def transcribe(job_id, video, start, length, lang, jdir, idx):
    wh = find_whisper()
    if not wh:
        return None
    exe, model = wh
    wav = jdir / f"w{idx}.wav"
    run_ffmpeg([FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(start),
                "-t", str(length), "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(wav)], job_id=job_id)
    prefix = jdir / f"w{idx}"
    threads = max(2, (os.cpu_count() or 4) // 2)
    run_proc([str(exe), "-m", str(model), "-f", str(wav), "-l", lang, "-osrt", "-of", str(prefix),
              "-ml", "22", "-sow", "-np", "-t", str(threads)], cwd=str(exe.parent), job_id=job_id)
    srt = Path(str(prefix) + ".srt")
    if not srt.is_file():
        return []
    return parse_srt(srt.read_text(encoding="utf-8", errors="ignore"))


HYPE_WORDS = (
    "no puede ser", "dios", "wow", "increible", "increíble", "que fue eso", "qué fue eso",
    "jaja", "joder", "mierda", "vamos", "no way", "oh my god", "omg", "let's go", "lets go",
    "insane", "what the", "holy", "cuidado", "gane", "gané", "le di", "lo mate", "lo maté",
    "gta", "trailer", "tráiler", "locura", "no lo creo", "mira", "mirad", "clip")


def speech_score(cues, length):
    """0..1: cuanta conversacion con emocion hay en el tramo (habla seguida + palabras de hype)."""
    if not cues:
        return 0.0, ""
    text = " ".join(c[2] for c in cues)
    low = text.lower()
    words = len(low.split())
    hits = sum(1 for w in HYPE_WORDS if w in low)
    dens = min(1.0, words / max(1.0, length) / 2.5)
    return min(1.0, 0.6 * dens + min(0.4, 0.15 * hits)), text[:90]


def smart_rank(job_id, video, cands, want, length, lang, jdir, job):
    """Re-ordena los tramos candidatos escuchando lo que se dice en cada uno."""
    wh = find_whisper()
    if not wh:
        return cands[:want], False
    top = max((m["score"] for m in cands), default=1.0) or 1.0
    t0, limit = time.time(), 240          # maximo 4 min de analisis extra
    scored = []
    for i, m in enumerate(cands, 1):
        job.update(msg=f"Escuchando los momentos... {i} de {len(cands)}",
                   pct=int(60 + 38 * i / len(cands)))
        sc, snippet = 0.0, ""
        if time.time() - t0 < limit:
            try:
                cues = transcribe(job_id, video, m["start"], m["len"], lang, jdir, 100 + i)
                sc, snippet = speech_score(cues or [], m["len"])
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                sc, snippet = 0.0, ""
        final = 0.6 * m["score"] / top + sc
        scored.append(dict(m, text=snippet, final=final))
    scored.sort(key=lambda m: m["final"], reverse=True)
    return scored[:want], True


# ------------------------------------------------------------ filtros ffmpeg
def drawtext(font, textfile, size, color, y, a, b):
    ff = f"fontfile='{ff_escape(font)}':" if font else ""
    return (f"drawtext=textfile={textfile}:expansion=none:{ff}fontsize={size}:fontcolor={color}:"
            f"borderw=7:bordercolor=black:line_spacing=12:x=(w-text_w)/2:y={y}:"
            f"enable='between(t,{a:.2f},{b:.2f})'")


def build_graph(o, length, filler_idx, logo_idx, n_texts, font, pre):
    mode = o["mode"]
    if mode == "split" and filler_idx is None:
        mode = "blur"
    if mode == "crop":
        g = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[v0]"
    elif mode == "split":
        g = ("[0:v]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[top];"
             f"[{filler_idx}:v]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[bot];"
             "[top][bot]vstack[v0]")
    else:
        # el fondo se desenfoca en pequeno y se agranda: se ve igual y cuesta mucho menos
        g = ("[0:v]split[a][b];"
             "[a]scale=144:256:force_original_aspect_ratio=increase,crop=144:256,boxblur=3:2,"
             "scale=1080:1920:flags=bilinear[bg];"
             "[b]scale=1080:-2[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
    state = {"last": "v0", "n": 0, "g": g}

    def stage(expr):
        state["n"] += 1
        new = f"v{state['n']}"
        state["g"] += f";[{state['last']}]{expr}[{new}]"
        state["last"] = new

    if o["zoom"] or o["shake"]:   # zoom y vibracion en un solo paso
        base = 1166 if o["shake"] else 1080
        w = f"{base}*(1+0.14*min(t/{length},1))" if o["zoom"] else str(base)
        if o["zoom"]:
            sc = f"scale=w='{w}':h=-2:eval=frame"
        else:
            sc = f"scale={base}:-2"
        if o["shake"]:
            cr = "crop=1080:1920:x='(iw-1080)/2+sin(t*37)*16':y='(ih-1920)/2+cos(t*29)*16'"
        else:
            cr = "crop=1080:1920"
        stage(sc + "," + cr)
    if o["fps"] in (30, 60):
        stage(f"fps={o['fps']}")
    if logo_idx is not None:
        pos = {"tl": "40:140", "tr": "W-w-40:140", "bl": "40:H-h-330",
               "br": "W-w-40:H-h-330"}[o["logo_pos"]]
        state["n"] += 1
        new = f"v{state['n']}"
        state["g"] += (f";[{logo_idx}:v]scale=220:-1,format=rgba,colorchannelmixer=aa=0.85[lg]"
                       f";[{state['last']}][lg]overlay={pos}[{new}]")
        state["last"] = new
    size = {"s": 64, "m": 84, "l": 108}[o["text_size"]]
    color = "0xF5C400" if o["text_color"] == "yellow" else "white"
    ypos = {"top": "h*0.10", "center": "(h-text_h)/2", "bottom": "h*0.74"}[o["text_pos"]]
    for k, t in enumerate(o["texts"][:n_texts]):
        stage(drawtext(font, f"{pre}t{k}.txt", size, color, ypos, t["start"], t["end"]))
    return state["g"], state["last"]


def build_command(video, out, start, length, o, enc, filler, logo, texts_n, cues, font, pre):
    inputs = ["-ss", str(start), "-i", str(video)]
    idx, filler_idx, logo_idx = 1, None, None
    if o["mode"] == "split" and filler:
        inputs += ["-stream_loop", "-1", "-i", str(filler)]
        filler_idx, idx = idx, idx + 1
    if logo:
        inputs += ["-i", str(logo)]
        logo_idx, idx = idx, idx + 1
    graph, last = build_graph(o, length, filler_idx, logo_idx, texts_n, font, pre)
    # subtitulos: un drawtext por frase, al final de la cadena
    for j, (a, b, _t) in enumerate(cues):
        new = f"vs{j}"
        graph += f";[{last}]{drawtext(font, f'{pre}s{j}.txt', 78, 'white', 'h*0.60', a, b)}[{new}]"
        last = new
    cmd = [FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1",
           "-nostats"] + inputs + ["-t", str(length), "-filter_complex", graph,
                                   "-map", f"[{last}]", "-map", "0:a?"] + \
          video_args(enc, o["quality"]) + \
          ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           str(out)]
    return cmd


# ------------------------------------------------------------------- ajustes
def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    clean = {k: v for k, v in (data or {}).items() if k in SETTING_KEYS}
    raw = json.dumps(clean, ensure_ascii=False)
    if len(raw) > 20000:
        raise ValueError("Ajustes demasiado grandes")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def asset_path(role):
    found = sorted(ASSET_DIR.glob(role + ".*")) if ASSET_DIR.is_dir() else []
    return found[0] if found else None


def clean_opts(raw, seg_len=120):
    raw = raw if isinstance(raw, dict) else {}

    def pick(key, allowed, default):
        return raw.get(key) if raw.get(key) in allowed else default

    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return default

    texts = []
    for t in (raw.get("texts") or [])[:4]:
        if isinstance(t, dict) and str(t.get("text", "")).strip():
            a = num(t.get("start"), 0, 600, 0)
            b = num(t.get("end"), 0, 600, 3)
            texts.append({"text": str(t["text"]).strip()[:80], "start": a, "end": max(b, a + 0.5)})
    return {
        "mode": pick("mode", ("blur", "crop", "split"), "blur"),
        "zoom": bool(raw.get("zoom")), "shake": bool(raw.get("shake")),
        "texts": texts,
        "text_pos": pick("text_pos", ("top", "center", "bottom"), "top"),
        "text_color": pick("text_color", ("yellow", "white"), "yellow"),
        "text_size": pick("text_size", ("s", "m", "l"), "m"),
        "subs": bool(raw.get("subs")),
        "sub_lang": pick("sub_lang", ("es", "en", "auto"), "es"),
        "logo_pos": pick("logo_pos", ("tl", "tr", "bl", "br"), "tr"),
        "fps": int(pick("fps", (0, 30, 60), 0)),
        "quality": pick("quality", ("fast", "best"), "fast"),
        "gpu": bool(raw.get("gpu")), "autosave": bool(raw.get("autosave")),
    }


def safe_stem(name):
    stem = re.sub(r"[^\w\-]+", "_", Path(str(name)).stem, flags=re.UNICODE).strip("_")
    return stem[:40] or "video"


def unique_path(folder, base, ext):
    p = Path(folder) / f"{base}{ext}"
    n = 2
    while p.exists():
        p = Path(folder) / f"{base}_{n}{ext}"
        n += 1
    return p


# --------------------------------------------------------------- trabajos
def new_job(name):
    job_id = uuid.uuid4().hex[:10]
    (WORK / job_id).mkdir(parents=True, exist_ok=True)
    JOBS[job_id] = {"id": job_id, "name": name, "state": "queued", "msg": "En cola...",
                    "pct": 0, "clips": [], "saved": [], "warnings": [], "moments": [],
                    "wave": [], "wave_step": 1, "duration": 0}
    rt(job_id)
    return job_id


def download_video(job_id, url, jdir, rng=None):
    if not importlib.util.find_spec("yt_dlp"):
        raise RuntimeError("Esta version no incluye la descarga por link.")
    import yt_dlp
    job, state = JOBS[job_id], rt(job_id)

    def hook(d):
        if state["cancel"].is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                frac = d.get("downloaded_bytes", 0) / total
                job["pct"] = int(2 + frac * 8)
                job["msg"] = f"Descargando el video... {int(frac * 100)}%"

    opts = {"outtmpl": str(jdir / "input.%(ext)s"), "noplaylist": True, "quiet": True,
            "no_warnings": True, "progress_hooks": [hook], "merge_output_format": "mp4",
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b"}
    if os.path.isabs(FFMPEG):
        opts["ffmpeg_location"] = str(Path(FFMPEG).parent)
    offset = 0
    job.update(msg="Mirando el link...", pct=3)
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as y0:
        meta = y0.extract_info(url, download=False) or {}
    if state["cancel"].is_set():
        raise Cancelled()
    if meta.get("is_live"):   # directo: no se descarga "todo", se graba un rato
        return record_live(job_id, meta, jdir, rng)
    dur = meta.get("duration") or 0
    start, end, last = rng or (0, None, 0)
    if not (start or end or last) and dur > 3600:
        last = 20   # VOD largo sin tramo elegido: solo el final, para no bajar horas
        job["note"] = "Es un video largo: use solo los ultimos 20 minutos."
    if True:
        if last:   # "solo los ultimos N minutos"
            start, end = (max(0, dur - last * 60), None) if dur else (0, None)
        if start or end:
            offset = start or 0
            opts["download_ranges"] = yt_dlp.utils.download_range_func(
                None, [(offset, end if end else float("inf"))])
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [f for f in jdir.glob("input.*") if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("No se pudo descargar el video.")
    job["name"] = (str((info or {}).get("title") or "video"))[:80] + ".mp4"
    job["offset"] = offset
    return max(files, key=lambda f: f.stat().st_size)


def record_live(job_id, meta, jdir, rng):
    """Directo: graba lo que haya disponible (hasta N minutos) y se puede parar cuando quieras."""
    job, state = JOBS[job_id], rt(job_id)
    cap = int((rng[2] if rng and rng[2] else 0) or 10)     # minutos como maximo
    stream = meta.get("url")
    headers = dict(meta.get("http_headers") or {})
    if not stream:
        fm = [f for f in (meta.get("formats") or []) if f.get("url") and f.get("vcodec") != "none"]
        if fm:
            stream = fm[-1]["url"]
            headers = dict(fm[-1].get("http_headers") or headers)
    if not stream:
        raise RuntimeError("No pude abrir el directo.")
    out = jdir / "input.mp4"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    if ".m3u8" in stream:
        cmd += ["-live_start_index", "-100000"]   # empieza por lo mas viejo que el directo guarde
    cmd += ["-i", stream, "-t", str(cap * 60), "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov", str(out)]
    job.update(live=True, msg="Grabando el directo...", pct=5)
    job["name"] = (str(meta.get("title") or "directo"))[:80] + ".mp4"

    def on_line(line):
        if line.startswith("out_time_us="):
            try:
                sec = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                return
            job["msg"] = f"Grabando el directo... {fmt_time(sec)} (maximo {cap} min)"
            job["pct"] = int(5 + min(1.0, sec / (cap * 60)) * 50)

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    with state["lock"]:
        state["procs"].add(p)
        state["stdin"] = p.stdin
    tail = deque(maxlen=10)
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", errors="ignore").strip()
            tail.append(line)
            on_line(line)
        p.wait()
    finally:
        with state["lock"]:
            state["procs"].discard(p)
            state["stdin"] = None
        job["live"] = False
    if state["cancel"].is_set():
        raise Cancelled()
    if not out.is_file() or out.stat().st_size < 10000:
        raise RuntimeError("No se pudo grabar el directo. " + " ".join(tail)[-200:])
    job["offset"] = 0
    return out


def analyze_job(job_id, video=None, url=None, want=5, length=30, rng=None, smart=None):
    job, jdir = JOBS[job_id], WORK / job_id
    try:
        if url:
            job.update(state="analyzing", msg="Descargando el video...", pct=2)
            video = download_video(job_id, url, jdir, rng)
        job.update(state="analyzing", msg="Extrayendo el audio...", pct=12)
        duration = probe_duration(video)
        energy = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wav = Path(tmp) / "a.wav"
                run_ffmpeg([FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i",
                            str(video), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav",
                            str(wav)], job_id=job_id)
                job.update(msg="Buscando los mejores momentos...", pct=60)
                energy = energy_per_second(wav)
        except RuntimeError:
            energy = []   # video sin audio: se reparten los clips
        if not duration:
            duration = float(len(energy))
        if duration <= 0:
            raise RuntimeError("No se pudo leer el video.")
        wave_data, step = waveform(energy)
        note = "Momentos listos"
        moments = None
        if smart and energy and max(energy) >= 20 and find_whisper():
            cands = pick_moments(energy, duration, length, min(15, max(want * 3, want + 4)))
            moments, used = smart_rank(job_id, video, cands, want, length, smart["lang"],
                                       jdir, job)
            moments = sorted(moments, key=lambda m: m["start"])
            note = "Momentos elegidos por volumen y por lo que se dice"
        if moments is None:
            moments = pick_moments(energy, duration, length, want)
        if not energy or max(energy) < 20:
            note = "No se detecto sonido util en este video: repartí los clips parejos, ajustalos a mano."
        if job.get("note"):
            note += ". " + job["note"]
        job.update(state="ready", msg=note, pct=100, duration=duration,
                   wave=wave_data, wave_step=step,
                   moments=moments)
    except Cancelled:
        job.update(state="cancelled", msg="Cancelado")
    except Exception as e:  # noqa: BLE001
        if rt(job_id)["cancel"].is_set():
            job.update(state="cancelled", msg="Cancelado")
        else:
            job.update(state="error", msg=str(e)[-300:] or "No se pudo analizar el video.")


def render_job(job_id, video, segments, o):
    job, jdir = JOBS[job_id], WORK / job_id
    state = rt(job_id)
    try:
        job.update(state="rendering", msg="Preparando...", pct=0, clips=[], saved=[], warnings=[])
        font = find_font()
        if (o["texts"] or o["subs"]) and not font:
            job["warnings"].append("No se encontro una fuente para los textos; salieron sin texto.")
        filler = asset_path("filler") if o["mode"] == "split" else None
        if o["mode"] == "split" and not filler:
            job["warnings"].append("Falta el video de relleno; se uso el fondo borroso.")
        logo = asset_path("logo")
        enc_box = [ENCODER["name"] if (o["gpu"] and ENCODER["name"]) else "libx264"]
        out_dir = None
        if o["autosave"]:
            try:
                out_dir = Path(str(load_settings().get("out_dir") or "")).expanduser()
                if str(out_dir) in ("", "."):
                    out_dir = None
                else:
                    out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                out_dir = None
                job["warnings"].append("No se pudo usar la carpeta de guardado.")
        total = len(segments)
        fracs = [0.0] * total
        results = [None] * total
        lock = threading.Lock()
        offset = job.get("offset", 0)

        def report():
            done = sum(1 for r in results if r)
            job["pct"] = int(sum(fracs) * 100 / total)
            job["msg"] = f"Clips terminados: {done} de {total}"

        def warn(text):
            with lock:
                if text not in job["warnings"]:
                    job["warnings"].append(text)

        def make(i, seg):
            if state["cancel"].is_set():
                raise Cancelled()
            start, length = seg["start"], seg["len"]
            pre = f"c{i}_"

            def progress(frac):
                fracs[i - 1] = frac * (0.9 if o["subs"] else 1.0) + (0.1 if o["subs"] else 0)
                report()

            cues = []
            if o["subs"] and font:
                try:
                    got = transcribe(job_id, video, start, length, o["sub_lang"], jdir, i)
                    if got is None:
                        warn("Esta version no incluye subtitulos.")
                    else:
                        cues = got[:60]
                except Cancelled:
                    raise
                except Exception:  # noqa: BLE001
                    warn(f"No se pudieron generar los subtitulos del clip {i}.")
            wrap_w = {"s": 20, "m": 16, "l": 12}[o["text_size"]]
            texts = o["texts"] if font else []
            for k, t in enumerate(texts):
                (jdir / f"{pre}t{k}.txt").write_text(
                    "\n".join(textwrap.wrap(t["text"].upper(), wrap_w)[:4]), encoding="utf-8")
            for j, (a, b, body) in enumerate(cues):
                (jdir / f"{pre}s{j}.txt").write_text(
                    "\n".join(textwrap.wrap(body.upper(), 18)[:2]), encoding="utf-8")
            out = jdir / f"clip{i}.mp4"
            enc = enc_box[0]
            cmd = build_command(video, out, start, length, o, enc, filler, logo, len(texts),
                                cues, font, pre)
            try:
                run_ffmpeg(cmd, cwd=str(jdir), job_id=job_id, length=length, on_progress=progress)
            except RuntimeError:
                if enc == "libx264" or state["cancel"].is_set():
                    raise
                warn("La tarjeta de video fallo; se uso el procesador.")
                enc_box[0] = "libx264"
                cmd = build_command(video, out, start, length, o, "libx264", filler, logo,
                                    len(texts), cues, font, pre)
                run_ffmpeg(cmd, cwd=str(jdir), job_id=job_id, length=length, on_progress=progress)
            saved = None
            if out_dir:
                try:
                    dest = unique_path(out_dir, f"{safe_stem(job['name'])}_clip{i}", ".mp4")
                    shutil.copyfile(out, dest)
                    saved = str(dest)
                except OSError:
                    warn(f"No se pudo guardar el clip {i} en la carpeta.")
            with lock:
                fracs[i - 1] = 1.0
                results[i - 1] = {"name": out.name, "start": fmt_time(start + offset),
                                  "len": length, "saved": saved}
                report()

        cpus = os.cpu_count() or 2
        workers = 1 if (cpus <= 2 or total == 1) else (2 if cpus < 8 else 3)
        if o["subs"]:
            workers = min(workers, 2)
        errors = []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(make, i, seg) for i, seg in enumerate(segments, 1)]
            for f in futs:
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    if not isinstance(e, Cancelled) and not state["cancel"].is_set():
                        state["cancel"].set()   # un fallo real: parar los demas clips
                        with state["lock"]:
                            for pr in list(state["procs"]):
                                try:
                                    pr.kill()
                                except OSError:
                                    pass
        if state["cancel"].is_set() and not any(not isinstance(e, Cancelled) for e in errors):
            raise Cancelled()
        real = [e for e in errors if not isinstance(e, Cancelled)]
        if real:
            raise real[0]
        job["clips"] = [{"name": r["name"], "start": r["start"], "len": r["len"]} for r in results if r]
        job["saved"] = [r["saved"] for r in results if r and r["saved"]]
        job.update(state="done", msg="Listo", pct=100)
    except Cancelled:
        job.update(state="cancelled", msg="Cancelado")
    except Exception as e:  # noqa: BLE001
        if isinstance(e, (KeyError, TypeError, ValueError)):
            msg = "Alguna opcion no es valida. Revisa los textos y los tiempos."
        else:
            msg = str(e)[-300:] or "No se pudieron crear los clips."
        job.update(state="error", msg=msg)
    finally:
        try:
            Path(video).unlink()
        except OSError:
            pass


def queue_local_file(path):
    """Un video arrastrado sobre Clipper.exe (o pasado por la linea de comandos)."""
    src = Path(path)
    if not src.is_file() or src.suffix.lower() not in VIDEO_EXTS:
        return None
    job_id = new_job(src.name)
    dest = WORK / job_id / ("input" + src.suffix.lower())
    shutil.copyfile(src, dest)
    st = load_settings()
    want = int(clean_num(st.get("n"), 1, 40, 5))
    seg = int(clean_num(st.get("len"), 3, 120, 30))
    lang = st.get("sub_lang") if st.get("sub_lang") in ("es", "en", "auto") else "es"
    smart = {"lang": lang} if st.get("smart") else None
    threading.Thread(target=analyze_job, args=(job_id, dest, None, want, seg, None, smart),
                     daemon=True).start()
    return job_id


def cleanup_all():
    """Al cerrar: borra los videos y clips temporales de esta sesion."""
    if not WORK.exists():
        return
    for d in WORK.iterdir():
        if d.is_dir() and d.name != "perfil":
            shutil.rmtree(d, ignore_errors=True)


def cleanup_old_jobs():
    now = time.time()
    if not WORK.exists():
        return
    for d in WORK.iterdir():
        if not d.is_dir() or d.name == "perfil":
            continue
        try:
            if now - d.stat().st_mtime > JOB_MAX_AGE_SEC:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ------------------------------------------------------------- actualizacion
def check_update():
    if BUILD == "dev":
        UPDATE["state"] = "done"
        return
    try:
        req = urllib.request.Request(UPDATE_API, headers={"User-Agent": "Clipper"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        sha = str(data.get("target_commitish", ""))[:7]
        asset = next((a for a in data.get("assets", []) if a.get("name") == "ClipperSetup.exe"), None)
        if asset and re.fullmatch(r"[0-9a-f]{7}", sha) and sha != BUILD[:7]:
            UPDATE.update(available=True, version=sha, url=asset["browser_download_url"])
    except Exception:  # noqa: BLE001
        pass
    UPDATE["state"] = "done"


def install_update():
    """Baja el instalador nuevo, lo abre y cierra Clipper para poder reemplazarlo."""
    url = UPDATE.get("url", "")
    if not IS_WIN or not url.startswith("https://github.com/Y4ll/Clipper/"):
        return False
    dest = Path(tempfile.gettempdir()) / "ClipperSetup.exe"
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if dest.stat().st_size < 1000000 or dest.read_bytes()[:2] != b"MZ":
        return False
    subprocess.Popen([str(dest)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return True


# ---------------------------------------------------------------------- web
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --- utilidades
    def _json(self, obj, code=200):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, RuntimeError):
            data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, code=204):
        self.send_response(code)
        self.end_headers()

    def _host_ok(self):
        if LAN_MODE:
            return True
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost")

    def _auth(self, query):
        if not self._host_ok():
            return False
        tok = self.headers.get("X-Token") or query.get("t", [""])[0]
        return secrets.compare_digest(str(tok), TOKEN)

    def _body_json(self, limit=1000000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _serve_file(self, f, ctype):
        size = f.stat().st_size
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _job_or_404(self, job_id):
        job = JOBS.get(job_id)
        if not job:
            self._json({"error": "Trabajo no encontrado"}, 404)
        return job

    # --- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            if not self._host_ok():
                self.send_error(403)
                return
            try:
                html = resource_path("ui.html").read_text(encoding="utf-8")
            except OSError:
                self.send_error(500, "Falta ui.html")
                return
            data = html.replace("__TOKEN__", TOKEN).replace("__BUILD__", BUILD).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/ping":
            LAST_PING[0] = time.time()
            self._empty()
            return
        if u.path == "/favicon.ico":
            self._empty()
            return
        if not self._auth(q):
            self.send_error(403)
            return
        if u.path == "/status":
            job = self._job_or_404(q.get("id", [""])[0])
            if job:
                self._json(dict(job))
        elif u.path == "/caps":
            self._json({"build": BUILD, "whisper": find_whisper() is not None,
                        "ytdlp": importlib.util.find_spec("yt_dlp") is not None,
                        "encoder": ENCODER["name"], "encoder_ready": ENCODER["ready"],
                        "font": find_font() is not None, "lan": LAN_MODE})
        elif u.path == "/settings":
            self._json(load_settings())
        elif u.path == "/assets":
            self._json({r: (asset_path(r).name if asset_path(r) else None)
                        for r in ("logo", "filler")})
        elif u.path == "/update":
            self._json(dict(UPDATE))
        elif u.path.startswith("/source/"):
            jid = unquote(u.path).split("/")[2] if len(u.path.split("/")) > 2 else ""
            if not jid.isalnum():
                self.send_error(404)
                return
            files = [f for f in (WORK / jid).glob("input.*") if f.suffix not in (".part", ".ytdl")]
            if not files:
                self.send_error(404)
                return
            self._serve_file(files[0], "video/mp4")
        elif u.path.startswith("/clips/"):
            parts = unquote(u.path).split("/")
            if len(parts) != 4 or not parts[2].isalnum() or os.path.basename(parts[3]) != parts[3]:
                self.send_error(404)
                return
            f = WORK / parts[2] / parts[3]
            if not f.is_file() or f.suffix != ".mp4":
                self.send_error(404)
                return
            self._serve_file(f, "video/mp4")
        else:
            self.send_error(404)

    # --- POST
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            self.send_error(403)
            return
        if u.path == "/upload":
            self._upload(q)
        elif u.path == "/fetch":
            body = self._body_json()
            url = str((body or {}).get("url", "")).strip()
            if not re.match(r"^https?://\S+$", url) or len(url) > 2000:
                self._json({"error": "Pega un link valido que empiece con http o https."}, 400)
                return
            if not importlib.util.find_spec("yt_dlp"):
                self._json({"error": "Esta version no incluye la descarga por link."}, 400)
                return
            job_id = new_job("video")
            want = int(clean_num(body.get("n"), 1, 40, 5))
            length = int(clean_num(body.get("len"), 3, 120, 30))
            a = clean_num(body.get("from_min"), 0, 2000, 0) * 60
            b = clean_num(body.get("to_min"), 0, 2000, 0) * 60
            last = clean_num(body.get("last_min"), 0, 2000, 0)
            rng = (a, b if b > a else None, last)
            smart = {"lang": body.get("lang") if body.get("lang") in ("es", "en", "auto") else "es"} \
                if body.get("smart") else None
            threading.Thread(target=analyze_job, args=(job_id, None, url, want, length, rng, smart),
                             daemon=True).start()
            self._json({"id": job_id})
        elif u.path == "/render":
            self._render()
        elif u.path == "/cancel":
            jid = q.get("id", [""])[0]
            if jid in RT:
                RT[jid]["cancel"].set()
                with RT[jid]["lock"]:
                    procs = list(RT[jid]["procs"])
                for proc in procs:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                if jid in JOBS and JOBS[jid]["state"] in ("ready", "queued"):
                    JOBS[jid].update(state="cancelled", msg="Cancelado")
            self._json({"ok": True})
        elif u.path == "/stoplive":
            jid = q.get("id", [""])[0]
            st = RT.get(jid)
            if st:
                with st["lock"]:
                    pipe = st.get("stdin")
                try:
                    if pipe:
                        pipe.write(b"q\n")
                        pipe.flush()
                except OSError:
                    pass
            self._json({"ok": True})
        elif u.path == "/settings":
            body = self._body_json(30000)
            try:
                save_settings(body)
                self._json({"ok": True})
            except (OSError, ValueError):
                self._json({"error": "No se pudieron guardar los ajustes."}, 400)
        elif u.path == "/asset/delete":
            role = q.get("role", [""])[0]
            if role in ("logo", "filler"):
                for f in (ASSET_DIR.glob(role + ".*") if ASSET_DIR.is_dir() else []):
                    f.unlink(missing_ok=True)
            self._json({"ok": True})
        elif u.path == "/openfolder":
            folder = str(load_settings().get("out_dir") or "")
            if folder and Path(folder).is_dir():
                try:
                    if IS_WIN:
                        os.startfile(folder)  # noqa: S606
                    else:
                        subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                except OSError:
                    pass
                self._json({"ok": True})
            else:
                self._json({"error": "Elige primero una carpeta de guardado."}, 400)
        elif u.path == "/update/install":
            try:
                ok = UPDATE["available"] and install_update()
            except Exception:  # noqa: BLE001
                ok = False
            self._json({"ok": bool(ok)})
        else:
            self.send_error(404)

    def _upload(self, q):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._json({"error": "No llego ningun video"}, 400)
            return
        if length > MAX_UPLOAD:
            self._json({"error": "El archivo es demasiado grande (maximo 4 GB)."}, 413)
            return
        role = q.get("role", ["video"])[0]
        name = q.get("name", ["video.mp4"])[0]
        ext = Path(name).suffix.lower()
        if role in ("logo", "filler"):
            if ext not in (LOGO_EXTS if role == "logo" else VIDEO_EXTS):
                self._json({"error": "Formato de archivo no valido."}, 400)
                return
            ASSET_DIR.mkdir(parents=True, exist_ok=True)
            for old in ASSET_DIR.glob(role + ".*"):
                old.unlink(missing_ok=True)
            dest = ASSET_DIR / (role + ext)
        else:
            if ext not in VIDEO_EXTS:
                ext = ".mp4"
            job_id = new_job(name)
            dest = WORK / job_id / f"input{ext}"
        remaining = length
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if role in ("logo", "filler"):
            self._json({"ok": True, "name": dest.name})
            return
        want = int(clean_num(q.get("n", ["5"])[0], 1, 40, 5))
        seg = int(clean_num(q.get("len", ["30"])[0], 3, 120, 30))
        lang = q.get("lang", ["es"])[0]
        smart = {"lang": lang if lang in ("es", "en", "auto") else "es"} \
            if q.get("smart", ["0"])[0] == "1" else None
        threading.Thread(target=analyze_job, args=(job_id, dest, None, want, seg, None, smart),
                         daemon=True).start()
        self._json({"id": job_id})

    def _render(self):
        body = self._body_json()
        if body is None:
            self._json({"error": "Datos invalidos"}, 400)
            return
        job = JOBS.get(str(body.get("id", "")))
        if not job or job["state"] != "ready":
            self._json({"error": "Este video ya no esta listo para cortar."}, 400)
            return
        files = [f for f in (WORK / job["id"]).glob("input.*") if f.suffix not in (".part", ".ytdl")]
        if not files:
            self._json({"error": "No encuentro el video original."}, 400)
            return
        segs = []
        for s in (body.get("segments") or [])[:40]:
            try:
                start = max(0.0, float(s.get("start")))
                length = max(3.0, min(120.0, float(s.get("len"))))
            except (TypeError, ValueError, AttributeError):
                continue
            if job["duration"] and start >= job["duration"]:
                continue
            segs.append({"start": round(start, 1), "len": round(length, 1)})
        if not segs:
            self._json({"error": "Agrega al menos un momento."}, 400)
            return
        o = clean_opts(body.get("opts"))
        job.update(state="rendering", msg="Preparando...", pct=0)
        threading.Thread(target=render_job, args=(job["id"], files[0], segs, o),
                         daemon=True).start()
        self._json({"ok": True})


def clean_num(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------- ventana de app
def msg_box(text):
    """Aviso visible: el .exe no tiene consola donde mostrar errores."""
    try:
        if IS_WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "Clipper", 0x10)
            return
    except Exception:  # noqa: BLE001
        pass
    print(text)


def ask_box(text):
    if not IS_WIN:
        return False
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, text, "Clipper", 0x24) == 6  # Si = 6
    except Exception:  # noqa: BLE001
        return False


def webview2_ready():
    """True si el motor WebView2 de Windows esta instalado."""
    if not IS_WIN:
        return True
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID),
            (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID),
            (winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID),
        ]
        for hive, path in keys:
            try:
                with winreg.OpenKey(hive, path) as k:
                    version = winreg.QueryValueEx(k, "pv")[0]
                    if version and version != "0.0.0.0":
                        return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


def install_webview2():
    """Descarga el instalador oficial de WebView2 y lo instala en silencio."""
    dest = Path(tempfile.gettempdir()) / "MicrosoftEdgeWebview2Setup.exe"
    with urllib.request.urlopen(WEBVIEW2_URL, timeout=30) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if dest.stat().st_size < 500000 or dest.read_bytes()[:2] != b"MZ":
        return False
    subprocess.run([str(dest), "/silent", "/install"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=NO_WINDOW, timeout=600)
    return webview2_ready()


class Api:
    """Funciones que la pagina puede pedirle al programa (ventana nativa)."""

    def __init__(self):
        self._window = None  # con guion bajo: pywebview no lo expone a la pagina

    def save_clip(self, job_id, name):
        import webview
        job_id, name = str(job_id), str(name)
        if not job_id.isalnum() or os.path.basename(name) != name or not name.endswith(".mp4"):
            return {"ok": False}
        src = WORK / job_id / name
        if not src.is_file():
            return {"ok": False}
        save_type = getattr(getattr(webview, "FileDialog", None), "SAVE", None)
        if save_type is None:
            save_type = webview.SAVE_DIALOG
        dest = self._window.create_file_dialog(
            save_type, save_filename=name, file_types=("Video MP4 (*.mp4)",))
        if not dest:
            return {"ok": False, "cancel": True}
        path = dest if isinstance(dest, str) else dest[0]
        shutil.copyfile(src, path)
        return {"ok": True, "path": str(path)}

    def pick_folder(self):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:
            kind = webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]


def run_native_window(url):
    """Abre Clipper en su propia ventana (motor WebView2), sin navegador.
    Devuelve True si se uso y se cerro; False si no se pudo (usa el respaldo)."""
    try:
        import webview
    except Exception:  # noqa: BLE001
        return False
    try:
        if IS_WIN and not webview2_ready():
            if not ask_box("Clipper necesita el componente WebView2 de Microsoft para abrir su "
                           "ventana (descarga de unos 2 MB). Quieres instalarlo ahora?"):
                return False
            if not install_webview2():
                msg_box("No se pudo instalar WebView2. Clipper se abrira en una ventana del "
                        "navegador. Puedes instalarlo a mano desde "
                        "developer.microsoft.com/microsoft-edge/webview2/consumer")
                return False
        api = Api()
        win = webview.create_window("Clipper | Shadow Mode", url, js_api=api, width=980,
                                    height=1000, min_size=(420, 600), background_color="#08090c")
        api._window = win
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:  # noqa: BLE001
            pass
        icon = resource_path("icon.ico")
        webview.start(icon=str(icon) if icon.is_file() else None)
        return True
    except Exception:  # noqa: BLE001
        return False


def find_app_browser():
    """Busca Edge, Chrome o Brave para abrir Clipper como ventana de app."""
    cands = []
    if IS_WIN:
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rels = ["Microsoft\\Edge\\Application\\msedge.exe",
                "Google\\Chrome\\Application\\chrome.exe",
                "BraveSoftware\\Brave-Browser\\Application\\brave.exe"]
        for rel in rels:
            for root in roots:
                if root:
                    cands.append(os.path.join(root, rel))
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome", "brave-browser"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def open_app_window(url):
    """Ventana propia, sin pestanas ni barra de direcciones (como una app)."""
    exe = find_app_browser()
    if exe:
        try:
            subprocess.Popen(
                [exe, "--app=" + url, "--user-data-dir=" + str(WORK / "perfil"),
                 "--window-size=980,1000", "--no-first-run", "--no-default-browser-check"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def watchdog(srv):
    """Cierra el programa cuando cierras la ventana (y no hay un video procesandose)."""
    started = time.time()
    while True:
        time.sleep(3)
        if any(j["state"] in ("queued", "analyzing", "rendering") for j in list(JOBS.values())):
            continue
        if LAST_PING[0]:
            if time.time() - LAST_PING[0] > IDLE_LIMIT:
                break
        elif time.time() - started > 90:   # la ventana nunca llego a abrirse
            break
    srv.shutdown()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "tu-ip-local"


def main():
    global FFMPEG, LAN_MODE
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--lan", action="store_true",
                   help="Modo servidor: sin ventana, accesible desde otro dispositivo de tu wifi")
    p.add_argument("files", nargs="*", help="Videos a abrir (puedes arrastrarlos sobre Clipper.exe)")
    args = p.parse_args()
    LAN_MODE = args.lan
    os.environ["PATH"] = str(APP_DIR) + os.pathsep + os.environ.get("PATH", "")
    local_ff = APP_DIR / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if local_ff.is_file():
        FFMPEG = str(local_ff)
    elif not shutil.which("ffmpeg"):
        msg_box("No encuentro ffmpeg. Pon ffmpeg.exe en la misma carpeta que Clipper.exe.")
        return
    WORK.mkdir(parents=True, exist_ok=True)
    cleanup_old_jobs()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        srv = ThreadingHTTPServer((host, args.port), Handler)
    except OSError:  # el puerto esta ocupado: usa uno libre
        srv = ThreadingHTTPServer((host, 0), Handler)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"Clipper en {url}")
    opened = [queue_local_file(f) for f in args.files[:5]]
    opened = [j for j in opened if j]
    if opened:
        url += "?open=" + ",".join(opened)
    threading.Thread(target=detect_encoder, daemon=True).start()
    threading.Thread(target=check_update, daemon=True).start()
    if args.lan:
        print(f"Desde el iPhone (misma wifi): http://{lan_ip()}:{port}")
        print("Aviso: cualquiera en tu wifi puede entrar mientras esto este abierto.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nCerrado.")
        return
    server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    server_thread.start()
    if run_native_window(url):  # ventana propia; vuelve cuando la cierras
        srv.shutdown()
        cleanup_all()
        return
    # Respaldo: ventana de navegador en modo app
    threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
    open_app_window(url)
    server_thread.join()
    cleanup_all()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        msg_box("Clipper tuvo un error: " + str(e))
