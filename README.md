# Clipper

App de Windows para sacar clips verticales (9:16) de videos y streams largos. Detecta los mejores momentos, los pasa a vertical y les pone efectos, textos y subtítulos. Gratis, sin marca de agua, todo se procesa en tu PC.

## Instalar (Windows)
1. Entra a **Releases** → **Clipper para Windows**.
2. Baja **ClipperSetup.exe** y ábrelo (crea el acceso directo y el desinstalador). Si prefieres no instalar, baja **Clipper-Windows.zip**, descomprímelo y abre `Clipper.exe`.
3. Ya trae ffmpeg, la descarga por link (yt-dlp) y los subtítulos (Whisper). No necesitas instalar nada más.

**Si Windows bloquea Clipper** («Windows protegió su PC»): clic en **Más información → Ejecutar de todas formas**, o añade una exclusión en Seguridad de Windows. Pasa porque el programa no tiene firma digital (es de pago).

Requisitos: Windows 10/11 de 64 bits, 4 GB de RAM (8 GB mejor). Clipper abre su ventana con WebView2 de Microsoft; si no lo tienes, te pregunta si lo instala. Si falla, se abre en una ventana de Chrome, Edge o Brave.

## Cómo se usa
1. **Tu video:** arrastra un archivo (MP4, MOV, MKV…), arrastra un video encima de `Clipper.exe`, o pega un link (YouTube, Kick…). Usa solo videos tuyos o con permiso de su dueño.
2. **Solo un tramo de un stream largo:** en «Solo los últimos (minutos)» o «desde/hasta el minuto» baja solo esa parte, mucho más rápido que un VOD de 4 horas.
3. **Formato:** TikTok, Reels o Shorts; fondo borroso, recorte lleno o pantalla dividida (necesita un video de relleno tuyo); zoom lento y vibración.
4. **Textos, subtítulos y logo** (panel «Textos, subtítulos y logo»): hasta 4 textos con tiempo, subtítulos automáticos sin internet y un logo en una esquina.
5. **Revisar:** ves la gráfica de volumen con los momentos marcados; los arrastras, quitas o añades, y le das a **Cortar clips**.
6. **Descargar:** cada clip tiene su botón; o activa «Guardar los clips solos en una carpeta».

### Ajustes útiles (Más ajustes)
- **Velocidad:** *Rápido* (menos calidad, mucho más veloz) o *Mejor calidad*.
- **Tarjeta de video:** si tienes NVIDIA, Intel o AMD, la usa sola; arriba a la derecha ves si está activa. Si falla, usa el procesador.
- **Selección inteligente:** además del volumen, escucha lo que se dice para elegir mejor. Tarda más en analizar.
- **Cola:** puedes soltar varios videos; se procesan uno tras otro.

### Dónde se guardan tus cosas
Ajustes, logo y video de relleno: `%APPDATA%\Clipper`. Los clips y videos temporales se borran al cerrar la app.

## Actualizaciones
Clipper avisa cuando hay una versión nueva y la instala con un clic.

## Con Python (opcional)
Necesitas Python 3 y ffmpeg: `python clipper_app.py` (opcional `--lan` para usarlo desde el celular en la misma wifi; mientras esté abierto, cualquiera en tu wifi puede entrar).
