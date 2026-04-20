# suno-cli

CLI para generar canciones en Suno AI v5.5 desde terminal, sin interfaz web.

---

## Setup inicial

```bat
setup.bat
```

Instala dependencias Python y verifica que todo esté listo.

---

## Login (primera vez y cada pocas semanas)

```bat
suno-login.exe
```

Abre una ventana con suno.com. Loguéate normalmente. Al detectar el login, guarda la sesión y se cierra solo. No necesita volver a abrirse hasta que la sesión expire.

---

## Generar canciones

```bash
# Instrumental (lo más común)
python suno.py generate \
  --style "acoustic banjo, cinematic, orchestral swell, 68 BPM" \
  --title "Mi canción" \
  --wait

# Con letra (archivo .txt o texto directo)
python suno.py generate \
  --style "indie folk, fingerpicking" \
  --title "Mi canción" \
  --lyrics lyrics.txt \
  --vocals \
  --wait

# Descargar MP3 al terminar
python suno.py generate \
  --style "..." --title "..." \
  --wait --download --out ~/music/suno

# Con todos los controles
python suno.py generate \
  --style "..." --title "..." \
  --exclude "drums, electric guitar" \
  --weirdness 70 \
  --style-influence 40 \
  --wait
```

---

## Flags

| Flag | Descripción | Default |
|------|-------------|---------|
| `--style` *(requerido)* | Estilos, géneros, instrumentos separados por coma | — |
| `--title` *(requerido)* | Título de la canción | — |
| `--lyrics` | Letra: texto directo o ruta a `.txt`. Sin esto → instrumental | — |
| `--vocals` | Incluir voces (sin esto → instrumental) | off |
| `--vocal-gender` | `male` / `female` (solo con `--vocals`) | — |
| `--lyrics-mode` | `manual` / `auto` | Suno decide |
| `--exclude` | Estilos a evitar | — |
| `--weirdness` | 0–100, cuánto se sale de lo convencional | 50 |
| `--style-influence` | 0–100, qué tanto respeta el style tag | 50 |
| `--wait` | Esperar a que termine la generación | off |
| `--download` | Descargar MP3s al terminar (requiere `--wait`) | off |
| `--out` | Carpeta de destino para MP3s | `~/music/suno` |
| `--token` | JWT explícito (uso por agentes, saltea el browser) | — |

---

## Metatags en letra

```
[Intro - solo acoustic banjo, sparse, distant]
[Verse - melody unfolds, meditative]
[Build - picking quickens, pads emerge, tension]
[Chorus - full bloom, orchestral sweep, peak]
[Bridge - maximum power, triumphant]
[Outro - fades into silence]
```

---

## Exit codes (para agentes)

| Código | Significado |
|--------|-------------|
| `0` | OK |
| `2` | Error de auth — re-ejecutar `suno-login.exe` |
| `3` | Rate limit — esperar unos minutos |
| `4` | Error de API — ver mensaje |
| `5` | Timeout — la generación puede seguir en suno.com |

---

## Uso desde agente Claude

Si no hay `session_cookie` guardada, el agente puede capturar un JWT fresco:

```python
# 1. Arrancar servidor de captura (background)
python -c "
import threading, http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n=int(self.headers.get('Content-Length',0))
        d=json.loads(self.rfile.read(n))
        open('/tmp/suno_jwt.txt','w').write(d.get('jwt',''))
        self.send_response(200); self.send_header('Access-Control-Allow-Origin','*'); self.end_headers(); self.wfile.write(b'ok')
        threading.Thread(target=srv.shutdown).start()
    def do_OPTIONS(self):
        self.send_response(200); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Methods','POST'); self.send_header('Access-Control-Allow-Headers','Content-Type'); self.end_headers()
    def log_message(self,*a): pass
srv=http.server.HTTPServer(('127.0.0.1',7823),H); srv.serve_forever()
" &

# 2. En consola del browser (suno.com abierto):
# window.Clerk.session.getToken().then(jwt => fetch('http://127.0.0.1:7823/',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jwt})}))

# 3. Generar con el token capturado
JWT=$(cat '/tmp/suno_jwt.txt')
python suno.py generate --style "..." --title "..." --token "$JWT" --wait
```

---

## Auth fallback manual

Si `suno-login.exe` no está disponible:

```bash
python suno.py auth
```

Abre una consola interactiva. Pega el JWT desde la DevTools de suno.com:
```js
copy(await window.Clerk.session.getToken())
```
Válido ~60 segundos — suficiente para lanzar una generación.

---

## Estructura del proyecto

```
suno-cli/
├── suno.py          ← CLI principal
├── suno-login.exe   ← herramienta de login (Tauri/WebView2)
├── setup.bat        ← instalación en máquina nueva
├── suno-login/      ← código fuente de suno-login.exe (Rust/Tauri)
│   └── build.bat    ← rebuilds futuros: tauri build --bundles none
└── CLAUDE.md        ← contexto para agentes Claude

~/.suno/config.json  ← sesión guardada (no commitear)
```
