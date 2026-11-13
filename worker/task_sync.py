#!/usr/bin/env python3
"""工具机上的 GitHub 同步与打包。

- 每 6 分钟把当前任务目录推到用户唯一的仓库（任务分支）
- 交付时在工具机上打包 zip 并上传到 GitHub，主站只给下载链接
只用标准库，工具机不依赖 httpx。
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile

API = "https://api.github.com"
SYNC_INTERVAL = 360  # 6 分钟
PROXY = os.environ.get("GITHUB_PROXY", "https://gh-proxy.com/").rstrip("/") + "/"


def _headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+api+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "newera-ai-worker",
    }


def _request(method, url, token, payload=None, timeout=45):
    data = None
    headers = _headers(token)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {"_raw": raw}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError("GitHub %s %d：%s" % (method, e.code, body))
    except Exception as e:
        raise RuntimeError("GitHub 请求失败：%s" % str(e)[:200])


def push_files(owner, repo, branch, files, token, message="sync", prefix=""):
    """把 {path: text} 推到分支，返回 commit sha。"""
    if not files:
        return ""
    base = "%s/repos/%s/%s" % (API, owner, repo)
    parent = ""
    base_tree = ""
    try:
        ref = _request("GET", base + "/git/ref/heads/%s" % branch, token)
        parent = ((ref or {}).get("object") or {}).get("sha") or ""
    except Exception:
        parent = ""
    if parent:
        try:
            commit = _request("GET", base + "/git/commits/%s" % parent, token)
            base_tree = (commit or {}).get("tree", {}).get("sha") or ""
        except Exception:
            base_tree = ""
    items = []
    for path, content in files.items():
        raw = (content if isinstance(content, str) else str(content)).encode("utf-8")
        blob = _request("POST", base + "/git/blobs", token, {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        })
        items.append({"path": (prefix.rstrip("/") + "/" + path) if prefix else path,
                      "mode": "100644", "type": "blob", "sha": blob["sha"]})
    payload = {"tree": items}
    if base_tree:
        payload["base_tree"] = base_tree
    tree = _request("POST", base + "/git/trees", token, payload)
    commit_payload = {"message": message, "tree": tree["sha"], "parents": []}
    if parent:
        commit_payload["parents"] = [parent]
    new_commit = _request("POST", base + "/git/commits", token, commit_payload)
    sha = new_commit["sha"]
    try:
        _request("PATCH", base + "/git/refs/heads/%s" % branch, token,
                 {"sha": sha, "force": False})
    except Exception:
        _request("POST", base + "/git/refs", token,
                 {"ref": "refs/heads/%s" % branch, "sha": sha})
    return sha


_PUSH_LOCK = threading.RLock()


def push_bytes(owner, repo, branch, path, data, token, message="upload"):
    """串行推送，避免后台同步和打包同时改同一分支导致 GitHub 400（版本冲突）。"""
    last = None
    with _PUSH_LOCK:
        for attempt in range(3):
            try:
                return _push_bytes_once(owner, repo, branch, path, data, token, message)
            except Exception as e:
                last = e
                msg = str(e)
                if "400" in msg or "409" in msg or "422" in msg:
                    time.sleep(0.8 + attempt * 0.5)
                    continue
                raise
    raise last


def _push_bytes_once(owner, repo, branch, path, data, token, message="upload"):
    """把二进制文件（如 zip）作为 blob 推到分支，返回 raw 下载地址。"""
    base = "%s/repos/%s/%s" % (API, owner, repo)
    parent = ""
    base_tree = ""
    try:
        ref = _request("GET", base + "/git/ref/heads/%s" % branch, token)
        parent = ((ref or {}).get("object") or {}).get("sha") or ""
    except Exception:
        parent = ""
    if parent:
        try:
            commit = _request("GET", base + "/git/commits/%s" % parent, token)
            base_tree = (commit or {}).get("tree", {}).get("sha") or ""
        except Exception:
            base_tree = ""
    blob = _request("POST", base + "/git/blobs", token, {
        "content": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
    })
    payload = {"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]}
    if base_tree:
        payload["base_tree"] = base_tree
    tree = _request("POST", base + "/git/trees", token, payload)
    commit_payload = {"message": message, "tree": tree["sha"], "parents": []}
    if parent:
        commit_payload["parents"] = [parent]
    new_commit = _request("POST", base + "/git/commits", token, commit_payload)
    sha = new_commit["sha"]
    try:
        _request("PATCH", base + "/git/refs/heads/%s" % branch, token,
                 {"sha": sha, "force": False})
    except Exception:
        _request("POST", base + "/git/refs", token,
                 {"ref": "refs/heads/%s" % branch, "sha": sha})
    return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, branch, path)


def make_zip(src_dir, out_path):
    """把目录打包成 zip（相对路径，目录名为空）。"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, names in os.walk(src_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src_dir).replace("\\", "/")
                z.write(full, rel)
    return out_path


def download_branch_files(owner, repo, branch, prefix, token):
    """从 GitHub 拉取任务分支上的文件（工具机换机器后用来恢复工作副本）。"""
    url = "%s/repos/%s/%s/zipball/%s" % (API, owner, repo, branch)
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("拉取任务分支失败 HTTP %d" % e.code)
    import io
    import zipfile as _zip
    zin = _zip.ZipFile(io.BytesIO(raw))
    names = zin.namelist()
    top = ""
    if names:
        top = names[0].split("/")[0] + "/"
    out = {}
    for n in names:
        if n.endswith("/"):
            continue
        rel = n[len(top):] if top and n.startswith(top) else n
        if not rel:
            continue
        if prefix:
            if not rel.startswith(prefix.rstrip("/") + "/"):
                continue
            rel = rel[len(prefix.rstrip("/") + "/"):]
        try:
            out[rel] = zin.read(n).decode("utf-8", "replace")
        except Exception:
            continue
    return out


class TaskSync:
    """把任务目录绑到 GitHub 仓库上，每 6 分钟自动同步一次。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}   # cid -> {owner, repo, branch, prefix, token, dir, thread, last}

    def bind(self, cid, owner, repo, branch, prefix, token, src_dir):
        with self.lock:
            item = self.tasks.get(cid) or {}
            item.update({"owner": owner, "repo": repo, "branch": branch,
                         "prefix": prefix or "", "token": token or "", "dir": src_dir})
            self.tasks[cid] = item
            if item.get("thread") is None or not item["thread"].is_alive():
                t = threading.Thread(target=self._loop, args=(cid,), daemon=True)
                t.start()
                item["thread"] = t
            return item

    def unbind(self, cid):
        with self.lock:
            self.tasks.pop(cid, None)

    def status(self, cid):
        with self.lock:
            item = self.tasks.get(cid) or {}
            return {"bound": bool(item), "last_sync": item.get("last", 0),
                    "branch": item.get("branch", ""), "repo": item.get("repo", "")}

    def sync_once(self, cid):
        with self.lock:
            item = dict(self.tasks.get(cid) or {})
        if not item.get("token") or not item.get("dir"):
            return {"ok": False, "error": "任务尚未绑定 GitHub 仓库"}
        files = {}
        for root, dirs, names in os.walk(item["dir"]):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, item["dir"]).replace("\\", "/")
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        files[rel] = f.read()
                except Exception:
                    continue
        if not files:
            return {"ok": True, "files": 0, "note": "任务目录为空"}
        sha = push_files(item["owner"], item["repo"], item["branch"], files,
                         item["token"], message="sync task %s" % cid,
                         prefix=item.get("prefix") or "")
        with self.lock:
            if cid in self.tasks:
                self.tasks[cid]["last"] = time.time()
        return {"ok": True, "files": len(files), "commit": sha}

    def _loop(self, cid):
        while True:
            time.sleep(SYNC_INTERVAL)
            with self.lock:
                if cid not in self.tasks:
                    return
            try:
                self.sync_once(cid)
            except Exception:
                pass

    def run(self, action, params):
        """自研 /run 协议里的同步类动作入口。"""
        params = params or {}
        cid = params.get("cid") or ""
        src_dir = params.get("dir") or ""
        try:
            if action == "bind_task":
                item = self.bind(cid, params.get("owner") or "", params.get("repo") or "",
                                 params.get("branch") or "", params.get("prefix") or "",
                                 params.get("token") or "", src_dir)
                return {"ok": True, "bound": True, "branch": item.get("branch"),
                        "repo": item.get("repo")}
            if action == "sync_task":
                return self.sync_once(cid)
            if action == "package_zip":
                return self.package_zip(cid, proxy=PROXY)
            if action == "restore_task":
                return self.restore(cid, params.get("owner") or "", params.get("repo") or "",
                                    params.get("branch") or "", params.get("prefix") or "",
                                    params.get("token") or "", src_dir)
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        return {"ok": False, "error": "不支持的同步动作: %s" % action}

    def restore(self, cid, owner, repo, branch, prefix, token, src_dir):
        """工具机换机器后，从 GitHub 任务分支恢复工作副本。"""
        files = download_branch_files(owner, repo, branch, prefix, token)
        if not files:
            return {"ok": False, "error": "GitHub 上没有找到该任务的文件"}
        os.makedirs(src_dir, exist_ok=True)
        for path, content in files.items():
            full = os.path.abspath(os.path.join(src_dir, path))
            if not full.startswith(os.path.abspath(src_dir) + os.sep):
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
        return {"ok": True, "files": len(files)}

    def package_zip(self, cid, proxy=""):
        with self.lock:
            item = dict(self.tasks.get(cid) or {})
        if not item.get("token") or not item.get("dir"):
            return {"ok": False, "error": "任务尚未绑定 GitHub 仓库"}
        if not os.path.isdir(item["dir"]):
            return {"ok": False, "error": "任务目录不存在"}
        out = os.path.join(os.path.dirname(item["dir"]), "package-%s.zip" % cid)
        try:
            make_zip(item["dir"], out)
            with open(out, "rb") as f:
                data = f.read()
        except Exception as e:
            return {"ok": False, "error": "打包失败：%s" % str(e)[:200]}
        path = "packages/%s.zip" % cid
        try:
            url = push_bytes(item["owner"], item["repo"], item["branch"], path, data,
                             item["token"], message="package task %s" % cid)
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        if proxy:
            url = proxy.rstrip("/") + "/" + url
        return {"ok": True, "url": url, "path": path, "size": len(data),
                "repo": item.get("repo", ""), "branch": item.get("branch", "")}
