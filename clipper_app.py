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
import base64
import hashlib
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# El .exe se crea sin consola: en ese caso no hay stdout/stderr y print() fallaria
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
BELOW_NORMAL = 0x00004000 if IS_WIN else 0   # prioridad baja: cortes y renders ceden ante la grabacion
ABOVE_NORMAL = 0x00008000 if IS_WIN else 0   # prioridad alta: la grabacion del directo no debe perder datos


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
KEY_FILE = DATA_DIR / "gemini.key"      # la clave va aparte de los ajustes y nunca se manda a la pagina
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS = ("gemini-2.5-flash", "gemini-flash-latest")

FFMPEG = "ffmpeg"
LAN_MODE = False
LAST_PING = [0.0]
JOBS = {}                 # id -> datos que ve la pagina
RT = {}                   # id -> {cancel: Event, proc: Popen}
ENERGY = {}               # id -> energia por segundo (para zoom en los picos)
ENCODER = {"name": None, "ready": False}
HAS_ASS = [False]
UPDATE = {"state": "idle", "available": False, "version": "", "url": "",
          "dl": "idle", "pct": 0, "msg": ""}
UPDATE_PREFIX = "https://github.com/Y4ll/Clipper/"

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
                "review", "autosave", "out_dir", "quality", "smart", "intro_seen", "snap", "cam_corner", "cam_size", "ai_title", "punch", "sub_style", "zoom_auto", "bar", "enhance", "cut_pauses", "music", "music_vol", "sub_translate", "parts", "parts_n", "ai_tone", "res", "yt_auto", "yt_privacy", "clip_every_min"}
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp")
MUSIC_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac")


# ------------------------------------------------------- monitor de directos
STREAMERS_FILE = DATA_DIR / "streamers.json"
MONITOR_EVERY = 120        # segundos entre revisiones
MONITOR_SEGMENT_MIN = 60   # cada grabacion dura como maximo esto; si el directo sigue, arranca otra
MONITOR_MAX_HOURS = 6      # tope por directo para no llenar el disco
MONITOR = {}               # url -> {live, job_id, last_check, recorded, error}
MONITOR_LOCK = threading.Lock()
MONITOR_WAKE = threading.Event()


def load_streamers():
    try:
        data = json.loads(STREAMERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for s in data if isinstance(data, list) else []:
        if isinstance(s, dict) and re.match(r"^https?://\S+$", str(s.get("url", ""))):
            out.append({"url": str(s["url"])[:300], "name": str(s.get("name") or "")[:60],
                        "enabled": bool(s.get("enabled", True))})
    return out


def save_streamers(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STREAMERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:50], ensure_ascii=False), encoding="utf-8")
    tmp.replace(STREAMERS_FILE)


def normalize_streamer_url(text):
    """Acepta un link o solo el usuario (se asume Kick)."""
    t = str(text or "").strip()
    if re.match(r"^https?://\S+$", t) and len(t) <= 300:
        return t
    if re.match(r"^[A-Za-z0-9_\-]{2,40}$", t):
        return "https://kick.com/" + t
    return ""


def streamer_is_live(url):
    """True si el canal esta en directo ahora. Cualquier error cuenta como 'no esta'."""
    if not importlib.util.find_spec("yt_dlp"):
        return False
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                               "socket_timeout": 20}) as y:
            meta = y.extract_info(url, download=False) or {}
        return bool(meta.get("is_live"))
    except Exception:  # noqa: BLE001
        return False


def monitor_job_active(job_id):
    """True mientras ese trabajo siga grabando (el analisis posterior ya no cuenta)."""
    job = JOBS.get(job_id or "")
    return bool(job) and job.get("state") in ("queued", "analyzing", "rendering") \
        and not job.get("_rec_done")


def monitor_start_segment(url, name, st):
    job_id = new_job(name or url)
    JOBS[job_id]["monitor"] = True
    if st.get("segments"):
        JOBS[job_id]["_live_edge"] = True     # los tramos siguientes empiezan desde ahora, sin repetir
    with MONITOR_LOCK:
        st["job_id"] = job_id
        st["segments"] = st.get("segments", 0) + 1
    smart = {"lang": "es", "query": "", "ai_rank": False, "live_clips": True}
    threading.Thread(target=analyze_job,
                     args=(job_id, None, url, 5, 30, (0, None, MONITOR_SEGMENT_MIN), smart, True),
                     daemon=True).start()


def monitor_check_once():
    for s in load_streamers():
        if not s["enabled"]:
            continue
        url = s["url"]
        live = streamer_is_live(url)
        start = False
        with MONITOR_LOCK:
            st = MONITOR.setdefault(url, {"live": False, "job_id": None})
            st.update(live=live, last_check=time.time())
            if not live:                    # directo terminado: el proximo vuelve a grabarse desde cero
                for k in ("session_start", "segments", "fails", "stopped", "note"):
                    st.pop(k, None)
                if not monitor_job_active(st["job_id"]):
                    st["job_id"] = None     # no arrastrar el trabajo del directo anterior
                continue
            if st.get("stopped") or monitor_job_active(st["job_id"]):
                continue
            prev = JOBS.get(st["job_id"] or "")
            if prev and prev.get("state") == "cancelled":
                st.update(stopped=True, note="Parado por ti: no se vuelve a grabar este directo.")
                continue
            if prev and prev.get("state") == "error":
                st["fails"] = st.get("fails", 0) + 1
                if st["fails"] >= 3:
                    st.update(stopped=True, note="La grabacion fallo 3 veces seguidas: se dejo de intentar.")
                    continue
            now = time.time()
            st.setdefault("session_start", now)
            if now - st["session_start"] >= MONITOR_MAX_HOURS * 3600:
                st.update(stopped=True, note=f"Tope de {MONITOR_MAX_HOURS} horas alcanzado en este directo.")
                continue
            start = True
        if start:
            monitor_start_segment(url, s["name"], st)


def monitor_loop():
    while True:
        try:
            monitor_check_once()
        except Exception:  # noqa: BLE001
            pass
        MONITOR_WAKE.wait(MONITOR_EVERY)
        MONITOR_WAKE.clear()


def streamers_view():
    with MONITOR_LOCK:
        mon = {k: dict(v) for k, v in MONITOR.items()}
    out = []
    for s in load_streamers():
        st = mon.get(s["url"], {})
        out.append(dict(s, live=bool(st.get("live")) and s["enabled"],
                        job_id=st.get("job_id") if monitor_job_active(st.get("job_id")) else None,
                        last_check=st.get("last_check"), note=st.get("note") or "",
                        segments=st.get("segments", 0)))
    return out


# ------------------------------------------------- subida a YouTube (API oficial)
YT_FILE = DATA_DIR / "youtube.json"      # client_id, client_secret y refresh_token: nunca van a la pagina
YT_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YT_MAX_PER_DAY = 10        # tope propio: evita spam y que te marquen la cuenta
YT_AUTH = {"status": "idle", "msg": ""}
YT_TOKEN = {"value": "", "exp": 0.0}
YT_LOCK = threading.Lock()
YT_SENT = []               # momentos de las subidas de las ultimas 24 h


def yt_load():
    try:
        data = json.loads(YT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def yt_save(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    YT_FILE.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(YT_FILE, 0o600)
    except OSError:
        pass


def yt_connected():
    return bool(yt_load().get("refresh_token"))


def yt_post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            msg = body.get("error_description") or str(body.get("error"))
        except Exception:  # noqa: BLE001
            msg = f"HTTP {e.code}"
        raise RuntimeError(msg)


def yt_access_token():
    cfg = yt_load()
    if YT_TOKEN["value"] and time.time() < YT_TOKEN["exp"] - 60:
        return YT_TOKEN["value"]
    if not cfg.get("refresh_token"):
        raise RuntimeError("YouTube no esta conectado.")
    r = yt_post_form(YT_TOKEN_URL, {"client_id": cfg.get("client_id", ""),
                                    "client_secret": cfg.get("client_secret", ""),
                                    "refresh_token": cfg["refresh_token"], "grant_type": "refresh_token"})
    YT_TOKEN.update(value=r["access_token"], exp=time.time() + float(r.get("expires_in", 3000)))
    return YT_TOKEN["value"]


def yt_connect_start():
    """Abre el permiso de Google en el navegador y espera la respuesta en 127.0.0.1 (flujo oficial de apps de escritorio)."""
    cfg = yt_load()
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        raise ValueError("Primero guarda el ID de cliente y el secreto de Google.")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(12)
    result = {}

    class CB(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] == state and (q.get("code") or q.get("error")):
                result.update(code=q.get("code", [""])[0], error=q.get("error", [""])[0])
                text = "Listo. Ya puedes cerrar esta pestaña y volver a Clipper."
            else:
                text = "Respuesta no valida."
            data = f"<meta charset='utf-8'><body style='font-family:sans-serif;background:#111;color:#fc0;padding:40px'>{text}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), CB)
    srv.timeout = 5
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    url = YT_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": YT_SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    YT_AUTH.update(status="waiting", msg="Esperando tu permiso en el navegador...")

    def wait():
        end = time.time() + 300
        try:
            while not result and time.time() < end:
                srv.handle_request()
            if not result.get("code"):
                YT_AUTH.update(status="error", msg="No se dio el permiso (o se acabo el tiempo).")
                return
            r = yt_post_form(YT_TOKEN_URL, {"code": result["code"], "client_id": cfg["client_id"],
                                            "client_secret": cfg["client_secret"], "redirect_uri": redirect,
                                            "grant_type": "authorization_code", "code_verifier": verifier})
            if not r.get("refresh_token"):
                raise RuntimeError("Google no devolvio permiso permanente. Vuelve a conectar.")
            cfg2 = yt_load()
            cfg2["refresh_token"] = r["refresh_token"]
            yt_save(cfg2)
            YT_TOKEN.update(value=r.get("access_token", ""), exp=time.time() + float(r.get("expires_in", 3000)))
            YT_AUTH.update(status="ok", msg="Conectado")
        except Exception as e:  # noqa: BLE001
            YT_AUTH.update(status="error", msg=str(e)[:200])
        finally:
            srv.server_close()

    threading.Thread(target=wait, daemon=True).start()
    return url


def yt_upload_file(path, title, desc, tags, privacy):
    tok = yt_access_token()
    meta = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": tags[:10],
                        "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    size = Path(path).stat().st_size
    init = urllib.request.Request(
        YT_UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    try:
        with urllib.request.urlopen(init, timeout=60) as r:
            loc = r.headers["Location"]
        with open(path, "rb") as f:
            put = urllib.request.Request(loc, data=f, method="PUT",
                                         headers={"Content-Length": str(size), "Content-Type": "video/mp4"})
            with urllib.request.urlopen(put, timeout=900) as r:
                return json.load(r)["id"]
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]["errors"][0].get("reason") or f"HTTP {e.code}"
        except Exception:  # noqa: BLE001
            msg = f"HTTP {e.code}"
        raise RuntimeError(msg)


def yt_upload_clips(job_id, clips, privacy):
    """Sube los clips terminados de un lote, de uno en uno, con tope diario propio."""
    job, jdir = JOBS[job_id], WORK / job_id
    privacy = privacy if privacy in ("private", "unlisted", "public") else "private"
    for n, c in enumerate(clips, 1):
        with YT_LOCK:
            now = time.time()
            YT_SENT[:] = [t for t in YT_SENT if now - t < 86400]
            if len(YT_SENT) >= YT_MAX_PER_DAY:
                job["warnings"].append(f"YouTube: tope de {YT_MAX_PER_DAY} subidas por dia; este clip no se subio.")
                continue
            f = jdir / c["name"]
            title = (str(c.get("title") or "").strip() or f"{str(job.get('name') or 'Clip')[:70]} ({n})") + " #Shorts"
            tags = c.get("tags") or []
            tags = [t.strip("# ") for t in (tags.split() if isinstance(tags, str) else tags)
                    if isinstance(t, str) and t.strip("# ")]
            desc = (str(c.get("desc") or "").strip() + "\n\n#Shorts " +
                    " ".join("#" + t for t in tags[:3])).strip()
            try:
                vid = yt_upload_file(f, title, desc, tags, privacy)
                YT_SENT.append(time.time())
                job["warnings"].append(f"Subido a YouTube ({privacy}): https://youtu.be/{vid}")
            except Exception as e:  # noqa: BLE001
                job["warnings"].append("YouTube: no se pudo subir un clip (" + str(e)[:120] + ")")


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


def live_recording_active():
    """True mientras algun directo se esta grabando (lo demas debe ceder CPU para no causar huecos)."""
    try:
        return any(j.get("live") for j in list(JOBS.values()))
    except RuntimeError:
        return False


def run_proc(cmd, cwd=None, job_id=None, on_line=None):
    """Ejecuta un programa, se puede cancelar desde fuera. Devuelve el final de su salida."""
    state = rt(job_id) if job_id else None
    if state and state["cancel"].is_set():
        raise Cancelled()
    flags = NO_WINDOW | (BELOW_NORMAL if live_recording_active() else 0)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=cwd, creationflags=flags)
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


def score_moments(moments, energy, smart_used):
    """Nota 0-100 de 'vale la pena publicar': volumen y variacion, habla, subida al inicio y duracion."""
    if not energy:
        return moments
    ref = sorted(energy)[int(0.95 * (len(energy) - 1))] or max(energy) or 1.0
    out = []
    for m in moments:
        st, ln = int(m["start"]), int(m["len"])
        audio = min(1.0, m.get("score", 0.0) / 2.2)
        first = energy[st:st + 3] or [0]
        pre = energy[max(0, st - 5):st] or None
        if pre:
            ratio = (sum(first) / len(first)) / ((sum(pre) / len(pre)) + 0.05 * ref)
            rise = max(0.0, min(1.0, (ratio - 1.0)))
        else:
            rise = 0.5
        if 15 <= ln <= 45:
            dur = 1.0
        else:
            dur = max(0.0, 1.0 - min(abs(ln - 15), abs(ln - 45)) / 30.0)
        if smart_used:
            val = 0.5 * audio + 0.3 * m.get("speech", 0.0) + 0.1 * rise + 0.1 * dur
        else:
            val = 0.65 * audio + 0.2 * rise + 0.15 * dur
        why = "Volumen %d%% · Subida al inicio %d%% · Duración %d%%" % (audio * 100, rise * 100, dur * 100)
        if smart_used:
            why = "Habla %d%% · " % (m.get("speech", 0.0) * 100) + why
        out.append(dict(m, points=int(round(max(1, min(100, val * 100)))), why=why))
    return out


def keep_intervals(words, length):
    """Tramos a conservar quitando pausas largas entre palabras. Solo si el clip es de gente hablando."""
    if len(words) < 6 or len(words) / max(1.0, length) < 1.0:
        return None
    pad = 0.3
    segs = []
    st, en = max(0.0, words[0][0] - pad), words[0][1]
    for a, b, _w in words[1:]:
        if a - en > 0.9:
            segs.append((st, en + pad))
            st, en = a - pad, b
        else:
            en = max(en, b)
    segs.append((st, min(float(length), en + 0.5)))
    total = sum(e - x for x, e in segs)
    if length - total < 1.0 or total < 5:
        return None
    return segs


def remap_time(t, keep):
    off = 0.0
    for x, e in keep:
        if t < x:
            return off
        if t <= e:
            return off + (t - x)
        off += e - x
    return off


def keep_expr(keep):
    return "+".join(f"between(t,{x:.3f},{e:.3f})" for x, e in keep)


def clip_peaks(energy, start, length, n=3):
    """Segundos mas ruidosos del clip (separados) para dar un golpe de zoom ahi."""
    if not energy:
        return []
    cand = sorted(((energy[t], t) for t in range(int(start) + 1, min(int(start + length) - 1, len(energy)))),
                  reverse=True)
    out = []
    for val, t in cand:
        if val <= 0:
            break
        if all(abs(t - x) >= 4 for x in out):
            out.append(t)
        if len(out) == n:
            break
    return sorted(round(t - start + 0.4, 2) for t in out)


def has_audio_stream(video):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=NO_WINDOW, timeout=60)
        return b"Audio:" in r.stderr
    except Exception:  # noqa: BLE001
        return True


def build_audio(o, keep, has_audio, music_idx):
    """Cadena de audio: quitar pausas, mejorar voz, normalizar y musica de fondo que baja cuando se habla."""
    parts, cur = [], None
    if has_audio:
        chain = []
        if keep:
            chain += [f"aselect='{keep_expr(keep)}'", "asetpts=N/SR/TB"]
        if o["enhance"]:
            chain += ["highpass=f=80", "afftdn=nf=-25", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        if chain:
            parts.append("[0:a]" + ",".join(chain) + "[ao]")
            cur = "ao"
        else:
            cur = "0:a"
    if music_idx is not None:
        vol = max(0.0, min(1.0, o["music_vol"] / 100.0))
        if cur:
            parts.append(f"[{cur}]asplit=2[vo][vk]")
            parts.append(f"[{music_idx}:a]volume={vol}[mv]")
            parts.append("[mv][vk]sidechaincompress=threshold=0.05:ratio=10:attack=20:release=400[md]")
            parts.append("[vo][md]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            parts.append(f"[{music_idx}:a]volume={vol}[aout]")
        return ";".join(parts), "[aout]"
    if parts:
        return ";".join(parts), "[ao]"
    return None, None


def get_gemini_key():
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_gemini_key(key):
    key = (key or "").strip()
    if not key:
        KEY_FILE.unlink(missing_ok=True)
        return True
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{20,200}", key):
        return False
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return True


def gemini_title(text, lang, tone="directo"):
    """Pide a Gemini un titulo gancho y hashtags. Solo se envia el texto del clip, nunca el video."""
    key = get_gemini_key()
    if not key or len(text.strip()) < 15:
        return None
    idioma = {"en": "ingles", "es": "espanol"}.get(lang, "el mismo idioma del texto")
    tono = {"directo": "directo y llamativo", "gracioso": "gracioso y con humor",
            "dramatico": "dramatico y con tension", "misterioso": "misterioso, que deje con ganas de ver mas",
            "epico": "epico y exagerado"}.get(tone, "directo y llamativo")
    prompt = ("Eres editor de clips virales para TikTok/Reels/Shorts. Del texto de este clip "
              f"escribe un TITULO GANCHO de maximo 6 palabras en {idioma}, tono {tono}, en mayusculas, "
              "sin comillas ni emojis; una descripcion corta para publicar (maximo 140 caracteres) y "
              "3 hashtags relevantes. Responde solo JSON: "
              '{"title": "...", "desc": "...", "tags": ["#a", "#b", "#c"]}\n\nTexto del clip:\n' + text[:3000])
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json",
                                            "temperature": 0.7}}).encode("utf-8")
    last_err = None
    for model in GEMINI_MODELS:
        req = urllib.request.Request(
            f"{GEMINI_BASE}/models/{model}:generateContent", data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            out = json.loads(raw)
            title = re.sub(r"[\r\n]+", " ", str(out.get("title", ""))).strip().upper()[:60]
            tags = [str(t).strip() for t in (out.get("tags") or []) if str(t).strip().startswith("#")][:4]
            desc = re.sub(r"\s+", " ", str(out.get("desc", ""))).strip()[:160]
            return {"title": title, "tags": tags, "desc": desc} if title else None
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 400):
                continue          # modelo no disponible: prueba el siguiente
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    raise RuntimeError(f"Gemini no respondio ({getattr(last_err, 'code', 'error')})")


def gemini_rank(cands, want, length, job):
    """Pide a Gemini que elija y ordene los mejores momentos.

    Solo se manda metadata de cada tramo (segundo de inicio, duracion, nota de audio,
    puntaje de volumen). Nunca se sube video ni audio a Google.
    Devuelve (momentos_ordenados, True) o (cands[:want], False) si falla.
    """
    key = get_gemini_key()
    if not key or not cands:
        return cands[:want], False

    items = []
    for i, m in enumerate(cands):
        items.append({"id": i, "inicio_s": round(m["start"], 1),
                      "duracion_s": length,
                      "nota_audio": round(m.get("score", 0), 2),
                      "pico_max": round(m.get("peak", 0), 1)})

    prompt = (
        "Eres editor de clips virales para TikTok/Reels/Shorts. "
        "Te doy una lista de momentos de un video con su segundo de inicio, duracion, "
        "nota de audio (0-1) y pico maximo de volumen. "
        f"Elige los {want} mejores momentos para hacer clips virales e impactantes. "
        "Prioriza picos de accion y momentos con mucha energia. "
        "Responde SOLO con JSON: {\"ids\": [lista de ids en orden de mejor a peor]}. "
        "Nada mas.\n\nMomentos:\n" + json.dumps(items, ensure_ascii=False))

    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json",
                                            "temperature": 0.3}}).encode("utf-8")
    for model in GEMINI_MODELS:
        req = urllib.request.Request(
            f"{GEMINI_BASE}/models/{model}:generateContent", data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            ids = json.loads(raw).get("ids", [])
            seen, ranked = set(), []
            for i in ids:
                if isinstance(i, int) and 0 <= i < len(cands) and i not in seen:
                    ranked.append(cands[i])
                    seen.add(i)
            # Si Gemini no lleno los cupos, rellena con los mejores por nota
            rest = [c for j, c in enumerate(cands) if j not in seen]
            rest.sort(key=lambda m: m.get("score", 0), reverse=True)
            ranked = (ranked + rest)[:want]
            return ranked, True
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                continue
            break
        except Exception:  # noqa: BLE001
            break
    return cands[:want], False


def snap_ends(moments, energy, duration, window=4):
    """Mueve el final de cada clip a la pausa mas cercana (+-4 s) para que no acabe a media frase."""
    if not energy:
        return moments
    out = []
    ordered = sorted(moments, key=lambda m: m["start"])
    for k, m in enumerate(ordered):
        start, length = m["start"], m["len"]
        limit = ordered[k + 1]["start"] if k + 1 < len(ordered) else int(duration)
        end = start + length
        lo, hi = max(start + 3, end - window), min(end + window, limit, len(energy))
        best = None
        for t in range(int(lo), int(hi)):
            if t < len(energy) and (best is None or energy[t] < energy[best]):
                best = t
        if best is not None:
            seg = energy[start:end] or [0]
            if energy[best] < 0.6 * (sum(seg) / len(seg)):
                m = dict(m, len=max(3, best + 1 - start))
        out.append(m)
    return out


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
def detect_ass():
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-filters"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, timeout=30)
        HAS_ASS[0] = b" ass " in r.stdout
    except Exception:  # noqa: BLE001
        HAS_ASS[0] = False


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
    detect_ass()
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


def transcribe(job_id, video, start, length, lang, jdir, idx, translate=False):
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
              "-ml", "1", "-sow", "-np", "-t", str(threads)] + (["-tr"] if translate else []), cwd=str(exe.parent), job_id=job_id)
    srt = Path(str(prefix) + ".srt")
    if not srt.is_file():
        return []
    return parse_srt(srt.read_text(encoding="utf-8", errors="ignore"))


def group_words(cues, per=3, gap=0.6):
    """Junta palabras sueltas en grupos cortos para mostrarlos juntos."""
    groups, cur = [], []
    for a, b, w in cues:
        if cur and (len(cur) >= per or a - cur[-1][1] > gap or re.search(r"[.!?]$", cur[-1][2])):
            groups.append(cur)
            cur = []
        cur.append((a, b, w))
    if cur:
        groups.append(cur)
    return groups


FONT_FAMILY = {"impact.ttf": ("Impact", 0), "arialbd.ttf": ("Arial", 1), "segoeuib.ttf": ("Segoe UI", 1),
               "arial.ttf": ("Arial", 1), "dejavusans-bold.ttf": ("DejaVu Sans", 1),
               "liberationsans-bold.ttf": ("Liberation Sans", 1)}


def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(path, cues, font, style="karaoke"):
    """Subtitulos animados. Estilos: karaoke (grupo y palabra en amarillo), one (una palabra grande),
    box (caja oscura) y clean (blancos, sin resaltar)."""
    fam, bold = FONT_FAMILY.get(Path(font).name.lower(), ("Arial", 1)) if font else ("Arial", 1)
    size, outline, bstyle, back, per = 84, 7, 1, "&H00000000", 3
    if style == "one":
        size, per = 130, 1
    elif style == "box":
        size, outline, bstyle, back = 76, 14, 3, "&H99000000"
    elif style == "clean":
        size, per = 78, 4
    elif style in ("green", "pop", "red"):
        size, per = 88, 3
    hot_col = {"green": "&H0050E04A&", "red": "&H004040FF&"}.get(style, "&H0000C4F5&")   # BGR
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Default,{fam},{size},&H00FFFFFF,&H00FFFFFF,{back if bstyle == 3 else '&H00000000'},{back},"
            f"{-1 if bold else 0},0,0,0,100,100,0,0,{bstyle},{outline},0,2,60,60,640,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")

    def clean(w):
        return re.sub(r"[{}\\]", "", w).strip().upper()

    lines = []
    for grp in group_words(cues, per=per):
        for i, (a, b, _w) in enumerate(grp):
            end = grp[i + 1][0] if i + 1 < len(grp) and grp[i + 1][0] - b < 0.3 else b
            end = max(end, a + 0.12)
            if style == "one":
                pop = "{\\c&H0000C4F5&\\fscx70\\fscy70\\t(0,110,\\fscx100\\fscy100)}"
                text = pop + clean(grp[i][2])
            else:
                parts = []
                for j, (_a, _b, w) in enumerate(grp):
                    hot = j == i and style != "clean"
                    color = hot_col if hot else "&H00FFFFFF&"   # BGR: amarillo F5C400
                    grow = "{\\fscx118\\fscy118}" if (hot and style in ("pop", "green", "red")) else "{\\fscx100\\fscy100}"
                    parts.append("{\\c" + color + "}" + grow + clean(w))
                text = " ".join(parts)
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(end)},Default,,0,0,0,," + text)
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


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


def norm_text(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(t).lower()) if not unicodedata.combining(c))


def smart_rank(job_id, video, cands, want, length, lang, jdir, job, query=""):
    """Re-ordena los tramos candidatos escuchando lo que se dice en cada uno."""
    wh = find_whisper()
    if not wh:
        return cands[:want], False
    top = max((m["score"] for m in cands), default=1.0) or 1.0
    t0, limit = time.time(), 240          # maximo 4 min de analisis extra
    scored = []
    terms = [t for t in norm_text(query).split() if len(t) >= 3][:6]
    for i, m in enumerate(cands, 1):
        job.update(msg=f"Escuchando los momentos... {i} de {len(cands)}",
                   pct=int(60 + 38 * i / len(cands)))
        sc, snippet, match = 0.0, "", False
        if time.time() - t0 < limit:
            try:
                cues = transcribe(job_id, video, m["start"], m["len"], lang, jdir, 100 + i)
                sc, snippet = speech_score(cues or [], m["len"])
                if terms:
                    full = norm_text(" ".join(c[2] for c in (cues or [])))
                    match = any(t in full for t in terms)
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                sc, snippet = 0.0, ""
        final = 0.6 * m["score"] / top + sc + (2.0 if match else 0.0)
        scored.append(dict(m, text=snippet, speech=sc, final=final, match=match))
    scored.sort(key=lambda m: m["final"], reverse=True)
    return scored[:want], True


# ------------------------------------------------------------ filtros ffmpeg
def drawtext(font, textfile, size, color, y, a, b):
    ff = f"fontfile='{ff_escape(font)}':" if font else ""
    return (f"drawtext=textfile={textfile}:expansion=none:{ff}fontsize={size}:fontcolor={color}:"
            f"borderw=7:bordercolor=black:line_spacing=12:x=(w-text_w)/2:y={y}:"
            f"enable='between(t,{a:.2f},{b:.2f})'")


def build_graph(o, length, filler_idx, logo_idx, n_texts, font, pre, keep=None, peaks=None):
    mode = o["mode"]
    if mode == "split" and filler_idx is None:
        mode = "blur"
    if mode == "cam":   # juego arriba, tu camara (recortada del mismo video) abajo
        cs = o["cam_size"] / 100.0
        cx = "0" if o["cam_corner"] in ("tl", "bl") else f"iw-iw*{cs}"
        cy = "0" if o["cam_corner"] in ("tl", "tr") else f"ih-iw*{cs}*0.7111"
        g = ("[0:v]split[gm][cm];"
             "[gm]scale=1080:1152:force_original_aspect_ratio=increase,crop=1080:1152[top];"
             f"[cm]crop=w=iw*{cs}:h=iw*{cs}*0.7111:x={cx}:y={cy},scale=1080:768[bot];"
             "[top][bot]vstack[v0]")
    elif mode == "crop":
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
    if keep:   # quitar pausas: solo pasan los tramos elegidos
        g = f"[0:v]select='{keep_expr(keep)}',setpts=N/FRAME_RATE/TB[sv];" + g.replace("[0:v]", "[sv]")
    state = {"last": "v0", "n": 0, "g": g}

    def stage(expr):
        state["n"] += 1
        new = f"v{state['n']}"
        state["g"] += f";[{state['last']}]{expr}[{new}]"
        state["last"] = new

    peaks = peaks if (o["zoom_auto"] and peaks) else []
    if o["zoom"] or o["shake"] or o["punch"] or peaks:   # zoom, vibracion y golpes en un solo paso
        base = 1166 if o["shake"] else 1080
        f = []
        if o["zoom"]:
            f.append(f"(1+0.14*min(t/{length},1))")
        if o["punch"]:   # el video arranca acercado y se asienta en medio segundo
            f.append("(1+0.2*max(0,1-t/0.5))")
        if peaks:   # golpe de zoom en los momentos mas fuertes
            bumps = "+".join(f"max(0,1-abs(t-{p})/0.35)" for p in peaks[:3])
            f.append(f"(1+0.09*({bumps}))")
        if f:
            sc = f"scale=w='{base}*" + "*".join(f) + "':h=-2:eval=frame"
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
    if o["bar"]:   # barra de progreso del clip
        state["n"] += 1
        new = f"v{state['n']}"
        state["g"] += (f";color=c=0xF5C400:s=1080x14:r=30[bar];[{state['last']}][bar]"
                       f"overlay=x='-1080+1080*t/{length:.2f}':y=1730:shortest=1[{new}]")
        state["last"] = new
    if o.get("part_label"):   # etiqueta fija arriba a la izquierda: PARTE 1/3
        ff = f"fontfile='{ff_escape(font)}':" if font else ""
        stage(f"drawtext=textfile={pre}part.txt:expansion=none:{ff}fontsize=64:fontcolor=0xF5C400:"
              "borderw=6:bordercolor=black:x=60:y=150")
    size = {"s": 64, "m": 84, "l": 108}[o["text_size"]]
    color = "0xF5C400" if o["text_color"] == "yellow" else "white"
    ypos = {"top": "h*0.10", "center": "(h-text_h)/2", "bottom": "h*0.74"}[o["text_pos"]]
    for k, t in enumerate(o["texts"][:n_texts]):
        stage(drawtext(font, f"{pre}t{k}.txt", size, color, ypos, t["start"], t["end"]))
    return state["g"], state["last"]


def build_command(video, out, start, length, o, enc, filler, logo, texts_n, cues, font, pre, ass=None,
                  keep=None, peaks=None, has_audio=True, music=None):
    inputs = ["-ss", str(start), "-i", str(video)]
    idx, filler_idx, logo_idx = 1, None, None
    if o["mode"] == "split" and filler:
        inputs += ["-stream_loop", "-1", "-i", str(filler)]
        filler_idx, idx = idx, idx + 1
    if logo:
        inputs += ["-i", str(logo)]
        logo_idx, idx = idx, idx + 1
    music_idx = None
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_idx, idx = idx, idx + 1
    graph, last = build_graph(o, length, filler_idx, logo_idx, texts_n, font, pre, keep, peaks)
    if ass:   # subtitulos palabra por palabra (libass); la fuente va en la carpeta fonts
        graph += f";[{last}]ass={ass}:fontsdir=fonts[vass]"
        last = "vass"
        cues = []
    # sin libass: un drawtext por frase, al final de la cadena
    for j, (a, b, _t) in enumerate(cues):
        new = f"vs{j}"
        graph += f";[{last}]{drawtext(font, f'{pre}s{j}.txt', 78, 'white', 'h*0.60', a, b)}[{new}]"
        last = new
    if o.get("res") == 720:
        graph += f";[{last}]scale=720:1280[vres]"
        last = "vres"
    agraph, alabel = build_audio(o, keep, has_audio, music_idx)
    if agraph:
        graph += ";" + agraph
    amap = ["-map", alabel] if alabel else ["-map", "0:a?"]
    cmd = [FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1",
           "-nostats"] + inputs + ["-t", str(length), "-filter_complex", graph,
                                   "-map", f"[{last}]"] + amap + \
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
        "mode": pick("mode", ("blur", "crop", "split", "cam"), "blur"),
        "cam_corner": pick("cam_corner", ("tl", "tr", "bl", "br"), "br"),
        "cam_size": int(num(raw.get("cam_size"), 10, 50, 28)),
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
        "ai_title": bool(raw.get("ai_title")),
        "ai_tone": pick("ai_tone", ("directo", "gracioso", "dramatico", "misterioso", "epico"), "directo"),
        "res": int(pick("res", (720, 1080), 1080)), "punch": bool(raw.get("punch")),
        "sub_style": pick("sub_style", ("karaoke", "one", "box", "clean", "green", "pop", "red"), "karaoke"),
        "sub_translate": bool(raw.get("sub_translate")),
        "parts": bool(raw.get("parts")), "part_len": int(num(raw.get("part_len") or raw.get("len"), 8, 120, 30)),
        "zoom_auto": bool(raw.get("zoom_auto")), "bar": bool(raw.get("bar")),
        "enhance": bool(raw.get("enhance")), "cut_pauses": bool(raw.get("cut_pauses")),
        "music": bool(raw.get("music")), "music_vol": int(num(raw.get("music_vol"), 0, 100, 25)),
        "gpu": bool(raw.get("gpu")), "autosave": bool(raw.get("autosave")),
        "yt_upload": bool(raw.get("yt_upload")),
        "yt_privacy": pick("yt_privacy", ("private", "unlisted", "public"), "private"),
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
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "concurrent_fragment_downloads": 8}
    if os.path.isabs(FFMPEG):
        opts["ffmpeg_location"] = str(Path(FFMPEG).parent)
    offset = 0
    job.update(msg="Mirando el link...", pct=3)
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as y0:
        meta = y0.extract_info(url, download=False) or {}
    if state["cancel"].is_set():
        raise Cancelled()
    if meta.get("is_live"):   # directo: no se descarga "todo", se graba un rato
        return record_live(job_id, meta, jdir, rng,
                           live_opts=job.get("_live_opts"))
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


LIVE_CLIP_EVERY = 15 * 60  # cada cuantos segundos grabados se analiza un tramo nuevo
LIVE_MIN_POINTS = 35       # nota minima para que un momento de un directo se corte solo
LIVE_MAX_SPAN = 32 * 60    # un tramo nunca analiza mas de 32 min (30 de intervalo + solape)


def video_gaps(video, min_gap=0.6):
    """Huecos de video (se pierden fotogramas pero el audio sigue): lista de (inicio, fin) en segundos.

    Lee solo los tiempos de cada fotograma (sin decodificar), asi que tarda ~2 s por cada 30 min."""
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-i", str(video), "-map", "0:v:0",
                            "-c", "copy", "-f", "framecrc", "-"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=300,
                           creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        return []
    tb, pts = 1.0 / 90000, []
    for line in r.stdout.decode("utf-8", "ignore").splitlines():
        if line.startswith("#tb 0:"):
            try:
                a, b = line.split()[-1].split("/")
                tb = float(a) / float(b)
            except (ValueError, ZeroDivisionError):
                pass
        elif line and line[0] == "0":
            f = line.split(",")
            try:
                pts.append(int(f[1]) * tb)
            except (IndexError, ValueError):
                continue
    pts.sort()
    return [(a, b) for a, b in zip(pts, pts[1:]) if b - a > min_gap]


def fit_around_gaps(start, length, gaps, pad=0.3):
    """Recorta un momento para que no incluya huecos de video. Devuelve (inicio, largo) o None."""
    end = start + length
    free, cur = [], start
    for a, b in gaps:
        if b + pad <= cur or a - pad >= end:
            continue
        if a - pad > cur:
            free.append((cur, a - pad))
        cur = max(cur, b + pad)
    if end > cur:
        free.append((cur, end))
    if not free:
        return None
    a, b = max(free, key=lambda x: x[1] - x[0])
    if b - a < max(8.0, 0.5 * length):
        return None
    return round(a, 1), round(b - a, 1)


def _frag_cut(src, dst, start, dur):
    """Copia un tramo del directo que se esta grabando (sin tocar el archivo principal)."""
    try:
        subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}",
                        "-i", str(src), "-t", f"{dur:.2f}", "-c", "copy", str(dst)],
                       stdin=subprocess.DEVNULL, timeout=180, creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        return False
    return dst.is_file() and dst.stat().st_size > 10000


def spawn_live_child(parent_id, out, seg_start, seg_dur, opts, direct=False, part_offset=0.0):
    """Crea un lote hijo con un tramo del directo. direct=True: se corta entero (boton 'Clip ya').

    seg_start es relativo al archivo actual (input_N.mp4).
    part_offset es el tiempo absoluto acumulado de partes anteriores.
    _abs_offset = part_offset + seg_start: posicion real en el directo completo.
    """
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M")
    child_id = new_job(f"{opts['name'][:40]} · {'Clip ya' if direct else 'tramo'} {ts}")
    child = JOBS[child_id]
    abs_offset = part_offset + float(seg_start)
    child.update(_auto_render=True, _live_parent=parent_id, _abs_offset=abs_offset,
                 _direct=bool(direct))
    JOBS[parent_id].setdefault("children", []).append(child_id)
    jdir = WORK / child_id
    frag = jdir / "input.mp4"
    if not _frag_cut(out, frag, seg_start, seg_dur):
        child.update(state="error", msg="No se pudo cortar ese tramo del directo.")
        return child_id
    if direct:
        child.update(state="ready", msg="Cortando el clip...", pct=100,
                     duration=seg_dur, moments=[{"start": 0, "len": round(seg_dur), "points": 0}])
        auto_render(child_id, frag, child["moments"])
        return child_id
    threading.Thread(target=analyze_job,
                     args=(child_id, frag, None, opts["want"], opts["length"], None,
                           opts["smart"], opts["snap"]), daemon=True).start()
    return child_id


def _live_clipper(job_id, live_ctx, state, opts):
    """Mientras se graba el directo, analiza cada tramo nuevo y corta los mejores momentos solos.

    Cada ronda mira desde un poco antes de donde acabo la anterior (para no partir un momento
    por la mitad) hasta casi el final de lo grabado. Al terminar cada parte hace una ronda final
    de esa parte; al terminar el directo completo hace la ronda final global.

    live_ctx es un dict compartido con record_live (acceso protegido por live_ctx["lock"]):
      - "out"          : Path al archivo de la parte actual
      - "part_offset"  : segundos grabados en partes anteriores
      - "part_ended"   : threading.Event — record_live lo activa al terminar cada parte
      - "part_ready"   : threading.Event — _live_clipper lo activa al terminar la ronda final
    """
    length = int(opts["length"])
    clip_every = max(60, int(opts.get("clip_every") or LIVE_CLIP_EVERY))
    min_needed = max(60, length * 2)
    lock = live_ctx["lock"]
    done_until = 0.0   # relativo a la parte actual
    while not state["cancel"].is_set():
        time.sleep(2)
        job = JOBS.get(job_id)
        if not job:
            break

        # Leer el contexto de la parte actual de forma atomica
        with lock:
            out = live_ctx["out"]
            part_offset = float(live_ctx["part_offset"])
            part_ended: threading.Event = live_ctx["part_ended"]
            part_ready: threading.Event = live_ctx["part_ready"]

        cur = float(job.get("_rec_sec", 0) or 0)
        rec_done = bool(job.get("_rec_done"))
        all_done = bool(job.get("_live_all_done"))

        due = cur >= min_needed and cur - done_until >= clip_every
        # "final": la parte termino Y hay material nuevo suficiente para analizar
        final = part_ended.is_set() and cur - done_until >= 20

        if part_ended.is_set() and not final:
            if all_done:
                break
            # Parte terminada pero tramo demasiado corto: liberar de todos modos
            if not part_ready.is_set():
                part_ready.set()
            continue

        if not (due or final):
            continue

        sec_end = max(0.0, cur - 2)
        sec_start = max(0.0, done_until - length)
        if sec_end - sec_start > LIVE_MAX_SPAN:
            sec_start = sec_end - LIVE_MAX_SPAN
        if sec_end - sec_start < 15:
            if final and not part_ready.is_set():
                part_ready.set()   # nada que analizar: liberar de todos modos
            continue

        spawn_live_child(job_id, out, sec_start, sec_end - sec_start, opts,
                         part_offset=part_offset)
        done_until = sec_end

        if final:
            if all_done:
                break
            # Ronda final de esta parte terminada: avisar a record_live y preparar la siguiente
            if not part_ready.is_set():
                part_ready.set()
            done_until = 0.0


def record_live(job_id, meta, jdir, rng, live_opts=None):
    """Directo: graba en partes (input_1.mp4, input_2.mp4...) hasta que el directo termine.

    Cada parte dura 'cap' minutos (por defecto 60). Al terminar una parte se comprueba si el
    directo sigue en vivo; si es asi, se abre una nueva parte automaticamente.
    _live_clipper se queda corriendo todo el tiempo y sabe en que parte esta gracias a live_ctx.
    """
    job, state = JOBS[job_id], rt(job_id)
    given = int((rng[2] if rng and rng[2] else 0) or 0)
    # Cap por parte: si el usuario no pone limite, 60 min por parte (con clipeo auto) o 10 sin el
    cap = given or (60 if live_opts else 10)
    stream = meta.get("url")
    headers = dict(meta.get("http_headers") or {})
    if not stream:
        fm = [f for f in (meta.get("formats") or []) if f.get("url") and f.get("vcodec") != "none"]
        if fm:
            stream = fm[-1]["url"]
            headers = dict(fm[-1].get("http_headers") or headers)
    if not stream:
        raise RuntimeError("No pude abrir el directo.")
    title = (str(meta.get("title") or "directo"))[:80]
    job.update(live=True, msg="Conectando con el directo...", pct=3)
    job["name"] = title + ".mp4"

    # Contexto compartido con _live_clipper.
    # "lock" protege las actualizaciones atomicas de out/part_offset al cambiar de parte.
    # "part_ended" : record_live lo activa cuando la parte termina de grabarse.
    # "part_ready" : _live_clipper lo activa cuando termino la ronda final de esa parte.
    live_ctx: dict = {
        "lock": threading.Lock(),
        "out": jdir / "input_1.mp4",
        "part_offset": 0.0,
        "part_ended": threading.Event(),
        "part_ready": threading.Event(),
    }
    job["_live_out"] = str(live_ctx["out"])   # para /clipnow

    def build(out_path, bsf, oldest=True, reconn=True, part_num=1):
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        if stream.startswith("http") and reconn:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if ".m3u8" in stream and oldest and not job.get("_live_edge"):
            cmd += ["-live_start_index", "-100000"]
        cmd += ["-i", stream, "-t", str(cap * 60), "-c", "copy"]
        if bsf:
            cmd += ["-bsf:a", "aac_adtstoasc"]
        return cmd + ["-movflags", "frag_keyframe+empty_moov", str(out_path)]

    logf = jdir / "grabacion.log"
    # si una forma de abrir el directo no avanza, prueba la siguiente (cada una mas simple)
    variants = [(True, False, False), (True, True, False), (True, False, True), (False, False, False)]

    started_clipper = False
    part_num = 0
    part_offset = 0.0   # segundos reales grabados en partes anteriores
    first_out = None    # primer archivo valido (para devolver si no hay live_opts)

    try:
        while not state["cancel"].is_set():
            part_num += 1
            out = jdir / f"input_{part_num}.mp4"
            with live_ctx["lock"]:
                live_ctx["out"] = out
                live_ctx["part_offset"] = part_offset
                live_ctx["part_ended"] = threading.Event()   # nuevo evento: esta parte aun no termino
                live_ctx["part_ready"] = threading.Event()   # _live_clipper aun no termino su ronda
            job["_live_out"] = str(out)
            job["_part_offset_snapshot"] = part_offset   # para /clipnow
            job["_rec_sec"] = 0.0
            job["_rec_done"] = False

            partes_txt = f" (parte {part_num})" if part_num > 1 else ""
            job.update(live=True, msg=f"Conectando con el directo{partes_txt}...", pct=3)

            tail = deque(maxlen=12)
            got = {"v": False}

            def on_line(line, pnum=part_num):
                if line.startswith("out_time_us="):
                    try:
                        sec = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        return
                    job["_rec_sec"] = sec
                    total_sec = part_offset + sec
                    lotes = len(job.get("children", []))
                    extra = f" · {lotes} tramo(s) analizados" if lotes else ""
                    ptxt = f" · parte {pnum}" if pnum > 1 else ""
                    job["msg"] = (f"Grabando el directo... {fmt_time(total_sec)} "
                                  f"(max {cap} min/parte){ptxt}{extra}")
                    job["pct"] = int(5 + min(1.0, sec / (cap * 60)) * 50)

            # Intentar variantes hasta que alguna reciba video
            for n, (bsf, oldest, reconn) in enumerate(variants):
                cmd = build(out, bsf, oldest, reconn, part_num)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW | ABOVE_NORMAL)
                with state["lock"]:
                    state["procs"].add(p)
                    state["stdin"] = p.stdin

                # Arrancar _live_clipper solo una vez, la primera vez que hay video
                if live_opts and not started_clipper:
                    started_clipper = True
                    live_opts["name"] = title
                    threading.Thread(target=_live_clipper,
                                     args=(job_id, live_ctx, state, live_opts),
                                     daemon=True).start()

                t0 = time.time()
                stalled = {"v": False}

                def watchdog(p=p, t0=t0, stalled=stalled):
                    while p.poll() is None and not state["cancel"].is_set():
                        time.sleep(1)
                        if not got["v"] and time.time() - t0 > 30:
                            stalled["v"] = True
                            try:
                                p.kill()
                            except Exception:  # noqa: BLE001
                                pass
                            return
                threading.Thread(target=watchdog, daemon=True).start()

                try:
                    for raw in p.stdout:
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if line and not re.match(
                                r"^(frame|fps|stream_\d|bitrate|total_size|out_time|"
                                r"dup_frames|drop_frames|speed|progress)", line):
                            tail.append(line)
                        if line.startswith("out_time_us=") and job.get("_rec_sec", 0) > 0.5:
                            got["v"] = True
                        on_line(line)
                    p.wait()
                finally:
                    with state["lock"]:
                        state["procs"].discard(p)
                        state["stdin"] = None

                try:
                    with open(logf, "a", encoding="utf-8") as lf:
                        lf.write(f"parte {part_num} intento {n + 1} bsf={bsf} oldest={oldest} "
                                 f"reconnect={reconn} codigo={p.returncode} "
                                 f"avance={job.get('_rec_sec', 0):.1f}s\n"
                                 + "\n".join(tail) + "\n")
                except OSError:
                    pass

                if state["cancel"].is_set() or got["v"]:
                    break
                job["msg"] = (f"El directo no respondio, probando otra forma "
                              f"({n + 2}/{len(variants)})...")

            # Marcar esta parte como terminada: _live_clipper hara su ronda final
            job["_rec_done"] = True
            live_ctx["part_ended"].set()

            if state["cancel"].is_set():
                break

            if not got["v"]:
                # Ninguna variante recibio video en esta parte
                if part_num == 1:
                    raise RuntimeError("No se pudo grabar el directo. "
                                       + " | ".join(list(tail)[-3:])[-300:])
                # En partes posteriores puede ser que el directo acabo: salir normal
                job.update(msg="El directo parece haber terminado.", pct=100)
                job["_live_all_done"] = True
                break

            if first_out is None:
                first_out = out

            # Acumular el tiempo grabado en esta parte
            sec_recorded = float(job.get("_rec_sec", 0) or 0)
            part_offset += sec_recorded

            # Esperar a que _live_clipper termine la ronda final de esta parte (max 2 min)
            # part_ready se activa cuando el clipper termino de analizar o no habia nada
            part_ready_ev: threading.Event = live_ctx["part_ready"]
            part_ready_ev.wait(timeout=120)
            # (si expira el timeout seguimos igualmente: no bloqueamos el directo)

            # Comprobar si el directo sigue en vivo
            if job.get("_stop_requested"):
                job.update(msg="Grabacion detenida por ti.", pct=100)
                job["_live_all_done"] = True
                break

            # Solo seguir si el directo esta en vivo (y no se puso un limite manual)
            if given:
                job["_live_all_done"] = True
                break

            job.update(msg="Comprobando si el directo sigue en vivo...", pct=5)
            still_live = streamer_is_live(meta.get("webpage_url") or meta.get("url") or "")
            if not still_live:
                job.update(msg="El directo ha terminado.", pct=100)
                job["_live_all_done"] = True
                break

            # El directo sigue: preparar la siguiente parte
            job.update(msg=f"El directo sigue. Arrancando parte {part_num + 1}...", pct=5)
            # La siguiente parte empieza desde el borde en vivo (no rebobinar)
            job["_live_edge"] = True

            # Borrar la parte anterior solo cuando no queden hijos activos que la usen
            # (hijos "activos" = en estado queued/analyzing/rendering)
            prev_out = jdir / f"input_{part_num}.mp4"
            if prev_out.is_file() and prev_out != first_out:
                children_of_part = [
                    cid for cid in JOBS.get(job_id, {}).get("children", [])
                    if float(JOBS.get(cid, {}).get("_abs_offset", -1)) >= (part_offset - sec_recorded)
                    and float(JOBS.get(cid, {}).get("_abs_offset", -1)) < part_offset
                ]
                # Esperar hasta 3 min a que todos los hijos de esta parte terminen
                wait_start = time.time()
                while time.time() - wait_start < 180:
                    still_active = [
                        cid for cid in children_of_part
                        if JOBS.get(cid, {}).get("state") in ("queued", "analyzing", "rendering")
                    ]
                    if not still_active:
                        break
                    time.sleep(5)
                try:
                    prev_out.unlink()
                    with open(logf, "a", encoding="utf-8") as lf:
                        lf.write(f"parte {part_num}: archivo borrado "
                                 f"(offset {part_offset - sec_recorded:.0f}s - {part_offset:.0f}s)\n")
                except OSError:
                    pass

    finally:
        job["live"] = False
        job["_rec_done"] = True
        job["_live_all_done"] = True   # garantizar que _live_clipper termine

    if state["cancel"].is_set():
        raise Cancelled()
    out_final = first_out or (jdir / "input_1.mp4")
    if not out_final.is_file() or out_final.stat().st_size < 10000:
        raise RuntimeError("No se pudo grabar el directo. " + " | ".join(list(tail)[-3:])[-300:])
    job["offset"] = 0
    return out_final


def auto_render(job_id, video, moments):
    """Lote de un directo: corta los clips solo con los ajustes guardados y los guarda en la carpeta elegida."""
    gaps = video_gaps(video)
    if gaps:
        fixed, lost = [], 0
        for m in moments:
            fit = fit_around_gaps(float(m["start"]), float(m.get("len") or 30), gaps)
            if fit:
                fixed.append(dict(m, start=fit[0], len=fit[1]))
            else:
                lost += 1
        total = round(sum(b - a for a, b in gaps), 1)
        JOBS[job_id].setdefault("_pre_warn", []).append(
            f"El directo tuvo {len(gaps)} hueco(s) sin imagen ({total} s en total, suele ser el directo o la conexion). "
            f"Se recortaron los clips para evitarlos" + (f" y se saltaron {lost}." if lost else "."))
        moments = fixed
        if not moments:
            JOBS[job_id].update(state="done", msg="Todos los momentos caian en un hueco de video", pct=100)
            return
    par = JOBS[job_id].get("_live_parent")
    if par and not JOBS[job_id].get("_direct"):
        # directo: solo momentos fuertes y que no repitan lo ya cortado en rondas anteriores
        off = float(JOBS[job_id].get("_abs_offset", 0))
        pj = JOBS.get(par, {})
        used = pj.setdefault("_used", [])
        keep = []
        for m in sorted(moments, key=lambda m: m.get("points", 0), reverse=True):
            if m.get("points", 0) < LIVE_MIN_POINTS:
                continue
            a_, b_ = off + float(m["start"]), off + float(m["start"]) + float(m.get("len") or 30)
            dup = any(min(b_, y) - max(a_, x) > 0.3 * (b_ - a_) for x, y in used)
            if not dup:
                keep.append(m)
                used.append((a_, b_))
        moments = keep
        if not moments:
            JOBS[job_id].update(state="done", msg="Sin momentos fuertes en este tramo", pct=100)
            return
    cfg = dict(load_settings())
    cfg["autosave"] = True      # si hay carpeta de salida, se guardan ahi; si no, quedan para descargar
    cfg["yt_upload"] = bool(cfg.get("yt_auto")) and yt_connected()
    o = clean_opts(cfg)
    segs = [{"start": round(float(m["start"]), 1), "len": round(float(m.get("len") or 30), 1),
             "points": int(m.get("points") or 0)} for m in moments[:40]]
    JOBS[job_id].update(state="rendering", msg="Cortando clips solos...", pct=0)
    threading.Thread(target=render_job, args=(job_id, video, segs, o), daemon=True).start()


def analyze_job(job_id, video=None, url=None, want=5, length=30, rng=None, smart=None, snap=True):
    job, jdir = JOBS[job_id], WORK / job_id
    try:
        if url:
            job.update(state="analyzing", msg="Descargando el video...", pct=2)
            # Si el stream es un directo y se pide clipeo automatico, dejamos las opts en el job
            if smart and smart.get("live_clips"):
                job["_live_opts"] = {"want": want, "length": length,
                                     "smart": {k: v for k, v in smart.items()
                                               if k not in ("live_clips", "clip_every")},
                                     "clip_every": int(smart.get("clip_every") or 15 * 60),
                                     "snap": snap, "auto_render": True}
            video = download_video(job_id, url, jdir, rng)
            if job.get("_live_opts"):   # los clips ya salieron por tramos; no se re-analiza todo
                time.sleep(3)           # deja arrancar la ronda final
                n = len(job.get("children", []))
                job.update(state="done", pct=100, msg=f"Directo terminado: {n} tramo(s) analizados. "
                           "Los clips estan en cada lote.")
                return
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
        ENERGY[job_id] = energy
        note = "Momentos listos"
        moments = None
        smart_used = False
        ai_rank_used = False
        if smart and energy and max(energy) >= 20:
            query = smart.get("query", "")
            use_ai_rank = smart.get("ai_rank") and get_gemini_key()
            use_whisper = find_whisper()
            ncand = min(25, max(want * 4, 15)) if query else min(15, max(want * 3, want + 4))
            cands = pick_moments(energy, duration, length, ncand)
            if use_whisper:
                moments, used = smart_rank(job_id, video, cands, want, length, smart["lang"],
                                           jdir, job, query)
                moments = sorted(moments, key=lambda m: m["start"])
                smart_used = bool(used)
                note = "Momentos elegidos por volumen y por lo que se dice"
                if query:
                    hits = sum(1 for m in moments if m.get("match"))
                    note = (f"Busqueda «{query[:30]}»: {hits} momento(s) donde se dice. "
                            "Solo se busca en los tramos mas fuertes del video.")
            if use_ai_rank:
                # Gemini re-ordena los mejores candidatos (sin subir video ni audio)
                pool = moments if moments else cands
                job.update(msg="La IA elige los mejores momentos...", pct=95)
                ai_moments, ok = gemini_rank(pool, want, length, job)
                if ok:
                    moments = sorted(ai_moments, key=lambda m: m["start"])
                    ai_rank_used = True
                    if smart_used:
                        note = "Momentos elegidos por voz y reordenados por la IA"
                    else:
                        note = "Momentos elegidos por la IA"
        if moments is None:
            moments = pick_moments(energy, duration, length, want)
        if snap and energy and max(energy) >= 20:
            moments = snap_ends(moments, energy, duration)
        if energy and max(energy) >= 20:
            moments = score_moments(moments, energy, smart_used)
        if not energy or max(energy) < 20:
            note = "No se detecto sonido util en este video: repartí los clips parejos, ajustalos a mano."
        if smart and smart.get("query") and not find_whisper():
            note = "Esta instalacion no trae Whisper: no se pudo buscar por lo que se dice."
        if smart and smart.get("ai_rank") and not get_gemini_key():
            note += ". Falta la clave de Gemini: se eligieron los momentos por volumen."
        if job.get("note"):
            note += ". " + job["note"]
        job.update(state="ready", msg=note, pct=100, duration=duration,
                   wave=wave_data, wave_step=step,
                   moments=moments)
        if job.get("_auto_render") and moments:
            auto_render(job_id, video, moments)
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
        job.update(state="rendering", msg="Preparando...", pct=0, clips=[], saved=[], warnings=[],
                   ready_clips=[])
        job["warnings"].extend(job.pop("_pre_warn", []))
        font = find_font()
        if (o["texts"] or o["subs"]) and not font:
            job["warnings"].append("No se encontro una fuente para los textos; salieron sin texto.")
        filler = asset_path("filler") if o["mode"] == "split" else None
        if o["mode"] == "split" and not filler:
            job["warnings"].append("Falta el video de relleno; se uso el fondo borroso.")
        logo = asset_path("logo")
        music = asset_path("music") if o["music"] else None
        if o["music"] and not music:
            job["warnings"].append("Falta la musica de fondo; salio sin musica.")
        audio_on = has_audio_stream(video)
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
        if o["parts"]:   # un momento largo se corta en partes seguidas: PARTE 1/3, 2/3...
            expanded = []
            for sg in segments:
                n = max(1, round(sg["len"] / max(8, o["part_len"])))
                if n == 1:
                    expanded.append(sg)
                    continue
                each = round(sg["len"] / n, 1)
                for k in range(n):
                    expanded.append(dict(sg, start=round(sg["start"] + k * each, 1), len=each,
                                         part=(k + 1, n)))
            segments = expanded[:60]
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
            heard = []
            if (o["subs"] or (o["ai_title"] and get_gemini_key())) and font:
                try:
                    got = transcribe(job_id, video, start, length, o["sub_lang"], jdir, i,
                                     o["sub_translate"])
                    if got is None:
                        warn("Esta version no incluye subtitulos ni titulos con IA.")
                    else:
                        heard = got[:200]
                        cues = heard if o["subs"] else []
                except Cancelled:
                    raise
                except Exception:  # noqa: BLE001
                    warn(f"No se pudieron generar los subtitulos del clip {i}.")
            keep, out_len = None, length
            if o["cut_pauses"] and heard:
                keep = keep_intervals(heard, length)
            peaks = clip_peaks(ENERGY.get(job_id), start, length) if o["zoom_auto"] else []
            if keep:
                out_len = round(sum(e - x for x, e in keep), 2)
                cues = [(remap_time(a, keep), remap_time(b, keep), w) for a, b, w in cues]
                cues = [c for c in cues if c[1] > c[0] + 0.05]
                peaks = [round(remap_time(t, keep), 2) for t in peaks]
            ai = None
            if o["ai_title"] and heard:
                try:
                    ai = gemini_title(" ".join(w[2] for w in heard), o["sub_lang"], o["ai_tone"])
                except Exception as e:  # noqa: BLE001
                    warn(str(e)[:120])
            oc = dict(o)
            if seg.get("part"):
                oc["part_label"] = f"PARTE {seg['part'][0]}/{seg['part'][1]}"
                (jdir / f"{pre}part.txt").write_text(oc["part_label"], encoding="utf-8")
            if ai and not o["texts"]:
                oc["texts"] = [{"text": ai["title"], "start": 0.3, "end": min(4.0, out_len)}]
            wrap_w = {"s": 20, "m": 16, "l": 12}[o["text_size"]]
            texts = oc["texts"] if font else []
            for k, t in enumerate(texts):
                (jdir / f"{pre}t{k}.txt").write_text(
                    "\n".join(textwrap.wrap(t["text"].upper(), wrap_w)[:4]), encoding="utf-8")
            phrases = [(g[0][0], g[-1][1], " ".join(w[2] for w in g)) for g in group_words(cues)]
            ass = None
            if cues and HAS_ASS[0]:
                try:
                    (jdir / "fonts").mkdir(exist_ok=True)
                    if font:
                        shutil.copyfile(font, jdir / "fonts" / Path(font).name)
                    write_ass(jdir / f"{pre}subs.ass", cues, font, o["sub_style"])
                    ass = f"{pre}subs.ass"
                except OSError:
                    ass = None
            for j, (a, b, body) in enumerate(phrases):
                (jdir / f"{pre}s{j}.txt").write_text(
                    "\n".join(textwrap.wrap(body.upper(), 18)[:2]), encoding="utf-8")
            out = jdir / f"clip{i}.mp4"
            enc = enc_box[0]

            def attempt(enc_name, use_ass):
                cmd = build_command(video, out, start, out_len, oc, enc_name, filler, logo,
                                    len(texts), phrases, font, pre, ass if use_ass else None,
                                    keep, peaks, audio_on, music)
                run_ffmpeg(cmd, cwd=str(jdir), job_id=job_id, length=out_len, on_progress=progress)

            use_ass = ass is not None
            try:
                attempt(enc, use_ass)
            except RuntimeError:
                if state["cancel"].is_set():
                    raise
                if use_ass:   # puede ser libass o la fuente: reintenta con el metodo simple
                    warn("Los subtitulos animados fallaron; se uso el estilo simple.")
                    use_ass = False
                    try:
                        attempt(enc, False)
                    except RuntimeError:
                        if enc == "libx264":
                            raise
                        warn("La tarjeta de video fallo; se uso el procesador.")
                        enc_box[0] = "libx264"
                        attempt("libx264", False)
                elif enc == "libx264":
                    raise
                else:
                    warn("La tarjeta de video fallo; se uso el procesador.")
                    enc_box[0] = "libx264"
                    attempt("libx264", False)
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
                                  "len": round(out_len), "saved": saved,
                                  "title": (ai or {}).get("title", ""), "tags": (ai or {}).get("tags", []),
                                  "desc": (ai or {}).get("desc", ""),
                                  "points": seg.get("points", 0),
                                  "part": list(seg["part"]) if seg.get("part") else None}
                job["ready_clips"] = [
                    {"name": r["name"], "start": r["start"], "len": r["len"], "title": r["title"],
                     "tags": r["tags"], "desc": r["desc"], "points": r["points"], "part": r["part"]}
                    for r in results if r]
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
        job["clips"] = [{"name": r["name"], "start": r["start"], "len": r["len"],
                         "title": r["title"], "tags": r["tags"], "desc": r["desc"], "points": r["points"], "part": r["part"]}
                        for r in results if r]
        job["saved"] = [r["saved"] for r in results if r and r["saved"]]
        job.update(state="done", msg="Listo", pct=100)
        if o.get("yt_upload") and job["clips"]:
            threading.Thread(target=yt_upload_clips, args=(job_id, list(job["clips"]), o["yt_privacy"]),
                             daemon=True).start()
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
    seg = int(clean_num(st.get("len"), 3, 600, 30))
    lang = st.get("sub_lang") if st.get("sub_lang") in ("es", "en", "auto") else "es"
    smart = {"lang": lang, "ai_rank": bool(st.get("ai_rank"))} if (st.get("smart") or st.get("ai_rank")) else None
    threading.Thread(target=analyze_job,
                     args=(job_id, dest, None, want, seg, None, smart, st.get("snap") is not False),
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
    """Empieza a bajar el instalador nuevo en segundo plano. Devuelve True si arranco."""
    url = UPDATE.get("url", "")
    if not IS_WIN or not url.startswith(UPDATE_PREFIX):
        return False
    if UPDATE["dl"] in ("downloading", "launching"):
        return True
    UPDATE.update(dl="downloading", pct=0, msg="Conectando...")
    threading.Thread(target=_download_update, args=(url,), daemon=True).start()
    return True


def _download_update(url):
    dest = Path(tempfile.gettempdir()) / "ClipperSetup.exe"
    part = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got, last = 0, time.time()
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and time.time() - last > 0.3:
                    last = time.time()
                    UPDATE.update(pct=int(got * 100 / total),
                                  msg=f"{got // 1048576} de {total // 1048576} MB")
        if part.stat().st_size < 1000000 or part.read_bytes()[:2] != b"MZ":
            raise RuntimeError("El archivo bajado no es un instalador valido.")
        part.replace(dest)
    except Exception as e:  # noqa: BLE001
        part.unlink(missing_ok=True)
        UPDATE.update(dl="error", msg=str(e)[:150] or "No se pudo bajar la actualizacion.")
        return
    UPDATE.update(dl="launching", pct=100, msg="Abriendo el instalador...")
    subprocess.Popen([str(dest)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    threading.Timer(1.5, lambda: os._exit(0)).start()


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
                        "font": find_font() is not None, "lan": LAN_MODE,
                        "gemini": bool(get_gemini_key())})
        elif u.path == "/streamers":
            self._json({"streamers": streamers_view(), "every": MONITOR_EVERY})
        elif u.path == "/youtube":
            cfg, st = yt_load(), load_settings()
            self._json({"configured": bool(cfg.get("client_id") and cfg.get("client_secret")),
                        "connected": bool(cfg.get("refresh_token")), "auth": dict(YT_AUTH),
                        "auto": bool(st.get("yt_auto")), "privacy": st.get("yt_privacy") or "private",
                        "max_per_day": YT_MAX_PER_DAY})
        elif u.path == "/settings":
            self._json(load_settings())
        elif u.path == "/assets":
            self._json({r: (asset_path(r).name if asset_path(r) else None)
                        for r in ("logo", "filler", "music")})
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
            length = int(clean_num(body.get("len"), 3, 600, 30))
            a = clean_num(body.get("from_min"), 0, 2000, 0) * 60
            b = clean_num(body.get("to_min"), 0, 2000, 0) * 60
            last = clean_num(body.get("last_min"), 0, 2000, 0)
            rng = (a, b if b > a else None, last)
            qy = str(body.get("query") or "").strip()[:80]
            live_clips = bool(body.get("live_clips"))
            clip_every = clean_num(body.get("clip_every_min"), 1, 60, 15)
            clip_every = clip_every if clip_every in (3, 15, 30) else 15
            smart = {"lang": body.get("lang") if body.get("lang") in ("es", "en", "auto") else "es",
                     "query": qy,
                     "ai_rank": bool(body.get("ai_rank")),
                     "live_clips": live_clips, "clip_every": clip_every * 60} if (body.get("smart") or qy or body.get("ai_rank") or live_clips) else None
            if live_clips and not smart:
                smart = {"lang": "es", "query": "", "ai_rank": False, "live_clips": True}
            snap = body.get("snap") is not False
            threading.Thread(target=analyze_job,
                             args=(job_id, None, url, want, length, rng, smart, snap),
                             daemon=True).start()
            self._json({"id": job_id})
        elif u.path == "/youtube/config":
            body = self._body_json() or {}
            cid, sec = str(body.get("client_id") or "").strip(), str(body.get("client_secret") or "").strip()
            if not re.fullmatch(r"[\w.\-]{10,200}", cid) or not re.fullmatch(r"[\w.\-]{10,200}", sec):
                self._json({"error": "El ID de cliente o el secreto no tienen el formato de Google."}, 400)
                return
            cfg = yt_load()
            if cfg.get("client_id") != cid:
                cfg.pop("refresh_token", None)
            cfg.update(client_id=cid, client_secret=sec)
            yt_save(cfg)
            self._json({"ok": True})
        elif u.path == "/youtube/connect":
            try:
                self._json({"url": yt_connect_start()})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif u.path == "/youtube/disconnect":
            YT_FILE.unlink(missing_ok=True)
            YT_TOKEN.update(value="", exp=0.0)
            YT_AUTH.update(status="idle", msg="")
            self._json({"ok": True})
        elif u.path in ("/streamers/add", "/streamers/toggle", "/streamers/delete"):
            body = self._body_json() or {}
            items = load_streamers()
            if u.path == "/streamers/add":
                url = normalize_streamer_url(body.get("url"))
                if not url:
                    self._json({"error": "Pon un link (https://kick.com/usuario) o solo el usuario."}, 400)
                    return
                if any(x["url"].lower() == url.lower() for x in items):
                    self._json({"error": "Ese canal ya esta en la lista."}, 400)
                    return
                if len(items) >= 50:
                    self._json({"error": "Maximo 50 canales."}, 400)
                    return
                name = str(body.get("name") or "").strip()[:60] or url.rstrip("/").split("/")[-1]
                items.append({"url": url, "name": name, "enabled": True})
            else:
                url = str(body.get("url") or "")
                if u.path == "/streamers/delete":
                    items = [x for x in items if x["url"] != url]
                else:
                    for x in items:
                        if x["url"] == url:
                            x["enabled"] = bool(body.get("enabled"))
            try:
                save_streamers(items)
            except OSError:
                self._json({"error": "No pude guardar la lista."}, 500)
                return
            MONITOR_WAKE.set()
            self._json({"ok": True})
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
        elif u.path == "/clipnow":
            jid = q.get("id", [""])[0]
            pj = JOBS.get(jid)
            lo = (pj or {}).get("_live_opts")
            if not pj or not lo or not pj.get("live"):
                self._json({"error": "Este directo ya no se esta grabando."}, 400)
                return
            cur = float(pj.get("_rec_sec", 0) or 0)
            dur = float(lo["length"])
            if cur < 8:
                self._json({"error": "Espera unos segundos: aun no hay video grabado."}, 400)
                return
            dur = min(dur, cur - 2)
            # Usar el archivo de la parte actual (input_N.mp4) y su offset acumulado
            live_out_path = pj.get("_live_out")
            out_path = Path(live_out_path) if live_out_path else WORK / jid / "input_1.mp4"
            part_offset = float(pj.get("_part_offset_snapshot", 0.0))
            spawn_live_child(jid, out_path, max(0.0, cur - 2 - dur), dur, lo,
                             direct=True, part_offset=part_offset)
            self._json({"ok": True})
        elif u.path == "/stoplive":
            jid = q.get("id", [""])[0]
            pj = JOBS.get(jid)
            if pj:
                pj["_stop_requested"] = True   # impide que el bucle abra otra parte
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
        elif u.path == "/apikey":
            body = self._body_json(2000)
            ok = set_gemini_key((body or {}).get("key", ""))
            self._json({"ok": ok} if ok else {"error": "La clave no parece valida."}, 200 if ok else 400)
        elif u.path == "/settings":
            body = self._body_json(30000)
            try:
                save_settings(body)
                self._json({"ok": True})
            except (OSError, ValueError):
                self._json({"error": "No se pudieron guardar los ajustes."}, 400)
        elif u.path == "/asset/delete":
            role = q.get("role", [""])[0]
            if role in ("logo", "filler", "music"):
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
        if role in ("logo", "filler", "music"):
            if ext not in {"logo": LOGO_EXTS, "filler": VIDEO_EXTS, "music": MUSIC_EXTS}[role]:
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
        if role in ("logo", "filler", "music"):
            self._json({"ok": True, "name": dest.name})
            return
        want = int(clean_num(q.get("n", ["5"])[0], 1, 40, 5))
        seg = int(clean_num(q.get("len", ["30"])[0], 3, 600, 30))
        lang = q.get("lang", ["es"])[0]
        qy = q.get("query", [""])[0].strip()[:80]
        ai_rank_q = q.get("ai_rank", ["0"])[0] == "1"
        smart = {"lang": lang if lang in ("es", "en", "auto") else "es", "query": qy,
                 "ai_rank": ai_rank_q} \
            if (q.get("smart", ["0"])[0] == "1" or qy or ai_rank_q) else None
        snap = q.get("snap", ["1"])[0] != "0"
        threading.Thread(target=analyze_job, args=(job_id, dest, None, want, seg, None, smart, snap),
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
                length = max(3.0, min(600.0, float(s.get("len"))))
            except (TypeError, ValueError, AttributeError):
                continue
            if job["duration"] and start >= job["duration"]:
                continue
            segs.append({"start": round(start, 1), "len": round(length, 1),
                         "points": int(clean_num(s.get("points"), 0, 100, 0))})
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
        win = webview.create_window("Clipper | Shadow Mode", url, js_api=api, width=1280,
                                    height=800, min_size=(420, 600), background_color="#08090c")
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
                 "--window-size=1280,800", "--no-first-run", "--no-default-browser-check"],
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
    threading.Thread(target=monitor_loop, daemon=True).start()
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
