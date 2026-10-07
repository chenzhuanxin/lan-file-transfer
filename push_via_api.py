# -*- coding: utf-8 -*-
"""
push_via_api.py —— 在 github.com 主站被墙、但 api.github.com 可直连时，
用 GitHub Contents API 把本地文件推送到仓库。

背景：
  GFW 封 github.com 主站（git push 走的 HTTPS 会被 TLS 打断），
  但 api.github.com 通常可直连。所以改用 REST API 逐文件写。

特性：
  - 递归遍历目录，自动跳过 .git / build / dist / __pycache__
  - 先查文件是否已存在（拿 sha），存在则 update，不存在则 create
  - 支持并发上传（默认 4 线程），大幅提速
  - 失败重试
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

OWNER = "chenzhuanxin"
REPO = "lan-file-transfer"
BRANCH = "main"
ROOT = r"D:\lan-file-transfer"
JOBS = 4

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""

SKIP_DIRS = {".git", "build", "dist", "__pycache__", ".venv", "state",
             "node_modules", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".part", ".resume"}
SKIP_FILES = {"exe_out.log", "_code.txt", "procs.txt"}

# 不走代理（api.github.com 直连更快更稳）
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

API = f"https://api.github.com/repos/{OWNER}/{REPO}/contents"
lock = threading.Lock()          # 保护计数器与打印
commit_lock = threading.Lock()   # 串行化「查 sha -> 写」，避免 409 撞车
done = 0
failures: list[str] = []


def api(method: str, url: str, payload=None, retries: int = 3):
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "lan-file-transfer-deployer",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            if e.code == 404:
                return 404, {}
            # 409 = 并发写同一分支撞车；429/5xx = 限流或服务端抖动。都值得重试
            if e.code in (409, 429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            if attempt == retries - 1:
                try:
                    return e.code, json.loads(raw)
                except Exception:
                    return e.code, {"_raw": raw[:300]}
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            if attempt == retries - 1:
                return 0, {"_err": str(exc)}
            time.sleep(1.5 * (attempt + 1))
    return 0, {}


def urlify(rel: str) -> str:
    """把相对路径编码进 URL —— 中文文件名必须转义，否则 urllib 抛 ASCII 编码错。"""
    from urllib.parse import quote
    return quote(rel, safe="/")


def collect(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES:
                continue
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append(rel)
    return sorted(out)


def upload(rel: str) -> None:
    global done, last_commit
    full = os.path.join(ROOT, rel.replace("/", os.sep))

    # 关键：串行化「查 sha -> 写」这段，否则并发写同一分支会 409 撞车。
    # 加锁只覆盖 API 交互，文件读取放在锁外。
    with open(full, "rb") as f:
        content = f.read()

    payload = {
        "message": f"upload: {rel}",
        "content": base64.b64encode(content).decode(),
        "branch": BRANCH,
    }

    with commit_lock:
        url = f"{API}/{urlify(rel)}"
        st, info = api("GET", f"{url}?ref={BRANCH}")
        sha = info.get("sha") if st == 200 else None
        if sha:
            payload["sha"] = sha
        st, res = api("PUT", url, payload)

    with lock:
        done += 1
        if st in (200, 201):
            action = "update" if sha else "create"
            print(f"  [{done:3}/{total}] ✓ {action:6} {rel}  ({len(content)} B)")
        else:
            failures.append(rel)
            print(f"  [{done:3}/{total}] ✗ {rel}  HTTP {st} {str(res)[:110]}")


if not TOKEN:
    print("用法: python push_via_api.py <github_token>")
    sys.exit(1)

files = collect(ROOT)
total = len(files)
print("=" * 68)
print(f"通过 GitHub Contents API 推送  ->  {OWNER}/{REPO} (branch {BRANCH})")
print(f"待上传文件: {total} 个")
print("=" * 68)

t0 = time.time()
with ThreadPoolExecutor(max_workers=JOBS) as ex:
    futs = [ex.submit(upload, f) for f in files]
    for _ in as_completed(futs):
        pass

print("=" * 68)
print(f"完成：成功 {total - len(failures)} / {total}，耗时 {time.time() - t0:.1f}s")
if failures:
    print("失败列表：")
    for f in failures:
        print("   -", f)
print("=" * 68)
sys.exit(1 if failures else 0)
