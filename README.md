<div align="center">

<!-- Replace with actual logo once ready -->
<img src="assets/logo.png" alt="SunoTap logo" width="180" />

# SunoTap

**Tap into Suno AI from your terminal.**

Generate music on [Suno AI v5.5](https://suno.com) without touching the web interface — scriptable, headless, agent-friendly.

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://python.org)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078d4?logo=windows&logoColor=white)](https://github.com)
[![Unofficial](https://img.shields.io/badge/Suno-Unofficial%20Client-ff6b35)](https://suno.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

<!-- Replace with your terminal demo GIF -->
<img src="assets/demo.gif" alt="SunoTap demo" width="700" />

</div>

---

## What is this?

SunoTap is an **alternative client for your own Suno account**. It talks to the same API endpoints your browser uses, authenticated with your own session. No scraping, no account bypassing, no credit manipulation — just a cleaner interface for people who live in the terminal.

One login. Your session is saved. After that, it's pure HTTP — no browser process, no GUI.

---

## How the auth works

```mermaid
flowchart LR
    A[suno-login.exe\nWebView2 window] -->|captures httpOnly cookies\nvia COM ICoreWebView2CookieManager| B[(~/.suno/config.json)]
    B -->|long-lived __session cookie\nlasts weeks/months| C[suno.py]
    C -->|POST clerk.suno.com/v1/client/sessions/{id}/tokens| D[JWT Bearer\n~60s TTL]
    D -->|Authorization: Bearer| E[studio-api-prod.suno.com\n/api/generate/v2-web/]
    E --> F[2 clips submitted]
    F -->|poll every 8–20s| G[MP3 ready ✓]
```

> **Why the Tauri app?** `httpOnly` cookies can't be read via JavaScript (`document.cookie`). `suno-login.exe` accesses them through WebView2's native COM API as the host process — the only reliable way on Windows without admin rights.

---

## A note on responsible use

This tool is intentionally designed to be a **polite API client**.

The polling loop uses **human-like, jittered intervals** — not because the code can't poll faster, but because it shouldn't:

| Situation | Behavior |
|-----------|----------|
| Normal polling | 8–20s random interval (Gaussian, σ=3.5s) |
| Rate limited (HTTP 429) | Backs off 30–60s automatically |
| Network error | Retries after 9–18s |
| JWT expiry | Proactively refreshes every 50s (expires at 60s) |

Suno's servers are doing real generative ML work. A full song takes 60–120 seconds to render. Polling every 8+ seconds is more than sufficient and puts zero meaningful load on their infrastructure. The jitter ensures the request pattern looks like a human checking back, not a script hammering an endpoint.

> **Not affiliated with or endorsed by Suno AI.** Use in accordance with [Suno's Terms of Service](https://suno.com/terms).

---

## Setup

```bat
setup.bat
```

Checks Python, installs dependencies (`requests`, `browser-cookie3`), creates `~/.suno/`.

---

## Login

```bat
suno-login.exe
```

Opens a small window with suno.com. Log in normally. When login is detected it saves your session cookies and closes with a ✓ overlay. You won't need to open it again until the session expires (weeks to months).

> No `suno-login.exe`? Build it from source: `cd suno-login && build.bat` (requires Rust + Tauri CLI). Or use the [manual fallback](#manual-auth-fallback).

---

## Generate

```bash
# Instrumental
python suno.py generate \
  --style "acoustic banjo, cinematic, orchestral swell, 68 BPM" \
  --title "Remnants of Kharak" \
  --wait

# With lyrics (file or inline text)
python suno.py generate \
  --style "indie folk, fingerpicking" \
  --title "My Song" \
  --lyrics lyrics.txt \
  --vocals \
  --wait

# Download MP3s when done
python suno.py generate \
  --style "..." --title "..." \
  --wait --download --out ~/music/suno

# All controls
python suno.py generate \
  --style "..." --title "..." \
  --exclude "drums, electric guitar" \
  --weirdness 70 \
  --style-influence 40 \
  --wait
```

---

## Flags

| Flag | Description | Default |
|------|-------------|---------|
| `--style` *(required)* | Styles, genres, instruments (comma-separated) | — |
| `--title` *(required)* | Song title | — |
| `--lyrics` | Lyrics: inline text or path to `.txt`. Omit → instrumental | — |
| `--vocals` | Include vocals | off |
| `--vocal-gender` | `male` / `female` (only with `--vocals`) | — |
| `--lyrics-mode` | `manual` / `auto` | Suno decides |
| `--exclude` | Styles to avoid (negative tags) | — |
| `--weirdness` | 0–100, how far it diverges from conventional | 50 |
| `--style-influence` | 0–100, how closely it follows the style tag | 50 |
| `--wait` | Wait until generation completes | off |
| `--download` | Download MP3s when done (requires `--wait`) | off |
| `--out` | Output folder for MP3s | `~/music/suno` |
| `--token` | Explicit JWT (agent use — bypasses saved session) | — |

---

## Lyrics metatags

Suno v5.5 understands structural metatags inline with the lyrics:

```
[Intro - solo acoustic banjo, sparse, distant]
[Verse - melody unfolds, meditative]
[Build - picking quickens, pads emerge, tension]
[Chorus - full bloom, orchestral sweep, peak]
[Bridge - maximum power, triumphant]
[Outro - fades into silence]
```

---

## Exit codes

For use in scripts and agents:

| Code | Meaning |
|------|---------|
| `0` | OK |
| `2` | Auth error — re-run `suno-login.exe` |
| `3` | Rate limit — wait a few minutes |
| `4` | API error — see message |
| `5` | Timeout — generation may still be running at suno.com |

---

## Agent use (LLM / Claude automation)

<details>
<summary>Capture a fresh JWT from the browser for agent-driven generation</summary>

If there's no saved session, an agent can capture a short-lived JWT directly from an open suno.com tab:

```python
# 1. Start ephemeral capture server (background)
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

# 2. In browser console (suno.com open):
# window.Clerk.session.getToken().then(jwt => fetch('http://127.0.0.1:7823/',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jwt})}))

# 3. Generate
JWT=$(cat '/tmp/suno_jwt.txt')
python suno.py generate --style "..." --title "..." --token "$JWT" --wait
```

The JWT is valid ~60 seconds — enough to launch a generation. Subsequent polling uses the saved session cookie automatically.
</details>

---

## Manual auth fallback

```bash
python suno.py auth
```

Interactive prompt. Paste the JWT from suno.com DevTools console:
```js
copy(await window.Clerk.session.getToken())
```
Valid ~60 seconds — enough to launch a generation.

---

## Project structure

```
suno-cli/
├── suno.py              ← main CLI (Python)
├── setup.bat            ← install dependencies
├── suno-login/          ← suno-login.exe source (Rust/Tauri v2)
│   ├── build.bat        ← rebuild: tauri build --bundles none
│   └── src-tauri/
│       └── src/main.rs  ← WebView2 COM cookie extraction
└── assets/
    ├── logo.png         ← app logo
    └── demo.gif         ← terminal demo

~/.suno/config.json      ← saved session (never committed)
```

---

## Building suno-login.exe from source

Requires [Rust](https://rustup.rs/) and [Tauri CLI](https://tauri.app/start/):

```bat
cd suno-login
build.bat
```

Produces a ~2.5 MB self-contained `suno-login.exe` in the project root. No Chromium, no Electron — just WebView2 (already on every Windows 10/11 machine).
