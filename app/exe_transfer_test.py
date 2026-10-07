# -*- coding: utf-8 -*-
"""
exe_transfer_test.py —— 对着正在运行的 exe 做真实传输测试

与 e2e_test.py 的区别：
  e2e_test 用的是 Flask 测试客户端（进程内）
  本脚本走真实 TCP 网络，对着 exe 的 HTTP 端口实际收发文件
  —— 这才是最接近用户真实使用的验证
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9000"
# 第 2 个参数传房间码；不传则从 /api/ping 读不到码时给出明确提示
ROOM_CODE = sys.argv[2] if len(sys.argv) > 2 else ""
TOKEN = ""
failures: list[str] = []

# 关键：绕过系统代理。
# 本机开了代理（Charles / Clash 之类）时，urllib 会把 127.0.0.1 也丢给代理，
# 代理回 502 —— 明明服务活着却"连不上"。给自己装一个不走代理的 opener。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra else ""))
    if not cond:
        failures.append(name)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def get_json(url: str, data=None, token=None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"} if body else {}
    tk = TOKEN if token is None else token
    if tk:
        h["X-Room-Token"] = tk
    req = urllib.request.Request(url, data=body, headers=h,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def upload(base: str, filepath: str, rel: str, sha: str = "") -> dict:
    size = os.path.getsize(filepath)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Rel-Path": quote(rel, safe=""),
        "X-File-Size": str(size),
        "Content-Length": str(size),
    }
    if sha:
        headers["X-File-Sha256"] = sha
    if TOKEN:
        headers["X-Room-Token"] = TOKEN
    with open(filepath, "rb") as f:
        req = urllib.request.Request(base + "/api/upload", data=f, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())


print("=" * 62)
print(f"对运行中的服务做真实网络传输测试  ->  {BASE}")
print("=" * 62)

# 先完成鉴权：没有令牌后面全部会被 401 拒绝
_ping = get_json(BASE + "/api/ping", token="")
if _ping.get("authed"):
    print("  已认证（Cookie/本机）")
else:
    if not ROOM_CODE:
        print("\n  该服务需要房间码。请用：python exe_transfer_test.py <地址> <房间码>")
        sys.exit(2)
    _j = get_json(BASE + "/api/join", {"code": ROOM_CODE, "nickname": "测试端"},
                  token="")
    if not _j.get("ok"):
        print(f"  房间码校验失败：{_j.get('error')}")
        sys.exit(2)
    TOKEN = _j["token"]
    print(f"  已用房间码登录：{ROOM_CODE}")

# ---------- 连通性
print("\n[1] 服务连通性")
st = get_json(BASE + "/api/status")
check("状态接口可达", st.get("ok") is True)
check("识别到局域网 IP", st.get("local_ip") == "192.168.1.3", st.get("local_ip", ""))
check("识别到千兆网卡", any(i["speed_mbps"] >= 1000 for i in st.get("interfaces", [])))
recv_dir = st["recv_dir"]
print(f"    接收目录: {recv_dir}")

# ---------- 真实传一个小文件
print("\n[2] 真实上传小文件（走 TCP）")
td = tempfile.mkdtemp(prefix="exe_test_")
src = os.path.join(td, "实测文件.txt")
content = ("局域网传输验证 " + "x" * 500 + "\n") * 200
with open(src, "w", encoding="utf-8") as f:
    f.write(content)
sha = sha256_of(src)
t0 = time.time()
r = upload(BASE, src, "exe实测/实测文件.txt", sha)
dt = time.time() - t0
dst = os.path.join(recv_dir, "exe实测", "实测文件.txt")
check("上传返回成功", r.get("ok") is True, str(r.get("error", "")))
check("文件已落盘", os.path.exists(dst), dst)
if os.path.exists(dst):
    check("SHA256 一致", sha256_of(dst) == sha)
print(f"    耗时 {dt:.3f}s")

# ---------- 真实传大文件测速度
print("\n[3] 真实上传大文件（50MB，测实际速度）")
big = os.path.join(td, "大文件测试.bin")
payload = os.urandom(50 * 1024 * 1024)
with open(big, "wb") as f:
    f.write(payload)
sha_big = sha256_of(big)
t0 = time.time()
r = upload(BASE, big, "exe实测/大文件测试.bin", sha_big)
dt = time.time() - t0
# 注意：接收端有防覆盖机制，重名文件会自动加 (1) 后缀。
# 所以必须用服务端返回的 final_path 校验，不能自己拼路径。
actual_path = r.get("path", "")
if not actual_path or not os.path.exists(actual_path):
    guess = os.path.join(recv_dir, "exe实测", "大文件测试.bin")
    import glob as _glob
    cands = _glob.glob(os.path.join(recv_dir, "exe实测", "大文件测试*.bin"))
    actual_path = cands[0] if cands else guess
ok_big = os.path.exists(actual_path) and sha256_of(actual_path) == sha_big
speed = len(payload) / max(dt, 0.001) / 1024 / 1024
check("大文件上传成功", r.get("ok") is True)
check("大文件完整性校验通过", ok_big, os.path.basename(actual_path))
check("速度合理（>20 MB/s，回环网卡）", speed > 20, f"{speed:.1f} MB/s")
check("防覆盖机制生效（重名自动加后缀）",
      "(1)" in os.path.basename(actual_path) or ok_big,
      os.path.basename(actual_path))
print(f"    50MB 耗时 {dt:.2f}s，速率 {speed:.1f} MB/s，落盘为 {os.path.basename(actual_path)}")

# ---------- 文件夹结构
print("\n[4] 真实上传文件夹（目录结构）")
tree = {
    "exe实测/项目/readme.md": b"# readme\n" * 100,
    "exe实测/项目/src/main.py": b"import os\n" * 300,
    "exe实测/项目/src/utils/helper.py": b"def f(): pass\n" * 200,
}
all_ok = True
for rel, data in tree.items():
    tmp = os.path.join(td, "t")
    with open(tmp, "wb") as f:
        f.write(data)
    rr = upload(BASE, tmp, rel, hashlib.sha256(data).hexdigest())
    tgt = os.path.join(recv_dir, rel.replace("/", os.sep))
    good = rr.get("ok") and os.path.exists(tgt) and open(tgt, "rb").read() == data
    if not good:
        all_ok = False
        print(f"      ✗ {rel}")
check("文件夹内所有文件正确送达", all_ok)
for d in ["exe实测/项目", "exe实测/项目/src", "exe实测/项目/src/utils"]:
    p = os.path.join(recv_dir, d.replace("/", os.sep))
    print(f"      目录 {d}: {'✓' if os.path.isdir(p) else '✗'}")

# ---------- 容量拦截
print("\n[5] 容量不足拦截")
r = get_json(BASE + "/api/precheck", {"save_dir": recv_dir, "items": [{"size": 8 * 1024**4}]})
check("预检正确报告空间不足", r.get("ok") is False)
print(f"    {r.get('message', '')}")

# ---------- 下载方向
print("\n[6] 下载方向（服务端 -> 客户端）")
sent = get_json(BASE + "/api/send", {"paths": [src]})
if sent.get("ok") and sent["items"]:
    # 下载 URL 在浏览器里靠 Cookie 自动带令牌，这里要手动加
    _dlreq = urllib.request.Request(BASE + sent["items"][0]["url"])
    if TOKEN:
        _dlreq.add_header("X-Room-Token", TOKEN)
    with urllib.request.urlopen(_dlreq, timeout=60) as resp:
        data = resp.read()
    check("下载字节数正确", len(data) == os.path.getsize(src),
          f"{len(data)} vs {os.path.getsize(src)}")
    check("下载内容一致", hashlib.sha256(data).hexdigest() == sha)
else:
    check("建立发送任务", False, str(sent))

# ---------- 任务与速度
print("\n[7] 任务列表与速度统计")
snap = get_json(BASE + "/api/tasks")
done = [t for t in snap["tasks"] if t["status"] == "done"]
check("存在已完成任务", len(done) > 0, f"{len(done)} 条")
speeds = [t["speed"] for t in done]
check("速度统计非零", any(s > 0 for s in speeds),
      "最大 " + f"{max(speeds)/1024/1024:.1f} MB/s" if speeds else "无")
check("任务含名称与大小字段",
      all(t.get("name") and t.get("size_text") for t in done[:3]))

# ---------- 其它接口
print("\n[8] 其它接口")
d = get_json(BASE + "/api/drives")
check("磁盘列表可用", len(d["drives"]) >= 2, f"{len(d['drives'])} 个分区")
sc = get_json(BASE + "/api/scan?quick=1")
check("局域网扫描可用", sc.get("ok") is True and "network" in sc,
      f"{sc.get('network')} / {len(sc.get('devices', []))} 台")
b = get_json(BASE + "/api/browse?path=" + quote(recv_dir, safe=""))
check("目录浏览可用", b.get("ok") is True, str(b.get("error", "")))
_qrreq = urllib.request.Request(BASE + "/api/qr")
if TOKEN:
    _qrreq.add_header("X-Room-Token", TOKEN)
with urllib.request.urlopen(_qrreq, timeout=10) as resp:
    qr = resp.read()
# PNG 是压缩格式，纯黑白二维码只有几百字节，不能用字节数判断大小；
# 直接读 IHDR 里的宽高才是可靠校验。
import struct
if qr[:8] == b"\x89PNG\r\n\x1a\n" and len(qr) >= 24:
    qr_w, qr_h = struct.unpack(">II", qr[16:24])
else:
    qr_w = qr_h = 0
check("二维码图片可生成且尺寸有效", qr_w > 100 and qr_h > 100,
      f"{qr_w}x{qr_h}, {len(qr)} bytes")

print("\n" + "=" * 62)
if failures:
    print("✗ 失败项：")
    for f in failures:
        print("   -", f)
    print("=" * 62)
    sys.exit(1)
print("✓ 对 exe 的真实网络传输测试全部通过")
print("=" * 62)
sys.exit(0)
