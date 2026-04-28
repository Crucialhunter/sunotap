#!/usr/bin/env python3
"""
suno.py — Suno AI CLI (v5.5)
Uses your existing browser session (Edge/Chrome) to generate songs.

Usage:
  python suno.py generate --style "acoustic banjo, cinematic" --title "My Song" --wait
  python suno.py generate --style "..." --lyrics lyrics.txt --title "..." --wait --download
  python suno.py generate --style "..." --title "..." --exclude "orchestral" --weirdness 75 --style-influence 30 --wait
  python suno.py auth                           # manual JWT paste (fallback)

Note: first-time login is handled by suno-login.exe (Tauri app, pendiente).
"""

import argparse
import base64
import json
import random
import sys
import time
import uuid
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests browser-cookie3")
    sys.exit(1)

SUNO_API  = "https://studio-api-prod.suno.com"
CLERK_API = "https://clerk.suno.com"
MODEL     = "chirp-fenix"          # Suno v5.5
CLERK_VER = "_clerk_js_version=5.35.1"
SUNO_LOGIN_EXE = Path(__file__).parent / "suno-login.exe"

# hCaptcha tokens are single-use; Suno's custom endpoint appears to keep them
# valid for about 120s — use 90s to be safe.
HCAPTCHA_TTL = 90

# ── Error types ───────────────────────────────────────────────────────────────

class SunoAuthError(Exception):
    """JWT expired, session invalid, or no auth found."""

class SunoRateLimitError(Exception):
    """API returned 429 — too many requests."""

class SunoAPIError(Exception):
    """Unexpected API error with status code and body."""

class SunoCaptchaError(Exception):
    """hCaptcha token required but not available."""

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://suno.com",
    "Referer": "https://suno.com/",
}

# ── JWT helpers ──────────────────────────────────────────────────────────────

def _jwt_exp(jwt: str) -> float:
    """Decode JWT exp claim (epoch seconds). Returns 0 on error."""
    try:
        payload = jwt.split(".")[1]
        data = json.loads(base64.b64decode(payload + "=="))
        return float(data.get("exp", 0))
    except Exception:
        return 0


def _update_rotated_cookies(response) -> None:
    """Save cookies Clerk rotates in Set-Cookie response headers."""
    if not response.cookies:
        return
    config = _load_config()
    saved = config.get("browser_cookies")
    if not saved:
        return
    changed = False
    for name, val in response.cookies.items():
        if name in saved and saved[name] != val:
            saved[name] = val
            changed = True
    if changed:
        _save_config({"browser_cookies": saved})


def _offer_reauth() -> None:
    """Offer re-authentication guidance and exit with auth error code."""
    import platform
    if platform.system() == "Windows" and SUNO_LOGIN_EXE.exists():
        import subprocess
        print(f"\nLaunching {SUNO_LOGIN_EXE.name} to re-authenticate...")
        subprocess.Popen([str(SUNO_LOGIN_EXE)])
        print("Log in to Suno in the window that opened, then retry this command.")
    else:
        print("\nSession expired. To re-authenticate:")
        print("  1. Run suno-login.exe on your Windows machine")
        print("  2. Copy ~/.suno/config.json to this machine")
        print("     e.g.: scp windows-host:~/.suno/config.json ~/.suno/config.json")
    sys.exit(2)


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_jwt() -> str:
    """Extract session cookies from browser and exchange for a fresh Clerk JWT."""
    try:
        import browser_cookie3
    except ImportError:
        print("Missing dependency: pip install browser-cookie3")
        sys.exit(1)

    cookies = _load_browser_cookies(browser_cookie3)
    if not cookies:
        print("ERROR: No Suno session found. Log in to suno.com in Edge or Chrome first.")
        sys.exit(1)

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return _clerk_jwt(cookie_header)


def _load_browser_cookies(bc) -> dict:
    for fn in [bc.edge, bc.chrome]:
        try:
            jar = fn(domain_name=".suno.com")
            d = {c.name: c.value for c in jar}
            if d.get("__session") or d.get("__client_uat"):
                return d
        except Exception:
            continue
    return {}


def _clerk_jwt(cookie_header: str) -> str:
    headers = {**BASE_HEADERS, "Cookie": cookie_header}
    try:
        r = requests.get(f"{CLERK_API}/v1/client?{CLERK_VER}", headers=headers, timeout=10)
    except requests.exceptions.ConnectionError:
        raise SunoAuthError("Network error: cannot reach clerk.suno.com — check internet connection.")
    except requests.exceptions.Timeout:
        raise SunoAuthError("Timeout reaching clerk.suno.com — Suno may be down.")

    if r.status_code == 401:
        raise SunoAuthError("Session cookie rejected (401) — run suno-login.exe to re-authenticate.")
    if r.status_code != 200:
        raise SunoAuthError(f"Clerk returned {r.status_code}: {r.text[:200]}")

    # Save any cookies Clerk rotated in this response (e.g. __client)
    _update_rotated_cookies(r)

    sessions = r.json().get("response", {}).get("sessions", [])
    if not sessions:
        raise SunoAuthError("No active Clerk session — log in to suno.com first, or run suno-login.exe.")

    # Prefer sessions tagged "active"; fall back to first if none tagged
    active = [s for s in sessions if s.get("status") == "active"]
    sid = (active or sessions)[0]["id"]

    r2 = requests.post(
        f"{CLERK_API}/v1/client/sessions/{sid}/tokens?{CLERK_VER}",
        headers=headers, timeout=10,
    )
    if r2.status_code == 401:
        raise SunoAuthError("Session expired (401 on token exchange) — run suno-login.exe.")
    if r2.status_code != 200:
        raise SunoAuthError(f"Token exchange failed {r2.status_code}: {r2.text[:200]}")
    return r2.json()["jwt"]


def _load_config() -> dict:
    CONFIG_FILE = Path.home() / ".suno" / "config.json"
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def _save_config(data: dict):
    CONFIG_DIR = Path.home() / ".suno"
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_FILE = CONFIG_DIR / "config.json"
    existing = _load_config()
    existing.update(data)
    with open(CONFIG_FILE, "w") as f:
        json.dump(existing, f, indent=2)


def _jwt_from_session_cookie(session_cookie: str) -> str:
    """Exchange the long-lived __session cookie for a fresh short-lived JWT."""
    cookie_header = f"__session={session_cookie}"
    return _clerk_jwt(cookie_header)


def _jwt_from_browser_cookies(cookies: dict) -> str:
    """Build cookie header from saved browser cookies and exchange for JWT.
    Only Clerk cookies are sent — sending all cookies causes 431 (header too large).
    """
    clerk_cookies = {k: v for k, v in cookies.items()
                     if k.startswith(("__session", "__client", "clerk"))}
    if not clerk_cookies:
        clerk_cookies = cookies  # fallback: send all if no Clerk cookies found
    cookie_header = "; ".join(f"{k}={v}" for k, v in clerk_cookies.items())
    return _clerk_jwt(cookie_header)


def _get_jwt_with_fallback(token_arg: str | None) -> str:
    # 1. Explicit --token (agent use)
    if token_arg:
        return token_arg

    config = _load_config()

    # 2. Cached JWT still valid (check actual exp claim, 120s safety margin)
    if config.get("jwt"):
        exp = _jwt_exp(config["jwt"])
        if exp and time.time() < exp - 120:
            return config["jwt"]

    # 3a. Refresh using all browser cookies (saved by suno-login — preferred)
    if config.get("browser_cookies"):
        try:
            jwt = _jwt_from_browser_cookies(config["browser_cookies"])
            _save_config({"jwt": jwt, "jwt_saved_at": time.time()})
            return jwt
        except SunoAuthError as e:
            print(f"  Browser cookies expired or invalid: {e}")
            print("  Run suno-login.exe to re-authenticate.")
        except Exception as e:
            print(f"  Browser cookies refresh failed: {e}")

    # 3b. Fallback: legacy single session_cookie
    if config.get("session_cookie") and not config.get("browser_cookies"):
        try:
            jwt = _jwt_from_session_cookie(config["session_cookie"])
            _save_config({"jwt": jwt, "jwt_saved_at": time.time()})
            return jwt
        except SunoAuthError as e:
            print(f"  Session cookie expired or invalid: {e}")
            print("  Run suno-login.exe to re-authenticate.")
        except Exception as e:
            print(f"  Session cookie refresh failed: {e}")
            print("  Run suno-login.exe to re-authenticate.")

    # 4. Fallback: browser-cookie3 (may fail on Windows due to App-Bound Encryption)
    try:
        return get_jwt()
    except SystemExit:
        raise
    except Exception as e:
        raise SunoAuthError(
            f"All auth methods failed. Last error: {e}\n"
            "Fix: run suno-login.exe, or use --token with a fresh JWT from suno.com console: "
            "window.Clerk.session.getToken()"
        ) from e

# ── Human-like throttle ───────────────────────────────────────────────────────

def _jitter(min_s: float, max_s: float) -> float:
    return max(min_s, min(max_s, random.gauss((min_s + max_s) / 2, 3.5)))


def _human_wait(min_s=8, max_s=20, label="Polling in"):
    delay = _jitter(min_s, max_s)
    print(f"  {label} {delay:.1f}s...   ", end="\r")
    time.sleep(delay)


def _browser_token() -> str:
    """Fresh Browser-Token header — timestamp-based, required by Suno API since ~2026-04."""
    ts = int(time.time() * 1000)
    payload = json.dumps({"timestamp": ts}, separators=(',', ':'))
    token_b64 = base64.b64encode(payload.encode()).decode()
    return json.dumps({"token": token_b64}, separators=(',', ':'))


def _check_captcha_required(jwt: str) -> bool:
    """Ask Suno whether an hCaptcha token is required for generation."""
    headers = {
        **BASE_HEADERS,
        "Authorization": f"Bearer {jwt}",
        "Browser-Token": _browser_token(),
        "Device-Id": _device_id(),
    }
    try:
        r = requests.post(
            f"{SUNO_API}/api/c/check",
            headers=headers,
            json={"ctype": "generation"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("required", False) is True
    except Exception as e:
        print(f"  Warning: captcha check failed ({e}) — assuming required")
    return True  # safe default


def _get_cached_captcha_token() -> str | None:
    """Return a cached hCaptcha token if it's still within TTL, else None."""
    config = _load_config()
    token = config.get("captcha_token")
    saved_at = config.get("captcha_token_saved_at", 0)
    if token and (time.time() - saved_at) < HCAPTCHA_TTL:
        return token
    return None


def _captcha_capture_server() -> str | None:
    """Start a local HTTP server to receive an hCaptcha token pasted from the browser."""
    import threading
    import http.server

    PORT = 7824
    token_holder: list[str | None] = [None]
    srv_holder: list[http.server.HTTPServer | None] = [None]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(200)
            for h, v in [("Access-Control-Allow-Origin", "*"),
                         ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                         ("Access-Control-Allow-Headers", "Content-Type")]:
                self.send_header(h, v)
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n))
            token_holder[0] = data.get("token") or ""
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"ok")
            threading.Thread(target=srv_holder[0].shutdown).start()  # type: ignore[union-attr]

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    srv_holder[0] = srv

    print("\n  hCaptcha token required. Open suno.com in your browser,")
    print("  press F12 → Console, and run:\n")
    print(f"    (async()=>{{")
    print(f"      let f=document.querySelector('#__next').__reactFiber;")
    print(f"      function fs(f,d=0){{if(!f||d>100)return null;")
    print(f"      const v=f.memoizedProps?.value;")
    print(f"      if(v?.session?.getCaptchaTokenIfRequired)return v.session;")
    print(f"      return fs(f.child,d+1)||fs(f.sibling,d+1);}}")
    print(f"      const t=await fs(f).getCaptchaTokenIfRequired('generation');")
    print(f"      await fetch('http://127.0.0.1:{PORT}/',{{method:'POST',")
    print(f"        headers:{{'Content-Type':'application/json'}},")
    print(f"        body:JSON.stringify({{token:t}})}});")
    print(f"    }})()\n")
    print(f"  Waiting for token (60s timeout)...")

    timer = threading.Timer(62, srv.shutdown)
    timer.start()
    srv.serve_forever()
    timer.cancel()

    token = token_holder[0]
    if token:
        _save_config({"captcha_token": token, "captcha_token_saved_at": time.time()})
        print("  hCaptcha token received and cached.")
    else:
        print("  Timed out waiting for hCaptcha token.")
    return token or None


def _get_user_tier() -> str | None:
    """Fetch plan ID from billing API. Cached in config to avoid extra calls."""
    config = _load_config()
    if config.get("user_tier"):
        return config["user_tier"]
    try:
        jwt = _get_jwt_with_fallback(None)
        headers = {**BASE_HEADERS, "Authorization": f"Bearer {jwt}",
                   "Device-Id": _device_id(), "Browser-Token": _browser_token()}
        r = requests.get(f"{SUNO_API}/api/billing/info/", headers=headers, timeout=10)
        if r.status_code == 200:
            tier = r.json().get("plan", {}).get("id")
            if tier:
                _save_config({"user_tier": tier})
                return tier
    except Exception:
        pass
    return None


def _device_id() -> str:
    """Device-Id header — from saved suno_device_id cookie, or a stable generated UUID."""
    config = _load_config()
    raw = (config.get("browser_cookies") or {}).get("suno_device_id") or config.get("device_id")
    if not raw:
        raw = str(uuid.uuid4())
        _save_config({"device_id": raw})
    return f"default-{raw}"


# ── API calls ────────────────────────────────────────────────────────────────

def generate(jwt: str, args) -> list[str]:
    """Build payload from args and submit generation. Returns clip IDs."""
    headers = {
        **BASE_HEADERS,
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Device-Id": _device_id(),
        "Browser-Token": _browser_token(),
    }

    # ── hCaptcha check ───────────────────────────────────────────────────────
    captcha_token: str | None = None
    captcha_token_arg = getattr(args, "captcha_token", None)
    if _check_captcha_required(jwt):
        captcha_token = captcha_token_arg or _get_cached_captcha_token()
        if not captcha_token:
            raise SunoCaptchaError(
                "hCaptcha token required. Re-run with --captcha-token or "
                "use --captcha-server to capture one interactively."
            )

    # Load lyrics from file if path given
    prompt = args.lyrics or ""
    def _is_file_path(s: str) -> bool:
        try:
            return bool(s) and Path(s).exists()
        except OSError:
            return False
    if _is_file_path(prompt):
        prompt = Path(prompt).read_text(encoding="utf-8")

    user_tier = _get_user_tier()

    payload: dict = {
        "project_id":               None,
        "token":                    captcha_token,
        "generation_type":          "TEXT",
        "title":                    args.title,
        "tags":                     args.style,
        "negative_tags":            args.exclude or "",
        "mv":                       MODEL,
        "prompt":                   prompt,
        "make_instrumental":        not args.vocals,
        "user_uploaded_images_b64": None,
        "metadata": {
            "web_client_pathname":          "/create",
            "is_max_mode":                  False,
            "is_mumble":                    False,
            "create_mode":                  "custom" if prompt else "default",
            "user_tier":                    user_tier,
            "create_session_token":         str(uuid.uuid4()),
            "disable_volume_normalization": False,
            "control_sliders":              {"style_weight": 1},
        },
        "override_fields":   [],
        "cover_clip_id":     None,
        "cover_start_s":     None,
        "cover_end_s":       None,
        "persona_id":        None,
        "artist_clip_id":    None,
        "artist_start_s":    None,
        "artist_end_s":      None,
        "continue_clip_id":  None,
        "continued_aligned_prompt": None,
        "continue_at":       None,
        "transaction_uuid":  str(uuid.uuid4()),
    }

    if args.vocal_gender:
        g = args.vocal_gender.lower()
        payload["vocal_gender"] = "male" if g in ("male", "m") else "female"

    if args.lyrics_mode:
        payload["lyrics_mode"] = args.lyrics_mode

    if args.weirdness is not None:
        payload["weirdness"] = max(0, min(100, args.weirdness))

    if args.style_influence is not None:
        payload["style_influence"] = max(0, min(100, args.style_influence))

    try:
        r = requests.post(f"{SUNO_API}/api/generate/v2-web/", headers=headers, json=payload, timeout=30)
    except requests.exceptions.ConnectionError:
        raise SunoAPIError("Network error: cannot reach studio-api-prod.suno.com.")
    except requests.exceptions.Timeout:
        raise SunoAPIError("Timeout on generate request — Suno may be overloaded.")

    if r.status_code == 401:
        raise SunoAuthError("JWT rejected by Suno API (401) — token may have expired. Retry.")
    if r.status_code == 429:
        raise SunoRateLimitError("Rate limited (429) — too many requests. Wait a few minutes.")
    if r.status_code == 402:
        raise SunoAPIError("Insufficient credits (402). Check your Suno subscription.")
    if r.status_code != 200:
        raise SunoAPIError(f"Generate failed {r.status_code}: {r.text[:300]}")

    clips = r.json().get("clips", [])
    if not clips:
        raise SunoAPIError(f"Generate returned no clips. Response: {r.text[:200]}")
    return [c["id"] for c in clips]


def poll_until_ready(jwt: str, clip_ids: list[str], timeout: int = 360,
                     token_arg: str | None = None) -> list[dict]:
    """Poll until all clips are complete. Auto-refreshes JWT before it expires."""
    current_jwt = jwt
    jwt_refreshed_at = time.time()
    start = time.time()

    while time.time() - start < timeout:
        # Refresh JWT when it has <120s remaining (actual exp from JWT claim).
        # Never reuse token_arg — it was used to start generation and may now be expired.
        jwt_exp = _jwt_exp(current_jwt)
        needs_refresh = jwt_exp and time.time() > jwt_exp - 120
        if needs_refresh or (not jwt_exp and time.time() - jwt_refreshed_at > 3300):
            try:
                current_jwt = _get_jwt_with_fallback(None)
                jwt_refreshed_at = time.time()
            except SunoAuthError as e:
                raise SunoAuthError(f"JWT refresh failed during poll: {e}") from e

        headers = {
            **BASE_HEADERS,
            "Authorization": f"Bearer {current_jwt}",
            "Content-Type": "application/json",
            "Device-Id": _device_id(),
            "Browser-Token": _browser_token(),
        }
        try:
            r = requests.post(f"{SUNO_API}/api/feed/v3", headers=headers,
                              json={"ids": clip_ids}, timeout=15)
        except requests.exceptions.ConnectionError:
            print("  Network error during poll — retrying...   ", end="\r")
            _human_wait(9, 18)
            continue
        except requests.exceptions.Timeout:
            print("  Poll request timed out — retrying...      ", end="\r")
            _human_wait(9, 18)
            continue

        if r.status_code == 401:
            # Force JWT refresh on next iteration
            jwt_refreshed_at = 0
            _human_wait(5, 10, "Token expired, refreshing in")
            continue
        if r.status_code == 429:
            print("  Rate limited — backing off...             ", end="\r")
            _human_wait(30, 60, "Rate limited, retrying in")
            continue
        if r.status_code != 200:
            raise SunoAPIError(f"Poll returned {r.status_code}: {r.text[:200]}")

        data = r.json()
        clips = data if isinstance(data, list) else data.get("clips", [])
        ours = [c for c in clips if c.get("id") in clip_ids]
        done = [c for c in ours if c.get("status") == "complete"]
        failed = [c for c in ours if c.get("status") in ("error", "failed")]
        elapsed = int(time.time() - start)
        print(f"  Generating... {len(done)}/{len(clip_ids)} ready ({elapsed}s)   ", end="\r")

        if failed:
            ids = [c["id"] for c in failed]
            raise SunoAPIError(f"Generation failed server-side for clips: {ids}")
        if len(done) >= len(clip_ids):
            print()
            return done

        _human_wait(9, 18)

    raise TimeoutError(f"Generation timed out after {timeout}s — clips may still be processing at suno.com")


def download_clips(clips: list[dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for clip in clips:
        url = clip.get("audio_url")
        if not url:
            print(f"  WARNING: clip {clip.get('id', '?')[:8]} has no audio_url — skipping.")
            continue
        safe_title = clip.get("title", "untitled").replace("/", "-").replace("\\", "-")[:60]
        dest = out_dir / f"{safe_title}_{clip['id'][:8]}.mp3"
        try:
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise SunoAPIError(f"Network error downloading clip {clip['id'][:8]}.")
        except requests.exceptions.HTTPError as e:
            raise SunoAPIError(f"Download failed for clip {clip['id'][:8]}: {e}")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        saved.append(dest)
    return saved

# ── Commands ──────────────────────────────────────────────────────────────────

def _get_session_expiry_from_config() -> float | None:
    """Return Unix timestamp of Clerk session expiry, or None on error."""
    config = _load_config()
    cookies = config.get("browser_cookies") or {}
    clerk = {k: v for k, v in cookies.items() if k.startswith(("__session", "__client"))}
    if not clerk and config.get("session_cookie"):
        clerk = {"__session": config["session_cookie"]}
    if not clerk:
        return None
    cookie_header = "; ".join(f"{k}={v}" for k, v in clerk.items())
    headers = {**BASE_HEADERS, "Cookie": cookie_header}
    try:
        r = requests.get(f"{CLERK_API}/v1/client?{CLERK_VER}", headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        _update_rotated_cookies(r)
        sessions = r.json().get("response", {}).get("sessions", [])
        if not sessions:
            return None
        active = [s for s in sessions if s.get("status") == "active"]
        session = (active or sessions)[0]
        exp = session.get("expire_at") or session.get("expireAt")
        if exp:
            return exp / 1000 if exp > 1e10 else float(exp)
    except Exception:
        pass
    return None


def cmd_status(_args):
    """Check auth state without generating anything."""
    config = _load_config()
    has_cookies = bool(config.get("browser_cookies"))
    has_session = bool(config.get("session_cookie"))

    cached_jwt = config.get("jwt", "")
    jwt_exp = _jwt_exp(cached_jwt) if cached_jwt else 0
    jwt_remaining = jwt_exp - time.time() if jwt_exp else -1
    jwt_status = f"valid ({jwt_remaining:.0f}s left)" if jwt_remaining > 0 else "expired"

    print(f"Config:  ~/.suno/config.json")
    print(f"  browser_cookies saved:  {'yes (' + str(len(config['browser_cookies'])) + ' keys)' if has_cookies else 'NO'}")
    print(f"  session_cookie saved:   {'yes' if has_session else 'NO'}")
    print(f"  cached JWT:             {jwt_status}")

    if not has_cookies and not has_session:
        print("\nNO AUTH — run suno-login.exe to save session cookie.")
        _offer_reauth()

    print("\nTesting JWT refresh...")
    try:
        jwt = _get_jwt_with_fallback(None)
        new_exp = _jwt_exp(jwt)
        print(f"  OK — JWT valid for {(new_exp - time.time()) / 60:.0f} min")
    except SunoAuthError as e:
        print(f"  FAILED: {e}")
        print("\nSession expired.")
        _offer_reauth()

    print("\nChecking session lifetime...")
    expiry = _get_session_expiry_from_config()
    if expiry:
        days = (expiry - time.time()) / 86400
        exp_str = time.strftime("%Y-%m-%d", time.localtime(expiry))
        print(f"  Session expires: {exp_str} ({days:.0f} days from now)")
        if days < 30:
            print(f"  WARNING: session expires in {days:.0f} days — run suno-login.exe soon.")
    else:
        print("  (could not read session expiry)")

    print("\nChecking captcha requirement...")
    try:
        jwt_for_check = _get_jwt_with_fallback(None)
        captcha_req = _check_captcha_required(jwt_for_check)
        cached_tok = _get_cached_captcha_token()
        print(f"  hCaptcha required for generate: {'YES' if captcha_req else 'no'}")
        if captcha_req:
            if cached_tok:
                config = _load_config()
                age = time.time() - config.get("captcha_token_saved_at", 0)
                print(f"  Cached token: valid ({age:.0f}s old, TTL {HCAPTCHA_TTL}s)")
            else:
                print(f"  Cached token: none — use --captcha-server before generating")
    except Exception as e:
        print(f"  (captcha check failed: {e})")

    print("\nAuth is working.")


def cmd_auth(args):
    CONFIG_DIR = Path.home() / ".suno"
    CONFIG_FILE = CONFIG_DIR / "config.json"
    CONFIG_DIR.mkdir(exist_ok=True)

    if args.token:
        token = args.token.strip()
    else:
        print("\nOpen suno.com in your browser, press F12 -> Console, and run:\n")
        print("    copy(await window.Clerk.session.getToken())\n")
        print("Paste the result here and press Enter:")
        token = input("> ").strip().strip('"')

    if not token.startswith("eyJ"):
        print("ERROR: That doesn't look like a valid JWT.")
        sys.exit(1)

    _save_config({"jwt": token, "jwt_saved_at": time.time()})
    print(f"\nToken saved to {CONFIG_FILE}")
    print("Valid ~60 seconds. Re-run 'auth' before each session.")


def cmd_generate(args):
    token_arg = getattr(args, "token", None)

    # Interactive hCaptcha capture requested before generate
    if getattr(args, "captcha_server", False):
        token = _captcha_capture_server()
        if not token:
            print("\nNo captcha token received — aborting.", file=sys.stderr)
            sys.exit(2)
        print()

    try:
        print("Getting token...")
        jwt = _get_jwt_with_fallback(token_arg)
        print("  Token OK\n")

        print(f"Submitting: \"{args.title}\"")
        clip_ids = generate(jwt, args)
        print(f"  Clips queued: {clip_ids}\n")

        if args.wait:
            clips = poll_until_ready(jwt, clip_ids, token_arg=token_arg)
            if args.download:
                out = Path(args.out).expanduser()
                print(f"Downloading to {out} ...")
                for p in download_clips(clips, out):
                    print(f"  Saved: {p}")
            else:
                for c in clips:
                    print(f"  Ready: {c.get('audio_url')}")
        else:
            print("Submitted. Check suno.com/create for results.")
            print(f"Clip IDs: {clip_ids}")

    except SunoAuthError as e:
        print(f"\nAUTH ERROR: {e}", file=sys.stderr)
        _offer_reauth()
    except SunoCaptchaError as e:
        print(f"\nCAPTCHA ERROR: {e}", file=sys.stderr)
        print("\nOptions:", file=sys.stderr)
        print("  1. Interactive:  python suno.py generate --captcha-server ...", file=sys.stderr)
        print("  2. Manual token: python suno.py generate --captcha-token <token> ...", file=sys.stderr)
        sys.exit(2)
    except SunoRateLimitError as e:
        print(f"\nRATE LIMIT: {e}", file=sys.stderr)
        sys.exit(3)
    except SunoAPIError as e:
        print(f"\nAPI ERROR: {e}", file=sys.stderr)
        sys.exit(4)
    except TimeoutError as e:
        print(f"\nTIMEOUT: {e}", file=sys.stderr)
        sys.exit(5)

# ── CLI definition ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Suno CLI v5.5 — generate music from the terminal"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="Check auth state and test JWT refresh")

    # auth (manual fallback)
    a = sub.add_parser("auth", help="Manually paste a JWT token (fallback)")
    a.add_argument("--token", default=None, help="JWT string (omit for interactive prompt)")

    # generate
    g = sub.add_parser("generate", help="Generate a song")

    # Core fields
    g.add_argument("--style",          required=True,  help="Style/genre tags (Styles field)")
    g.add_argument("--title",          required=True,  help="Song title")
    g.add_argument("--lyrics",         default="",     help="Lyrics text or path to .txt file (leave empty for instrumental)")

    # More Options fields
    g.add_argument("--exclude",        default=None,   help="Styles to avoid (Exclude styles field)")
    g.add_argument("--vocals",         action="store_true", default=False,
                                                        help="Include vocals — default is instrumental")
    g.add_argument("--vocal-gender",   default=None,   dest="vocal_gender",
                                                        choices=["male","female","m","f"],
                                                        help="Vocal gender (only when --vocals is set)")
    g.add_argument("--lyrics-mode",    default=None,   dest="lyrics_mode",
                                                        choices=["manual","auto"],
                                                        help="Lyrics mode (default: don't override)")
    g.add_argument("--weirdness",      default=None,   type=int, metavar="0-100",
                                                        help="Weirdness slider (default: Suno's 50)")
    g.add_argument("--style-influence",default=None,   type=int, metavar="0-100",
                                                        dest="style_influence",
                                                        help="Style Influence slider (default: Suno's 50)")

    # Output / control
    g.add_argument("--wait",           action="store_true", help="Block until generation completes")
    g.add_argument("--download",       action="store_true", help="Download MP3s when done (requires --wait)")
    g.add_argument("--out",            default="~/music/suno", help="Output directory for MP3s")
    g.add_argument("--token",          default=None,   help="JWT token (for agent use, skips browser)")
    g.add_argument("--captcha-token",  default=None,   dest="captcha_token",
                                                        help="hCaptcha token (get from browser console)")
    g.add_argument("--captcha-server", action="store_true", dest="captcha_server",
                                                        help="Start local server to capture hCaptcha token interactively")

    args = parser.parse_args()
    if args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "auth":
        cmd_auth(args)
    elif args.cmd == "generate":
        cmd_generate(args)


if __name__ == "__main__":
    main()
