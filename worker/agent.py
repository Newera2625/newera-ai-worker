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
from task_sync import TaskSync  # noqa: E402
from validate_ops import ValidateOps  # noqa: E402
import mcp_server  # noqa: E402
import mcp_aggregate  # noqa: E402

# MCP 聚合网关：Playwright MCP + Filesystem MCP + Desktop Commander MCP + 原生工具
MCP_WORKDIR = os.path.abspath(os.path.join(tempfile.gettempdir(), "newera-projects"))

VERSION = "1.1.0"
STOP = threading.Event()
JOBS = queue.Queue(maxsize=8)
SERVICES = {"ops": None}
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
def _trim_uniform_border(img, tol=12, max_ratio=0.25):
    """自动裁掉四周纯色边框（如虚拟显示器的黑边/白边）。"""
    try:
        w, h = img.size
        px = img.load()
        corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
        bg = corners[0]
        if any(abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > tol * 3 for c in corners):
            return img

        def row_bg(y):
            step = max(1, w // 60)
            for x in range(0, w, step):
                c = px[x, y]
                if abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > tol * 3:
                    return False
            return True

        def col_bg(x):
            step = max(1, h // 60)
            for y in range(0, h, step):
                c = px[x, y]
                if abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > tol * 3:
                    return False
            return True

        top = 0
        while top < h * max_ratio and row_bg(top):
            top += 1
        bottom = h - 1
        while bottom > h * (1 - max_ratio) and row_bg(bottom):
            bottom -= 1
        left = 0
        while left < w * max_ratio and col_bg(left):
            left += 1
        right = w - 1
        while right > w * (1 - max_ratio) and col_bg(right):
            right -= 1
        if right - left < 40 or bottom - top < 40:
            return img
        return img.crop((left, top, right + 1, bottom + 1))
    except Exception:
        return img


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

    def _shot(self, win=None, crop=None, trim=False):
        shot = self._shot_raw(win)
        try:
            data, size = shot if isinstance(shot, tuple) else (shot, None)
            if not data:
                return shot
            if isinstance(crop, dict) or trim:
                from io import BytesIO
                from PIL import Image
                img = Image.open(BytesIO(base64.b64decode(data))).convert("RGB")
                if isinstance(crop, dict):
                    l = int(float(crop.get("left") or crop.get("x") or 0))
                    t = int(float(crop.get("top") or crop.get("y") or 0))
                    r = int(float(crop.get("right") or 0))
                    b = int(float(crop.get("bottom") or 0))
                    w = int(float(crop.get("w") or crop.get("width") or 0))
                    h = int(float(crop.get("h") or crop.get("height") or 0))
                    box = (max(0, l), max(0, t),
                           img.width - max(0, r) if r else (l + w if w else img.width),
                           img.height - max(0, b) if b else (t + h if h else img.height))
                    if box[2] > box[0] and box[3] > box[1]:
                        img = img.crop(box)
                if trim:
                    img = _trim_uniform_border(img)
                buf = BytesIO()
                img.save(buf, format="PNG")
                return base64.b64encode(buf.getvalue()).decode("ascii"), img.size
        except Exception as e:
            print("[shot-crop] %s" % str(e)[:120], flush=True)
        return shot

    def _shot_raw(self, win=None):
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
                or (app in ("calculator", "calc") and ("calc" in low or "galculator" in low))
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

    def _ocr_elements(self, b64):
        if not b64:
            return []
        tesseract = shutil.which("tesseract")
        if not tesseract and os.name == "nt":
            for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
                if os.path.exists(p):
                    tesseract = p
                    break
        if not tesseract:
            return []
        path = os.path.join(tempfile.gettempdir(), "ocr_%s.png" % uuid.uuid4().hex[:8])
        scale = 1
        try:
            raw = base64.b64decode(b64)
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw))
                scale = 3
                img = img.resize((max(1, img.width * scale), max(1, img.height * scale)), Image.LANCZOS)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("L")
                img.save(path, format="PNG")
            except Exception:
                with open(path, "wb") as f:
                    f.write(raw)
                scale = 1
            out = subprocess.run([tesseract, path, "stdout", "--psm", "6", "tsv"],
                                 capture_output=True, text=True, timeout=25).stdout
            els = []
            for line in out.splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) < 12:
                    continue
                try:
                    conf = float(parts[10])
                    left = int(float(parts[6])) // scale
                    top = int(float(parts[7])) // scale
                    w = max(1, int(float(parts[8])) // scale)
                    h = max(1, int(float(parts[9])) // scale)
                except Exception:
                    continue
                text = (parts[11] or "").strip()
                if conf < 25 or not text or len(text) > 40:
                    continue
                els.append({"text": text, "x": left, "y": top, "w": w, "h": h,
                            "cx": left + w // 2, "cy": top + h // 2, "conf": int(conf)})
            seen = set()
            outels = []
            for e in sorted(els, key=lambda e: (e["y"], e["x"])):
                key = (e["text"], e["cx"] // 10, e["cy"] // 10)
                if key in seen:
                    continue
                seen.add(key)
                outels.append(e)
            return outels[:40]
        except Exception:
            return []
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def _result(self, action, sid, shot, extra=None, win=None):
        out = {"ok": True, "action": action, "session_id": sid,
               "screenshot_b64": shot[0], "screen": [shot[1][0], shot[1][1]],
               "windows": self._windows()[:20], "sessions": self.count()}
        if win:
            out["window_title"] = win.get("title", "")
        try:
            elements = self._ocr_elements(shot[0])
            if elements:
                out["elements"] = elements
        except Exception:
            pass
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
                "calculator": ["galculator"], "calc": ["galculator"],
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
        shot = self._shot(win, crop=params.get("crop"), trim=bool(params.get("trim")))
        return self._result("desktop_screenshot", sid or "d_desktop", shot, win=win)

    def click(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        x = float(params.get("x") or 0)
        y = float(params.get("y") or 0)
        if self.os_name != "windows" and win and win.get("id"):
            # X11 下用 xdotool 的窗口相对坐标点击，避免窗口边框/绝对坐标偏差
            env = self._env_display()
            try:
                subprocess.run(["xdotool", "windowactivate", win["id"]], env=env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["xdotool", "mousemove", "--window", win["id"],
                                str(int(x)), str(int(y)), "click", "1"], env=env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pg = self._load()
                pg.click((win["x"] if win else 0) + x, (win["y"] if win else 0) + y)
        else:
            pg = self._load()
            pg.click((win["x"] if win else 0) + x, (win["y"] if win else 0) + y)
        time.sleep(0.5)
        return self._result("desktop_click", sid, self._shot(win), win=win)


    def _abs(self, win, x, y):
        return (int((win or {}).get("x") or 0) + int(x), int((win or {}).get("y") or 0) + int(y))

    def drag(self, params):
        """应用内滑动/拖拽（滚动条、滑块、拖拽排序）。"""
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        pg = self._load()
        x1 = float(params.get("x") or 0); y1 = float(params.get("y") or 0)
        x2 = float(params.get("to_x") if params.get("to_x") is not None else x1 + 200)
        y2 = float(params.get("to_y") if params.get("to_y") is not None else y1)
        ax1, ay1 = self._abs(win, x1, y1)
        ax2, ay2 = self._abs(win, x2, y2)
        try:
            pg.moveTo(ax1, ay1, duration=0.15)
            pg.mouseDown()
            pg.moveTo(ax2, ay2, duration=float(params.get("duration") or 0.45))
            pg.mouseUp()
        except Exception as e:
            return {"ok": False, "action": "desktop_drag", "error": str(e)[:200]}
        time.sleep(0.4)
        return self._result("desktop_drag", sid, self._shot(win), win=win)

    def scroll(self, params):
        """应用内滚动滚轮（正数向上、负数向下）。"""
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        pg = self._load()
        clicks = int(params.get("clicks") or params.get("dy") or -5)
        if params.get("x") is not None:
            ax, ay = self._abs(win, params.get("x") or 0, params.get("y") or 0)
            pg.moveTo(ax, ay)
        pg.scroll(clicks)
        time.sleep(0.4)
        return self._result("desktop_scroll", sid, self._shot(win), win=win)

    def double_click(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        pg = self._load()
        ax, ay = self._abs(win, params.get("x") or 0, params.get("y") or 0)
        pg.doubleClick(ax, ay, interval=0.12)
        time.sleep(0.4)
        return self._result("desktop_double_click", sid, self._shot(win), win=win)

    def key_combo(self, params):
        """组合键，如 ctrl+c、ctrl+shift+t、alt+F4。"""
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        combo = str(params.get("combo") or params.get("key") or "").strip()
        if not combo:
            return {"ok": False, "error": "combo 不能为空，例如 ctrl+c"}
        keys = [k.strip().lower() for k in combo.replace(" ", "").split("+") if k.strip()]
        pg = self._load()
        pg.hotkey(*keys)
        time.sleep(0.4)
        return self._result("desktop_key_combo", sid, self._shot(win), {"combo": combo}, win=win)

    def type_text(self, params):
        sid = params.get("session_id")
        win = self._get_window(sid) or self._active_window()
        text = str(params.get("text") or "")
        if not text:
            return {"ok": False, "error": "text 不能为空", "sessions": self.count()}
        pasted = False
        if self.os_name != "windows" and win and win.get("id"):
            env = self._env_display()
            try:
                self._clipboard().copy(text)
                subprocess.run(["xdotool", "windowactivate", win["id"]], env=env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], env=env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if params.get("submit"):
                    subprocess.run(["xdotool", "key", "Return"], env=env, timeout=5,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                pasted = True
            except Exception:
                pasted = False
        if not pasted:
            pg = self._load()
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
        key = str(params.get("key") or "enter").strip()
        sent = False
        if self.os_name != "windows" and win and win.get("id"):
            env = self._env_display()
            try:
                subprocess.run(["xdotool", "windowactivate", win["id"]], env=env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["xdotool", "key", "--clearmodifiers", key], env=env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                sent = True
            except Exception:
                sent = False
        if not sent:
            self._load().press(key)
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
                    "desktop_focus": self.focus, "desktop_drag": self.drag,
                    "desktop_scroll": self.scroll, "desktop_double_click": self.double_click,
                    "desktop_key_combo": self.key_combo,
                }.get(action)
                if not fn:
                    return {"ok": False, "error": "不支持的应用工具动作: %s" % action, "sessions": self.count()}
                return fn(params)
            except Exception as e:
                return {"ok": False, "action": action, "error": str(e)[:400], "sessions": self.count()}


# ---------------------------------------------------------------- project files
class ProjectOps:
    """工具机上的项目工作目录：每台机器保存一份任务工作副本。"""

    def __init__(self):
        self.root = os.path.abspath(os.path.join(tempfile.gettempdir(), "newera-projects"))
        try:
            os.makedirs(self.root, exist_ok=True)
        except Exception:
            pass
        self.lock = threading.RLock()

    def _safe_cid(self, cid):
        s = re.sub(r"[^A-Za-z0-9._-]", "-", str(cid or "task")).strip("-.")
        return (s or "task")[:60]

    def _dir(self, cid):
        d = os.path.join(self.root, self._safe_cid(cid))
        os.makedirs(d, exist_ok=True)
        return d

    def dir_for(self, cid):
        return self._dir(cid)

    def _clean_path(self, path):
        parts = []
        for p in str(path or "").replace("\\", "/").split("/"):
            p = p.strip()
            if not p or p in (".", ".."):
                continue
            p = re.sub(r"[^A-Za-z0-9._-]", "-", p)
            parts.append(p[:80])
        return "/".join(parts) or "artifact.txt"

    def _files(self, params):
        files = params.get("files")
        if not isinstance(files, dict):
            return {}
        out = {}
        for path, content in files.items():
            out[self._clean_path(path)] = str(content if content is not None else "")
        return out

    def _write(self, d, files):
        for path, content in files.items():
            full = os.path.abspath(os.path.join(d, path))
            if not full.startswith(os.path.abspath(d) + os.sep):
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

    def init(self, params):
        cid = params.get("cid") or ""
        with self.lock:
            d = self._dir(cid)
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
            files = self._files(params)
            self._write(d, files)
        return {"ok": True, "action": "project_init", "project_dir": d,
                "files": sorted(files), "count": len(files)}

    def write(self, params):
        cid = params.get("cid") or ""
        files = self._files(params)
        with self.lock:
            d = self._dir(cid)
            self._write(d, files)
        return {"ok": True, "action": "project_write", "project_dir": d,
                "files": sorted(files), "count": len(files)}

    def read(self, params):
        cid = params.get("cid") or ""
        path = self._clean_path(params.get("path") or params.get("filename") or "")
        with self.lock:
            d = self._dir(cid)
            full = os.path.abspath(os.path.join(d, path))
            if not full.startswith(os.path.abspath(d) + os.sep) or not os.path.isfile(full):
                return {"ok": False, "error": "文件不存在：%s" % path, "project_dir": d}
            text = open(full, encoding="utf-8", errors="replace").read()
        lines = text.splitlines()
        total = len(lines)
        try:
            start = int(params.get("start_line") or 1)
        except Exception:
            start = 1
        try:
            end = int(params.get("end_line") or total)
        except Exception:
            end = total
        start = max(1, start)
        end = min(max(start, end), total)
        return {"ok": True, "action": "project_read", "project_dir": d, "path": path,
                "content": "\n".join(lines[start - 1:end]), "total_lines": total,
                "start_line": start, "end_line": end}

    def list(self, params):
        cid = params.get("cid") or ""
        with self.lock:
            d = self._dir(cid)
            out = []
            for root, _dirs, names in os.walk(d):
                for name in names:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, d).replace("\\", "/")
                    try:
                        text = open(full, encoding="utf-8", errors="replace").read()
                    except Exception:
                        text = ""
                    out.append({"path": rel, "lines": len(text.splitlines()),
                                "chars": len(text)})
        return {"ok": True, "action": "project_list", "project_dir": d,
                "files": sorted(out, key=lambda x: x["path"])}

    def search(self, params):
        cid = params.get("cid") or ""
        query = str(params.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query 不能为空"}
        try:
            limit = int(params.get("max_results") or 50)
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))
        hits = []
        with self.lock:
            d = self._dir(cid)
            for root, _dirs, names in os.walk(d):
                for name in names:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, d).replace("\\", "/")
                    try:
                        lines = open(full, encoding="utf-8", errors="replace").read().splitlines()
                    except Exception:
                        continue
                    for i, line in enumerate(lines, 1):
                        if query.lower() in line.lower():
                            hits.append("%s:%d: %s" % (rel, i, line.strip()[:400]))
                            if len(hits) >= limit:
                                break
                    if len(hits) >= limit:
                        break
        return {"ok": True, "action": "project_search", "project_dir": d,
                "matches": hits, "count": len(hits)}

    def edit(self, params):
        """在文件里做局部替换；old_string 必须唯一（除非 replace_all）。"""
        cid = params.get("cid") or ""
        path = self._clean_path(params.get("path") or params.get("filename") or "")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        if old_string is None or new_string is None:
            return {"ok": False, "error": "edit 需要 old_string 与 new_string"}
        with self.lock:
            d = self._dir(cid)
            full = os.path.abspath(os.path.join(d, path))
            if not full.startswith(os.path.abspath(d) + os.sep) or not os.path.isfile(full):
                return {"ok": False, "error": "文件不存在：%s" % path}
            body = open(full, encoding="utf-8", errors="replace").read()
            if old_string == "":
                return {"ok": False, "error": "edit 的 old_string 不能为空"}
            if old_string not in body:
                return {"ok": False, "error": "没有在 %s 中找到 old_string" % path}
            count = body.count(old_string)
            replace_all = bool(params.get("replace_all"))
            if count > 1 and not replace_all:
                return {"ok": False,
                        "error": "old_string 在 %s 中出现 %d 处，不唯一。请提供更长上下文或设置 replace_all。" % (path, count)}
            new_body = body.replace(old_string, new_string, -1 if replace_all else 1)
            with open(full, "w", encoding="utf-8") as f:
                f.write(new_body)
        return {"ok": True, "action": "project_edit", "path": path,
                "replaced": count if replace_all else 1, "chars": len(new_body)}

    def delete(self, params):
        cid = params.get("cid") or ""
        path = self._clean_path(params.get("path") or params.get("filename") or "")
        with self.lock:
            d = self._dir(cid)
            full = os.path.abspath(os.path.join(d, path))
            if not full.startswith(os.path.abspath(d) + os.sep) or not os.path.isfile(full):
                return {"ok": False, "error": "文件不存在：%s" % path}
            try:
                os.unlink(full)
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}
        return {"ok": True, "action": "project_delete", "path": path}

    def read_all(self, params):
        cid = params.get("cid") or ""
        try:
            cap = int(params.get("max_chars") or 400000)
        except Exception:
            cap = 400000
        cap = max(1000, min(cap, 400000))
        with self.lock:
            d = self._dir(cid)
            parts = []
            total = 0
            used = 0
            truncated = False
            for root, _dirs, names in os.walk(d):
                for name in sorted(names):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, d).replace("\\", "/")
                    try:
                        body = open(full, encoding="utf-8", errors="replace").read()
                    except Exception:
                        continue
                    header = "===== %s（%d 行 / %d 字符）=====\n" % (
                        rel, len(body.splitlines()), len(body))
                    piece = header + body
                    if total + len(piece) + 2 > cap:
                        parts.append(header + body[:max(0, cap - total - len(header) - 80)] +
                                     "\n…（达到 max_chars 上限，剩余文件未返回）")
                        truncated = True
                        break
                    parts.append(piece)
                    total += len(piece) + 2
                    used += 1
                if truncated:
                    break
        text = "\n\n".join(parts) if parts else "（空）"
        return {"ok": True, "action": "project_read_all", "content": text,
                "files": used, "chars": len(text), "truncated": truncated}

    def manifest(self, params):
        cid = params.get("cid") or ""
        with self.lock:
            d = self._dir(cid)
            out = []
            for root, _dirs, names in os.walk(d):
                for name in names:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, d).replace("\\", "/")
                    try:
                        body = open(full, encoding="utf-8", errors="replace").read()
                    except Exception:
                        body = ""
                    out.append({"path": rel, "lines": len(body.splitlines()),
                                "chars": len(body)})
        return {"ok": True, "action": "project_manifest",
                "files": sorted(out, key=lambda x: x["path"])}

    def run(self, action, params):
        with self.lock:
            try:
                fn = {
                    "project_init": self.init, "project_write": self.write,
                    "project_read": self.read, "project_list": self.list,
                    "project_search": self.search, "project_edit": self.edit,
                    "project_delete": self.delete, "project_read_all": self.read_all,
                    "project_manifest": self.manifest,
                }.get(action)
                if not fn:
                    return {"ok": False, "error": "不支持的项目文件动作: %s" % action}
                return fn(params)
            except Exception as e:
                return {"ok": False, "action": action,
                        "error": (str(e) or e.__class__.__name__)[:400]}


# ---------------------------------------------------------------- job loop
def job_loop(os_name):
    bops = BrowserOps(max_sessions=8, idle_timeout=14400)
    tops = TerminalOps(os_name, max_sessions=16, idle_timeout=14400)
    dops = DesktopOps(os_name)
    pops = ProjectOps()
    validate_op = ValidateOps()
    sync_op = TaskSync()
    SERVICES["ops"] = {"browser": bops, "terminal": tops, "desktop": dops, "project": pops,
                       "validate": validate_op, "sync": sync_op}
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
            if action.startswith("project_"):
                out = pops.run(action, params)
            elif action == "terminal_exec":
                out = tops.run(params)
            elif action.startswith("desktop_"):
                out = dops.run(action, params)
            elif action == "validate_preview":
                out = validate_op.run({"dir": pops.dir_for(params.get("cid") or ""),
                                       "manifest": params.get("manifest") or []})
            elif action in ("bind_task", "sync_task", "package_zip", "restore_task",
                            "task_status"):
                params = dict(params or {})
                params["dir"] = pops.dir_for(params.get("cid") or "")
                out = sync_op.run(action, params)
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


def submit_job(action, params, timeout=180):
    """MCP / HTTP 调用入口：把动作排进串行队列，等结果返回。"""
    job = {"job_id": uuid.uuid4().hex[:16], "action": action or "",
           "params": params or {}, "event": threading.Event(), "result": None}
    try:
        JOBS.put(job, timeout=10)
    except queue.Full:
        return {"ok": False, "error": "工具机繁忙，请稍后重试"}
    if not job["event"].wait(timeout=timeout):
        return {"ok": False, "error": "工具机任务超时"}
    return job.get("result") or {"ok": False, "error": "工具机没有返回结果"}


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
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {
                "ok": True, "version": VERSION, "os": STATE["os"],
                "slot": STATE["slot"], "worker_id": STATE["worker_id"],
                "busy": STATE["busy"], "job": STATE["job"],
                "sessions": STATE["sessions"], "done": STATE["done"],
                "uptime": int(time.time() - STATE["started"]), "last_error": STATE["last_error"],
                "mcp": True, "mcp_path": "/mcp",
            })
        elif path == "/mcp":
            # MCP over HTTP：GET 只用于探测，实际调用走 POST JSON-RPC
            self._send(200, {"ok": True, "mcp": True, "protocol": mcp_server.PROTOCOL,
                             "tools": len(mcp_server.TOOLS),
                             "transport": "streamable-http"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _mcp_post(self):
        token = os.environ.get("AI_WORKER_TOKEN", "")
        got = self.headers.get("X-Worker-Token", "")
        if not token or not got or not _safe_eq(token, got):
            self._send(403, {"ok": False, "error": "invalid worker token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 32 * 1024 * 1024:
                raise ValueError("bad length")
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
        except Exception:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
            return

        def submit(action, params):
            return submit_job(action, params, timeout=int(os.environ.get("MCP_JOB_TIMEOUT", "150")))

        def hub():
            return mcp_aggregate.start(MCP_WORKDIR, submit)

        if isinstance(body, list):
            replies = []
            for msg in body[:20]:
                rep = mcp_aggregate.handle(msg, hub)
                if rep is not None:
                    replies.append(rep)
            if not replies:
                self._send(202, {"ok": True})
            else:
                self._send(200, replies)
            return
        rep = mcp_aggregate.handle(body, hub)
        if rep is None:
            self._send(202, {"ok": True})
            return
        self._send(200, rep)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/mcp":
            self._mcp_post()
            return
        if path != "/run":
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
    # 先起 MCP 聚合网关（Playwright / Filesystem / Desktop Commander），再对外提供 /mcp
    try:
        os.makedirs(MCP_WORKDIR, exist_ok=True)
        mcp_aggregate.start(MCP_WORKDIR, submit_job)
    except Exception as e:
        print("[mcp] hub start failed: %s" % e, flush=True)

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
