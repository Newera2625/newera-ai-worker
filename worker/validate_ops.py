#!/usr/bin/env python3
"""工具机上的页面验收：桌面端 + 手机端渲染检查、控制台错误、404、破图、横向滚动等。

主站不再跑浏览器，全部验收都在工具机上完成，避免主站内存/磁盘压力。
"""
import base64
import functools
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time

_ASSET_ATTR_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)

_METRICS_JS = """() => {
    const txt = (document.body && document.body.innerText || '').trim();
    const imgs = Array.from(document.images || []);
    const broken = imgs.filter(im => im.complete && im.naturalWidth === 0 && im.getAttribute('src'))
                        .map(im => im.getAttribute('src')).slice(0, 8);
    const svgs = Array.from(document.querySelectorAll('svg')).map(sv => {
        const r = sv.getBoundingClientRect();
        return {w: Math.round(r.width), h: Math.round(r.height)};
    }).filter(r => r.w > innerWidth * 0.9 || r.h > innerHeight * 0.9);
    const textEls = Array.from(document.querySelectorAll('body *')).filter(el =>
        el.children.length === 0 && (el.textContent || '').trim());
    const tiny = textEls.filter(el => parseFloat(getComputedStyle(el).fontSize || '16') < 9);
    const sheets = Array.from(document.styleSheets || []);
    return {
        textLen: txt.length,
        bodyChildren: document.body ? document.body.children.length : 0,
        brokenImages: broken,
        hugeSvgs: svgs.length,
        textEls: textEls.length,
        tinyText: tiny.length,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: innerWidth,
        pageHeight: document.documentElement.scrollHeight,
        sheets: sheets.length,
        title: document.title || ''
    };
}"""

_MOBILE_JS = """() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: innerWidth,
    textLen: ((document.body && document.body.innerText) || '').trim().length
})"""


def _clean_path(path, fallback="artifact.txt"):
    parts = []
    for p in str(path or "").replace("\\", "/").split("/"):
        p = p.strip()
        if not p or p in (".", ".."):
            continue
        p = re.sub(r"[^A-Za-z0-9._-]", "-", p)
        parts.append(p[:80])
    return "/".join(parts) or fallback


def _read_dir(src_dir):
    files = {}
    for root, dirs, names in os.walk(src_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, src_dir).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    files[rel] = f.read()
            except Exception:
                continue
    return files


def _find_html(files):
    if "index.html" in files:
        return "index.html", files["index.html"]
    for path in sorted(files):
        if path.lower().endswith((".html", ".htm")):
            return path, files[path]
    return "", ""


def _local_asset_errors(html_name, html, files):
    errors = []
    base_dir = os.path.dirname(html_name or "").replace("\\", "/")
    for raw in _ASSET_ATTR_RE.findall(html or ""):
        url = str(raw or "").strip()
        if (not url or url.startswith(("#", "http://", "https://", "//", "data:",
                                      "mailto:", "tel:", "javascript:", "blob:"))):
            continue
        path = url.split("#", 1)[0].split("?", 1)[0].strip()
        if not path:
            continue
        if path.startswith("/"):
            candidate = path.lstrip("/")
        else:
            candidate = os.path.normpath(os.path.join(base_dir, path)).replace("\\", "/")
        if not candidate or candidate.startswith("../"):
            continue
        if candidate not in (files or {}):
            errors.append("引用的本地文件不存在：%s（在 %s 中）" % (url[:160], html_name))
    return errors


def _syntax_errors(files):
    errors = []
    node = shutil.which("node")
    for path, content in (files or {}).items():
        low = path.lower()
        if low.endswith(".py"):
            try:
                compile(content or "", path, "exec")
            except SyntaxError as e:
                errors.append("Python 语法错误：%s 第 %s 行：%s" % (path, e.lineno or "?", e.msg or "语法错误"))
            except Exception as e:
                errors.append("Python 文件无法解析：%s：%s" % (path, str(e)[:160]))
        elif node and low.endswith((".js", ".mjs", ".cjs")):
            tmp = ""
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as f:
                    f.write(content or "")
                    tmp = f.name
                proc = subprocess.run([node, "--check", tmp], capture_output=True,
                                      text=True, timeout=12)
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:240]
                    errors.append("JavaScript 语法错误：%s：%s" % (path, detail))
            except Exception as e:
                errors.append("JavaScript 文件无法检查：%s：%s" % (path, str(e)[:160]))
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
    return errors


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


class ValidateOps:
    """对任务目录做完整页面验收，输出错误、警告与桌面/手机截图。"""

    def __init__(self):
        self.lock = threading.RLock()

    def run(self, params):
        params = params or {}
        src_dir = params.get("dir") or ""
        if not src_dir or not os.path.isdir(src_dir):
            return {"ok": False, "error": "任务目录不存在"}
        manifest = [p for p in (params.get("manifest") or []) if p]
        files = _read_dir(src_dir)
        errors = []
        warnings = []
        metrics = {}
        started = time.time()

        missing = [p for p in manifest if p not in files]
        if missing:
            errors.append("缺少必须交付的文件：" + "、".join(missing[:20]))
        errors.extend(_syntax_errors(files))

        html_name, html = _find_html(files)
        if not html_name:
            ok = not errors
            return {"ok": ok, "errors": errors[:30], "warnings": warnings,
                    "screenshots": [], "html_name": "", "metrics": {"files": len(files)},
                    "elapsed": round(time.time() - started, 2)}
        if not (html or "").strip():
            errors.append("HTML 文件内容为空，页面没有实际内容")
            return {"ok": False, "errors": errors[:30], "warnings": warnings,
                    "screenshots": [], "html_name": html_name, "metrics": {},
                    "elapsed": round(time.time() - started, 2)}

        for path, body in files.items():
            if path.lower().endswith((".html", ".htm")):
                errors.extend(_local_asset_errors(path, body, files))
        if not re.search(r"<title[^>]*>\s*\S", html or "", re.I):
            warnings.append("页面没有设置 <title>")
        if not re.search(r"<meta[^>]+name=[\"']viewport[\"']", html or "", re.I):
            warnings.append("页面没有设置 viewport，手机端可能缩放异常")

        shots = []
        server = None
        port = 0
        try:
            handler = functools.partial(_QuietHandler, directory=src_dir)
            socketserver.TCPServer.allow_reuse_address = True
            server = socketserver.TCPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                                 daemon=True)
            t.start()
            url = "http://127.0.0.1:%d/%s" % (port, html_name)

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = None
                try:
                    browser = p.chromium.launch(headless=True, channel="chrome")
                except Exception:
                    browser = p.chromium.launch(headless=True, channel="chromium")
                page = browser.new_page(viewport={"width": 1280, "height": 900})

                def _on_console(msg):
                    if msg.type != "error":
                        return
                    try:
                        loc = msg.location() or {}
                        url = loc.get("url", "")
                    except Exception:
                        url = ""
                    if url.startswith(("http://", "https://")):
                        return
                    errors.append("控制台错误：%s" % msg.text[:300])

                def _on_request_failed(req):
                    url = req.url or ""
                    if url.startswith(("http://", "https://")):
                        return
                    errors.append("本地资源加载失败：%s" % url[-180:])

                page.on("console", _on_console)
                page.on("pageerror", lambda exc: errors.append("JS 异常：%s" % str(exc)[:300]))
                page.on("requestfailed", _on_request_failed)
                page.goto(url, wait_until="load", timeout=25000)
                page.wait_for_timeout(1000)

                desktop = page.evaluate(_METRICS_JS)
                metrics["desktop"] = desktop
                if int(desktop.get("textLen") or 0) < 20:
                    errors.append("页面正文过少（可能没有渲染成功）")
                if desktop.get("brokenImages"):
                    errors.append("图片加载失败：" + "、".join(desktop["brokenImages"]))
                if int(desktop.get("hugeSvgs") or 0) >= 2:
                    errors.append("检测到多个尺寸异常的 SVG 图标，页面布局可能已经崩坏")
                if (int(desktop.get("textEls") or 0) > 20 and
                        int(desktop.get("tinyText") or 0) / max(1, int(desktop.get("textEls") or 0)) > 0.5):
                    warnings.append("超过一半的文字小于 9px，请确认页面样式是否正常加载")
                if int(desktop.get("scrollWidth") or 0) > int(desktop.get("innerWidth") or 0) + 2:
                    errors.append("桌面端出现横向滚动条，页面宽度溢出")
                if int(desktop.get("pageHeight") or 0) > 30000:
                    warnings.append("页面高度超过 30000px，可能布局异常")
                png = page.screenshot(full_page=True, type="png")
                shots.append(base64.b64encode(png).decode("ascii"))

                page.set_viewport_size({"width": 375, "height": 812})
                page.wait_for_timeout(350)
                mobile = page.evaluate(_MOBILE_JS)
                metrics["mobile"] = mobile
                if int(mobile.get("scrollWidth") or 0) > int(mobile.get("innerWidth") or 0) + 2:
                    errors.append("手机端（375px）出现横向滚动条，页面宽度溢出")
                mpng = page.screenshot(full_page=True, type="png")
                shots.append(base64.b64encode(mpng).decode("ascii"))
                browser.close()
        except Exception as e:
            errors.append("页面打开失败：" + str(e)[:300])
        finally:
            if server is not None:
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:
                    pass

        errors = list(dict.fromkeys(errors))[:30]
        warnings = list(dict.fromkeys(warnings))[:20]
        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "screenshots": shots,
            "html_name": html_name,
            "metrics": metrics,
            "files": sorted(files),
            "elapsed": round(time.time() - started, 2),
        }
