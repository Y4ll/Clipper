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
import uuid
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SAMPLE_RATE = 4000
WORK = Path(tempfile.gettempdir()) / "clipper_app_jobs"
JOBS = {}  # id -> dict(state, msg, clips)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


# ---------------------------------------------------------------- video logic
def run_ffmpeg(cmd, cwd=None):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
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
    if has_hook:
        font_part = f"fontfile='{ff_escape(font)}':" if font else ""
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

        chain, last = build_filter(opts["mode"], opts["len"], opts["zoom"],
                                   opts["shake"], bool(hook), font)
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


# ----------------------------------------------------------------------- web
PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07080a">
<title>Clipper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Anton&display=swap" rel="stylesheet">
<style>
:root{--night:#07080a;--concrete:#12141a;--steel:#2a2e36;--fog:#9aa1ac;--ink:#eef0f3;--signal:#f5c400;--alert:#ff6b5e;
 --display:Anton,Impact,Haettenschweiler,"Arial Narrow Bold","Arial Narrow",sans-serif}
*{box-sizing:border-box}
html{background:var(--night)}
body{margin:0;min-height:100vh;color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 background:radial-gradient(120% 50% at 50% 0,#171a21 0,var(--night) 72%)}
.wrap{max-width:560px;margin:0 auto;padding:0 18px 56px}
button,input,select{font:inherit;color:inherit}

/* ---- hero: the skyline is the waveform, the beam is the detector ---- */
.sky{position:relative;height:290px;overflow:hidden;margin:0 -18px;background:linear-gradient(to top,#2a2f3a 0,#171a21 38%,transparent 78%)}
.sky h1{position:relative;z-index:2;margin:0;padding:36px 18px 0;font:400 76px/.92 var(--display);letter-spacing:.015em;text-transform:uppercase}
.sky p{position:relative;z-index:2;margin:12px 18px 0;max-width:30ch;color:var(--fog)}
#city{position:absolute;left:0;bottom:0;width:100%;height:96px;z-index:1}
.beam{position:absolute;z-index:0;left:72%;margin-left:-170px;bottom:70px;width:340px;height:320px;
 transform-origin:50% 100%;transform:rotate(-4deg);
 clip-path:polygon(47% 100%,53% 100%,100% 0,0 0);
 background:linear-gradient(to top,rgba(245,196,0,.62),rgba(245,196,0,.10) 70%,rgba(245,196,0,0));
 animation:sweep 2.6s cubic-bezier(.3,.7,.2,1) both}
@keyframes sweep{0%{transform:rotate(-40deg);opacity:0}25%{opacity:1}60%{transform:rotate(14deg)}100%{transform:rotate(-4deg);opacity:1}}
.busy .beam{animation:scan 3s ease-in-out infinite alternate}
@keyframes scan{from{transform:rotate(-18deg)}to{transform:rotate(16deg)}}

/* ---- console ---- */
.console{background:var(--concrete);border:1px solid var(--steel);border-radius:6px;padding:18px;margin-top:-6px;position:relative;z-index:3}
.drop{display:block;cursor:pointer;border:1.5px dashed #4a505b;border-radius:4px;padding:26px 16px;text-align:left;transition:border-color .15s,background .15s}
.drop strong{display:block;font:400 26px/1.1 var(--display);letter-spacing:.03em;word-break:break-all}
.drop span{color:var(--fog);font-size:14px}
.drop:hover,.drop.over{border-color:var(--signal);background:rgba(245,196,0,.06)}
.drop.has strong{color:var(--signal)}
:focus-visible{outline:2px solid var(--signal);outline-offset:3px}
.field{margin-top:18px}
.field>span,legend{display:block;margin:0 0 6px;padding:0;font-size:14px;color:var(--fog)}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}
input[type=number],input[type=text]{width:100%;padding:12px;background:var(--night);border:1px solid var(--steel);border-radius:4px;font-size:17px}
input[type=number]:focus,input[type=text]:focus{border-color:var(--signal);outline:none;box-shadow:0 0 0 3px rgba(245,196,0,.18)}
fieldset{border:0;margin:18px 0 0;padding:0}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.seg label{position:relative}
.seg input{position:absolute;opacity:0;inset:0}
.seg span{display:block;padding:12px 10px;border:1px solid var(--steel);border-radius:4px;text-align:center;background:var(--night);cursor:pointer;transition:background .15s,color .15s}
.seg input:checked+span{background:var(--signal);border-color:var(--signal);color:#0b0b0b;font-weight:700}
.seg input:focus-visible+span{outline:2px solid var(--signal);outline-offset:3px}
.sw{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--steel);cursor:pointer}
.sw:first-of-type{margin-top:12px}
.sw small{display:block;color:var(--fog);font-size:13px;line-height:1.3}
.sw input{appearance:none;-webkit-appearance:none;flex:none;width:46px;height:27px;border-radius:14px;background:var(--steel);position:relative;transition:background .2s;margin:0;cursor:pointer}
.sw input::after{content:"";position:absolute;top:3px;left:3px;width:21px;height:21px;border-radius:50%;background:#cfd3da;transition:transform .2s,background .2s}
.sw input:checked{background:var(--signal)}
.sw input:checked::after{transform:translateX(19px);background:#0b0b0b}
.go{position:relative;display:block;width:100%;margin-top:20px;padding:17px 16px 21px;border:0;border-radius:4px;background:var(--signal);color:#0b0b0b;
 font:400 25px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;cursor:pointer;overflow:hidden}
.go::after{content:"";position:absolute;left:0;right:0;bottom:0;height:6px;background:repeating-linear-gradient(-45deg,#0b0b0b 0 8px,transparent 8px 16px)}
.go:active{transform:translateY(1px)}
.go:disabled{opacity:.55;cursor:progress}

/* ---- status + results ---- */
#status{margin-top:20px}
#st{margin:0 0 8px;color:var(--ink)}
#st.err{color:var(--alert)}
.bar{height:4px;background:var(--steel);border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--signal);transition:width .7s}
#res h2{margin:34px 0 4px;font:400 34px/1 var(--display);letter-spacing:.03em;text-transform:uppercase}
.clip{margin-top:18px;background:var(--concrete);border:1px solid var(--steel);border-radius:6px;overflow:hidden}
.meta{display:flex;align-items:baseline;gap:12px;padding:12px 16px 4px}
.meta b{font:400 44px/1 var(--display);color:var(--signal)}
.meta span{color:var(--fog)}
.clip video{display:block;width:100%;max-height:480px;background:#000}
.dl{display:block;padding:14px 16px;text-align:center;font-weight:700;color:var(--signal);text-decoration:none;border-top:1px solid var(--steel)}
.dl:hover{background:rgba(245,196,0,.08)}
@media (prefers-reduced-motion:reduce){.beam,.busy .beam{animation:none;opacity:1;transform:rotate(-4deg)}.bar i,.drop,.seg span,.sw input,.sw input::after{transition:none}}
@media (max-width:380px){.sky h1{font-size:60px}}
</style></head><body class="">
<div class="wrap">
<header class="sky">
 <div class="beam" aria-hidden="true"></div>
 <h1>Clipper</h1>
 <p>Encuentra los mejores momentos de tu video y conviértelos en clips verticales.</p>
 <svg id="city" viewBox="0 0 560 96" preserveAspectRatio="none" aria-hidden="true"></svg>
</header>

<main class="console">
 <label class="drop" id="drop" tabindex="0">
  <input type="file" id="f" accept="video/*" hidden>
  <strong id="dname">Elige tu video</strong>
  <span id="dhint">Toca aquí o suéltalo en esta zona</span>
 </label>

 <div class="pair field">
  <label><span>Cantidad de clips</span><input type="number" id="n" value="5" min="1" max="20" inputmode="numeric"></label>
  <label><span>Segundos por clip</span><input type="number" id="len" value="30" min="5" max="90" inputmode="numeric"></label>
 </div>

 <fieldset>
  <legend>Formato vertical</legend>
  <div class="seg">
   <label><input type="radio" name="mode" value="blur" checked><span>Fondo borroso</span></label>
   <label><input type="radio" name="mode" value="crop"><span>Recortar lados</span></label>
  </div>
 </fieldset>

 <label class="sw"><div>Zoom lento<small>La imagen se acerca poco a poco</small></div><input type="checkbox" id="zoom" checked></label>
 <label class="sw"><div>Vibración de cámara<small>Sacudida suave durante todo el clip</small></div><input type="checkbox" id="shake"></label>

 <label class="field" style="display:block"><span style="display:block;margin-bottom:6px;font-size:14px;color:var(--fog)">Texto gancho (primeros 3 segundos)</span>
  <input type="text" id="hook" maxlength="60" placeholder="GTA 6 sale en 14 días"></label>

 <button class="go" id="go" type="button">Sacar clips</button>

 <div id="status" hidden aria-live="polite"><p id="st"></p><div class="bar"><i id="fill"></i></div></div>
</main>

<section id="res"></section>
</div>

<script>
const $=id=>document.getElementById(id);

/* skyline: deterministic, so it looks the same every time */
(function(){
 let s=11;const r=()=>(s=(s*9301+49297)%233280)/233280;
 const W=560,H=96,lamp=403;let x=0,out="";
 while(x<W){
  const w=14+Math.floor(r()*22);let h=22+Math.floor(r()*62);
  if(x<=lamp&&x+w>lamp){h=70}
  out+='<rect x="'+x+'" y="'+(H-h)+'" width="'+w+'" height="'+h+'" fill="#050607"/>';
  for(let wy=H-h+6;wy<H-6;wy+=9){for(let wx=x+4;wx<x+w-4;wx+=7){
   if(r()>0.87)out+='<rect x="'+wx+'" y="'+wy+'" width="3" height="4" fill="#f5c400" opacity="'+(0.25+r()*0.5).toFixed(2)+'"/>';}}
  x+=w+Math.floor(r()*3);
 }
 out+='<rect x="'+(lamp-6)+'" y="'+(H-73)+'" width="12" height="3" fill="#f5c400"/>';
 $("city").innerHTML=out;
})();

/* video picker */
const drop=$("drop");
function showFile(){
 const f=$("f").files[0];
 $("dname").textContent=f?f.name:"Elige tu video";
 $("dhint").textContent=f?(Math.round(f.size/1048576)+" MB. Toca para cambiarlo"):"Toca aquí o suéltalo en esta zona";
 drop.classList.toggle("has",!!f);
}
$("f").onchange=showFile;
drop.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();$("f").click()}});
["dragenter","dragover"].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.add("over")}));
["dragleave","drop"].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.remove("over")}));
drop.addEventListener("drop",e=>{
 const f=e.dataTransfer.files[0];
 if(f){const dt=new DataTransfer();dt.items.add(f);$("f").files=dt.files;showFile()}
});

/* status helpers */
let shown=0;
function say(msg,err){$("status").hidden=false;$("st").textContent=msg;$("st").className=err?"err":""}
function bar(p){$("fill").style.width=p+"%"}
function busy(on){document.body.classList.toggle("busy",on);$("go").disabled=on}
function pct(m){
 const k=m.match(/clip ([0-9]+) de ([0-9]+)/);
 if(k)return 25+70*(Number(k[1])-1)/Number(k[2]);
 if(m.indexOf("Buscando")===0)return 20;
 if(m.indexOf("Extrayendo")===0)return 10;
 return 6;
}

$("go").onclick=async()=>{
 const f=$("f").files[0];
 if(!f){say("Elige un video antes de sacar los clips.",true);return}
 busy(true);shown=0;$("res").innerHTML="";bar(4);say("Subiendo el video…");
 const q=new URLSearchParams({
  n:$("n").value,len:$("len").value,
  mode:document.querySelector("input[name=mode]:checked").value,
  zoom:$("zoom").checked?1:0,shake:$("shake").checked?1:0,
  hook:$("hook").value,name:f.name});
 try{
  const r=await fetch("/upload?"+q,{method:"POST",body:f});
  const j=await r.json();
  if(j.error)throw new Error(j.error);
  poll(j.id);
 }catch(e){
  say("No se pudo subir el video. Revisa que Clipper siga abierto y vuelve a intentarlo. ("+e.message+")",true);
  busy(false);
 }
};

async function poll(id){
 let j;
 try{j=await (await fetch("/status?id="+id)).json()}
 catch(e){say("Se perdió la conexión con Clipper. Ábrelo de nuevo y repite el proceso.",true);busy(false);return}
 if(j.state==="error"){say("No se pudo procesar el video: "+String(j.msg).slice(-220),true);busy(false);return}
 say(j.msg==="Listo"?"Listo. Tus clips están abajo.":j.msg);
 bar(j.state==="done"?100:pct(j.msg));
 if(j.clips.length&&j.clips.length!==shown){
  shown=j.clips.length;
  $("res").innerHTML="<h2>Tus clips</h2>"+j.clips.map((c,i)=>
   '<article class="clip"><div class="meta"><b>'+(i+1)+'</b><span>Empieza en el '+c.start+' del video original</span></div>'+
   '<video controls playsinline preload="metadata" src="/clips/'+id+'/'+c.name+'#t=0.1"></video>'+
   '<a class="dl" href="/clips/'+id+'/'+c.name+'" download>Descargar clip '+(i+1)+'</a></article>').join("");
 }
 if(j.state==="done"){busy(false)}else{setTimeout(()=>poll(id),1500)}
}
</script></body></html>
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--lan", action="store_true",
                   help="Permite entrar desde otro dispositivo de tu wifi (ej. iPhone)")
    args = p.parse_args()
    # Si ffmpeg.exe esta junto al programa, se usa ese (no hace falta instalarlo)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    os.environ["PATH"] = str(base) + os.pathsep + os.environ.get("PATH", "")
    if not shutil.which("ffmpeg"):
        sys.exit("No encuentro ffmpeg. Pon ffmpeg.exe en la misma carpeta que este programa.")
    WORK.mkdir(parents=True, exist_ok=True)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, args.port), Handler)
    print(f"Abre en la laptop: http://127.0.0.1:{args.port}")
    if args.lan:
        print(f"Desde el iPhone (misma wifi): http://{lan_ip()}:{args.port}")
        print("Aviso: cualquiera en tu wifi puede entrar mientras esto este abierto.")
    print("Ctrl+C para cerrar.")
    if not args.lan:
        try:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")


if __name__ == "__main__":
    main()
