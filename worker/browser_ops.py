"""浏览器操作内核 —— 网站本机与 GitHub Actions 工具机共用。

设计要点：
- 同步 Playwright，所有调用必须发生在同一个线程（网站用单线程执行器，工具机用专用线程）。
- 每个 session 一个独立 BrowserContext + Page，可多步操作（打开→点击→输入→截图）。
- 只允许 http/https，禁止 file://、chrome://、javascript:，避免工具机被用来读取本地文件。
- 返回统一结构：{ok, action, session_id, page:{title,url,text,links}, screenshot_b64, error}
"""
import base64
import os
import re
import threading
import time
import uuid

MAX_TEXT = 8000
MAX_LINKS = 40
SHOT_QUALITY = 72

BLOCKED_SCHEMES = ("file:", "chrome:", "chrome-extension:", "devtools:", "javascript:", "data:")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--lang=zh-CN",
    "--window-size=1920,1080",
    "--start-maximized",
    "--use-gl=angle",
    "--use-angle=swiftshader",
]

# 让网站看到一台普通的 Windows 桌面 Chrome，而不是 Playwright/无头浏览器
STEALTH_JS = r"""
(() => {
  const defineGetter = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); } catch (e) {}
  };
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false, configurable: true }); } catch (e) {}
  defineGetter(Navigator.prototype, 'platform', 'Win32');
  defineGetter(Navigator.prototype, 'vendor', 'Google Inc.');
  defineGetter(Navigator.prototype, 'productSub', '20030107');
  defineGetter(Navigator.prototype, 'languages', ['zh-CN', 'zh', 'en']);
  defineGetter(Navigator.prototype, 'hardwareConcurrency', 8);
  defineGetter(Navigator.prototype, 'deviceMemory', 8);
  defineGetter(Navigator.prototype, 'maxTouchPoints', 0);
  defineGetter(Navigator.prototype, 'pdfViewerEnabled', true);
  defineGetter(Navigator.prototype, 'appVersion', '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36');

  const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer', 'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
  const plugins = names.map(name => {
    let p;
    try { p = Object.create(Plugin.prototype); } catch (e) { p = {}; }
    let mime0;
    try { mime0 = Object.create(MimeType.prototype); } catch (e) { mime0 = {}; }
    try {
      Object.defineProperties(mime0, {
        type: { value: 'application/pdf', enumerable: true },
        suffixes: { value: 'pdf', enumerable: true },
        description: { value: 'Portable Document Format', enumerable: true },
        enabledPlugin: { value: p, enumerable: true }
      });
      Object.defineProperties(p, {
        name: { value: name, enumerable: true },
        filename: { value: 'internal-pdf-viewer', enumerable: true },
        description: { value: 'Portable Document Format', enumerable: true },
        length: { value: 1, enumerable: true },
        0: { value: mime0, enumerable: true }
      });
    } catch (e) {}
    return p;
  });
  let pluginArray;
  try {
    pluginArray = Object.create(PluginArray.prototype);
    plugins.forEach((p, i) => {
      Object.defineProperty(pluginArray, i, { get: () => p, enumerable: true, configurable: true });
      Object.defineProperty(pluginArray, p.name, { get: () => p, enumerable: false, configurable: true });
    });
    Object.defineProperty(pluginArray, 'length', { get: () => plugins.length, configurable: true });
  } catch (e) {
    pluginArray = Object.assign([], plugins);
    Object.defineProperty(pluginArray, 'length', { get: () => plugins.length, configurable: true });
  }
  pluginArray.item = i => plugins[i] || null;
  pluginArray.namedItem = n => plugins.find(p => p.name === n) || null;
  pluginArray.refresh = () => {};
  defineGetter(Navigator.prototype, 'plugins', pluginArray);

  const mime = plugins[0][0];
  let mimeArray;
  try {
    mimeArray = Object.create(MimeTypeArray.prototype);
    Object.defineProperty(mimeArray, 0, { get: () => mime, enumerable: true, configurable: true });
    Object.defineProperty(mimeArray, 'length', { get: () => 1, configurable: true });
  } catch (e) {
    mimeArray = [mime];
  }
  mimeArray.item = i => (i === 0 ? mime : null);
  mimeArray.namedItem = n => n === 'application/pdf' ? mime : null;
  defineGetter(Navigator.prototype, 'mimeTypes', mimeArray);

  window.chrome = window.chrome || {};
  window.chrome.runtime = window.chrome.runtime || {};
  window.chrome.app = window.chrome.app || {
    isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { RUNNING: 'running', CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run' }
  };
  window.chrome.csi = window.chrome.csi || function () { return {}; };
  window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };

  try {
    const uaData = {
      brands: [
        { brand: 'Chromium', version: '136' },
        { brand: 'Google Chrome', version: '136' },
        { brand: 'Not.A/Brand', version: '99' }
      ],
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: async () => ({
        architecture: 'x86', bitness: '64', brands: uaData.brands,
        fullVersionList: [
          { brand: 'Chromium', version: '136.0.0.0' },
          { brand: 'Google Chrome', version: '136.0.0.0' },
          { brand: 'Not.A/Brand', version: '99.0.0.0' }
        ],
        mobile: false, model: '', platform: 'Windows',
        platformVersion: '15.0.0', uaFullVersion: '136.0.0.0', wow64: false
      }),
      toJSON: () => ({ brands: uaData.brands, mobile: false, platform: 'Windows' })
    };
    Object.defineProperty(Navigator.prototype, 'userAgentData', { get: () => uaData, configurable: true });
  } catch (e) {}

  try { Object.defineProperty(Notification, 'permission', { get: () => 'default', configurable: true }); } catch (e) {}
  try {
    const query = navigator.permissions && navigator.permissions.query;
    if (query) {
      navigator.permissions.query = p => {
        if (p && p.name === 'notifications') return Promise.resolve({ state: 'prompt', onchange: null });
        return query.call(navigator.permissions, p);
      };
    }
  } catch (e) {}

  const spoofGL = proto => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return 'Google Inc. (NVIDIA)';
      if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return orig.apply(this, arguments);
    };
  };
  try { spoofGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { spoofGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}

  try {
    const origToString = Function.prototype.toString;
    const fake = new WeakSet();
    if (window.chrome.csi) fake.add(window.chrome.csi);
    if (window.chrome.loadTimes) fake.add(window.chrome.loadTimes);
    Function.prototype.toString = function () {
      if (fake.has(this)) return 'function () { [native code] }';
      return origToString.call(this);
    };
  } catch (e) {}
})();
"""

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

        env_headless = os.environ.get("BROWSER_HEADLESS", "1").strip().lower()
        headless = env_headless not in ("0", "false", "no", "off")
        channel = os.environ.get("BROWSER_CHANNEL", "chrome").strip() or None
        attempts = []
        if channel:
            attempts.append((channel, headless))
        attempts.append((None, headless))
        if not headless:
            if channel:
                attempts.append((channel, True))
            attempts.append((None, True))

        seen = set()
        last = None
        for ch, hl in attempts:
            key = (ch or "", hl)
            if key in seen:
                continue
            seen.add(key)
            try:
                kwargs = {"headless": hl, "args": LAUNCH_ARGS}
                if ch:
                    kwargs["channel"] = ch
                self._browser = self._pw.chromium.launch(**kwargs)
                print("[browser] launched channel=%s headless=%s" % (ch or "chromium", hl), flush=True)
                return
            except Exception as e:
                last = e
                print("[browser] launch failed channel=%s headless=%s: %s" %
                      (ch or "chromium", hl, str(e)[:180]), flush=True)
        raise RuntimeError("浏览器启动失败: %s" % (str(last)[:300] if last else "unknown"))

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
        ctx_kwargs = {
            "viewport": {"width": 1366, "height": 768},
            "screen": {"width": 1920, "height": 1080},
            "user_agent": UA,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "color_scheme": "light",
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
            "accept_downloads": False,
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-CH-UA-Mobile": "?0",
            },
        }
        try:
            ctx = self._browser.new_context(**ctx_kwargs)
        except TypeError:
            ctx_kwargs.pop("screen", None)
            ctx = self._browser.new_context(**ctx_kwargs)
        ctx.add_init_script(STEALTH_JS)
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
