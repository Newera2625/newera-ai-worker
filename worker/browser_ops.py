"""浏览器操作内核 —— 网站本机与 GitHub Actions 工具机共用。

设计要点：
- 同步 Playwright，所有调用必须发生在同一个线程（网站用单线程执行器，工具机用专用线程）。
- 每个 session 一个独立 BrowserContext + Page，可多步操作（打开→点击→输入→截图）。
- 只允许 http/https，禁止 file://、chrome://、javascript:，避免工具机被用来读取本地文件。
- 返回统一结构：{ok, action, session_id, page:{title,url,text,links}, screenshot_b64, error}
"""
import base64
import re
import threading
import time
import uuid

MAX_TEXT = 8000
MAX_LINKS = 40
SHOT_QUALITY = 72

BLOCKED_SCHEMES = ("file:", "chrome:", "chrome-extension:", "devtools:", "javascript:", "data:")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SNAPSHOT_JS = """
() => {
  const body = document.body;
  let text = body ? (body.innerText || '') : '';
  text = text.replace(/\\n{3,}/g, '\\n\\n').slice(0, 9000);
  const links = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    const h = a.href || '';
    if (!t || !/^https?:/i.test(h) || seen.has(h)) continue;
    seen.add(h);
    links.push({text: t, href: h});
    if (links.length >= 40) break;
  }
  return {title: document.title || '', url: location.href, text, links};
}
"""


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("url 不能为空")
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}", url, re.I):
            url = "https://" + url
        else:
            raise ValueError("只支持 http/https 网址")
    low = url.lower()
    for s in BLOCKED_SCHEMES:
        if low.startswith(s):
            raise ValueError("不支持的协议")
    return url


class BrowserOps:
    def __init__(self, max_sessions=6, idle_timeout=600):
        self._pw = None
        self._browser = None
        self._sessions = {}
        self._max_sessions = max_sessions
        self._idle_timeout = idle_timeout
        self._lock = threading.RLock()

    # ---------- lifecycle ----------
    def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--lang=zh-CN",
            ],
        )

    def _cleanup(self):
        now = time.time()
        dead = [sid for sid, s in self._sessions.items() if now - s["last"] > self._idle_timeout]
        for sid in dead:
            self._close_session(sid)
        if len(self._sessions) > self._max_sessions:
            oldest = sorted(self._sessions.items(), key=lambda kv: kv[1]["last"])
            for sid, _ in oldest[: len(self._sessions) - self._max_sessions]:
                self._close_session(sid)

    def _close_session(self, sid):
        s = self._sessions.pop(sid, None)
        if not s:
            return
        try:
            s["context"].close()
        except Exception:
            pass

    def _new_session(self):
        self._ensure_browser()
        self._cleanup()
        ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            java_script_enabled=True,
            accept_downloads=False,
        )
        page = ctx.new_page()
        page.set_default_timeout(20000)
        page.set_default_navigation_timeout(30000)
        sid = "s_" + uuid.uuid4().hex[:12]
        self._sessions[sid] = {"context": ctx, "page": page, "last": time.time(), "created": time.time()}
        return sid, self._sessions[sid]

    def _get(self, sid):
        s = self._sessions.get(sid)
        if not s:
            raise ValueError("会话不存在或已过期，请重新调用 browser_navigate")
        s["last"] = time.time()
        return s

    # ---------- snapshot ----------
    def _snapshot(self, page, full_page=False, with_shot=True):
        try:
            page.wait_for_timeout(200)
        except Exception:
            pass
        info = {}
        try:
            info = page.evaluate(SNAPSHOT_JS) or {}
        except Exception:
            try:
                info = {"title": page.title(), "url": page.url, "text": "", "links": []}
            except Exception:
                info = {"title": "", "url": "", "text": "", "links": []}
        data = {
            "title": (info.get("title") or "")[:300],
            "url": (info.get("url") or "")[:2000],
            "text": (info.get("text") or "")[:MAX_TEXT],
            "links": (info.get("links") or [])[:MAX_LINKS],
            "full_page": bool(full_page),
        }
        shot = None
        if with_shot:
            try:
                png = page.screenshot(full_page=bool(full_page), type="jpeg", quality=SHOT_QUALITY)
                shot = base64.b64encode(png).decode("ascii")
            except Exception:
                shot = None
        return data, shot

    def _after(self, page, wait_ms=900):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass

    # ---------- actions ----------
    def navigate(self, params):
        url = _clean_url(params.get("url"))
        full_page = bool(params.get("full_page"))
        session_id = params.get("session_id")
        if session_id:
            s = self._get(session_id)
            page = s["page"]
        else:
            session_id, s = self._new_session()
            page = s["page"]
        page.goto(url, wait_until="domcontentloaded", timeout=int(params.get("timeout") or 30000))
        self._after(page, int(params.get("wait_ms") or 1200))
        data, shot = self._snapshot(page, full_page=full_page)
        return {"ok": True, "action": "navigate", "session_id": session_id,
                "page": data, "screenshot_b64": shot}

    def screenshot(self, params):
        s = self._get(params.get("session_id") or "")
        data, shot = self._snapshot(s["page"], full_page=bool(params.get("full_page")))
        return {"ok": True, "action": "screenshot", "session_id": params.get("session_id"),
                "page": data, "screenshot_b64": shot}

    def click(self, params):
        s = self._get(params.get("session_id") or "")
        page = s["page"]
        selector = (params.get("selector") or "").strip()
        if selector:
            page.click(selector, timeout=int(params.get("timeout") or 15000))
        elif params.get("x") is not None and params.get("y") is not None:
            page.mouse.click(float(params["x"]), float(params["y"]))
        else:
            raise ValueError("需要 selector 或 x/y 坐标")
        self._after(page)
        data, shot = self._snapshot(page)
        return {"ok": True, "action": "click", "session_id": params.get("session_id"),
                "page": data, "screenshot_b64": shot}

    def type(self, params):
        s = self._get(params.get("session_id") or "")
        page = s["page"]
        text = str(params.get("text") or "")
        if not text:
            raise ValueError("text 不能为空")
        selector = (params.get("selector") or "").strip()
        if selector:
            page.fill(selector, text, timeout=int(params.get("timeout") or 15000))
        else:
            page.keyboard.type(text)
        if params.get("submit"):
            page.keyboard.press("Enter")
        self._after(page)
        data, shot = self._snapshot(page)
        return {"ok": True, "action": "type", "session_id": params.get("session_id"),
                "page": data, "screenshot_b64": shot}

    def press(self, params):
        s = self._get(params.get("session_id") or "")
        key = (params.get("key") or "").strip()
        if not key:
            raise ValueError("key 不能为空")
        s["page"].keyboard.press(key)
        self._after(s["page"], 700)
        data, shot = self._snapshot(s["page"])
        return {"ok": True, "action": "press", "session_id": params.get("session_id"),
                "page": data, "screenshot_b64": shot}

    def scroll(self, params):
        s = self._get(params.get("session_id") or "")
        dy = int(params.get("dy") or 600)
        s["page"].mouse.wheel(0, dy)
        self._after(s["page"], 700)
        data, shot = self._snapshot(s["page"])
        return {"ok": True, "action": "scroll", "session_id": params.get("session_id"),
                "page": data, "screenshot_b64": shot}

    def back(self, params):
        s = self._get(params.get("session_id") or "")
        s["page"].go_back(wait_until="domcontentloaded", timeout=20000)
        self._after(s["page"])
        data, shot = self._snapshot(s["page"])
        return {"ok": True, "action": "back", "session_id": params.get("session_id"),
                "page": data, "screenshot_b64": shot}

    def close(self, params):
        sid = params.get("session_id") or ""
        self._close_session(sid)
        return {"ok": True, "action": "close", "session_id": sid, "page": {}, "screenshot_b64": None}

    # ---------- entry ----------
    def run(self, action, params=None):
        params = dict(params or {})
        with self._lock:
            try:
                fn = getattr(self, action, None)
                if action not in ("navigate", "screenshot", "click", "type", "press",
                                  "scroll", "back", "close") or fn is None:
                    return {"ok": False, "action": action, "error": "不支持的工具动作: %s" % action}
                out = fn(params)
                out["sessions"] = len(self._sessions)
                return out
            except Exception as e:
                msg = str(e).strip().replace("\n", " ")[:500] or e.__class__.__name__
                return {"ok": False, "action": action, "error": msg,
                        "session_id": params.get("session_id"), "sessions": len(self._sessions)}

    def count(self):
        with self._lock:
            return len(self._sessions)

    def cleanup(self):
        with self._lock:
            self._cleanup()

    def shutdown(self):
        with self._lock:
            for sid in list(self._sessions):
                self._close_session(sid)
            try:
                if self._browser:
                    self._browser.close()
            except Exception:
                pass
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._browser = None
            self._pw = None
