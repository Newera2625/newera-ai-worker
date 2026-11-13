#!/usr/bin/env python3
"""工具机 MCP 服务（标准 Model Context Protocol）。

工具机在 127.0.0.1:8080 同时提供：
  GET  /health   存活探测（主站调度用）
  POST /mcp      MCP Streamable HTTP 端点（主站作为 MCP client 调用）
  POST /run      旧版自研 HTTP 通道（过渡期保留）

暴露的工具：浏览器、终端命令、桌面应用、项目文件读写、页面验收、GitHub 同步与打包。
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
from task_sync import TaskSync  # noqa: E402
from validate_ops import ValidateOps  # noqa: E402

SYNC = TaskSync()
VALIDATE = ValidateOps()
PROXY = os.environ.get("GITHUB_PROXY", "https://gh-proxy.com/").rstrip("/") + "/"


def _run(action, params, timeout=180):
    try:
        return agent.submit_job(action, params, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}


def _project_dir(cid):
    ops = agent.SERVICES.get("ops") or {}
    pops = ops.get("project")
    if pops is None:
        return ""
    return pops.dir_for(cid)


def _build_mcp():
    from mcp.server.fastmcp import FastMCP
    try:
        mcp = FastMCP("newera-tools", stateless_http=True, json_response=True)
    except TypeError:
        mcp = FastMCP("newera-tools")
    try:
        mcp.settings.streamable_http_path = "/mcp"
    except Exception:
        pass

    # ---------------- 浏览器 ----------------
    @mcp.tool(name="browser_navigate", description="打开一个网址，返回页面标题、正文摘要与截图")
    def browser_navigate(url: str, session_id: str = "", full_page: bool = False,
                         timeout: int = 30000, wait_ms: int = 1200) -> str:
        return _json(_run("navigate", {"url": url, "session_id": session_id,
                                       "full_page": full_page, "timeout": timeout,
                                       "wait_ms": wait_ms}))

    @mcp.tool(name="browser_screenshot", description="对当前浏览器会话截图")
    def browser_screenshot(session_id: str, full_page: bool = False) -> str:
        return _json(_run("screenshot", {"session_id": session_id, "full_page": full_page}))

    @mcp.tool(name="browser_click", description="点击网页元素（优先用 selector，其次用 x/y 坐标）")
    def browser_click(session_id: str, selector: str = "", x: float = -1, y: float = -1,
                      timeout: int = 15000) -> str:
        return _json(_run("click", {"session_id": session_id, "selector": selector,
                                    "x": None if x < 0 else x, "y": None if y < 0 else y,
                                    "timeout": timeout}))

    @mcp.tool(name="browser_type", description="在网页输入框输入文字")
    def browser_type(session_id: str, text: str, selector: str = "", submit: bool = False) -> str:
        return _json(_run("type", {"session_id": session_id, "text": text,
                                   "selector": selector, "submit": submit}))

    @mcp.tool(name="browser_press", description="在网页上按键，如 Enter、Tab、Escape")
    def browser_press(session_id: str, key: str) -> str:
        return _json(_run("press", {"session_id": session_id, "key": key}))

    @mcp.tool(name="browser_scroll", description="滚动网页")
    def browser_scroll(session_id: str, dy: int = 600) -> str:
        return _json(_run("scroll", {"session_id": session_id, "dy": dy}))

    @mcp.tool(name="browser_back", description="浏览器后退")
    def browser_back(session_id: str) -> str:
        return _json(_run("back", {"session_id": session_id}))

    @mcp.tool(name="browser_close", description="关闭浏览器会话")
    def browser_close(session_id: str) -> str:
        return _json(_run("close", {"session_id": session_id}))

    # ---------------- 命令执行 ----------------
    @mcp.tool(name="run_command", description="在工具机上执行一条系统命令（Linux bash / Windows cmd）")
    def run_command(command: str, cwd: str = "", timeout: int = 30, shell: str = "",
                    session_id: str = "") -> str:
        return _json(_run("terminal_exec", {"command": command, "cwd": cwd,
                                            "timeout": timeout, "shell": shell,
                                            "session_id": session_id}))

    # ---------------- 桌面应用 ----------------
    @mcp.tool(name="desktop_launch", description="在虚拟桌面里启动应用（计算器/记事本/浏览器等）")
    def desktop_launch(app: str, wait: float = 2.0, session_id: str = "") -> str:
        return _json(_run("desktop_launch", {"app": app, "wait": wait, "session_id": session_id}))

    @mcp.tool(name="desktop_screenshot", description="截取桌面应用窗口")
    def desktop_screenshot(session_id: str) -> str:
        return _json(_run("desktop_screenshot", {"session_id": session_id}))

    @mcp.tool(name="desktop_click", description="点击桌面应用窗口坐标")
    def desktop_click(session_id: str, x: float, y: float) -> str:
        return _json(_run("desktop_click", {"session_id": session_id, "x": x, "y": y}))

    @mcp.tool(name="desktop_type", description="在桌面应用窗口输入文字")
    def desktop_type(session_id: str, text: str) -> str:
        return _json(_run("desktop_type", {"session_id": session_id, "text": text}))

    @mcp.tool(name="desktop_key", description="在桌面应用窗口按键")
    def desktop_key(session_id: str, key: str) -> str:
        return _json(_run("desktop_key", {"session_id": session_id, "key": key}))

    @mcp.tool(name="desktop_windows", description="列出当前打开的窗口")
    def desktop_windows(session_id: str = "") -> str:
        return _json(_run("desktop_windows", {"session_id": session_id}))

    @mcp.tool(name="desktop_focus", description="按标题聚焦某个窗口")
    def desktop_focus(session_id: str, title: str) -> str:
        return _json(_run("desktop_focus", {"session_id": session_id, "title": title}))

    # ---------------- 项目文件 ----------------
    @mcp.tool(name="project_init", description="初始化任务工作目录（会清空该目录）")
    def project_init(cid: str, files: dict = {}) -> str:
        return _json(_run("project_init", {"cid": cid, "files": files or {}}))

    @mcp.tool(name="project_write", description="写入任务文件，files 为 {路径: 内容}")
    def project_write(cid: str, files: dict) -> str:
        return _json(_run("project_write", {"cid": cid, "files": files or {}}))

    @mcp.tool(name="project_read", description="读取任务文件内容，可按行范围")
    def project_read(cid: str, path: str, start_line: int = 1, end_line: int = 0) -> str:
        return _json(_run("project_read", {"cid": cid, "path": path,
                                           "start_line": start_line, "end_line": end_line}))

    @mcp.tool(name="project_edit", description="在任务文件里做局部替换（old_string 必须唯一）")
    def project_edit(cid: str, path: str, old_string: str, new_string: str,
                     replace_all: bool = False) -> str:
        return _json(_run("project_edit", {"cid": cid, "path": path,
                                           "old_string": old_string,
                                           "new_string": new_string,
                                           "replace_all": replace_all}))

    @mcp.tool(name="project_delete", description="删除任务文件")
    def project_delete(cid: str, path: str) -> str:
        return _json(_run("project_delete", {"cid": cid, "path": path}))

    @mcp.tool(name="project_list", description="列出任务目录下的所有文件")
    def project_list(cid: str) -> str:
        return _json(_run("project_list", {"cid": cid}))

    @mcp.tool(name="project_search", description="在任务文件中搜索文本")
    def project_search(cid: str, query: str, filename: str = "", max_results: int = 50) -> str:
        return _json(_run("project_search", {"cid": cid, "query": query,
                                             "filename": filename,
                                             "max_results": max_results}))

    @mcp.tool(name="project_read_all", description="读取任务目录下所有文件内容")
    def project_read_all(cid: str, max_chars: int = 400000) -> str:
        return _json(_run("project_read_all", {"cid": cid, "max_chars": max_chars}))

    @mcp.tool(name="project_manifest", description="返回任务文件清单（路径、行数、字符数）")
    def project_manifest(cid: str) -> str:
        return _json(_run("project_manifest", {"cid": cid}))

    # ---------------- 页面验收 ----------------
    @mcp.tool(name="validate_project", description="对任务目录做页面验收：桌面端 + 手机端渲染、"
                                                   "控制台错误、404、破图、横向滚动、语法检查，返回截图")
    def validate_project(cid: str, manifest: list = []) -> str:
        try:
            out = VALIDATE.run({"dir": _project_dir(cid), "manifest": manifest or []})
        except Exception as e:
            out = {"ok": False, "error": str(e)[:400]}
        return _json(out)

    # ---------------- GitHub 同步与打包 ----------------
    @mcp.tool(name="bind_task", description="把当前任务绑定到用户仓库，之后每 6 分钟自动同步代码")
    def bind_task(cid: str, owner: str, repo: str, branch: str, prefix: str, token: str) -> str:
        try:
            item = SYNC.bind(cid, owner, repo, branch, prefix, token, _project_dir(cid))
            return _json({"ok": True, "bound": True, "branch": item.get("branch"),
                          "repo": item.get("repo")})
        except Exception as e:
            return _json({"ok": False, "error": str(e)[:300]})

    @mcp.tool(name="sync_task", description="立刻把任务目录同步到 GitHub")
    def sync_task(cid: str) -> str:
        try:
            return _json(SYNC.sync_once(cid))
        except Exception as e:
            return _json({"ok": False, "error": str(e)[:300]})

    @mcp.tool(name="package_zip", description="在工具机上打包任务目录成 zip 并上传 GitHub，返回下载链接")
    def package_zip(cid: str) -> str:
        try:
            return _json(SYNC.package_zip(cid, proxy=PROXY))
        except Exception as e:
            return _json({"ok": False, "error": str(e)[:300]})

    @mcp.tool(name="task_status", description="查看任务绑定与同步状态")
    def task_status(cid: str) -> str:
        return _json(SYNC.status(cid))

    @mcp.tool(name="restore_task", description="从 GitHub 任务分支把项目文件恢复到本机工作目录")
    def restore_task(cid: str, owner: str, repo: str, branch: str, prefix: str,
                     token: str = "") -> str:
        try:
            tok = token or os.environ.get("GITHUB_TOKEN", "")
            out = SYNC.restore(cid, owner, repo, branch, prefix, tok, _project_dir(cid))
            return _json(out)
        except Exception as e:
            return _json({"ok": False, "error": str(e)[:300]})

    return mcp


def _json(obj):
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return json.dumps({"ok": False, "error": "序列化失败"})


def build_app():
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    mcp = _build_mcp()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    async def health(request):
        return JSONResponse({
            "ok": True, "version": agent.VERSION, "os": agent.STATE.get("os"),
            "slot": agent.STATE.get("slot"), "worker_id": agent.STATE.get("worker_id"),
            "busy": agent.STATE.get("busy"), "job": agent.STATE.get("job"),
            "sessions": agent.STATE.get("sessions"), "done": agent.STATE.get("done"),
            "uptime": int(time.time() - agent.STATE.get("started", time.time())),
            "last_error": agent.STATE.get("last_error"), "mcp": True,
        })

    async def legacy_run(request):
        import json
        token = os.environ.get("AI_WORKER_TOKEN", "")
        got = request.headers.get("x-worker-token", "")
        if not token or not got or token != got:
            return JSONResponse({"ok": False, "error": "invalid worker token"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
        out = agent.submit_job(body.get("action") or "", body.get("params") or {}, timeout=150)
        return JSONResponse(out)

    return Starlette(
        routes=[Route("/health", health), Route("/run", legacy_run, methods=["POST"]),
                Mount("/", app=mcp.streamable_http_app())],
        lifespan=lifespan,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--os", choices=["linux", "windows"], required=True)
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--duration-min", type=float, default=330)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--remote-port", type=int, default=0)
    ap.add_argument("--tunnel-host", default=os.environ.get("TUNNEL_HOST", ""))
    ap.add_argument("--tunnel-port", type=int, default(int(os.environ.get("TUNNEL_PORT", "0") or 0)))
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

    agent.STATE["os"] = args.os
    agent.STATE["slot"] = args.slot
    agent.STATE["worker_id"] = ("linux-%d" if args.os == "linux" else "win-%d") % args.slot
    agent.STATE["started"] = time.time()

    if not args.no_tunnel:
        threading.Thread(target=agent.tunnel_loop, args=(args,), daemon=True).start()
    threading.Thread(target=agent.job_loop, args=(args.os,), daemon=True).start()

    import uvicorn
    print("AI worker %s MCP listening on %s:%d, duration %.0f min, remote HK port %d" % (
        agent.STATE["worker_id"], args.host, args.port, args.duration_min, args.remote_port),
        flush=True)

    timer = threading.Timer(max(args.duration_min * 60, 30), agent.STOP.set)
    timer.daemon = True
    timer.start()

    config = uvicorn.Config(build_app(), host=args.host, port=args.port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        agent.STOP.set()
        print("AI worker stopped at", time.strftime("%F %T"), flush=True)


if __name__ == "__main__":
    main()
