#!/usr/bin/env python3
"""工具机 MCP 聚合网关。

GitHub Actions 工具机上同时拉起多个成熟 MCP 服务（标准 stdio 协议），
再把它们聚合到一个 HTTP MCP 端点（/mcp），由香港 VPS 端口映射 + 主站 SSH -L 中转。

默认挂载：
  - browser: @playwright/mcp（真实浏览器，元素级点击，不用坐标猜）
  - files:   @modelcontextprotocol/server-filesystem（标准文件读写/搜索/移动）
  - shell:   @wonderwhy-er/desktop-commander（命令执行 + 文件编辑）

另外挂载工具机原生工具（验收截图、zip 打包、GitHub 6 分钟同步），
启动失败时自动降级到原生 MCP 工具，保证主站永远有工具可用。
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

VERSION = "2.1.0"
PROTOCOL = "2024-11-05"


def _log(msg):
    sys.stderr.write("[mcp-hub] %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------- stdio client
class StdioMCP:
    def __init__(self, name, cmd, env=None, cwd=None):
        self.name = name
        self.cmd = cmd
        self.env = env or {}
        self.cwd = cwd
        self.proc = None
        self.q = queue.Queue()
        self.seq = 0
        self.lock = threading.Lock()
        self.tools = []
        self.ok = False
        self.error = ""
        self.started_at = 0

    def _reader(self):
        while True:
            try:
                line = self.proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except Exception:
                continue

    def _drain(self):
        try:
            while True:
                err = self.proc.stderr.readline()
                if not err:
                    break
                text = err.decode("utf-8", "replace").strip()
                if text:
                    _log("%s: %s" % (self.name, text[:300]))
        except Exception:
            pass

    def start(self, timeout=75):
        env = dict(os.environ)
        env.update(self.env or {})
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=self.cwd,
                bufsize=1, universal_newlines=False)
        except Exception as e:
            self.error = str(e)[:300]
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        self.started_at = time.time()
        rep = self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
            "clientInfo": {"name": "newera-mcp-hub", "version": VERSION},
        }, timeout=timeout)
        if not rep or rep.get("error"):
            self.error = ((rep or {}).get("error") or {}).get("message") or "initialize failed"
            return False
        self.notify("notifications/initialized", {})
        rep = self.request("tools/list", {}, timeout=timeout)
        if not rep or rep.get("error"):
            self.error = "tools/list failed"
            return False
        self.tools = (rep.get("result") or {}).get("tools") or []
        self.ok = bool(self.tools)
        return self.ok

    def request(self, method, params, timeout=60):
        with self.lock:
            self.seq += 1
            rid = self.seq
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception as e:
            return {"error": {"message": str(e)[:200]}}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                rep = self.q.get(timeout=max(0.2, deadline - time.time()))
            except queue.Empty:
                break
            if isinstance(rep, dict) and rep.get("id") == rid:
                return rep
            # 其它消息（通知/别的响应）忽略
        return {"error": {"message": "timeout"}}

    def notify(self, method, params):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            pass

    def call(self, tool, args, timeout=120):
        rep = self.request("tools/call", {"name": tool, "arguments": args or {}},
                           timeout=timeout)
        if rep.get("error"):
            return {"ok": False, "error": ("MCP %s: %s" % (
                tool, (rep["error"] or {}).get("message") or "error"))[:400]}
        res = rep.get("result") or {}
        return _unwrap(res)

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


def _unwrap(res):
    """把 MCP tools/call 结果转成工具机统一结构（保留截图 base64）。"""
    out = res.get("structuredContent")
    if not isinstance(out, dict):
        out = {}
    texts = []
    images = []
    for item in (res.get("content") or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            texts.append(item.get("text") or "")
        elif item.get("type") == "image" and item.get("data"):
            images.append(item)
    text = "\n".join(t for t in texts if t)
    if text:
        out.setdefault("text", text[:200000])
    if images:
        out["screenshot_b64"] = images[-1].get("data") or ""
        out["screenshot_mime"] = images[-1].get("mimeType") or "image/png"
    out.setdefault("ok", not res.get("isError"))
    return out


# ---------------------------------------------------------------- hub
class MCPHub:
    def __init__(self):
        self.servers = {}
        self.routes = {}   # 对外工具名 -> (server, 原始工具名)
        self.by_name = {}  # 原始工具名 -> 对外工具名
        self.native = None
        self._native_routes = {}
        self.ready = False
        self.error = ""

    def add(self, server):
        self.servers[server.name] = server

    def start_all(self, native_handle):
        self.native = native_handle
        for s in self.servers.values():
            try:
                ok = s.start()
            except Exception as e:
                ok = False
                s.error = str(e)[:300]
            _log("server %s -> %s %s" % (s.name, "ok" if ok else "FAIL", s.error))
        for s in self.servers.values():
            if not s.ok:
                continue
            for t in s.tools:
                name = (t.get("name") or "").strip()
                if not name:
                    continue
                public = name
                if public in self.routes:
                    public = "%s_%s" % (s.name, name)
                self.routes[public] = (s.name, name)
                self.by_name.setdefault(name, public)
        if self.native:
            try:
                for t in self.native.list_tools():
                    self._native_routes["native_" + t["name"]] = t["name"]
            except Exception as e:
                _log("native routes failed: %s" % e)
        self.ready = bool(self.routes)
        return self.ready

    def tool_list(self):
        out = []
        for s in self.servers.values():
            if not s.ok:
                continue
            for t in s.tools:
                name = (t.get("name") or "").strip()
                if not name:
                    continue
                public = self.by_name.get(name) or name
                item = dict(t)
                item["name"] = public
                item["description"] = "[%s] %s" % (s.name, t.get("description") or "")
                out.append(item)
        if self.native:
            try:
                # 原生工具名统一加 native_ 前缀，避免和标准 MCP 撞名
                for t in self.native.list_tools():
                    pub = "native_" + t["name"]
                    if pub in self.routes:
                        continue
                    item = dict(t)
                    item["name"] = pub
                    item["description"] = "[native] " + (t.get("description") or "")
                    out.append(item)
                    self._native_routes[pub] = t["name"]
            except Exception as e:
                _log("native tools failed: %s" % e)
        return out

    def call(self, name, args, timeout=120):
        if name in self.routes:
            server, orig = self.routes[name]
            return self.servers[server].call(orig, args, timeout=timeout)
        native_routes = getattr(self, "_native_routes", {})
        if name in native_routes:
            return self.native.call(native_routes[name], args or {})
        if self.native and self.native.has(name):
            return self.native.call(name, args or {})
        return {"ok": False, "error": "未知 MCP 工具：%s" % name}

    def status(self):
        return {
            "ok": True, "ready": self.ready,
            "servers": [{"name": s.name, "ok": s.ok, "tools": len(s.tools),
                         "error": s.error} for s in self.servers.values()],
            "tools": len(self.tool_list()),
        }

    def stop_all(self):
        for s in self.servers.values():
            s.stop()


def _launcher(pkg, binname, extra=None, workdir=None):
    """优先用全局安装的可执行文件，找不到再用 npx 现拉，避免冷启动超时。"""
    path = shutil.which(binname)
    if path:
        cmd = [path]
    else:
        cmd = ["npx", "--yes", pkg]
    return cmd + list(extra or [])


def build_hub(workdir):
    """按环境变量决定挂载哪些 MCP 服务（可用 MCP_SERVERS 关闭）。"""
    enabled = (os.environ.get("MCP_SERVERS") or "browser,files,system").lower()
    hub = MCPHub()
    env_common = dict(os.environ)
    env_common.setdefault("HOME", "/root")
    if os.environ.get("DISPLAY"):
        env_common["DISPLAY"] = os.environ["DISPLAY"]

    if "browser" in enabled:
        extra = ["--isolated", "--no-sandbox"]
        if os.environ.get("MCP_BROWSER", "").strip():
            extra += ["--browser", os.environ["MCP_BROWSER"].strip()]
        hub.add(StdioMCP("browser", _launcher("@playwright/mcp@latest", "playwright-mcp",
                                              extra, workdir), env=env_common, cwd=workdir))

    if "files" in enabled:
        hub.add(StdioMCP("files", _launcher("@modelcontextprotocol/server-filesystem",
                                            "mcp-server-filesystem", [workdir], workdir),
                         env=env_common, cwd=workdir))

    if "system" in enabled:
        hub.add(StdioMCP("system", _launcher("@wonderwhy-er/desktop-commander",
                                             "desktop-commander", [], workdir),
                         env=env_common, cwd=workdir))
    return hub


# ---------------------------------------------------------------- native adapter
class NativeMCP:
    """工具机自带的验收/打包/同步工具，作为 MCP 原生命名空间暴露。"""

    def __init__(self, submit):
        self.submit = submit

    def list_tools(self):
        import mcp_server
        return mcp_server.TOOLS

    def has(self, name):
        import mcp_server
        names = {t["name"] for t in mcp_server.TOOLS}
        return name in names

    def call(self, name, args):
        import mcp_server
        return mcp_server.call_tool(name, args, self.submit)


_HUB = {"hub": None, "native": None}


def start(workdir, submit):
    if _HUB["hub"] is not None:
        return _HUB["hub"]
    native = NativeMCP(submit)
    hub = build_hub(workdir)
    _HUB["hub"] = hub
    _HUB["native"] = native

    def _boot():
        t0 = time.time()
        _log("starting MCP hub in %s" % workdir)
        try:
            hub.start_all(native)
        except Exception as e:
            hub.error = str(e)[:300]
        _log("hub ready=%s tools=%d in %.1fs" % (hub.ready, len(hub.tool_list()), time.time() - t0))

    threading.Thread(target=_boot, daemon=True).start()
    return hub


def handle(msg, start_hub):
    """MCP JSON-RPC：包含 initialize / tools/list / tools/call。"""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    rid = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    if rid is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "newera-mcp-hub", "version": VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        hub = start_hub()
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": hub.tool_list()}}
    if method == "tools/call":
        hub = start_hub()
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        try:
            out = hub.call(name, args)
        except Exception as e:
            out = {"ok": False, "error": ("MCP 调用异常：" + str(e))[:400]}
        text = out.get("text") or out.get("content") or out.get("error") or ""
        if not text:
            text = json.dumps(out, ensure_ascii=False)[:200000]
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": str(text)[:200000]}],
            "isError": not bool(out.get("ok", True)),
            "structuredContent": out,
        }}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}
