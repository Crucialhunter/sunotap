// Prevents console window in release builds on Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

#[cfg(windows)]
use {
    webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2_2,
    windows::core::HSTRING,
    windows_core::Interface,
};

// ── Shared state ──────────────────────────────────────────────────────────────

struct State {
    cache:      Mutex<Option<(String, Instant)>>,
    page_ready: AtomicBool,
    fetch_lock: Mutex<()>,
    tray:       Mutex<Option<tauri::tray::TrayIcon>>,
}

// ── Config ────────────────────────────────────────────────────────────────────

fn load_cookies() -> HashMap<String, String> {
    let path = dirs::home_dir()
        .unwrap_or_default()
        .join(".suno")
        .join("config.json");

    let raw = std::fs::read_to_string(&path).unwrap_or_default();
    let cfg: serde_json::Value =
        serde_json::from_str(&raw).unwrap_or(serde_json::json!({}));

    let Some(obj) = cfg.get("browser_cookies").and_then(|v| v.as_object()) else {
        eprintln!("[captcha] no browser_cookies in config.json — running unauthenticated");
        return HashMap::new();
    };

    obj.iter()
        .filter(|(k, _)| {
            k.starts_with("__session")
                || k.starts_with("__client")
                || k.starts_with("clerk")
                || k.contains("suno")
        })
        .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_owned())))
        .collect()
}

// ── Cookie injection (COM / WebView2) ─────────────────────────────────────────

#[cfg(windows)]
fn inject_cookies(window: &tauri::WebviewWindow, cookies: HashMap<String, String>) {
    let _ = window.with_webview(move |wv| {
        let r: windows_core::Result<()> = (|| unsafe {
            let core2: ICoreWebView2_2 = wv.controller().CoreWebView2()?.cast()?;
            let mgr = core2.CookieManager()?;
            for (name, value) in &cookies {
                let Ok(c) = mgr.CreateCookie(
                    &HSTRING::from(name.as_str()),
                    &HSTRING::from(value.as_str()),
                    &HSTRING::from(".suno.com"),
                    &HSTRING::from("/"),
                ) else {
                    continue;
                };
                let _ = mgr.AddOrUpdateCookie(&c);
            }
            Ok(())
        })();
        if let Err(e) = r {
            eprintln!("[captcha] cookie inject: {e}");
        }
    });
}

// ── URL percent-decode ────────────────────────────────────────────────────────

fn url_decode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '%' {
            let h1 = chars.next().unwrap_or('0');
            let h2 = chars.next().unwrap_or('0');
            if let Ok(b) = u8::from_str_radix(&format!("{h1}{h2}"), 16) {
                out.push(b as char);
            }
        } else if ch == '+' {
            out.push(' ');
        } else {
            out.push(ch);
        }
    }
    out
}

// ── hCaptcha JS: invisible widget → result via URL hash (avoids CSP) ─────────
//
// We pass the token back through window.location.hash instead of fetch(),
// so suno.com's connect-src CSP policy never blocks us.

const CAPTCHA_JS: &str = r#"
(function() {
  try {
    if (typeof window.hcaptcha === 'undefined') {
      window.location.hash = '__sc_err__' + encodeURIComponent('hcaptcha_not_loaded');
      return;
    }
    var old = document.getElementById('__sc__');
    if (old) { try { window.hcaptcha.remove(old._wid); } catch(e) {} old.remove(); }
    var c = document.createElement('div');
    c.id = '__sc__';
    c.style.display = 'none';
    document.body.appendChild(c);
    var wid = window.hcaptcha.render(c, {
      sitekey: 'd65453de-3f1a-4aac-9366-a0f06e52b2ce',
      size: 'invisible',
      callback: function(token) {
        window.location.hash = '__sc__' + encodeURIComponent(token);
      },
      'error-callback': function(e) {
        window.location.hash = '__sc_err__' + encodeURIComponent(String(e || 'render_error'));
      }
    });
    window.hcaptcha.execute(wid);
  } catch(e) {
    window.location.hash = '__sc_err__' + encodeURIComponent(String(e));
  }
})()
"#;

const HASH_CLEAR_JS: &str =
    "window.history.replaceState(null,'',\
     window.location.pathname+window.location.search)";

fn fetch_token(window: &tauri::WebviewWindow) -> Result<String, String> {
    // Clear any leftover hash from a previous run
    let _ = window.eval(&format!(
        "if(window.location.hash.startsWith('#__sc')){{{HASH_CLEAR_JS};}}"
    ));
    thread::sleep(Duration::from_millis(60));

    // Fire the invisible captcha widget
    let _ = window.eval(CAPTCHA_JS);

    // Poll the page URL for the result hash (max 15 s)
    for _ in 0..150 {
        thread::sleep(Duration::from_millis(100));
        if let Ok(url) = window.url() {
            let s = url.to_string();
            if let Some(frag) = s.split('#').nth(1) {
                if frag.starts_with("__sc_err__") {
                    let _ = window.eval(HASH_CLEAR_JS);
                    return Err(url_decode(&frag["__sc_err__".len()..]));
                }
                if frag.starts_with("__sc__") {
                    let token = url_decode(&frag["__sc__".len()..]);
                    let _ = window.eval(HASH_CLEAR_JS);
                    return Ok(token);
                }
            }
        }
    }
    Err("timeout".to_string())
}

// ── HTTP server ───────────────────────────────────────────────────────────────

const PORT: u16 = 7825;
const PAIR_PORT: u16 = 7826;
const TOKEN_TTL_SECS: u64 = 85;

fn http_reply(stream: &mut TcpStream, status: u16, body: &str) {
    let reason = if status == 200 { "OK" } else { "Error" };
    let _ = stream.write_all(
        format!(
            "HTTP/1.1 {status} {reason}\r\n\
             Content-Type: application/json\r\n\
             Content-Length: {}\r\n\
             Access-Control-Allow-Origin: *\r\n\
             Connection: close\r\n\r\n{body}",
            body.len()
        )
        .as_bytes(),
    );
}

fn handle(mut stream: TcpStream, state: Arc<State>, window: tauri::WebviewWindow) {
    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf).unwrap_or(0);
    let req = String::from_utf8_lossy(&buf[..n]);
    let line = req.lines().next().unwrap_or("");

    // GET /health
    if line.contains("GET /health") {
        let ready = state.page_ready.load(Ordering::Relaxed);
        return http_reply(
            &mut stream, 200,
            &format!(r#"{{"status":"ok","ready":{ready}}}"#),
        );
    }

    // GET /token
    if line.contains("GET /token") {
        // Fast path: return cached token if still fresh
        {
            let cache = state.cache.lock().unwrap();
            if let Some((t, ts)) = cache.as_ref() {
                if ts.elapsed().as_secs() < TOKEN_TTL_SECS && !t.is_empty() {
                    return http_reply(
                        &mut stream, 200,
                        &format!(r#"{{"token":"{t}","cached":true}}"#),
                    );
                }
            }
        }

        // Wait for page to be ready (max 30 s)
        for _ in 0..30 {
            if state.page_ready.load(Ordering::Relaxed) {
                break;
            }
            thread::sleep(Duration::from_secs(1));
        }
        if !state.page_ready.load(Ordering::Relaxed) {
            return http_reply(&mut stream, 503, r#"{"error":"page not ready"}"#);
        }

        // Only one fetch at a time
        let _guard = state.fetch_lock.lock().unwrap();

        // Re-check cache now that we hold the lock
        {
            let cache = state.cache.lock().unwrap();
            if let Some((t, ts)) = cache.as_ref() {
                if ts.elapsed().as_secs() < TOKEN_TTL_SECS && !t.is_empty() {
                    return http_reply(
                        &mut stream, 200,
                        &format!(r#"{{"token":"{t}","cached":true}}"#),
                    );
                }
            }
        }

        return match fetch_token(&window) {
            Ok(token) => {
                *state.cache.lock().unwrap() = Some((token.clone(), Instant::now()));
                http_reply(&mut stream, 200, &format!(r#"{{"token":"{token}"}}"#))
            }
            Err(e) => {
                eprintln!("[captcha] fetch error: {e}");
                http_reply(&mut stream, 500, &format!(r#"{{"error":"{e}"}}"#))
            }
        };
    }

    http_reply(&mut stream, 404, r#"{"error":"not found"}"#);
}

// ── Pairing: UDP broadcast so the LXC can discover this machine's IP ─────────

fn get_local_ip() -> Option<String> {
    // Trick: connect to an external address (no data sent) to find the
    // outbound interface IP — works even if the machine has multiple NICs.
    let s = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
    s.connect("8.8.8.8:80").ok()?;
    Some(s.local_addr().ok()?.ip().to_string())
}

fn broadcast_pairing() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let ip = get_local_ip().unwrap_or_else(|| "127.0.0.1".to_owned());
    // 4-digit code, derived from nanosecond timestamp (good enough — not crypto)
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    let code = format!("{:04}", (nanos % 9000) + 1000);

    let payload = format!(
        r#"{{"ip":"{ip}","port":{PORT},"code":"{code}"}}"#
    );

    let Ok(s) = std::net::UdpSocket::bind("0.0.0.0:0") else { return code };
    let _ = s.set_broadcast(true);
    let target = format!("255.255.255.255:{PAIR_PORT}");
    for _ in 0..5 {
        let _ = s.send_to(payload.as_bytes(), &target);
        thread::sleep(Duration::from_millis(300));
    }
    eprintln!("[captcha] broadcast sent — {ip}:{PORT}, code {code}");
    code
}

// ── HTTP server ───────────────────────────────────────────────────────────────

fn run_http(state: Arc<State>, window: tauri::WebviewWindow) {
    // Bind on all interfaces so the LXC can reach us over the LAN.
    let Ok(listener) = TcpListener::bind(format!("0.0.0.0:{PORT}")) else {
        eprintln!("[captcha] cannot bind port {PORT}");
        return;
    };
    eprintln!("[captcha] listening on 127.0.0.1:{PORT}");
    for stream in listener.incoming().flatten() {
        let (s, w) = (Arc::clone(&state), window.clone());
        thread::spawn(move || handle(stream, s, w));
    }
}

// ── Entry point ───────────────────────────────────────────────────────────────

fn main() {
    let cookies = load_cookies();

    let state = Arc::new(State {
        cache:      Mutex::new(None),
        page_ready: AtomicBool::new(false),
        fetch_lock: Mutex::new(()),
        tray:       Mutex::new(None),
    });

    tauri::Builder::default()
        .setup(move |app| {
            // System tray
            let menu = tauri::menu::Menu::with_items(app, &[
                &tauri::menu::MenuItem::with_id(
                    app, "pair", "Pair with LXC...", true, None::<&str>,
                )?,
                &tauri::menu::PredefinedMenuItem::separator(app)?,
                &tauri::menu::MenuItem::with_id(
                    app, "exit", "Exit", true, None::<&str>,
                )?,
            ])?;

            let state_tray = Arc::clone(&state);
            let tray = tauri::tray::TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Suno Captcha — starting...")
                .menu(&menu)
                .on_menu_event(move |_app, ev| {
                    match ev.id().as_ref() {
                        "pair" => {
                            // Fire-and-forget broadcast. Code stays visible in
                            // tooltip for 60s so user can type it into the app.
                            let state = Arc::clone(&state_tray);
                            thread::spawn(move || {
                                let code = broadcast_pairing();
                                if let Some(t) = state.tray.lock().unwrap().as_ref() {
                                    let _ = t.set_tooltip(Some(
                                        &format!("Pairing code: {code}  (60s)")
                                    ));
                                    thread::sleep(Duration::from_secs(60));
                                    let _ = t.set_tooltip(Some("Suno Captcha — ready"));
                                }
                            });
                        }
                        "exit" => std::process::exit(0),
                        _ => {}
                    }
                })
                .build(app)?;

            *state.tray.lock().unwrap() = Some(tray);

            // Hidden WebView2 window loading suno.com
            let window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External("https://suno.com".parse()?),
            )
            .visible(false)
            .skip_taskbar(true)
            .build()?;

            // Background thread: inject cookies → reload → wait for Clerk handshake + hcaptcha
            let w_init = window.clone();
            let s_init = Arc::clone(&state);
            let ck = cookies.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(2000));
                #[cfg(windows)]
                inject_cookies(&w_init, ck);
                // Reload so injected cookies are sent with the request
                let _ = w_init.eval("window.location.reload()");

                // Poll until URL settles on suno.com without Clerk handshake params.
                // Clerk does a redirect dance (?__clerk_handshake=...) after cookie injection
                // — we must wait for it to complete before hcaptcha.js is available.
                let mut stable = 0u32;
                let mut last = String::new();
                for _ in 0..120 { // max 60s
                    thread::sleep(Duration::from_millis(500));
                    let url = w_init.url().map(|u| u.to_string()).unwrap_or_default();
                    let settled = url.contains("suno.com")
                        && !url.contains("__clerk_handshake")
                        && !url.contains("sign-in")
                        && !url.is_empty();
                    if settled && url == last {
                        stable += 1;
                        if stable >= 6 { break; } // stable for 3s
                    } else {
                        stable = 0;
                        last = url;
                    }
                }

                // Extra wait for hcaptcha.js to finish loading
                thread::sleep(Duration::from_secs(3));
                s_init.page_ready.store(true, Ordering::Relaxed);
                eprintln!("[captcha] ready at: {last}");
            });

            // HTTP server thread
            let s_http = Arc::clone(&state);
            thread::spawn(move || run_http(s_http, window));

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("suno-captcha error");
}
