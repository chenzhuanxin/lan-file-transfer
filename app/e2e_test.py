# -*- coding: utf-8 -*-
"""
e2e_test.py —— 端到端实测

真实启动 HTTP 服务，然后用 HTTP 客户端实际传文件，验证：
  1. 普通上传（流式）数据完整性
  2. 大文件上传（跨多个 8MB 块）
  3. 文件夹上传后目录结构保留
  4. 容量不足时被正确拒绝
  5. 下载（发送方向）字节一致
  6. 任务状态与速度统计有值
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import create_app, find_free_port


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def http_get(url: str, token: str = "") -> bytes:
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Room-Token", token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def http_json(url: str, data=None, method=None, token: str = "") -> dict:
    body = None
    headers = {}
    if token:
        headers["X-Room-Token"] = token
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 让调用方能拿到 401/403 的错误体，而不是直接抛异常
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def upload_file(base: str, filepath: str, rel_path: str, sha: str = "",
                token: str = "") -> dict:
    """按前端的流式协议上传。"""
    size = os.path.getsize(filepath)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Rel-Path": quote(rel_path, safe=""),
        "X-File-Size": str(size),
    }
    if sha:
        headers["X-File-Sha256"] = sha
    if token:
        headers["X-Room-Token"] = token

    # 用分块流式发送，模拟真实大文件场景
    class ChunkedReader:
        def __init__(self, path):
            self.f = open(path, "rb")

        def read(self, n):
            return self.f.read(n)

        def __iter__(self):
            return self

        def __next__(self):
            b = self.f.read(256 * 1024)
            if not b:
                self.f.close()
                raise StopIteration
            return b

    req = urllib.request.Request(base + "/api/upload", data=ChunkedReader(filepath),
                                 headers=headers, method="POST")
    if "Content-Length" not in req.headers and "Content-length" not in req.headers:
        req.add_header("Content-Length", str(size))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def main() -> int:
    failures: list[str] = []
    td = tempfile.mkdtemp(prefix="lanfile_e2e_")
    recv = os.path.join(td, "recv")
    os.makedirs(recv, exist_ok=True)

    port = find_free_port(9300)
    app, state = create_app(os.path.dirname(os.path.abspath(__file__)), port)
    state.recv_dir = recv                      # 强制指到临时目录，别污染真实下载目录
    # 多了房间码鉴权后，测试必须带房主令牌 —— 顺便验证鉴权本身没把正常流程挡掉
    TOKEN = state.owner_session.token

    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False),
        daemon=True,
    )
    t.start()

    base = f"http://127.0.0.1:{port}"
    # 等服务起来
    for _ in range(60):
        try:
            http_json(base + "/api/status", token=TOKEN)
            break
        except Exception:
            time.sleep(0.25)

    print("=" * 62)
    print("端到端实测")
    print("=" * 62)

    # ---------- 1. 小文件上传（带 SHA 校验）
    print("\n[1] 小文件流式上传 + SHA256 校验")
    src1 = os.path.join(td, "小文件.txt")
    content1 = "局域网文件传输测试内容\n" * 1000
    with open(src1, "w", encoding="utf-8") as f:
        f.write(content1)
    sha1 = sha256_of(src1)
    r = upload_file(base, src1, "小文件.txt", sha1, TOKEN)
    dst1 = os.path.join(recv, "小文件.txt")
    ok = r.get("ok") and os.path.exists(dst1) and sha256_of(dst1) == sha1
    print(f"    ok={r.get('ok')} 落盘={os.path.exists(dst1)} SHA一致={sha256_of(dst1) == sha1 if os.path.exists(dst1) else False}")
    if not ok:
        failures.append("小文件上传")

    # ---------- 2. 大文件上传（20MB，跨 3 个块）
    print("\n[2] 大文件上传（20MB，跨 8MB 分块边界）")
    src2 = os.path.join(td, "大文件.bin")
    payload = os.urandom(20 * 1024 * 1024 + 7919)   # 故意不是块的整数倍
    with open(src2, "wb") as f:
        f.write(payload)
    sha2 = sha256_of(src2)
    t0 = time.time()
    r = upload_file(base, src2, "大文件.bin", sha2, TOKEN)
    dt = time.time() - t0
    dst2 = os.path.join(recv, "大文件.bin")
    same = os.path.exists(dst2) and sha256_of(dst2) == sha2
    speed = len(payload) / max(dt, 0.001) / 1024 / 1024
    print(f"    ok={r.get('ok')} 大小一致={same} 耗时={dt:.2f}s 速率={speed:.1f} MB/s")
    if not (r.get("ok") and same):
        failures.append("大文件上传")

    # ---------- 3. 文件夹上传（目录结构保留）
    print("\n[3] 文件夹上传（验证目录结构保留）")
    folder_files = [
        ("我的资料/说明.md", b"# readme\n" * 200),
        ("我的资料/图片/a.bin", os.urandom(300_000)),
        ("我的资料/图片/子目录/b.bin", os.urandom(150_000)),
        ("我的资料/代码/main.py", b"print('hi')\n" * 500),
    ]
    folder_ok = True
    for rel, data in folder_files:
        tmp = os.path.join(td, "tmp_upload")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        sha = hashlib.sha256(data).hexdigest()
        rr = upload_file(base, tmp, rel, sha, TOKEN)
        target = os.path.join(recv, rel.replace("/", os.sep))
        exists = os.path.exists(target)
        content_ok = exists and open(target, "rb").read() == data
        if not (rr.get("ok") and content_ok):
            folder_ok = False
            print(f"    ✗ {rel}: ok={rr.get('ok')} 存在={exists} 内容一致={content_ok}")
    # 检查目录树
    expected_dirs = ["我的资料", os.path.join("我的资料", "图片"),
                     os.path.join("我的资料", "图片", "子目录"), os.path.join("我的资料", "代码")]
    dirs_ok = all(os.path.isdir(os.path.join(recv, d)) for d in expected_dirs)
    print(f"    全部文件送达={folder_ok} 目录结构正确={dirs_ok}")
    for d in expected_dirs:
        print(f"      {d}: {'✓' if os.path.isdir(os.path.join(recv, d)) else '✗'}")
    if not (folder_ok and dirs_ok):
        failures.append("文件夹上传")

    # ---------- 4. 容量不足被拒绝
    print("\n[4] 容量不足时应被拒绝")
    r = http_json(base + "/api/precheck",
              {"save_dir": recv, "items": [{"size": 10 * 1024**4}]}, token=TOKEN)
    print(f"    ok={r['ok']}（期望 False）")
    print(f"    {r['message']}")
    if r["ok"]:
        failures.append("容量预检未拦截")

    # 实际尝试传一个"声明超大"的文件，应被拒
    src4 = os.path.join(td, "tiny.bin")
    with open(src4, "wb") as f:
        f.write(b"x" * 1024)
    size = os.path.getsize(src4)
    req = urllib.request.Request(
        base + "/api/upload", data=open(src4, "rb"),
        headers={"Content-Type": "application/octet-stream",
                 "X-Rel-Path": quote("超大.bin", safe=""),
                 "X-File-Size": str(20 * 1024**4),
                 "X-Room-Token": TOKEN}, method="POST")
    req.add_header("Content-Length", str(size))
    try:
        with urllib.request.urlopen(req, timeout=30) as rr:
            body = json.loads(rr.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
    rejected = not body.get("ok")
    print(f"    巨大文件被拒绝={rejected} -> {body.get('error', '')[:70]}")
    if not rejected:
        failures.append("超大文件未被拦截")

    # ---------- 5. 下载方向（我 → 对方）
    print("\n[5] 下载方向（服务端发文件给客户端）")
    src5 = os.path.join(td, "待发送.bin")
    with open(src5, "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024))
    sha5 = sha256_of(src5)
    sent = http_json(base + "/api/send", {"paths": [src5]}, token=TOKEN)
    if sent.get("ok") and sent["items"]:
        url = base + sent["items"][0]["url"]
        data = http_get(url, TOKEN)
        same = hashlib.sha256(data).hexdigest() == sha5
        print(f"    任务建立 ok 下载字节={len(data)} 内容一致={same}")
        if not same:
            failures.append("下载方向")
    else:
        print("    ✗ 建立发送任务失败:", sent)
        failures.append("建立发送任务")

    # ---------- 6. 任务与速度统计
    print("\n[6] 任务状态与速度统计")
    snap = http_json(base + "/api/tasks", token=TOKEN)
    s = snap["summary"]
    print(f"    汇总: 完成={s['done']} 失败={s['failed']} 进行中={s['active']}")
    speeds = [(t["name"], t["speed"], t["status"]) for t in snap["tasks"]]
    for name, sp, st in speeds[:6]:
        print(f"      {name[:26]:28s} 速度={sp/1024/1024:7.1f} MB/s 状态={st}")
    done_tasks = [t for t in snap["tasks"] if t["status"] == "done"]
    if not done_tasks:
        failures.append("无已完成任务")
    if not any(t["speed"] > 0 for t in done_tasks):
        failures.append("速度统计全为 0")

    # ---------- 7. 磁盘与扫描接口
    print("\n[7] 其它接口")
    d = http_json(base + "/api/drives", token=TOKEN)
    print(f"    磁盘分区 {len(d['drives'])} 个: " +
          ", ".join(f"{x['label']} {x['free_pct']}%" for x in d["drives"][:4]))
    sc = http_json(base + "/api/scan?quick=1", token=TOKEN)
    print(f"    局域网扫描: 网段={sc.get('network')} 设备={len(sc.get('devices', []))} 台 耗时={sc.get('elapsed')}s")
    b = http_json(base + "/api/browse?path=" + quote(td, safe=""), token=TOKEN)
    print(f"    目录浏览: {'ok' if b.get('ok') else '失败'} 条目={len(b.get('entries', []))}")

    # ---------- 结果
    print("\n" + "=" * 62)
    if failures:
        print("✗ 存在失败项：")
        for f in failures:
            print("   -", f)
        print("=" * 62)
        return 1
    print("✓ 全部端到端测试通过")
    print(f"  接收目录实际内容：")
    for root, dirs, files in os.walk(recv):
        level = root.replace(recv, "").count(os.sep)
        indent = "  " * (level + 2)
        print(f"{indent}{os.path.basename(root)}/")
        for fn in files:
            fp = os.path.join(root, fn)
            print(f"{indent}  {fn}  ({os.path.getsize(fp):,} bytes)")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
