#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::sync::mpsc;
use std::time::Duration;

#[cfg(windows)]
use {
    webview2_com::Microsoft::Web::WebView2::Win32::{
        ICoreWebView2_2, ICoreWebView2CookieList,
        ICoreWebView2GetCookiesCompletedHandler,
        ICoreWebView2GetCookiesCompletedHandler_Impl,
    },
    windows::core::{implement, Interface, HRESULT, HSTRING},
    windows_core::Ref,
};

// ── Cookie handler — collects ALL cookies ─────────────────────────────────────

#[cfg(windows)]
#[implement(ICoreWebView2GetCookiesCompletedHandler)]
struct CookieHandler {
    tx: mpsc::SyncSender<HashMap<String, String>>,
}

#[cfg(windows)]
impl ICoreWebView2GetCookiesCompletedHandler_Impl for CookieHandler_Impl {
    fn Invoke(
        &self,
        _error_code: HRESULT,
        cookie_list: Ref<'_, ICoreWebView2CookieList>,
    ) -> windows_core::Result<()> {
        let mut map = HashMap::new();
        if let Some(list) = cookie_list.as_ref() {
            unsafe {
                let mut count: u32 = 0;
                let _ = list.Count(&mut count as *mut u32);
                for i in 0..count {
                    let Ok(cookie) = list.GetValueAtIndex(i) else { continue };
                    let mut name_ptr = windows_core::PWSTR(std::ptr::null_mut());
                    let mut val_ptr  = windows_core::PWSTR(std::ptr::null_mut());
                    if cookie.Name(&mut name_ptr).is_err() { continue; }
                    let name = pwstr_to_string(name_ptr);
                    if cookie.Value(&mut val_ptr).is_ok() {
                        map.insert(name, pwstr_to_string(val_ptr));
                    }
                }
            }
        }
        let _ = self.tx.send(map);
        Ok(())
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

#[cfg(windows)]
unsafe fn pwstr_to_string(p: windows_core::PWSTR) -> String {
    if p.0.is_null() { return String::new(); }
    let mut len = 0usize;
    while *p.0.add(len) != 0 { len += 1; }
    String::from_utf16_lossy(std::slice::from_raw_parts(p.0 as *const u16, len))
}

fn is_post_login(url: &str) -> bool {
    if !url.contains("suno.com") { return false; }
    let deny = ["/sign-in", "/sign-up", "/login", "/register", "/oauth", "/callback", "/sso"];
    !deny.iter().any(|s| url.contains(s))
}

fn save_cookies(cookies: &HashMap<String, String>) -> Result<(), String> {
    let dir = dirs::home_dir().ok_or("no home dir")?.join(".suno");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;

    let path = dir.join("config.json");
    let mut cfg: serde_json::Value = if path.exists() {
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap_or_default())
            .unwrap_or(serde_json::json!({}))
    } else {
        serde_json::json!({})
    };

    // Save ALL cookies under "browser_cookies" for suno.py to build the Cookie header
    let cookie_map: serde_json::Map<String, serde_json::Value> = cookies
        .iter()
        .map(|(k, v)| (k.clone(), serde_json::Value::String(v.clone())))
        .collect();
    cfg["browser_cookies"] = serde_json::Value::Object(cookie_map);

    // Also keep "session_cookie" for the Suno-issued __session JWT
    // (may be needed as Bearer or for other purposes)
    if let Some(v) = cookies.get("__session") {
        cfg["session_cookie"] = serde_json::Value::String(v.clone());
    }

    // Clear stale short-lived JWT so suno.py refreshes immediately
    cfg["jwt"] = serde_json::Value::Null;
    cfg["jwt_saved_at"] = serde_json::Value::Null;

    std::fs::write(&path, serde_json::to_string_pretty(&cfg).unwrap())
        .map_err(|e| e.to_string())?;

    let names: Vec<&str> = cookies.keys().map(String::as_str).collect();
    println!("Saved {} cookies: {:?} → {}", cookies.len(), names, path.display());
    Ok(())
}

// ── WebView2 cookie extraction (gets ALL cookies for all domains) ─────────────

#[cfg(windows)]
fn extract_all_cookies(window: &tauri::WebviewWindow) -> Result<HashMap<String, String>, String> {
    let (tx, rx) = mpsc::sync_channel::<HashMap<String, String>>(1);

    window
        .with_webview(move |wv| {
            let r: windows_core::Result<()> = (|| {
                unsafe {
                    let ctrl = wv.controller();
                    let core2: ICoreWebView2_2 = ctrl.CoreWebView2()?.cast()?;
                    let mgr = core2.CookieManager()?;
                    let handler: ICoreWebView2GetCookiesCompletedHandler =
                        CookieHandler { tx }.into();
                    // Empty URI = get ALL cookies from all domains (incl. clerk.suno.com)
                    mgr.GetCookies(&HSTRING::from(""), &handler)?;
                    Ok(())
                }
            })();
            if let Err(e) = r { eprintln!("COM error: {e}"); }
        })
        .map_err(|e| format!("with_webview: {e}"))?;

    match rx.recv_timeout(Duration::from_secs(6)) {
        Ok(map) if !map.is_empty() => Ok(map),
        Ok(_)  => Err("no cookies found".into()),
        Err(_) => Err("cookie extraction timed out".into()),
    }
}

// ── Success overlay ───────────────────────────────────────────────────────────

const SUCCESS_JS: &str = r#"
(function() {
    var d = document.createElement('div');
    d.style.cssText = 'position:fixed;inset:0;background:#0a0a0a;z-index:2147483647;'
        + 'display:flex;flex-direction:column;align-items:center;justify-content:center;'
        + 'font-family:system-ui,-apple-system,sans-serif;color:#fff';
    d.innerHTML =
        '<div style="font-size:72px;line-height:1;margin-bottom:24px;color:#22c55e">&#10003;</div>'
        + '<h2 style="margin:0 0 8px;font-size:22px;font-weight:600">Login exitoso</h2>'
        + '<p style="color:#555;margin:0;font-size:14px">Cerrando...</p>';
    document.body.appendChild(d);
})();
"#;

// ── Entry point ───────────────────────────────────────────────────────────────

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External("https://suno.com/sign-in".parse()?),
            )
            .title("Suno Login")
            .inner_size(480.0, 740.0)
            .resizable(true)
            .build()?;

            let window_bg = window.clone();
            std::thread::spawn(move || {
                let mut last_url = String::new();
                loop {
                    std::thread::sleep(Duration::from_millis(900));

                    let url = match window_bg.url() {
                        Ok(u) => u.to_string(),
                        Err(_) => return,
                    };

                    if url == last_url || !is_post_login(&url) {
                        if !url.is_empty() { last_url = url; }
                        continue;
                    }
                    last_url = url;

                    // Wait for Clerk to finish setting cookies
                    std::thread::sleep(Duration::from_millis(1800));

                    #[cfg(windows)]
                    match extract_all_cookies(&window_bg) {
                        Ok(cookies) => {
                            // Need at least one Clerk/Suno auth cookie to proceed
                            let has_auth = cookies.contains_key("__session")
                                || cookies.contains_key("__client_uat")
                                || cookies.keys().any(|k| k.starts_with("__clerk"));
                            if !has_auth {
                                continue; // still loading, retry
                            }
                            match save_cookies(&cookies) {
                                Ok(()) => {
                                    let _ = window_bg.eval(SUCCESS_JS);
                                    std::thread::sleep(Duration::from_secs(2));
                                    let _ = window_bg.close();
                                    return;
                                }
                                Err(e) => eprintln!("save failed: {e}"),
                            }
                        }
                        Err(e) => {
                            if !e.contains("no cookies") { eprintln!("cookie error: {e}"); }
                        }
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("suno-login error");
}
