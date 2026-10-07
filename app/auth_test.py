# -*- coding: utf-8 -*-
"""
auth_test.py —— 访问控制与定向传输测试

模拟真实多人公司环境：
  - 无房间码的陌生同事能否偷看/投递文件
  - 输错码会怎样
  - 正确输码后能否正常互传
  - 房主设置「只收某人」后其他人能否绕过
  - 房主关闭外部连接、更换房间码的效果
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import create_app, find_free_port

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra else ""))
    if not cond:
        failures.append(name)


# 模拟客户端来源 IP：服务端 _client_ip 优先读 X-Forwarded-For，
# 本地测试全靠它模拟"公司里另一台电脑"。
#
# 为什么需要这个？
#   服务端有"回环免码"设计：从 127.0.0.1 来的请求直接当房主，不用输码。
#   测试跑在本机，所有请求默认都是 127.0.0.1 —— 会被免码放行，
#   于是"陌生人被拦"的用例全部假通过/假失败。
#   所以默认把来源伪装成内网另一台机器，只有显式 peer="127.0.0.1"
#   的用例才走本机免码路径。
FAKE_CLIENT_IP = ["192.168.1.88"]


def req(url: str, method="GET", data=None, token="", headers=None, raw=None,
        peer=None):
    """统一请求封装，返回 (status, body_dict)。

    peer: 不传 = 用 FAKE_CLIENT_IP[0]（模拟外部同事）；
          传 "127.0.0.1" = 模拟本机房主（免码）；
          传具体 IP = 模拟该来源。
    """
    h = dict(headers or {})
    if token:
        h["X-Room-Token"] = token
    fake = peer if peer is not None else FAKE_CLIENT_IP[0]
    if fake:
        h["X-Forwarded-For"] = fake
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
                return resp.status, {"_raw_len": len(txt)}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"_raw": txt[:200]}


def main() -> int:
    td = tempfile.mkdtemp(prefix="lanfile_auth_")
    recv = os.path.join(td, "recv")
    os.makedirs(recv, exist_ok=True)

    port = find_free_port(9400)
    app, state = create_app(os.path.dirname(os.path.abspath(__file__)), port)
    state.recv_dir = recv

    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                               use_reloader=False),
        daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/api/ping", timeout=2).read()
            break
        except Exception:
            time.sleep(0.25)

    code = state.auth.room_code
    owner_token = state.owner_session.token

    print("=" * 62)
    print("访问控制与定向传输测试")
    print("=" * 62)
    print(f"房间码: {code}")

    # ---------- 0. 本机免码（房主自己的体验）
    print("\n[0] 本机访问免输房间码")
    st, body = req(base + "/api/status", peer="127.0.0.1")
    check("本机(127.0.0.1)访问无需输码", st == 200 and body.get("ok") is True,
          f"HTTP {st}")
    st, body = req(base + "/api/room", peer="127.0.0.1")
    check("本机被视为房主", body.get("is_owner") is True)
    check("本机可看到房间码", body.get("room_code") == code)
    st, body = req(base + "/api/status", peer="192.168.1.88")
    check("外部IP仍需输码(401)", st == 401, f"HTTP {st}")

    # ---------- 1. 无凭证访问
    print("\n[1] 没有房间码的陌生同事（模拟公司同网段其他人）")
    st, body = req(base + "/api/status")
    check("访问 /api/status 被拒绝(401)", st == 401, f"HTTP {st}")
    check("响应提示需要房间码", body.get("code") == "NEED_ROOM_CODE", str(body)[:70])

    st, body = req(base + "/api/tasks")
    check("偷看传输记录被拒绝(401)", st == 401, f"HTTP {st}")

    st, body = req(base + "/api/drives")
    check("偷看磁盘信息被拒绝(401)", st == 401, f"HTTP {st}")

    st, body = req(base + "/api/browse?path=" + quote(td, safe=""))
    check("偷看本机目录被拒绝(401)", st == 401, f"HTTP {st}")

    # 尝试无码上传
    tmpf = os.path.join(td, "偷传.bin")
    with open(tmpf, "wb") as f:
        f.write(b"x" * 1024)
    st, body = req(
        base + "/api/upload", method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Rel-Path": quote("偷传.bin", safe=""),
                 "X-File-Size": "1024"},
        raw=open(tmpf, "rb").read(), peer="192.168.1.88")
    check("无码投递文件被拒绝(401)", st == 401, f"HTTP {st}")
    check("恶意文件未落盘", not os.path.exists(os.path.join(recv, "偷传.bin")))

    st, body = req(base + "/api/room")
    check("偷看房间码被拒绝(401)", st == 401, f"HTTP {st}")

    st, body = req(base + "/api/download/nonexistent")
    check("无码下载被拒绝(401)", st == 401, f"HTTP {st}")

    # 房间码没被泄露到任何未授权响应里
    leaks = []
    for path in ["/api/status", "/api/tasks", "/api/room", "/api/ping"]:
        _, b = req(base + path)
        if code in json.dumps(b):
            leaks.append(path)
    check("未授权响应中不泄露房间码", not leaks, str(leaks))

    # ---------- 2. 公开端点
    print("\n[2] 公开端点（无需认证即可访问）")
    st, body = req(base + "/api/ping")
    check("/api/ping 可访问", st == 200 and body.get("ok") is True)
    check("ping 提示需要房间码", body.get("need_code") is True)
    check("ping 显示未认证", body.get("authed") is False)
    st, _ = req(base + "/")
    check("登录页面可访问", st == 200, f"HTTP {st}")
    st, _ = req(base + "/static/app.js")
    check("静态资源可访问", st == 200, f"HTTP {st}")

    # ---------- 3. 输错房间码
    print("\n[3] 输错房间码")
    st, body = req(base + "/api/join", method="POST", data={"code": "AAAAAA"})
    check("错误码被拒绝(403)", st == 403, f"HTTP {st}")
    check("提示码不正确", "不正确" in body.get("error", ""), body.get("error", ""))
    check("未拿到令牌", not body.get("token"))

    st, body = req(base + "/api/join", method="POST", data={"code": ""})
    check("空码被拒绝", st == 403)

    # ---------- 4. 正确输码
    print("\n[4] 同事输入正确房间码")
    st, body = req(base + "/api/join", method="POST",
                   data={"code": code, "nickname": "同事小王"})
    check("正确码登录成功", body.get("ok") is True, str(body)[:80])
    guest_token = body.get("token", "")
    check("获得访问令牌", len(guest_token) > 10)
    check("昵称被记录", body.get("nickname") == "同事小王", str(body.get("nickname")))

    # 小写 + 带分隔符也应该能进（用户实际会这么输）
    st, b2 = req(base + "/api/join", method="POST",
                 data={"code": code.lower()[:3] + "-" + code.lower()[3:],
                       "nickname": "打字随意的人"})
    check("小写+分隔符也能登录", b2.get("ok") is True, str(b2.get("error", "")))

    # ---------- 5. 持码后可正常使用
    print("\n[5] 持码后功能正常")
    st, body = req(base + "/api/status", token=guest_token)
    check("可查看状态", st == 200 and body.get("ok") is True, f"HTTP {st}")

    st, body = req(base + "/api/tasks", token=guest_token)
    check("可查看任务列表", st == 200, f"HTTP {st}")

    # 上传文件
    srcf = os.path.join(td, "同事的文件.txt")
    with open(srcf, "wb") as f:
        f.write(("来自同事的内容\n" * 100).encode())
    import hashlib
    sha = hashlib.sha256(open(srcf, "rb").read()).hexdigest()
    st, body = req(
        base + "/api/upload", method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Rel-Path": quote("同事的文件.txt", safe=""),
                 "X-File-Size": str(os.path.getsize(srcf)),
                 "X-File-Sha256": sha},
        raw=open(srcf, "rb").read(), token=guest_token)
    check("持码可上传文件", body.get("ok") is True, str(body.get("error", ""))[:70])
    check("文件已落到接收目录",
          os.path.exists(os.path.join(recv, "同事的文件.txt")))

    # 普通成员看不到房间码
    st, body = req(base + "/api/room", token=guest_token)
    check("普通成员看不到房间码", "room_code" not in body, str(list(body.keys())))
    check("普通成员被标记为非房主", body.get("is_owner") is False)
    check("普通成员能看到自己身份", body.get("nickname") == "同事小王")

    # ---------- 6. 房主专属操作
    print("\n[6] 房主专属操作（普通成员不得越权）")
    st, body = req(base + "/api/room/rotate", method="POST", token=guest_token)
    check("普通成员不能更换房间码(403)", st == 403, f"HTTP {st}")
    st, body = req(base + "/api/room/allow", method="POST",
                   data={"allow": False}, token=guest_token)
    check("普通成员不能关闭外部连接(403)", st == 403, f"HTTP {st}")
    st, body = req(base + "/api/room/target", method="POST",
                   data={"peer": "1.2.3.4"}, token=guest_token)
    check("普通成员不能设置定向(403)", st == 403, f"HTTP {st}")

    # 房主可以
    st, body = req(base + "/api/room", token=owner_token, peer="127.0.0.1")
    check("房主可见房间码", body.get("room_code") == code, str(body.get("room_code")))
    check("房主可见成员列表", len(body.get("members", [])) >= 2,
          f"{len(body.get('members', []))} 个")
    names = [m["nickname"] for m in body.get("members", [])]
    check("成员列表含同事昵称", "同事小王" in names, str(names))

    # ---------- 7. 定向传输
    print("\n[7] 定向传输（只收某一个人）")
    # 房主指定只接收某 IP（用 guest_token 的 peer 做目标）
    peers = [m["peer"] for m in body.get("members", []) if not m["is_owner"]]
    target = peers[0] if peers else "127.0.0.1"
    st, body = req(base + "/api/room/target", method="POST",
                   data={"peer": target}, token=owner_token)
    check("房主可设置定向收件人", body.get("target_peer") == target,
          str(body.get("target_peer")))

    # 本地测试所有客户端 IP 都是 127.0.0.1，所以这里直接测鉴权逻辑本身
    from auth import AuthManager
    am = AuthManager()
    am.set_target("192.168.1.20")
    check("非指定IP被拒", am.check_target("192.168.1.99")["allowed"] is False)
    check("拒绝时给出可读理由",
          "不是发给你" in am.check_target("192.168.1.99")["reason"])
    check("指定IP放行", am.check_target("192.168.1.20")["allowed"] is True)
    am.set_target("")
    check("取消定向后所有人放行", am.check_target("192.168.1.99")["allowed"] is True)

    # 端到端：设置定向到一个不存在的 IP，上传应被拒
    st, body = req(base + "/api/room/target", method="POST",
                   data={"peer": "10.99.99.99"}, token=owner_token)
    st, body = req(
        base + "/api/upload", method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Rel-Path": quote("不该收的文件.txt", safe=""),
                 "X-File-Size": "10"},
        raw=b"1234567890", token=guest_token)
    check("定向生效时他人上传被拒(403)", st == 403, f"HTTP {st} {str(body)[:60]}")
    check("被拒文件未落盘",
          not os.path.exists(os.path.join(recv, "不该收的文件.txt")))

    # 取消定向恢复
    st, _ = req(base + "/api/room/target", method="POST",
                data={"peer": "*"}, token=owner_token)
    st, body = req(
        base + "/api/upload", method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Rel-Path": quote("恢复后文件.txt", safe=""),
                 "X-File-Size": "10"},
        raw=b"1234567890", token=guest_token)
    check("取消定向后可正常上传", body.get("ok") is True, str(body.get("error", ""))[:60])

    # ---------- 8. 关闭外部连接
    print("\n[8] 房主关闭外部连接")
    st, body = req(base + "/api/room/allow", method="POST",
                   data={"allow": False}, token=owner_token)
    check("关闭成功", body.get("allow_guests") is False)
    st, body = req(base + "/api/join", method="POST",
                   data={"code": code, "nickname": "关闭后想进的人"})
    check("关闭后新连接被拒", st == 403 or body.get("ok") is False,
          f"HTTP {st} {body.get('error', '')}")
    st, body = req(base + "/api/status", token=guest_token)
    check("关闭后旧成员令牌失效(401)", st == 401, f"HTTP {st}")
    # 房主自己不受影响
    st, body = req(base + "/api/status", token=owner_token)
    check("房主仍可正常使用", st == 200 and body.get("ok") is True)

    # 恢复
    st, _ = req(base + "/api/room/allow", method="POST",
                data={"allow": True}, token=owner_token)

    # ---------- 9. 更换房间码
    print("\n[9] 更换房间码")
    st, b = req(base + "/api/join", method="POST", data={"code": code, "nickname": "换码前进来"})
    old_guest = b.get("token", "")
    st, body = req(base + "/api/room/rotate", method="POST", token=owner_token)
    new_code = body.get("room_code", "")
    check("拿到新房间码", len(new_code) == 6 and new_code != code,
          f"{code} -> {new_code}")

    st, body = req(base + "/api/join", method="POST", data={"code": code})
    check("旧房间码失效", body.get("ok") is not True, str(body.get("error", ""))[:50])
    st, body = req(base + "/api/join", method="POST", data={"code": new_code})
    check("新房间码可用", body.get("ok") is True, str(body.get("error", ""))[:50])
    st, body = req(base + "/api/status", token=old_guest)
    check("换码前的老成员被强制掉线(401)", st == 401, f"HTTP {st}")
    st, body = req(base + "/api/status", token=owner_token)
    check("房主不受换码影响", st == 200)

    # ---------- 10. 暴力破解防护
    print("\n[10] 暴力猜码防护")
    from auth import AuthManager, MAX_FAILS
    am2 = AuthManager(room_code="ZZZZZZ")
    locked_at = None
    for i in range(MAX_FAILS + 3):
        r = am2.verify("AAAAAA", "10.0.0.5")
        if r.get("locked_for") and locked_at is None:
            locked_at = i + 1
    check(f"连续错误 {MAX_FAILS} 次后锁定", locked_at is not None,
          f"第 {locked_at} 次触发" if locked_at else "从未锁定")
    r = am2.verify("ZZZZZZ", "10.0.0.5")
    check("锁定期间正确码也被拒", r["ok"] is False, str(r.get("error")))
    r = am2.verify("ZZZZZZ", "10.0.0.6")
    check("锁定按 IP 隔离（他人不受影响）", r["ok"] is True)

    # ---------- 11. 房主特权与令牌隔离
    print("\n[11] 令牌隔离")
    from auth import AuthManager as AM
    am3 = AM()
    owner = am3.create_owner_session()
    guest = am3.verify(am3.room_code, "192.168.1.60")["session"]
    check("房主会话标记正确", owner.is_owner is True and guest.is_owner is False)
    check("房主令牌与访客不同", owner.token != guest.token)
    am3.rotate_code()
    check("换码后访客会话被清空", am3.get_session(guest.token) is None)
    check("换码后房主会话保留", am3.get_session(owner.token) is not None)

    print("\n" + "=" * 62)
    if failures:
        print(f"✗ 失败 {len(failures)} 项：")
        for f in failures:
            print("   -", f)
        print("=" * 62)
        return 1
    print("✓ 访问控制与定向传输测试全部通过")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
