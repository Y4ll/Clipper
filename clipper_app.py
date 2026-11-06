#!/usr/bin/env python3
"""
clipper_app.py - App local para sacar clips verticales (9:16) de videos largos.

Abre una pagina web en tu propia laptop: subes el video, eliges opciones y
descargas los clips. Todo corre local, gratis y sin marca de agua.

Que hace:
  - Detecta los momentos con mas energia de audio (gritos, explosiones, hype)
  - Corta cada momento y lo pasa a vertical 1080x1920
  - Fondo borroso (ideal gameplay) o recorte de lados
  - Efectos de camara: zoom lento (push-in) y vibracion (shake)
  - Texto gancho en los primeros 3 segundos

Requisitos: Python 3.8+ y ffmpeg instalado. No necesita instalar nada mas.

Uso:
    python clipper_app.py            # abre http://127.0.0.1:8000
    python clipper_app.py --lan      # tambien accesible desde tu iPhone (misma wifi)
"""

import argparse
import array
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# El .exe se crea sin consola: en ese caso no hay stdout/stderr y print() fallaria
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

SAMPLE_RATE = 4000
IDLE_LIMIT = 120      # segundos sin que la ventana responda antes de cerrar el programa
LAST_PING = [0.0]     # ultima vez que la ventana dijo "sigo aqui"
WORK = Path(tempfile.gettempdir()) / "clipper_app_jobs"
JOBS = {}  # id -> dict(state, msg, clips)
MAX_UPLOAD = 4 * 1024 * 1024 * 1024  # 4 GB
JOB_MAX_AGE_SEC = 24 * 3600  # borrar jobs de más de 24 h
FFMPEG = "ffmpeg"  # se resuelve en main() (ruta absoluta si está junto al .exe)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
]


# ---------------------------------------------------------------- video logic
def run_ffmpeg(cmd, cwd=None):
    # Sustituye el binario "ffmpeg" por la ruta resuelta en main()
    if cmd and cmd[0] == "ffmpeg":
        cmd = [FFMPEG] + list(cmd[1:])
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, cwd=cwd, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-800:])


def extract_audio(video, wav_path):
    run_ffmpeg(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1",
                "-ar", str(SAMPLE_RATE), "-f", "wav", str(wav_path)])


def energy_per_second(wav_path):
    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    out = []
    for i in range(0, len(samples) - rate + 1, rate):
        block = samples[i:i + rate]
        out.append(math.sqrt(sum(s * s for s in block) / rate))
    return out


def pick_moments(energy, length, count):
    total = len(energy)
    if total <= length:
        return [0]
    prefix = [0.0]
    for e in energy:
        prefix.append(prefix[-1] + e)
    cands = sorted(((prefix[s + length] - prefix[s], s)
                    for s in range(total - length + 1)), reverse=True)
    chosen = []
    for _, s in cands:
        if all(abs(s - o) >= length for o in chosen):
            chosen.append(s)
        if len(chosen) == count:
            break
    return sorted(chosen)


def find_font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def ff_escape(path):
    # Escapa una ruta para usarla dentro de un filtro de ffmpeg (Windows incluido)
    return str(path).replace("\\", "/").replace(":", "\\:")


def build_filter(mode, length, zoom, shake, has_hook, font):
    if mode == "crop":
        chain = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                 "crop=1080:1920[v0]")
    else:
        chain = ("[0:v]split[a][b];"
                 "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
                 "crop=1080:1920,boxblur=20:5[bg];"
                 "[b]scale=1080:-2[fg];"
                 "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
    last = "v0"
    if zoom:
        chain += (f";[{last}]scale=w='1080*(1+0.14*min(t/{length},1))':h=-2:"
                  f"eval=frame,crop=1080:1920[vz]")
        last = "vz"
    if shake:
        chain += (f";[{last}]scale=1166:2074,"
                  "crop=1080:1920:x='(iw-1080)/2+sin(t*37)*16':"
                  "y='(ih-1920)/2+cos(t*29)*16'[vs]")
        last = "vs"
    # Solo dibujar texto si hay fuente válida (evita fallos en Windows sin arialbd)
    if has_hook and font:
        font_part = f"fontfile='{ff_escape(font)}':"
        chain += (f";[{last}]drawtext=textfile=hook.txt:{font_part}fontsize=84:"
                  "fontcolor=white:borderw=7:bordercolor=black:line_spacing=12:"
                  "x=(w-text_w)/2:y=h*0.10:enable='between(t,0,3)'[vh]")
        last = "vh"
    return chain, last


def process_job(job_id, video, opts):
    job = JOBS[job_id]
    jdir = WORK / job_id
    try:
        job.update(state="working", msg="Extrayendo audio...")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "a.wav"
            extract_audio(video, wav)
            job["msg"] = "Buscando los mejores momentos..."
            energy = energy_per_second(wav)
        moments = pick_moments(energy, opts["len"], opts["n"])

        hook = opts["hook"].strip()
        if hook:
            wrapped = "\n".join(textwrap.wrap(hook.upper(), 16)[:3])
            (jdir / "hook.txt").write_text(wrapped, encoding="utf-8")
        font = find_font()
        # Si hay texto pero no encontramos fuente, avisamos pero seguimos sin texto
        use_hook = bool(hook) and bool(font)

        chain, last = build_filter(opts["mode"], opts["len"], opts["zoom"],
                                   opts["shake"], use_hook, font)
        for i, start in enumerate(moments, 1):
            job["msg"] = f"Cortando clip {i} de {len(moments)}..."
            out = jdir / f"clip{i}.mp4"
            run_ffmpeg([
                "ffmpeg", "-y", "-ss", str(start), "-i", str(video),
                "-t", str(opts["len"]), "-filter_complex", chain,
                "-map", f"[{last}]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                str(out),
            ], cwd=str(jdir))
            mm, ss = divmod(start, 60)
            job["clips"].append({"name": out.name, "start": f"{mm}:{ss:02d}"})
        job.update(state="done", msg="Listo")
    except Exception as e:  # noqa: BLE001
        job.update(state="error", msg=str(e))
    finally:
        # Liberar espacio: borrar el video de entrada (los clips se quedan)
        try:
            if video.is_file():
                video.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------- web
PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#08090c">
<title>Clipper | Shadow Mode</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Anton&display=swap" rel="stylesheet">

<style>
:root{
 --yellow:#f5c400;
 --black:#08090c;
 --panel:#111318;
 --panel2:#191c22;
 --line:#30343c;
 --text:#f3f3ed;
 --muted:#92969f;
 --display:Anton,Impact,"Arial Narrow Bold",sans-serif;
}

*{box-sizing:border-box}

body{
 margin:0;
 min-height:100vh;
 color:var(--text);
 background:
 radial-gradient(ellipse at 50% 10%,#24252b 0%,transparent 45%),
 linear-gradient(180deg,#111319,#08090c 70%);
 font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
}

button,input{font:inherit}

button:focus-visible,
input:focus-visible,
a:focus-visible,
label:focus-within{
 outline:3px solid var(--yellow);
 outline-offset:4px;
}

.wrap{
 width:min(100% - 30px,780px);
 margin:auto;
 padding-bottom:50px;
}

/* HEADER */

.topbar{
 height:70px;
 display:flex;
 align-items:center;
 justify-content:space-between;
 border-bottom:1px solid #303139;
}

.brand{
 display:flex;
 align-items:center;
 gap:12px;
 font:27px var(--display);
 letter-spacing:2px;
}

.brand-icon{
 width:34px;
 height:34px;
 display:grid;
 place-items:center;
 background:var(--yellow);
 color:#08090c;
 clip-path:polygon(50% 0,100% 100%,0 100%);
 padding-top:8px;
 font-size:18px;
}

.local{
 display:flex;
 align-items:center;
 gap:8px;
 color:#a3a5a9;
 font-size:10px;
 letter-spacing:2px;
 text-transform:uppercase;
}

.dot{
 width:8px;
 height:8px;
 background:#a5db72;
 border-radius:50%;
 box-shadow:0 0 12px #a5db72;
}

/* HERO */

.hero{
 position:relative;
 min-height:380px;
 padding:57px 0 150px;
 overflow:hidden;
}

.hero::before{
 content:"";
 position:absolute;
 top:-130px;
 right:4%;
 width:350px;
 height:450px;
 background:linear-gradient(180deg,rgba(245,196,0,.19),transparent);
 clip-path:polygon(43% 0,57% 0,100% 100%,0 100%);
 transform:rotate(-12deg);
 pointer-events:none;
 animation:beam 6s ease-in-out infinite alternate;
}

.hero::after{
 content:"";
 position:absolute;
 bottom:0;
 width:100%;
 height:1px;
 background:linear-gradient(90deg,transparent,var(--yellow),transparent);
}

@keyframes beam{
 from{transform:rotate(-15deg);opacity:.5}
 to{transform:rotate(-5deg);opacity:1}
}

.eyebrow{
 position:relative;
 color:var(--yellow);
 font-size:10px;
 font-weight:800;
 letter-spacing:3px;
 text-transform:uppercase;
}

h1{
 position:relative;
 margin:22px 0 0;
 font:clamp(85px,18vw,140px)/.82 var(--display);
 letter-spacing:-2px;
 text-transform:uppercase;
}

h1 span{color:var(--yellow)}

.hero-desc{
 position:relative;
 max-width:400px;
 margin-top:25px;
 color:#a7a9af;
 font-size:14px;
 line-height:1.7;
}

.hero-line{
 position:relative;
 display:flex;
 align-items:center;
 gap:12px;
 margin-top:25px;
 color:#777b83;
 font-size:10px;
 letter-spacing:2px;
 text-transform:uppercase;
}

.hero-line i{
 flex:1;
 height:1px;
 background:#44464a;
}

/* CITY BACKGROUND */

.city{
 position:absolute;
 bottom:0;
 left:0;
 width:100%;
 height:130px;
 opacity:.9;
 pointer-events:none;
 z-index:0;
}

.eyebrow,h1,.hero-desc,.hero-line{z-index:1}

.spotlight{
 position:absolute;
 right:18%;
 top:0;
 width:2px;
 height:140px;
 background:linear-gradient(var(--yellow),transparent);
 box-shadow:0 0 22px rgba(245,196,0,.35);
 opacity:.5;
}

/* MAIN */

.section-label{
 display:flex;
 align-items:center;
 justify-content:space-between;
 margin:34px 0 15px;
}

.section-label h2{
 margin:0;
 font:28px var(--display);
 letter-spacing:1px;
 text-transform:uppercase;
}

.section-label small{
 color:#858994;
 font-size:10px;
 letter-spacing:2px;
}

.console{
 position:relative;
 padding:24px;
 background:linear-gradient(135deg,#191b21,#101216 75%);
 border:1px solid #353942;
 border-radius:4px;
 box-shadow:0 25px 70px rgba(0,0,0,.45);
}

.console::before{
 content:"";
 position:absolute;
 left:-1px;
 top:25px;
 width:3px;
 height:55px;
 background:var(--yellow);
}

/* UPLOAD */

.drop{
 display:block;
 position:relative;
 padding:34px 15px;
 text-align:center;
 background:
 linear-gradient(135deg,rgba(245,196,0,.07),transparent),
 #090b0f;
 border:1px dashed #62562c;
 cursor:pointer;
 transition:.2s;
}

.drop:hover{
 border-color:var(--yellow);
 background:#171710;
}

.drop input{
 position:absolute;
 inset:0;
 width:100%;
 height:100%;
 opacity:0;
 cursor:pointer;
}

.upload-icon{
 width:53px;
 height:53px;
 margin:0 auto 15px;
 display:grid;
 place-items:center;
 border:1px solid #685b2a;
 color:var(--yellow);
 font-size:30px;
}

.drop strong{
 display:block;
 font:25px var(--display);
 overflow-wrap:anywhere;
}

.drop span{
 display:block;
 margin-top:8px;
 color:#9699a1;
 font-size:12px;
}

.drop.has strong{color:var(--yellow)}

.upload-note{
 margin-top:12px;
 text-align:center;
 color:#777b83;
 font-size:10px;
 letter-spacing:2px;
}

/* FIELDS */

.field{margin-top:23px}

.field>span,
legend{
 display:block;
 margin-bottom:10px;
 color:#c0c1c4;
 font-size:12px;
 font-weight:700;
}

.pair{
 display:grid;
 grid-template-columns:1fr 1fr;
 gap:13px;
}

input[type=number],
input[type=text]{
 width:100%;
 min-width:0;
 padding:14px;
 color:white;
 background:#090b0f;
 border:1px solid #373b44;
 border-radius:3px;
 font-size:15px;
}

input:focus{border-color:var(--yellow)}

input::placeholder{color:#737780}

/* MODE */

fieldset{
 border:0;
 padding:0;
 margin:25px 0 0;
}

.seg{
 display:grid;
 grid-template-columns:1fr 1fr;
 gap:10px;
}

.seg label{position:relative}

.seg input{
 position:absolute;
 opacity:0;
}

.seg span{
 min-height:49px;
 display:grid;
 place-items:center;
 padding:10px;
 background:#090b0f;
 border:1px solid #383b43;
 color:#aaaeb6;
 font-size:12px;
 font-weight:800;
 cursor:pointer;
}

.seg input:checked+span{
 background:var(--yellow);
 color:#08090c;
 border-color:var(--yellow);
}

.seg input:focus-visible+span{
 outline:3px solid var(--yellow);
 outline-offset:3px;
}

/* SWITCHES */

.options{
 margin-top:24px;
 border-top:1px solid var(--line);
}

.sw{
 display:flex;
 justify-content:space-between;
 align-items:center;
 gap:15px;
 padding:16px 0;
 border-bottom:1px solid var(--line);
 cursor:pointer;
}

.sw strong{
 display:block;
 font-size:13px;
}

.sw small{
 display:block;
 margin-top:4px;
 color:#92969e;
 font-size:11px;
}

.sw input{
 appearance:none;
 -webkit-appearance:none;
 flex:none;
 width:46px;
 height:25px;
 border-radius:30px;
 background:#444750;
 position:relative;
 cursor:pointer;
}

.sw input::after{
 content:"";
 position:absolute;
 left:3px;
 top:3px;
 width:19px;
 height:19px;
 border-radius:50%;
 background:white;
 transition:transform .2s;
}

.sw input:checked{
 background:var(--yellow);
}

.sw input:checked::after{
 transform:translateX(21px);
 background:#08090c;
}

.sw input:focus-visible{
 outline:3px solid var(--yellow);
 outline-offset:4px;
}

/* BUTTON */

.go{
 width:100%;
 min-height:63px;
 margin-top:25px;
 border:0;
 border-radius:3px;
 background:var(--yellow);
 color:#08090c;
 font:27px var(--display);
 letter-spacing:1.5px;
 cursor:pointer;
 transition:background .2s;
}

.go:hover{background:#ffda25}

.go:disabled{
 opacity:.55;
 cursor:wait;
}

/* STATUS */

#status{
 margin-top:24px;
 padding:17px;
 background:#090b0f;
 border:1px solid #343943;
}

#status[hidden]{display:none}

.status-head{
 display:flex;
 justify-content:space-between;
 gap:10px;
 margin-bottom:12px;
}

.status-head strong{
 color:var(--yellow);
 font:20px var(--display);
 letter-spacing:1px;
}

.status-head small{
 color:#858994;
 font-size:10px;
}

#st{
 margin:0 0 13px;
 color:#d1d2d3;
 font-size:12px;
}

#st.err{color:#ff655b}

.bar{
 height:6px;
 overflow:hidden;
 background:#30333b;
}

#fill{
 display:block;
 width:0;
 height:100%;
 background:var(--yellow);
 box-shadow:0 0 15px var(--yellow);
 transition:width .5s;
}

/* RESULTS */

#res{margin-top:38px}

.results-head{
 display:flex;
 justify-content:space-between;
 align-items:end;
 padding-bottom:14px;
 border-bottom:1px solid #393c43;
}

.results-head h2{
 margin:0;
 font:39px var(--display);
 text-transform:uppercase;
}

.results-head h2 span{color:var(--yellow)}

.results-head small{
 color:#92969e;
 font-size:10px;
}

.clip{
 margin-top:17px;
 padding:13px;
 background:var(--panel);
 border:1px solid #353942;
 border-radius:4px;
}

.meta{
 display:flex;
 justify-content:space-between;
 align-items:center;
 gap:13px;
 padding:4px 4px 14px;
}

.meta b{
 color:var(--yellow);
 font:40px var(--display);
}

.meta strong{
 display:block;
 font-size:12px;
 text-transform:uppercase;
}

.meta span{
 display:block;
 margin-top:4px;
 color:#92969e;
 font-size:11px;
}

.clip video{
 display:block;
 width:100%;
 max-height:600px;
 background:#000;
 border-radius:3px;
}

.dl{
 display:flex;
 justify-content:center;
 align-items:center;
 min-height:49px;
 margin-top:11px;
 background:var(--yellow);
 color:#08090c;
 text-decoration:none;
 font-size:12px;
 font-weight:900;
 letter-spacing:1px;
 text-transform:uppercase;
}

/* FOOTER */

.footer{
 display:flex;
 justify-content:space-between;
 gap:10px;
 margin-top:45px;
 padding-top:18px;
 border-top:1px solid #30333a;
 color:#777b83;
 font-size:10px;
 letter-spacing:1px;
 text-transform:uppercase;
}

.footer strong{color:var(--yellow)}

@media(max-width:450px){
 .wrap{width:calc(100% - 24px)}
 .hero{min-height:320px;padding-top:45px}
 h1{font-size:88px}
 .console{padding:17px}
 .section-label h2{font-size:23px}
 .local{font-size:9px;letter-spacing:1px}
 .footer{font-size:8px}
}

@media(prefers-reduced-motion:reduce){
 *,*::before,*::after{
  animation:none!important;
  transition:none!important;
  scroll-behavior:auto!important;
 }
}
</style>
</head>

<body>

<div class="wrap">

<header class="topbar">
 <div class="brand">
  <div class="brand-icon">C</div>
  CLIPPER
 </div>
 <div class="local">
  <span class="dot"></span>
  sistema activo
 </div>
</header>

<section class="hero">

 <div class="eyebrow">shadow mode · sistema audiovisual</div>

 <h1>Shadow<br><span>Clips.</span></h1>

 <p class="hero-desc">
  La ciudad nunca duerme. Los mejores momentos tampoco.
  Detecta, corta y transforma tus videos en clips verticales.
 </p>

 <div class="hero-line">
  <span>detectar</span>
  <i></i>
  <span>recortar</span>
  <i></i>
  <span>descargar</span>
 </div>

 <div class="spotlight"></div>
 <svg class="city" id="city" viewBox="0 0 760 130" preserveAspectRatio="none" aria-hidden="true"></svg>

</section>

<div class="section-label">
 <h2>Centro de control</h2>
 <small>01 / configuración</small>
</div>

<main class="console">

 <label class="drop" id="drop">

  <input type="file" id="f" accept="video/*">

  <div class="upload-icon">↑</div>

  <strong id="dname">Selecciona tu video</strong>

  <span id="dhint">Toca para buscar un archivo</span>

 </label>

 <div class="upload-note">mp4 · mov · mkv · webm · avi</div>

 <div class="pair field">

  <label>
   <span>Cantidad de clips</span>
   <input type="number" id="n" value="5" min="1" max="20" inputmode="numeric">
  </label>

  <label>
   <span>Duración por clip</span>
   <input type="number" id="len" value="30" min="5" max="90" inputmode="numeric">
  </label>

 </div>

 <fieldset>
  <legend>Formato vertical</legend>

  <div class="seg">

   <label>
    <input type="radio" name="mode" value="blur" checked>
    <span>Fondo borroso</span>
   </label>

   <label>
    <input type="radio" name="mode" value="crop">
    <span>Recortar lados</span>
   </label>

  </div>
 </fieldset>

 <div class="options">

  <label class="sw">
   <div>
    <strong>Zoom dinámico</strong>
    <small>Acercamiento de cámara</small>
   </div>
   <input type="checkbox" id="zoom" checked>
  </label>

  <label class="sw">
   <div>
    <strong>Vibración de cámara</strong>
    <small>Movimiento cinematográfico</small>
   </div>
   <input type="checkbox" id="shake">
  </label>

 </div>

 <label class="field" style="display:block">
  <span>Texto gancho</span>
  <input type="text" id="hook" maxlength="60" placeholder="Escribe una frase que atrape...">
 </label>

 <button class="go" id="go" type="button">
  Encontrar momentos →
 </button>

 <div id="status" hidden aria-live="polite">

  <div class="status-head">
   <strong>SHADOW ENGINE</strong>
   <small>procesamiento local</small>
  </div>

  <p id="st"></p>

  <div class="bar">
   <i id="fill"></i>
  </div>

 </div>

</main>

<section id="res"></section>

<footer class="footer">
 <span><strong>Clipper</strong> / shadow mode</span>
 <span>formato 9:16</span>
</footer>

</div>

<script>
const $=id=>document.getElementById(id);

/* avisa al programa que la ventana sigue abierta */
function beat(){fetch("/ping").catch(()=>{})}
beat();setInterval(beat,4000);

/* CIUDAD NOCTURNA */

(function(){

 let seed=19;

 const rand=()=>{
  seed=(seed*9301+49297)%233280;
  return seed/233280;
 };

 let x=0;
 let out="";
 const H=130;
 const W=760;

 while(x<W){

  const w=17+Math.floor(rand()*30);
  const h=25+Math.floor(rand()*95);

  out+='<rect x="'+x+'" y="'+(H-h)+'" width="'+w+'" height="'+h+'" fill="#050609"/>';

  for(let wy=H-h+9;wy<H-8;wy+=12){

   for(let wx=x+5;wx<x+w-4;wx+=10){

    if(rand()>.8){

     out+='<rect x="'+wx+'" y="'+wy+'" width="3" height="4" fill="#f5c400" opacity="'+(.2+rand()*.5)+'"/>';

    }

   }

  }

  x+=w+Math.floor(rand()*5);

 }

 out+='<rect x="565" y="18" width="3" height="112" fill="#f5c400" opacity=".7"/>';
 out+='<circle cx="566" cy="15" r="5" fill="#f5c400"/>';

 $("city").innerHTML=out;

})();

/* ARCHIVOS */

const drop=$("drop");

function showFile(){

 const f=$("f").files[0];

 $("dname").textContent=f?f.name:"Selecciona tu video";

 $("dhint").textContent=f
  ?Math.round(f.size/1048576)+" MB · Archivo seleccionado"
  :"Toca para buscar un archivo";

 drop.classList.toggle("has",!!f);

}

$("f").onchange=showFile;

["dragenter","dragover"].forEach(t=>{
 drop.addEventListener(t,e=>{
  e.preventDefault();
  drop.style.borderColor="#f5c400";
 });
});

["dragleave","drop"].forEach(t=>{
 drop.addEventListener(t,e=>{
  e.preventDefault();
  drop.style.borderColor="";
 });
});

drop.addEventListener("drop",e=>{

 const f=e.dataTransfer.files[0];

 if(f){

  const dt=new DataTransfer();
  dt.items.add(f);
  $("f").files=dt.files;

  showFile();

 }

});

/* ESTADO */

function say(msg,err){

 $("status").hidden=false;
 $("st").textContent=msg;
 $("st").className=err?"err":"";

}

function bar(p){
 $("fill").style.width=p+"%";
}

function busy(on){
 $("go").disabled=on;
}

function progress(msg){

 const k=msg.match(/clip ([0-9]+) de ([0-9]+)/i);

 if(k){
  return 25+70*(Number(k[1])-1)/Number(k[2]);
 }

 if(msg.indexOf("Buscando")===0)return 20;
 if(msg.indexOf("Extrayendo")===0)return 10;

 return 6;

}

/* RESULTADOS */

function renderClips(id,clips){

 const res=$("res");

 res.innerHTML="";

 if(!clips.length)return;

 const header=document.createElement("div");
 header.className="results-head";

 const heading=document.createElement("h2");
 heading.innerHTML='Tus <span>clips.</span>';

 const count=document.createElement("small");
 count.textContent=clips.length+" momentos encontrados";

 header.append(heading,count);
 res.append(header);

 clips.forEach((c,i)=>{

  const article=document.createElement("article");
  article.className="clip";

  const meta=document.createElement("div");
  meta.className="meta";

  const number=document.createElement("b");
  number.textContent=String(i+1).padStart(2,"0");

  const info=document.createElement("div");

  const title=document.createElement("strong");
  title.textContent="Momento destacado";

  const time=document.createElement("span");
  time.textContent="Inicio: "+c.start;

  info.append(title,time);
  meta.append(number,info);

  const video=document.createElement("video");
  video.controls=true;
  video.playsInline=true;
  video.preload="metadata";
  video.src="/clips/"+id+"/"+encodeURIComponent(c.name);

  const link=document.createElement("a");
  link.className="dl";
  link.href="/clips/"+id+"/"+encodeURIComponent(c.name);
  link.download=c.name;
  link.textContent="↓ Descargar clip "+(i+1);

  link.addEventListener("click",async e=>{
   if(window.pywebview&&window.pywebview.api&&window.pywebview.api.save_clip){
    e.preventDefault();
    const r=await window.pywebview.api.save_clip(id,c.name);
    if(r&&r.ok){say("Clip guardado en "+r.path)}
    else if(r&&!r.cancel){say("No se pudo guardar el clip.",true)}
   }
  });

  article.append(meta,video,link);
  res.append(article);

 });

}

/* SUBIR VIDEO */

$("go").onclick=async()=>{

 const f=$("f").files[0];

 if(!f){
  say("Selecciona un video antes de continuar.",true);
  return;
 }

 const n=$("n").value;
 const len=$("len").value;

 if(!Number.isInteger(Number(n))||Number(n)<1||Number(n)>20||
    !Number.isInteger(Number(len))||Number(len)<5||Number(len)>90){

  say("Revisa la cantidad de clips y su duración.",true);
  return;

 }

 busy(true);

 shown=0;
 $("res").innerHTML="";
 bar(4);
 say("Subiendo el video...");

 const q=new URLSearchParams({
  n:n,
  len:len,
  mode:document.querySelector('input[name="mode"]:checked').value,
  zoom:$("zoom").checked?1:0,
  shake:$("shake").checked?1:0,
  hook:$("hook").value,
  name:f.name
 });

 try{

  const r=await fetch("/upload?"+q,{
   method:"POST",
   body:f
  });

  const j=await r.json();

  if(!r.ok||j.error){
   throw new Error(j.error||"Error al subir el video");
  }

  if(!j.id)throw new Error("No se recibió el identificador del trabajo");

  poll(j.id);

 }catch(e){

  say("Error al subir el video: "+e.message,true);
  busy(false);

 }

};

/* PROCESAMIENTO */

let shown=0;

async function poll(id){

 let j;

 try{

  const r=await fetch("/status?id="+encodeURIComponent(id));

  if(!r.ok)throw new Error("Conexión perdida");

  j=await r.json();

 }catch(e){

  say("No se pudo conectar con el servidor local.",true);
  busy(false);
  return;

 }

 if(j.state==="error"){

  say(j.msg||"Error al procesar el video.",true);
  busy(false);
  return;

 }

 const clips=Array.isArray(j.clips)?j.clips:[];

 if(clips.length&&clips.length!==shown){

  shown=clips.length;
  renderClips(id,clips);

 }

 if(j.state==="working"){

  say(j.msg||"Procesando...");
  bar(Math.min(95,Math.max(10,progress(j.msg||""))));

 }

 if(j.state==="queued"){

  say(j.msg||"Esperando turno de procesamiento...");
  bar(5);

 }

 if(j.state==="done"){

  bar(100);
  say("Proceso terminado. Tus clips están listos.");

  if(clips.length){
   renderClips(id,clips);
  }

  busy(false);

  $("res").scrollIntoView({
   behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",
   block:"start"
  });

  return;

 }

 setTimeout(()=>poll(id),800);

}
</script>

</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/ping":
            LAST_PING[0] = time.time()
            self.send_response(204)
            self.end_headers()
        elif u.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif u.path == "/status":
            jid = parse_qs(u.query).get("id", [""])[0]
            job = JOBS.get(jid)
            self._json(job if job else {"state": "error", "msg": "Trabajo no encontrado", "clips": []})
        elif u.path.startswith("/clips/"):
            parts = unquote(u.path).split("/")
            if len(parts) != 4 or jid_bad(parts[2]) or "/" in parts[3] or ".." in parts[3]:
                self.send_error(404)
                return
            f = WORK / parts[2] / parts[3]
            if not f.is_file() or f.suffix != ".mp4":
                self.send_error(404)
                return
            size = f.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(f, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/upload":
            self.send_error(404)
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            opts = {
                "n": max(1, min(20, int(q.get("n", 5)))),
                "len": max(5, min(90, int(q.get("len", 30)))),
                "mode": q.get("mode", "blur") if q.get("mode") in ("blur", "crop") else "blur",
                "zoom": q.get("zoom") == "1",
                "shake": q.get("shake") == "1",
                "hook": q.get("hook", "")[:60],
            }
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json({"error": "Opciones invalidas"}, 400)
            return
        if length <= 0:
            self._json({"error": "No llego ningun video"}, 400)
            return
        if length > MAX_UPLOAD:
            self._json({"error": f"Video demasiado grande (máx. {MAX_UPLOAD // (1024**3)} GB)"}, 400)
            return
        job_id = uuid.uuid4().hex[:10]
        jdir = WORK / job_id
        jdir.mkdir(parents=True, exist_ok=True)
        ext = Path(q.get("name", "video.mp4")).suffix.lower()
        if ext not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
            ext = ".mp4"
        video = jdir / f"input{ext}"
        remaining = length
        with open(video, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        JOBS[job_id] = {"state": "queued", "msg": "En cola...", "clips": []}
        threading.Thread(target=process_job, args=(job_id, video, opts), daemon=True).start()
        self._json({"id": job_id})


def jid_bad(s):
    return not s.isalnum()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "tu-ip-local"


def cleanup_old_jobs():
    """Borra jobs más antiguos de JOB_MAX_AGE_SEC para no llenar el disco."""
    import time
    now = time.time()
    if not WORK.is_dir():
        return
    for d in list(WORK.iterdir()):
        if not d.is_dir() or d.name == "perfil":
            continue
        try:
            age = now - d.stat().st_mtime
            if age > JOB_MAX_AGE_SEC:
                shutil.rmtree(d, ignore_errors=True)
                JOBS.pop(d.name, None)
        except OSError:
            pass


def msg_box(text):
    """Aviso visible: el .exe no tiene consola donde mostrar errores."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "Clipper", 0x10)
            return
    except Exception:  # noqa: BLE001
        pass
    print(text)


def find_app_browser():
    """Busca Edge, Chrome o Brave para abrir Clipper como ventana de app."""
    cands = []
    if os.name == "nt":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe"]
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
                 "--window-size=820,960", "--no-first-run", "--no-default-browser-check"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"  # instalador oficial de Microsoft
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def resource_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def ask_box(text):
    """Pregunta Si/No en una ventana de Windows. Fuera de Windows devuelve False."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, text, "Clipper", 0x24) == 6  # Si = 6
    except Exception:  # noqa: BLE001
        return False


def webview2_ready():
    """True si el motor WebView2 de Windows esta instalado."""
    if os.name != "nt":
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
    import urllib.request
    dest = Path(tempfile.gettempdir()) / "MicrosoftEdgeWebview2Setup.exe"
    with urllib.request.urlopen(WEBVIEW2_URL, timeout=30) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if dest.stat().st_size < 500000 or dest.read_bytes()[:2] != b"MZ":
        return False
    subprocess.run([str(dest), "/silent", "/install"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=0x08000000 if os.name == "nt" else 0, timeout=600)
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


def run_native_window(url):
    """Abre Clipper en su propia ventana (motor WebView2), sin navegador.
    Devuelve True si se uso y se cerro; False si no se pudo (usa el respaldo)."""
    try:
        import webview
    except Exception:  # noqa: BLE001
        return False
    try:
        if os.name == "nt" and not webview2_ready():
            if not ask_box("Clipper necesita el componente WebView2 de Microsoft para abrir su "
                           "ventana (descarga de unos 2 MB). Quieres instalarlo ahora?"):
                return False
            if not install_webview2():
                msg_box("No se pudo instalar WebView2. Clipper se abrira en una ventana del "
                        "navegador. Puedes instalarlo a mano desde "
                        "developer.microsoft.com/microsoft-edge/webview2/consumer")
                return False
        api = Api()
        win = webview.create_window("Clipper | Shadow Mode", url, js_api=api, width=900,
                                    height=980, min_size=(420, 600), background_color="#08090c")
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


def watchdog(srv):
    """Cierra el programa cuando cierras la ventana (y no hay un video procesandose)."""
    started = time.time()
    while True:
        time.sleep(3)
        if any(j["state"] in ("queued", "working") for j in JOBS.values()):
            continue
        if LAST_PING[0]:
            if time.time() - LAST_PING[0] > IDLE_LIMIT:
                break
        elif time.time() - started > 90:   # la ventana nunca llego a abrirse
            break
    srv.shutdown()


def main():
    global FFMPEG
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--lan", action="store_true",
                   help="Modo servidor: sin ventana, accesible desde otro dispositivo de tu wifi")
    args = p.parse_args()
    # Si ffmpeg.exe esta junto al programa, se usa ese (no hace falta instalarlo)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    os.environ["PATH"] = str(base) + os.pathsep + os.environ.get("PATH", "")
    # Preferir la ruta absoluta si el binario esta al lado del .exe / script
    local_ff = base / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
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
        return
    # Respaldo: ventana de navegador en modo app
    threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
    open_app_window(url)
    server_thread.join()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        msg_box("Clipper tuvo un error: " + str(e))
