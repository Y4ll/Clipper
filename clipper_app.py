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
<title>Clipper</title><style>
body{font-family:system-ui,sans-serif;background:#0f1115;color:#eee;margin:0;padding:18px;max-width:560px;margin:auto}
h1{font-size:22px}label{display:block;margin:12px 0 4px;font-size:14px;color:#aab}
input,select{width:100%;padding:10px;border-radius:8px;border:1px solid #333;background:#1a1d24;color:#eee;box-sizing:border-box;font-size:16px}
.row{display:flex;gap:10px}.row>div{flex:1}.chk{display:flex;align-items:center;gap:8px;margin:10px 0}
.chk input{width:auto}button{width:100%;margin-top:18px;padding:14px;border:0;border-radius:10px;background:#ff5a36;color:#fff;font-size:17px;font-weight:600}
button:disabled{opacity:.5}#st{margin-top:14px;color:#9fd}.clip{margin-top:16px;background:#1a1d24;padding:10px;border-radius:10px}
video{width:100%;border-radius:8px;max-height:420px}a{color:#ff8a6b}
</style></head><body>
<h1>Clipper &middot; clips verticales</h1>
<label>Video</label><input type="file" id="f" accept="video/*">
<div class="row"><div><label>Cuantos clips</label><input type="number" id="n" value="5" min="1" max="20"></div>
<div><label>Segundos por clip</label><input type="number" id="len" value="30" min="5" max="90"></div></div>
<label>Formato</label><select id="mode"><option value="blur">Fondo borroso (gameplay)</option><option value="crop">Recortar lados (llena pantalla)</option></select>
<div class="chk"><input type="checkbox" id="zoom" checked><span>Zoom lento (push-in)</span></div>
<div class="chk"><input type="checkbox" id="shake"><span>Vibracion de camara</span></div>
<label>Texto gancho (primeros 3 seg, opcional)</label><input id="hook" placeholder="GTA 6 SALE EN 14 DIAS">
<button id="go">Sacar clips</button><div id="st"></div><div id="res"></div>
<script>
const $=id=>document.getElementById(id);
$("go").onclick=async()=>{
 const f=$("f").files[0]; if(!f){$("st").textContent="Elige un video primero";return}
 $("go").disabled=true;$("res").innerHTML="";$("st").textContent="Subiendo video...";
 const q=new URLSearchParams({n:$("n").value,len:$("len").value,mode:$("mode").value,
  zoom:$("zoom").checked?1:0,shake:$("shake").checked?1:0,hook:$("hook").value,name:f.name});
 try{
  const r=await fetch("/upload?"+q,{method:"POST",body:f}); const j=await r.json();
  if(j.error){throw new Error(j.error)}
  poll(j.id);
 }catch(e){$("st").textContent="Error: "+e.message;$("go").disabled=false}
};
async function poll(id){
 const j=await (await fetch("/status?id="+id)).json();
 $("st").textContent=j.msg;
 $("res").innerHTML=j.clips.map(c=>`<div class="clip"><video controls playsinline src="/clips/${id}/${c.name}"></video>
  <p>Desde ${c.start} &middot; <a href="/clips/${id}/${c.name}" download>Descargar ${c.name}</a></p></div>`).join("");
 if(j.state==="working"||j.state==="queued"){setTimeout(()=>poll(id),1500)}else{$("go").disabled=false}
}
</script></body></html>"""


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
