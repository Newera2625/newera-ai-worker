#!/usr/bin/env python3
"""新启年 AI 工具机代理（GitHub Actions Runner）。

- 在本机 127.0.0.1:8080 提供 /health 与 /run
- 通过 SSH -R 把 8080 映射到香港 VPS 的指定端口
- 主站经本机 SSH -L 转发到香港，再进入本 Agent，形成 主站→香港→工具机 的链路
- 支持浏览器操作与临时终端命令；到期自动退出
"""
import argparse
import base64
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_ops import BrowserOps  # noqa: E402

VERSION = "1.0.0"
STOP = threading.Event()
JOBS = queue.Queue(maxsize=4)
STATE = {
    "busy": False, "job": "", "sessions": 0,
    "os": "", "slot": 0, "worker_id": "", "started": time.time(),
    "last_error": "", "done": 0,
}

# ---------------------------------------------------------------- terminal
BLOCK_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(\s|$|\*)",
    r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/(\s|$|\*)",
    r"\bmkfs(\.|\s)", r"\bdd\b[^\n]*of=/dev/(sd|nvme|vd|hd)",
    r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r":\s*\(\s*\)\s*\{",
    r"\b(ssh|scp|sftp)\b", r"\b(sudo|su)\s", r"\.ssh", r"ai_worker_tunnel",
    r"/proc/[^\s]*environ", r"\b(printenv|env)\s*($|\|)",
    r"AI_WORKER_TOKEN", r"GITHUB_TOKEN", r"ACTIONS_RUNTIME_TOKEN",
    r"\bchmod\s+-R\s+777\s+/", r"\bchown\s+-R\b[^\n]*\s+/(\s|$)",
]
BLOCK_RE = re.compile("|".join("(?:%s)" % p for p in BLOCK_PATTERNS), re.I)
SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|ACTIONS|GITHUB|SSH|RUNNER_|NPM_TOKEN)", re.I)


class TerminalOps:
    def __init__(self, os_name, max_sessions=8, idle_timeout=1800):
        self.os_name = os_name
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        candidates = ([r"C:\workspace", r"C:\Users\Public\workspace"]
                      if os_name == "windows" else ["/tmp/workspace"])
        self.root = None
        for base in candidates:
            try:
                os.makedirs(base, exist_ok=True)
                probe = tempfile.mkdtemp(prefix=".probe_", dir=base)
                os.rmdir(probe)
                self.root = os.path.abspath(base)
                break
            except Exception:
                continue
        if not self.root:
            self.root = os.path.abspath(os.path.join(tempfile.gettempdir(), "workspace"))
            os.makedirs(self.root, exist_ok=True)
        self.sessions = {}
        self.lock = threading.RLock()

    def count(self):
        with self.lock:
            return len(self.sessions)

    def _cleanup(self):
        now = time.time()
        for sid in [s for s, v in self.sessions.items() if now - v["last"] > self.idle_timeout]:
            self._drop(sid)
        if len(self.sessions) > self.max_sessions:
            for sid, _ in sorted(self.sessions.items(), key=lambda kv: kv[1]["last"])[:len(self.sessions) - self.max_sessions]:
                self._drop(sid)

    def _drop(self, sid):
        s = self.sessions.pop(sid, None)
        if s:
            try:
                shutil.rmtree(s["dir"], ignore_errors=True)
            except Exception:
                pass

    def _new(self):
        self._cleanup()
        sid = "t_" + uuid.uuid4().hex[:12]
        d = tempfile.mkdtemp(prefix="s_", dir=self.root)
        self.sessions[sid] = {"dir": d, "cwd": d, "created": time.time(), "last": time.time()}
        return sid, self.sessions[sid]

    def _resolve_cwd(self, s, cwd):
        if not cwd:
            return s["cwd"]
        cwd = str(cwd)
        if not os.path.isabs(cwd):
            cwd = os.path.join(s["cwd"], cwd)
        real = os.path.realpath(cwd)
        root = os.path.realpath(self.root)
        if not (real == root or real.startswith(root + os.sep)):
            raise ValueError("cwd 必须位于工具机会话目录内")
        if not os.path.isdir(real):
            os.makedirs(real, exist_ok=True)
        return real

    def _env(self, s):
        env = {}
        for k, v in os.environ.items():
            if SECRET_KEY_RE.search(k):
                continue
            env[k] = v
        env["HOME"] = s["dir"]
        env["TMPDIR"] = s["dir"]
        env["TEMP"] = s["dir"]
        env["TMP"] = s["dir"]
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        env["TERM"] = "dumb"
        env["NEWERA_AI_TERM"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def run(self, params):
        cmd = str(params.get("command") or "").strip()
        if not cmd:
            return {"ok": False, "error": "command 不能为空"}
        if len(cmd) > 8000:
            return {"ok": False, "error": "命令过长（最多 8000 字符）"}
        if BLOCK_RE.search(cmd):
            return {"ok": False, "error": "命令包含被禁止的敏感/危险操作（密钥、SSH、磁盘、关机等）"}
        sid = str(params.get("session_id") or "")
        with self.lock:
            if sid:
                s = self.sessions.get(sid)
                if not s:
                    return {"ok": False, "error": "终端会话不存在或已过期，请重新执行（不传 session_id）"}
            else:
                sid, s = self._new()
            try:
                cwd = self._resolve_cwd(s, params.get("cwd"))
            except Exception as e:
                return {"ok": False, "error": str(e), "session_id": sid}
            s["last"] = time.time()

        shell = str(params.get("shell") or "").lower()
        timeout = int(min(max(float(params.get("timeout") or 30), 3), 120))
        if self.os_name == "windows":
            if shell == "powershell":
                argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd]
                shell_name = "powershell"
            else:
                argv = ["cmd.exe", "/d", "/s", "/c", "chcp 65001 >nul & " + cmd]
                shell_name = "cmd"
        else:
            if shell in ("cmd", "powershell"):
                return {"ok": False, "error": "Linux 工具机不支持 %s，请使用 bash" % shell, "session_id": sid}
            argv = ["/bin/bash", "-lc", cmd]
            shell_name = "bash"

        started = time.time()
        try:
            p = subprocess.run(argv, cwd=cwd, env=self._env(s), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
            exit_code = p.returncode
            stdout = p.stdout or ""
            stderr = p.stderr or ""
        except subprocess.TimeoutExpired as e:
            exit_code = -1
            stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
            stderr = "命令执行超时（%d 秒）" % timeout
        except Exception as e:
            return {"ok": False, "error": "命令启动失败：%s" % str(e)[:300], "session_id": sid}

        elapsed = round(time.time() - started, 2)
        s["cwd"] = cwd
        s["last"] = time.time()
        truncated = False
        if len(stdout) > 8000:
            stdout = stdout[:8000] + "\n…(stdout 已截断)"
            truncated = True
        if len(stderr) > 4000:
            stderr = stderr[:4000] + "\n…(stderr 已截断)"
            truncated = True
        output = stdout
        if stderr.strip():
            output += ("\n[stderr]\n" if output else "") + stderr
        if not output.strip():
            output = "（无输出）"
        return {
            "ok": True, "action": "terminal_exec", "session_id": sid,
            "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "output": output, "cwd": cwd, "shell": shell_name,
            "elapsed": elapsed, "truncated": truncated, "sessions": self.count(),
        }


# ---------------------------------------------------------------- desktop apps
class DesktopOps:
    """在虚拟显示器上启动桌面应用，截图并做简单鼠标/键盘操作。"""
    def __init__(self, os_name):
        self.os_name = os_name
        self.sessions = {}
        self.lock = threading.RLock()
        self._pg = None
        self._clip = None

    def count(self):
        with self.lock:
            return len(self.sessions)

    def _load(self):
        if self._pg is None:
            if self.os_name != "windows":
                auth = os.path.expanduser("~/.Xauthority")
                try:
                    if not os.path.exists(auth):
                        open(auth, "a").close()
                    os.environ.setdefault("XAUTHORITY", auth)
                except Exception:
                    pass
            import pyautogui
            pyautogui.FAILSAFE = False
            self._pg = pyautogui
        return self._pg

    def _clipboard(self):
        if self._clip is None:
            import pyperclip
            self._clip = pyperclip
        return self._clip

    def _hide_agent_windows(self):
        if self.os_name != "windows":
            return
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                try:
                    t = (w.title or "").lower()
                    if any(x in t for x in ("hosted-compute-agent", "hostedcomputeagent", "github")):
                        w.minimize()
                except Exception:
                    pass
        except Exception:
            pass

    def _env_display(self):
        env = dict(os.environ)
        if self.os_name != "windows":
            env.setdefault("DISPLAY", ":99")
            auth = os.path.expanduser("~/.Xauthority")
            try:
                if not os.path.exists(auth):
                    open(auth, "a").close()
            except Exception:
                pass
            env.setdefault("XAUTHORITY", auth)
        return env

    def _active_window(self):
        if self.os_name == "windows":
            try:
                import pygetwindow as gw
                w = gw.getActiveWindow()
                if w and w.title and w.width > 20 and w.height > 20:
                    return {"title": w.title, "x": int(w.left), "y": int(w.top),
                            "w": int(w.width), "h": int(w.height)}
            except Exception:
                pass
            return None
        try:
            env = self._env_display()
            wid = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True,
                                 timeout=5, env=env).stdout.strip()
            if not wid:
                return None
            name = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True,
                                  timeout=5, env=env).stdout.strip()
            geom = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                                  capture_output=True, text=True, timeout=5, env=env).stdout
            vals = {}
            for line in geom.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            x, y = int(vals.get("X", 0)), int(vals.get("Y", 0))
            w, h = int(vals.get("WIDTH", 0)), int(vals.get("HEIGHT", 0))
            if w > 20 and h > 20:
                return {"id": wid, "title": name, "x": x, "y": y, "w": w, "h": h}
        except Exception:
            pass
        return None

    @staticmethod
    def _png_size(data):
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return 0, 0

    def _capture_file(self, cmd_factory, win):
        path = os.path.join(tempfile.gettempdir(), "desk_%s.png" % uuid.uuid4().hex[:8])
        try:
            subprocess.run(cmd_factory(path), check=True, timeout=15, env=self._env_display(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not os.path.exists(path):
                return None
            data = open(path, "rb").read()
            try:
                os.remove(path)
            except Exception:
                pass
            w, h = self._png_size(data)
            if data:
                return base64.b64encode(data).decode("ascii"), (w or (win["w"] if win else 1920),
                                                               h or (win["h"] if win else 1080))
        except Exception:
            pass
        return None

    def _shot(self, win=None):
        if self.os_name != "windows":
            self._load()
            if win and win.get("id"):
                out = self._capture_file(lambda p: ["import", "-window", win["id"], p], win)
                if out:
                    return out
                out = self._capture_file(lambda p: ["scrot", "-u", "-o", p], win)
                if out:
                    return out
            out = self._capture_file(lambda p: ["scrot", "-o", p], None)
            if out:
                return out
        self._hide_agent_windows()
        pg = self._load()
        from io import BytesIO
        if win:
            try:
                img = pg.screenshot(region=(win["x"], win["y"], win["w"], win["h"]))
                buf = BytesIO()
                img.save(buf, format="PNG")
                return base64.b64encode(buf.getvalue()).decode("ascii"), img.size
            except Exception:
                pass
        img = pg.screenshot()
        buf = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii"), img.size

    def _get_window(self, sid):
        with self.lock:
            s = self.sessions.get(sid or "")
            return (s or {}).get("window")

    def _title_matches(self, app, title):
        low = (title or "").lower()
        return (app in low
                or (app in ("notepad", "text") and ("notepad" in low or "mousepad" in low or "gedit" in low or "text editor" in low))
                or (app in ("calculator", "calc") and "calc" in low)
                or (app in ("files", "explorer") and ("file" in low or "explorer" in low or "pcmanfm" in low or "nautilus" in low))
                or (app == "writer" and ("writer" in low or "word" in low))
                or (app == "paint" and "paint" in low)
                or (app == "chrome" and ("chrome" in low or "chromium" in low)))

    def _windows(self):
        bad = ("hosted-compute-agent", "hostedcomputeagent", "github", "system properties", "program manager")
        if self.os_name == "windows":
            try:
                import pygetwindow as gw
                return [w.title for w in gw.getAllWindows() if w.title and not any(b in (w.title or "").lower() for b in bad)]
            except Exception:
                return []
        try:
            out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
            titles = []
            for line in out.splitlines():
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    titles.append(parts[3])
            return [t for t in titles if not any(b in t.lower() for b in bad)]
        except Exception:
            return []

    def _focus(self, title):
        if not title:
            return
        if self.os_name == "windows":
            try:
                import pygetwindow as gw
                for w in gw.getWindowsWithTitle(title):
                    try:
                        w.activate()
                        time.sleep(0.4)
                        break
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            try:
                subprocess.run(["xdotool", "search", "--onlyvisible", "--name", title, "windowactivate"],
                               capture_output=True, timeout=5)
            except Exception:
                pass

    def _result(self, action, sid, shot, extra=None, win=None):
        out = {"ok": True, "action": action, "session_id": sid,
               "screenshot_b64": shot[0], "screen": [shot[1][0], shot[1][1]],
               "windows": self._windows()[:20], "sessions": self.count()}
        if win:
            out["window_title"] = win.get("title", "")
        if extra:
            out.update(extra)
        return out

    def launch(self, params):
        app = str(params.get("app") or "").strip().lower()
        if self.os_name == "windows":
            mapping = {
                "calculator": ["calc.exe"], "calc": ["calc.exe"],
                "notepad": ["notepad.exe"], "text": ["notepad.exe"],
                "files": ["explorer.exe"], "explorer": ["explorer.exe"],
                "paint": ["mspaint.exe"], "writer": ["wordpad.exe"],
                "chrome": ["chrome.exe"],
            }
        else:
            mapping = {
                "calculator": ["xcalc"], "calc": ["xcalc"],
                "notepad": ["mousepad"], "text": ["mousepad"],
                "files": ["pcmanfm"], "explorer": ["pcmanfm"],
                "writer": ["libreoffice", "--writer"],
                "chrome": ["google-chrome", "--start-maximized"],
            }
        if app not in mapping:
            return {"ok": False, "error": "不支持的应用，可用：" + "、".join(sorted(set(mapping))), "sessions": self.count()}
        env = dict(os.environ)
        if self.os_name != "windows":
            env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":99"))
        try:
            p = subprocess.Popen(mapping[app], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir())
        except Exception as e:
            return {"ok": False, "error": "应用启动失败：%s" % str(e)[:200], "sessions": self.count()}
        time.sleep(float(params.get("wait") or 2.5))
        titles = self._windows()
        win = None
        for t in titles:
            if self._title_matches(app, t):
                self._focus(t)
                time.sleep(0.45)
                win = self._active_window()
                break
        if win is None:
            win = self._active_window()
        sid = "d_" + uuid.uuid4().hex[:12]
        with self.lock:
            self.sessions[sid] = {"created": time.time(), "app": app, "window": win}
        shot = self._shot(win)
        return self._result("desktop_launch", sid, shot, {"app": app, "pid": p.pid}, win=win)

    def screenshot(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        if sid and win:
            with self.lock:
                s = self.sessions.get(sid)
                if s:
                    s["window"] = win
        shot = self._shot(win)
        return self._result("desktop_screenshot", sid or "d_desktop", shot, win=win)

    def click(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        pg = self._load()
        ox = win["x"] if win else 0
        oy = win["y"] if win else 0
        pg.click(ox + float(params.get("x") or 0), oy + float(params.get("y") or 0))
        time.sleep(0.5)
        return self._result("desktop_click", sid, self._shot(win), win=win)

    def type_text(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        pg = self._load()
        text = str(params.get("text") or "")
        if not text:
            return {"ok": False, "error": "text 不能为空", "sessions": self.count()}
        try:
            self._clipboard().copy(text)
            pg.hotkey("ctrl", "v")
        except Exception:
            pg.write(text, interval=0.02)
        if params.get("submit"):
            pg.press("enter")
        time.sleep(0.5)
        return self._result("desktop_type", sid, self._shot(win), win=win)

    def key(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        pg = self._load()
        pg.press(str(params.get("key") or "enter").strip())
        time.sleep(0.4)
        return self._result("desktop_key", sid, self._shot(win), win=win)

    def windows_list(self, params):
        return {"ok": True, "action": "desktop_windows", "session_id": params.get("session_id"),
                "windows": self._windows()[:40], "sessions": self.count()}

    def focus(self, params):
        title = str(params.get("title") or "").strip()
        sid = params.get("session_id")
        self._focus(title)
        time.sleep(0.5)
        win = self._active_window()
        if sid and win:
            with self.lock:
                s = self.sessions.get(sid)
                if s:
                    s["window"] = win
        return self._result("desktop_focus", sid, self._shot(win), {"title": title}, win=win)

    def run(self, action, params):
        with self.lock:
            try:
                fn = {
                    "desktop_launch": self.launch, "desktop_screenshot": self.screenshot,
                    "desktop_click": self.click, "desktop_type": self.type_text,
                    "desktop_key": self.key, "desktop_windows": self.windows_list,
                    "desktop_focus": self.focus,
                }.get(action)
                if not fn:
                    return {"ok": False, "error": "不支持的应用工具动作: %s" % action, "sessions": self.count()}
                return fn(params)
            except Exception as e:
                return {"ok": False, "action": action, "error": str(e)[:400], "sessions": self.count()}


# ---------------------------------------------------------------- job loop
def job_loop(os_name):
    bops = BrowserOps(max_sessions=4, idle_timeout=600)
    tops = TerminalOps(os_name)
    dops = DesktopOps(os_name)
    STATE["sessions"] = 0
    while not STOP.is_set():
        try:
            job = JOBS.get(timeout=1)
        except queue.Empty:
            continue
        if job is None:
            break
        STATE["busy"] = True
        STATE["job"] = job.get("job_id", "")
        started = time.time()
        try:
            action = job.get("action") or ""
            params = job.get("params") or {}
            if action == "terminal_exec":
                out = tops.run(params)
            elif action.startswith("desktop_"):
                out = dops.run(action, params)
            else:
                out = bops.run(action, params)
            out["elapsed"] = round(time.time() - started, 2)
            out["worker_id"] = STATE["worker_id"]
            STATE["sessions"] = bops.count() + tops.count() + dops.count()
            if not out.get("ok"):
                STATE["last_error"] = (out.get("error") or "")[:300]
            else:
                STATE["last_error"] = ""
        except Exception as e:
            out = {"ok": False, "error": ("工具机异常：" + str(e))[:400],
                   "worker_id": STATE["worker_id"]}
        job["result"] = out
        job["event"].set()
        STATE["busy"] = False
        STATE["job"] = ""
        STATE["done"] += 1


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "NeweraAIWorker/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send(200, {
                "ok": True, "version": VERSION, "os": STATE["os"],
                "slot": STATE["slot"], "worker_id": STATE["worker_id"],
                "busy": STATE["busy"], "job": STATE["job"],
                "sessions": STATE["sessions"], "done": STATE["done"],
                "uptime": int(time.time() - STATE["started"]), "last_error": STATE["last_error"],
            })
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/run":
            self._send(404, {"ok": False, "error": "not found"})
            return
        token = os.environ.get("AI_WORKER_TOKEN", "")
        got = self.headers.get("X-Worker-Token", "")
        if not token or not got or not _safe_eq(token, got):
            self._send(403, {"ok": False, "error": "invalid worker token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 2 * 1024 * 1024:
                raise ValueError("bad length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("bad body")
        except Exception:
            self._send(400, {"ok": False, "error": "bad request"})
            return
        job = {
            "job_id": body.get("job_id") or uuid.uuid4().hex[:12],
            "action": body.get("action") or "",
            "params": body.get("params") or {},
            "event": threading.Event(),
        }
        try:
            JOBS.put_nowait(job)
        except queue.Full:
            self._send(429, {"ok": False, "error": "worker busy"})
            return
        if not job["event"].wait(timeout=150):
            self._send(504, {"ok": False, "error": "worker job timeout"})
            return
        self._send(200, job.get("result") or {"ok": False, "error": "no result"})


def _safe_eq(a, b):
    try:
        import hmac
        return hmac.compare_digest(a, b)
    except Exception:
        return a == b


# ---------------------------------------------------------------- tunnel
def tunnel_loop(args):
    key_path = os.path.expanduser(args.key)
    if os.name == "nt" and os.path.exists(key_path):
        try:
            user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            subprocess.run(["icacls", key_path, "/inheritance:r", "/grant:r", user + ":(R)"],
                           capture_output=True, timeout=20)
        except Exception as e:
            print("[tunnel] icacls failed:", e, flush=True)
    null = "NUL" if os.name == "nt" else "/dev/null"
    log_path = os.path.join(tempfile.gettempdir(), "newera-ai-tunnel.log")
    cmd = [
        "ssh", "-N", "-i", key_path,
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + null,
        "-o", "LogLevel=ERROR", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=15",
        "-R", "127.0.0.1:%d:127.0.0.1:%d" % (args.remote_port, args.port),
        args.tunnel_user + "@" + args.tunnel_host, "-p", str(args.tunnel_port),
    ]
    deadline = time.time() + args.duration_min * 60
    quick_failures = 0
    while not STOP.is_set() and time.time() < deadline:
        started = time.time()
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as log:
                log.write("\n[%s] start tunnel -> HK 127.0.0.1:%d\n" %
                          (time.strftime("%F %T"), args.remote_port))
                log.flush()
                p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        except Exception as e:
            print("[tunnel] spawn failed:", e, flush=True)
            time.sleep(5)
            continue
        print("[tunnel] ssh pid %d -> HK 127.0.0.1:%d" % (p.pid, args.remote_port), flush=True)
        while p.poll() is None and not STOP.is_set() and time.time() < deadline:
            time.sleep(3)
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=8)
            except Exception:
                p.kill()
        ran = time.time() - started
        if STOP.is_set() or time.time() >= deadline:
            break
        if ran < 20:
            quick_failures += 1
            try:
                tail = open(log_path, "r", encoding="utf-8", errors="replace").read()[-600:]
                print("[tunnel] ssh exited quickly (%ds):\n%s" % (ran, tail), flush=True)
            except Exception:
                pass
        else:
            quick_failures = 0
        if quick_failures >= 3:
            print("[tunnel] failed 3 times, exiting worker", flush=True)
            STOP.set()
            break
        time.sleep(3)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--os", choices=["linux", "windows"], required=True)
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--duration-min", type=float, default=330)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--remote-port", type=int, default=0)
    ap.add_argument("--tunnel-host", default=os.environ.get("TUNNEL_HOST", ""))
    ap.add_argument("--tunnel-port", type=int, default=int(os.environ.get("TUNNEL_PORT", "0") or 0))
    ap.add_argument("--tunnel-user", default="tunnel")
    ap.add_argument("--key", default=os.path.expanduser("~/.ssh/ai_worker_tunnel"))
    ap.add_argument("--no-tunnel", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("AI_WORKER_TOKEN"):
        print("ERROR: AI_WORKER_TOKEN is empty", flush=True)
        sys.exit(2)
    if args.remote_port <= 0:
        args.remote_port = (25000 if args.os == "linux" else 26000) + args.slot
    if not args.no_tunnel and (not args.tunnel_host or not args.tunnel_port):
        print("ERROR: tunnel host/port missing (set TUNNEL_HOST / TUNNEL_PORT)", flush=True)
        sys.exit(2)
    if not args.no_tunnel and not os.path.exists(os.path.expanduser(args.key)):
        print("ERROR: tunnel key not found:", args.key, flush=True)
        sys.exit(2)

    STATE["os"] = args.os
    STATE["slot"] = args.slot
    STATE["worker_id"] = ("linux-%d" if args.os == "linux" else "win-%d") % args.slot
    STATE["started"] = time.time()

    if not args.no_tunnel:
        t = threading.Thread(target=tunnel_loop, args=(args,), daemon=True)
        t.start()
    w = threading.Thread(target=job_loop, args=(args.os,), daemon=True)
    w.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("AI worker %s listening on %s:%d, duration %.0f min, remote HK port %d" %
          (STATE["worker_id"], args.host, args.port, args.duration_min, args.remote_port), flush=True)

    def shutdown():
        STOP.set()
        JOBS.put(None)
        threading.Thread(target=server.shutdown, daemon=True).start()

    def on_signal(signum, frame):
        shutdown()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    timer = threading.Timer(max(args.duration_min * 60, 30), shutdown)
    timer.daemon = True
    timer.start()

    try:
        server.serve_forever(poll_interval=1)
    finally:
        STOP.set()
        try:
            server.server_close()
        except Exception:
            pass
        print("AI worker stopped at", time.strftime("%F %T"), flush=True)


if __name__ == "__main__":
    main()
