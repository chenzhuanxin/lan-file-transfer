# -*- coding: utf-8 -*-
"""
auth.py —— 房间码访问控制 + 指定收件人

解决的问题：公司/宿舍等多人共用的局域网里，任何同网段的人只要知道
你的 IP 就能打开面板、看到你的传输记录、给你发文件 —— 这不可接受。

机制设计：
  1. 房主启动程序 → 自动生成 6 位房间码（易读，排除易混字符）
  2. 其他人必须输入正确房间码才能进入面板（错误一律拒绝，不泄露任何信息）
  3. 验证通过后发一个会话 token（存在浏览器 localStorage）
  4. 可选「定向传输」：房主指定只接收某个人的文件

为什么用房间码而不是 IP 白名单？
  公司网络里 IP 是 DHCP 动态分配的，白名单维护成本高；
  而且房间码能跨网段用（只要可达），更灵活。

为什么排除某些字符？
  0/O、1/I/l 在口头传达和手写时极易混淆 —— 用户经常要念给别人听。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Any


# 房间码字符集：排除 0 O 1 I L 等易混字符
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6

# 会话有效期（秒）：12 小时，覆盖一个工作日
SESSION_TTL = 12 * 3600

# 连错多少次锁定多久（防暴力猜码）
MAX_FAILS = 10
LOCKOUT_SECONDS = 300


def generate_room_code(length: int = CODE_LENGTH) -> str:
    """
    生成易读的房间码。
    用 secrets 而非 random —— 这是访问凭证，必须密码学安全。
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def normalize_code(raw: str) -> str:
    """
    规整用户输入：大小写不敏感、去掉空格和分隔符。
    用户会把 `a3f-9k2` 或 `A3F 9K2` 这样输进来，都该认。
    """
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    # 把用户可能误输的易混字符做一次映射（O→0 类问题反过来处理）
    trans = str.maketrans({"0": "O", "1": "I", "L": "I"})
    # 注意：字符集里本来就没有 O/0，所以这里只是防抖，不改变语义
    _ = cleaned.translate(trans)
    return cleaned


class Session:
    """一个已认证的客户端会话。"""

    def __init__(self, token: str, peer: str, nickname: str = ""):
        self.token = token
        self.peer = peer                    # 客户端 IP
        self.nickname = nickname or peer    # 显示名，默认用 IP
        self.created_at = time.time()
        self.last_seen = time.time()
        self.is_owner = False               # 是不是房主本人
        self.uploaded = 0
        self.downloaded = 0

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL

    def touch(self) -> None:
        self.last_seen = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer": self.peer,
            "nickname": self.nickname,
            "is_owner": self.is_owner,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "uploaded": self.uploaded,
            "downloaded": self.downloaded,
            "online": (time.time() - self.last_seen) < 60,
        }


class AuthManager:
    """
    房间码与会话管理。

    安全要点：
      - 房间码用 hmac.compare_digest 比对，防时序攻击
      - 验证失败不区分「码错」和「码不存在」，避免信息泄露
      - 连续失败达阈值后锁定一段时间，防暴力枚举
    """

    def __init__(self, room_code: str | None = None, require_code: bool = True,
                 trust_loopback: bool = True):
        self.room_code = room_code or generate_room_code()
        self.require_code = require_code
        # 本机（127.0.0.1 / ::1）访问视为房主，免输码 —— 否则用户每次在
        # 地址栏手输 http://127.0.0.1:9000 都要输一遍码，体验很差且没意义
        # （能坐在你这台电脑前的人本来就有全部权限）。
        self.trust_loopback = trust_loopback
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self._fails: dict[str, list[float]] = {}      # peer -> [失败时间戳]
        self.created_at = time.time()
        self.owner_token = ""                          # 房主自己的令牌
        self.allow_guests = True                       # 是否允许他人连接
        self.target_peer = ""                          # 定向传输：只收这个 IP 的东西

    # ---------------------------------------------------------- 会话

    def _new_token(self) -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def is_loopback(peer: str) -> bool:
        return peer in ("127.0.0.1", "::1", "localhost", "")

    def verify(self, raw_code: str, peer: str, nickname: str = "") -> dict[str, Any]:
        """
        校验房间码并签发会话。
        返回 {ok, token, session, error, locked_for}
        """
        with self._lock:
            # 锁定检查
            now = time.time()
            fails = [t for t in self._fails.get(peer, []) if now - t < LOCKOUT_SECONDS]
            self._fails[peer] = fails
            if len(fails) >= MAX_FAILS:
                wait = int(LOCKOUT_SECONDS - (now - fails[0])) + 1
                return {
                    "ok": False, "error": "尝试次数过多，请稍后再试",
                    "locked_for": max(wait, 1),
                }

            if not self.allow_guests:
                return {"ok": False, "error": "主机未开放连接"}

            given = normalize_code(raw_code)
            expect = normalize_code(self.room_code)

            # compare_digest 防时序攻击：不要用 == 比较凭证
            if not given or not hmac.compare_digest(given, expect):
                self._fails.setdefault(peer, []).append(now)
                n = len(self._fails[peer])
                left = max(0, MAX_FAILS - n)
                return {
                    "ok": False,
                    "error": f"房间码不正确，还可尝试 {left} 次" if left else
                             "尝试次数过多，请稍后再试",
                }

            # 成功：清失败记录，签发 token
            self._fails.pop(peer, None)
            token = self._new_token()
            sess = Session(token, peer, nickname)
            self._sessions[token] = sess
            return {"ok": True, "token": token, "session": sess, "error": ""}

    def create_owner_session(self, peer: str = "127.0.0.1") -> Session:
        """给自己（房主）签发一个永久会话 —— 本机访问不需要输码。"""
        with self._lock:
            token = self._new_token()
            sess = Session(token, peer, "主机")
            sess.is_owner = True
            self._sessions[token] = sess
            self.owner_token = token
            return sess

    def get_session(self, token: str) -> Session | None:
        if not token:
            return None
        with self._lock:
            sess = self._sessions.get(token)
            if not sess:
                return None
            if sess.expired:
                del self._sessions[token]
                return None
            sess.touch()
            return sess

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all_guests(self) -> int:
        """踢掉所有访客（房主改房间码时用）。"""
        with self._lock:
            guests = [t for t, s in self._sessions.items() if not s.is_owner]
            for t in guests:
                del self._sessions[t]
            return len(guests)

    def kick_peer(self, peer: str) -> int:
        """踢出指定 IP 的所有访客连接。返回踢掉的数量。"""
        if not peer:
            return 0
        with self._lock:
            doomed = [t for t, s in self._sessions.items()
                      if s.peer == peer and not s.is_owner]
            for t in doomed:
                del self._sessions[t]
            return len(doomed)

    def known_peers(self) -> list[str]:
        """已在线的访客 IP 列表。"""
        with self._lock:
            return [s.peer for s in self._sessions.values() if not s.is_owner]

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._sessions.values()]

    def rotate_code(self) -> str:
        """更换房间码（旧访客全部失效）。"""
        with self._lock:
            self.room_code = generate_room_code()
            self.revoke_all_guests()
            self._fails.clear()
            return self.room_code

    # ---------------------------------------------------------- 定向传输

    def check_target(self, peer: str) -> dict[str, Any]:
        """
        检查来源是否是指定收件人。
        用于「我只想发给某一个人」的场景 —— 其他人即使拿到房间码也不能发。
        """
        if not self.target_peer:
            return {"allowed": True, "reason": ""}
        if peer == self.target_peer:
            return {"allowed": True, "reason": ""}
        return {
            "allowed": False,
            "reason": "房主已指定接收对象，本次传输不是发给你的",
        }

    def set_target(self, peer: str) -> None:
        with self._lock:
            self.target_peer = (peer or "").strip()

    # ---------------------------------------------------------- 状态

    def status(self) -> dict[str, Any]:
        with self._lock:
            guests = [s for s in self._sessions.values() if not s.is_owner]
            return {
                "require_code": self.require_code,
                "allow_guests": self.allow_guests,
                "guest_count": len(guests),
                "target_peer": self.target_peer,
                "uptime": round(time.time() - self.created_at, 1),
            }


if __name__ == "__main__":
    print("=== 房间码生成 ===")
    for _ in range(5):
        print("  ", generate_room_code())

    print("\n=== 易读性检查（不应含易混字符）===")
    codes = [generate_room_code() for _ in range(200)]
    joined = "".join(codes)
    bad = [ch for ch in "01OIL" if ch in joined]
    print(f"  200 个码中出现的易混字符: {bad if bad else '无 ✓'}")

    print("\n=== 校验流程测试 ===")
    am = AuthManager(room_code="A3F9K2")
    print("  错误码:", am.verify("WRONG1", "192.168.1.50")["error"])
    print("  小写+带分隔符:", am.verify("a3f-9k2", "192.168.1.50")["ok"], "（应 True）")
    r = am.verify("A3F9K2", "192.168.1.51")
    print("  正确码:", r["ok"], "token 长度:", len(r.get("token", "")))
    print("  用 token 取会话:", am.get_session(r["token"]).nickname)

    print("\n=== 暴力破解锁定测试 ===")
    am2 = AuthManager(room_code="ZZZZZZ")
    for i in range(12):
        r = am2.verify("AAAAAA", "192.168.1.99")
    print("  第 12 次尝试:", r["error"], "锁定秒数:", r.get("locked_for"))

    print("\n=== 定向传输测试 ===")
    am3 = AuthManager()
    print("  未指定时:", am3.check_target("192.168.1.77"))
    am3.set_target("192.168.1.20")
    print("  指定后他人:", am3.check_target("192.168.1.77")["reason"])
    print("  指定后本人:", am3.check_target("192.168.1.20")["allowed"])
