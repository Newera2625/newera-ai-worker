#!/usr/bin/env python3
"""工具机上的 MCP 服务（Model Context Protocol，JSON-RPC 2.0 over HTTP）。

- 工具机（GitHub Actions）在 127.0.0.1:8080/mcp 暴露 MCP
- 通过 ssh -R 映射到香港 VPS 的 25xxx/26xxx 端口
- 主站再 ssh -L 到本机端口，调用 MCP（主站→香港→工具机）
- 能力覆盖：文件操作、命令执行、浏览器、桌面应用、页面验收、打包交付
"""
import os
import platform
import time

VERSION = "2.0.0"
PROTOCOL = "2024-11-05"

# MCP 工具名 -> 工具机内部动作
_ACTION = {
    # 文件操作
    "fs_init": "project_init",
    "fs_write_many": "project_write",
    "fs_list": "project_list",
    "fs_read": "project_read",
    "fs_write": "project_write",
    "fs_edit": "project_edit",
    "fs_delete": "project_delete",
    "fs_search": "project_search",
    "fs_read_all": "project_read_all",
    "fs_manifest": "project_manifest",
    # 命令执行
    "exec_command": "terminal_exec",
    # 浏览器
    "browser_navigate": "navigate",
    "browser_screenshot": "screenshot",
    "browser_click": "click",
    "browser_type": "type",
    "browser_press": "press",
    "browser_scroll": "scroll",
    "browser_back": "back",
    "browser_close": "close",
    "browser_hover": "hover",
    "browser_drag": "drag",
    "browser_swipe": "swipe",
    "browser_select": "select",
    "browser_wait": "wait",
    "browser_info": "info",
    # 桌面应用
    "desktop_launch": "desktop_launch",
    "desktop_screenshot": "desktop_screenshot",
    "desktop_click": "desktop_click",
    "desktop_type": "desktop_type",
    "desktop_key": "desktop_key",
    "desktop_windows": "desktop_windows",
    "desktop_focus": "desktop_focus",
    "desktop_drag": "desktop_drag",
    "desktop_scroll": "desktop_scroll",
    "desktop_double_click": "desktop_double_click",
    "desktop_key_combo": "desktop_key_combo",
    # 验收与交付
    "validate_page": "validate_preview",
    "package_zip": "package_zip",
    "bind_task": "bind_task",
    "sync_task": "sync_task",
    "task_status": "task_status",
    "env_info": "env_info",
}

_OBJ = {"type": "object", "properties": {}, "additionalProperties": True}


def _schema(name, desc, props=None, required=None):
    return {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": props or {},
            "required": required or [],
        },
    }


_CID = {"type": "string", "description": "任务/对话 id（工具机上的工作目录标识）"}

TOOLS = [
    # ---------------- 文件操作 ----------------
    _schema("fs_init", "初始化任务目录（清空后用 files 写入完整项目）。",
            {"cid": _CID, "files": {"type": "object", "additionalProperties": {"type": "string"}}},
            ["cid"]),
    _schema("fs_write_many", "批量写入/更新多个文件，不删除其它已有文件。",
            {"cid": _CID, "files": {"type": "object", "additionalProperties": {"type": "string"}}},
            ["cid"]),
    _schema("fs_list", "列出任务目录里的文件（路径、行数、字符数）。",
            {"cid": _CID}, ["cid"]),
    _schema("fs_read", "读取任务目录里的文件内容，可按行号范围读取。",
            {"cid": _CID, "path": {"type": "string", "description": "文件相对路径"},
             "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            ["cid", "path"]),
    _schema("fs_write", "写入文件（新建或整文件覆盖），路径相对任务目录。",
            {"cid": _CID, "path": {"type": "string"},
             "content": {"type": "string", "description": "完整文件内容"}},
            ["cid", "path", "content"]),
    _schema("fs_edit", "对已有文件做字符串局部替换，old_string 必须唯一（或用 replace_all）。",
            {"cid": _CID, "path": {"type": "string"},
             "old_string": {"type": "string"}, "new_string": {"type": "string"},
             "replace_all": {"type": "boolean"}},
            ["cid", "path", "old_string", "new_string"]),
    _schema("fs_delete", "删除任务目录里的文件。",
            {"cid": _CID, "path": {"type": "string"}}, ["cid", "path"]),
    _schema("fs_search", "在任务目录所有文件里搜索文本，返回 文件:行号:内容。",
            {"cid": _CID, "query": {"type": "string"}, "max_results": {"type": "integer"}},
            ["cid", "query"]),
    _schema("fs_read_all", "一次读取任务目录全部文件（受 max_chars 限制）。",
            {"cid": _CID, "max_chars": {"type": "integer"}}, ["cid"]),
    _schema("fs_manifest", "返回任务目录文件清单与体量。", {"cid": _CID}, ["cid"]),
    # ---------------- 命令执行 ----------------
    _schema("exec_command", "在工具机上执行命令并返回退出码、stdout、stderr。",
            {"command": {"type": "string"}, "cwd": {"type": "string"},
             "timeout": {"type": "integer", "description": "秒，默认 30，最大 120"},
             "session_id": {"type": "string"}, "shell": {"type": "string"}},
            ["command"]),
    # ---------------- 浏览器 ----------------
    _schema("browser_navigate", "在工具机真实浏览器打开 http/https 网址，返回页面内容与截图。",
            {"url": {"type": "string"}, "session_id": {"type": "string"},
             "full_page": {"type": "boolean"}, "wait_ms": {"type": "integer"}},
            ["url"]),
    _schema("browser_screenshot", "对当前浏览器页面截图。",
            {"session_id": {"type": "string"}, "full_page": {"type": "boolean"}}, ["session_id"]),
    _schema("browser_click", "点击页面元素（selector 或 x/y 坐标）。",
            {"session_id": {"type": "string"}, "selector": {"type": "string"},
             "x": {"type": "integer"}, "y": {"type": "integer"}}, ["session_id"]),
    _schema("browser_type", "在页面输入框输入文字。",
            {"session_id": {"type": "string"}, "selector": {"type": "string"},
             "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["session_id", "text"]),
    _schema("browser_press", "在页面按键，例如 Enter、Escape。",
            {"session_id": {"type": "string"}, "key": {"type": "string"}}, ["session_id", "key"]),
    _schema("browser_scroll", "滚动页面。",
            {"session_id": {"type": "string"}, "dy": {"type": "integer"}}, ["session_id"]),
    _schema("browser_back", "浏览器返回上一页。", {"session_id": {"type": "string"}}, ["session_id"]),
    _schema("browser_close", "关闭浏览器会话。", {"session_id": {"type": "string"}}, ["session_id"]),
    # ---------------- 桌面应用 ----------------
    _schema("desktop_launch", "在工具机虚拟桌面启动应用并截图。",
            {"app": {"type": "string"}, "wait": {"type": "number"}}, ["app"]),
    _schema("desktop_screenshot", "截取当前桌面/应用窗口。", {"session_id": {"type": "string"}}, []),
    _schema("desktop_click", "在桌面坐标点击。",
            {"session_id": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}},
            ["x", "y"]),
    _schema("desktop_type", "向桌面窗口输入文字。",
            {"session_id": {"type": "string"}, "text": {"type": "string"}}, ["text"]),
    _schema("desktop_key", "向桌面窗口按键。",
            {"session_id": {"type": "string"}, "key": {"type": "string"}}, ["key"]),
    _schema("desktop_windows", "列出当前桌面窗口。", {"session_id": {"type": "string"}}, []),
    _schema("desktop_drag", "在应用窗口内拖动/滑动（滚动条、滑块、拖拽排序）。坐标为窗口相对坐标。",
            {"session_id": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
             "to_x": {"type": "number"}, "to_y": {"type": "number"},
             "duration": {"type": "number"}}, ["session_id", "x", "y", "to_x", "to_y"]),
    _schema("desktop_scroll", "在应用窗口内滚动滚轮（正数向上，负数向下）。",
            {"session_id": {"type": "string"}, "clicks": {"type": "number"},
             "x": {"type": "number"}, "y": {"type": "number"}}, ["session_id"]),
    _schema("desktop_double_click", "在应用窗口内双击（打开文件/进入编辑）。",
            {"session_id": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}},
            ["session_id", "x", "y"]),
    _schema("desktop_key_combo", "在应用窗口内按组合键，如 ctrl+c、ctrl+shift+t、alt+F4。",
            {"session_id": {"type": "string"}, "combo": {"type": "string"}}, ["session_id", "combo"]),
    _schema("desktop_focus", "聚焦指定窗口标题。", {"title": {"type": "string"}}, ["title"]),
    # ---------------- 验收与交付 ----------------
    _schema("validate_page", "在工具机浏览器打开任务页面做验收：桌面端+手机端截图、控制台错误、资源 404、横向滚动。",
            {"cid": _CID, "manifest": {"type": "array", "items": {"type": "string"}}}, ["cid"]),
    _schema("package_zip", "把任务目录打包成 zip 并推送到用户唯一仓库，返回下载地址。",
            {"cid": _CID,
             "owner": {"type": "string"}, "repo": {"type": "string"},
             "branch": {"type": "string"}, "prefix": {"type": "string"},
             "token": {"type": "string"}, "proxy": {"type": "string"}}, ["cid"]),
    _schema("bind_task", "把任务目录绑定到 GitHub 仓库，之后每 6 分钟自动上传一次。",
            {"cid": _CID, "owner": {"type": "string"}, "repo": {"type": "string"},
             "branch": {"type": "string"}, "prefix": {"type": "string"},
             "token": {"type": "string"}}, ["cid", "owner", "repo", "branch"]),
    _schema("sync_task", "立即同步一次任务目录到 GitHub。", {"cid": _CID}, ["cid"]),
    _schema("task_status", "查询任务的 GitHub 同步状态。", {"cid": _CID}, ["cid"]),
    _schema("env_info", "返回工具机系统、架构、磁盘与运行时间等环境信息。", {}, []),
]


def _to_action(name, args):
    args = dict(args or {})
    if name == "fs_write":
        return "project_write", {"cid": args.get("cid") or "", "files": {
            args.get("path") or "artifact.txt": args.get("content") or ""}}
    if name == "fs_edit":
        return "project_edit", {
            "cid": args.get("cid") or "", "path": args.get("path") or "",
            "old_string": args.get("old_string") or "",
            "new_string": args.get("new_string") or "",
            "replace_all": bool(args.get("replace_all")),
        }
    if name == "validate_page":
        return "validate_preview", {"cid": args.get("cid") or "",
                                    "manifest": args.get("manifest") or []}
    if name == "package_zip":
        return "package_zip", args
    if name == "bind_task":
        return "bind_task", args
    if name == "sync_task":
        return "sync_task", args
    if name == "task_status":
        return "task_status", args
    if name in ("fs_list", "fs_read", "fs_delete", "fs_search", "fs_read_all", "fs_manifest"):
        return {"fs_list": "project_list", "fs_read": "project_read",
                "fs_delete": "project_delete", "fs_search": "project_search",
                "fs_read_all": "project_read_all", "fs_manifest": "project_manifest"}[name], args
    return _ACTION.get(name), args


def env_info():
    try:
        total, used, free = 0, 0, 0
        import shutil as _sh
        total, used, free = _sh.disk_usage("/")
    except Exception:
        pass
    return {
        "ok": True,
        "os": "windows" if os.name == "nt" else "linux",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "disk_free_mb": int(free / 1024 / 1024) if free else 0,
        "time": time.time(),
        "worker_id": os.environ.get("AI_WORKER_ID", ""),
    }


def call_tool(name, args, submit):
    """submit(action, params) -> dict；由 agent.py 注入。"""
    if name == "env_info":
        return env_info()
    if name not in _ACTION:
        return {"ok": False, "error": "未知 MCP 工具：%s" % name}
    action, params = _to_action(name, args)
    if not action:
        return {"ok": False, "error": "未知 MCP 工具：%s" % name}
    return submit(action, params)


def _result(obj, is_error=False):
    text = ""
    if isinstance(obj, dict):
        import json
        text = json.dumps(obj, ensure_ascii=False)
        for key in ("content", "stdout", "output", "summary", "error"):
            if obj.get(key):
                text = str(obj[key])
                break
    else:
        text = str(obj)
    return {
        "content": [{"type": "text", "text": text[:200000]}],
        "isError": bool(is_error),
        "structuredContent": obj if isinstance(obj, dict) else {"value": text},
    }


def handle_rpc(msg, submit):
    """处理一条 JSON-RPC 消息，返回响应 dict；通知类消息返回 None。"""
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
            "serverInfo": {"name": "newera-ai-worker", "version": VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        try:
            out = call_tool(name, args, submit)
        except Exception as e:
            out = {"ok": False, "error": ("MCP 工具异常：" + str(e))[:400]}
        ok = bool((out or {}).get("ok", True))
        return {"jsonrpc": "2.0", "id": rid,
                "result": _result(out, is_error=not ok)}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}
