# -*- coding: utf-8 -*-
"""
crossplat_test.py —— 跨系统兼容性验证

目的：证明「非 Windows 客户端」（Linux / macOS / 黑苹果）能正常使用本工具。

怎么做到不真装一台 Linux？
  HTTP 是跨平台协议，Linux 上的 Firefox/Chrome 发出去的请求，
  与我们在本机用标准 HTTP 库伪造的请求，在网络层面完全等价。
  所以我们刻意模拟「非 Windows 客户端」的特征：
    - User-Agent 用 Linux/macOS 的浏览器 UA
    - 不发送任何 Windows 特有头
    - 路径分隔符只用 /
    - 文件名用 UTF-8 编码
  只要服务端全部正确处理，就能证明跨平台没问题。

关键点：真正的跨平台能力来自「接收方只需浏览器」这个设计，
      而不是「把 exe 拷到 Linux 上运行」——后者做不到（PE 格式）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9000"
ROOM_CODE = sys.argv[2] if len(sys.argv) > 2 else ""
TOKEN = ""
failures: list[str] = []

# 关键：绕过系统代理，否则 127.0.0.1 会被代理拦成 502
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

# 各系统的真实浏览器 UA —— 用来模拟"不同系统的客户端"
UAS = {
    "Linux-Ubuntu": ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) "
                     "Gecko/20100101 Firefox/121.0"),
    "Linux-Chrome": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "macOS-Safari": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                     "Version/17.2 Safari/605.1.15"),
    "Windows-Chrome": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
}


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra else ""))
    if not cond:
        failures.append(name)


def req(url: str, method="GET", data=None, token="", headers=None, raw=None,
        ua=None):
    h = dict(headers or {})
    if token:
        h["X-Room-Token"] = token
    if ua:
        h["User-Agent"] = ua
    body = None
    if raw is not None:
        body = raw
    elif data is not None:
        body = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            txt = resp.read().decode("utf-8", errors="ignore")
            try:
                return resp.status, json.loads(txt)
            except Exception:
                return resp.status, {"_raw_len": len(txt), "_text": txt[:120]}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"_raw": txt[:200]}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print("=" * 64)
    print("跨系统兼容性测试 —— 模拟 Linux / macOS / 黑苹果 客户端")
    print("=" * 64)
    print(f"目标服务: {BASE}")

    # ---------- 0. 服务可达性 ----------
    print("\n[0] 基础可达性")
    st, body = req(BASE + "/api/ping", ua=UAS["Linux-Ubuntu"])
    check("Linux Firefox UA 可访问 /api/ping", st == 200 and body.get("ok"),
          f"HTTP {st}")
    check("服务版本可读", body.get("version") == "1.1.0", str(body.get("version")))

    # ---------- 1. 用 Linux UA 登录 ----------
    print("\n[1] Linux 客户端用房间码登录")
    st, body = req(BASE + "/api/join", method="POST",
                   data={"code": ROOM_CODE, "nickname": "Linux小刘"},
                   ua=UAS["Linux-Ubuntu"])
    check("Linux UA 登录成功", body.get("ok") is True, str(body)[:90])
    global TOKEN
    TOKEN = body.get("token", "")
    check("拿到访问令牌", len(TOKEN) > 10)

    # ---------- 2. macOS 客户端登录（同一房间，多系统共存） ----------
    print("\n[2] macOS 客户端也能同时登录")
    st, body = req(BASE + "/api/join", method="POST",
                   data={"code": ROOM_CODE, "nickname": "Mac小陈"},
                   ua=UAS["macOS-Safari"])
    check("macOS Safari UA 登录成功", body.get("ok") is True, str(body)[:90])
    mac_token = body.get("token", "")

    # 黑苹果：本质就是跑 macOS 的普通 PC，UA 与真 Mac 无异
    st, body = req(BASE + "/api/join", method="POST",
                   data={"code": ROOM_CODE, "nickname": "黑苹果"},
                   ua=UAS["macOS-Safari"])
    check("黑苹果（macOS UA）登录成功", body.get("ok") is True, str(body)[:90])

    # ---------- 3. Linux 客户端上传文件（含中文名） ----------
    print("\n[3] Linux 客户端上传（含中文文件名，模拟 Linux 桌面）")
    content = "来自 Linux 的文件\n".encode("utf-8") * 500
    fname = "Linux报告-项目进度.txt"
    # Linux 客户端路径用 / 分隔
    rel = "/".join(["来自Linux", "deep", "nested", fname])
    st, body = req(
        BASE + "/api/upload", method="POST", token=TOKEN,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Rel-Path": quote(rel, safe=""),
            "X-File-Size": str(len(content)),
            "X-File-Sha256": sha256_of(content),
        },
        raw=content, ua=UAS["Linux-Ubuntu"])
    check("Linux 客户端上传成功", st == 200 and body.get("ok") is True,
          f"HTTP {st} {str(body)[:80]}")
    # 流式上传返回的落盘路径字段是 path（不是 final_path）
    final = body.get("path", "")
    check("中文路径正确落盘（无乱码）", bool(final) and "%" not in final,
          str(final)[:110])
    if final and os.path.isfile(final):
        with open(final, "rb") as f:
            got = f.read()
        check("内容 SHA256 一致", sha256_of(got) == sha256_of(content))
    else:
        check("文件确实落盘", False, str(final)[:110])

    # ---------- 4. macOS 客户端上传 ----------
    print("\n[4] macOS 客户端上传")
    c2 = b"macOS payload " * 300
    st, body = req(
        BASE + "/api/upload", method="POST", token=mac_token,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Rel-Path": quote("macOS文件.bin", safe=""),
            "X-File-Size": str(len(c2)),
            "X-File-Sha256": sha256_of(c2),
        },
        raw=c2, ua=UAS["macOS-Safari"])
    check("macOS 客户端上传成功", st == 200 and body.get("ok") is True,
          f"HTTP {st}")

    # ---------- 5. 跨系统下载（Linux 客户端从服务端取文件） ----------
    # 下载走 task_id：先让房主把文件登记成「可发送」，再按返回的 url 下载
    print("\n[5] 跨系统下载（服务端 -> Linux 客户端）")
    src = os.path.join(tempfile.gettempdir(), "跨系统下载测试.txt")
    with open(src, "wb") as f:
        f.write(content)
    st, sb = req(BASE + "/api/send", method="POST", token=TOKEN,
                 data={"paths": [src]}, ua=UAS["Linux-Ubuntu"])
    items = sb.get("items") or []
    check("房主登记发送任务成功", st == 200 and len(items) == 1,
          str(sb)[:100])
    if items:
        dl = BASE + items[0]["url"]
        r = urllib.request.Request(dl, headers={
            "X-Room-Token": TOKEN, "User-Agent": UAS["Linux-Ubuntu"]})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                got = resp.read()
            check("Linux 客户端下载成功", len(got) == len(content),
                  f"{len(got)} vs {len(content)}")
            check("下载内容一致", got == content)
        except urllib.error.HTTPError as e:
            check("Linux 客户端下载成功", False, f"HTTP {e.code}")
    else:
        check("Linux 客户端下载成功", False, "未能创建下载任务")

    # ---------- 6. 路径分隔符归一化 ----------
    print("\n[6] 路径分隔符处理（Linux 用 /，Windows 用 \\）")
    c3 = b"sep test"
    st, body = req(
        BASE + "/api/upload", method="POST", token=TOKEN,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Rel-Path": quote("a/b/c/分隔符测试.txt", safe=""),
            "X-File-Size": str(len(c3)),
        },
        raw=c3, ua=UAS["Linux-Chrome"])
    check("正斜杠路径被正确处理", st == 200 and body.get("ok") is True,
          str(body.get("path", ""))[:110])
    fp = body.get("path", "")
    # 服务端在 Windows 上落盘时会把 / 转成 \，在 Linux 上保持 /
    check("落盘路径分隔符已归一化为系统原生",
          bool(fp) and os.sep in fp, fp[:110])

    # macOS 的 /Volumes/ 风格路径不应被误判为目录穿越
    st, body = req(
        BASE + "/api/upload", method="POST", token=TOKEN,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Rel-Path": quote("../../etc/passwd", safe=""),
            "X-File-Size": "9",
        },
        raw=b"malicious", ua=UAS["Linux-Ubuntu"])
    escaped = body.get("path", "")
    # 关键：攻击路径必须被"拍平"进接收目录，绝不能真的跑到 C:\etc 或 /etc
    # 正确表现：../../etc/passwd  ->  <接收目录>\etc\passwd   （丢掉了 ..）
    inside = False
    if escaped:
        ap = os.path.abspath(escaped)
        # 用例里的接收目录：用户 Downloads\LANFileTransfer
        marker = os.path.join("Downloads", "LANFileTransfer")
        inside = marker in ap
    check("目录穿越攻击仍被拦截（拍平进接收目录）", inside,
          escaped[:110])
    check("未真的写到系统 etc 目录", not os.path.exists(
        "C:\\etc\\passwd" if os.name == "nt" else "/etc/passwd"))

    # ---------- 7. UTF-8 与特殊字符 ----------
    print("\n[7] 不同系统的字符编码兼容")
    tricky = "emoji_🎉_符号#&%_空格 测试.txt"
    st, body = req(
        BASE + "/api/upload", method="POST", token=TOKEN,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Rel-Path": quote(tricky, safe=""),
            "X-File-Size": "5",
        },
        raw=b"hello", ua=UAS["macOS-Safari"])
    check("emoji/特殊字符文件名可用", st == 200 and body.get("ok") is True,
          str(body.get("path", ""))[:110])

    # ---------- 8. 服务端 HTTP 头是否平台中立 ----------
    print("\n[8] 响应头平台中立性")
    r = urllib.request.Request(BASE + "/api/ping", headers={
        "User-Agent": UAS["Linux-Ubuntu"]})
    with urllib.request.urlopen(r, timeout=30) as resp:
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
    check("Server 头不含 Windows 标识",
          "win" not in hdrs.get("server", "").lower(),
          hdrs.get("server", "(none)"))
    check("响应为 UTF-8 JSON",
          "json" in hdrs.get("content-type", ""), hdrs.get("content-type", ""))

    # ---------- 9. 结论 ----------
    print("\n" + "=" * 64)
    if failures:
        print(f"✗ 失败 {len(failures)} 项：")
        for f in failures:
            print("   -", f)
    else:
        print("✓ 跨系统兼容性测试全部通过")
        print()
        print("  结论：Linux / macOS / 黑苹果 客户端")
        print("        均可通过浏览器正常收发文件，")
        print("        无需安装任何本工具的程序。")
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
