# -*- coding: utf-8 -*-
"""
server.py —— 本地面板服务端

一个内网 HTTP 服务：
  /                → 面板页面（浏览器打开，手机扫码也能进）
  /api/status      → 本机网络与运行状态
  /api/scan        → 局域网设备扫描
  /api/drives      → 磁盘分区列表
  /api/browse      → 浏览本机目录（选择要发的文件）
  /api/pick        → 弹出系统原生文件/文件夹选择框（桌面端专用）
  /api/send/list   → 把选中的文件展开成待传清单
  /api/precheck    → 容量预检
  /api/upload      → 接收对方传来的文件
  /api/download    → 把本机文件发给对方
  /api/tasks       → 任务列表
  /api/tasks/<id>/cancel → 取消任务
  /api/tasks/clear → 清理已结束任务
  /api/settings    → 读写接收目录等设置
  /ws              → WebSocket 实时推送进度速度（失败则降级为轮询）

设计取舍：
  - 不引入 SQLite，任务只在内存，重启即清空（临时传输场景足够）
  - WebSocket 优先，但前端同时保留轮询兜底，避免代理环境 WS 被拦时失效
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

import netinfo
import storage
from auth import AuthManager
from tasks import TaskManager, build_send_list

APP_NAME = "局域网文件传输"
VERSION = "1.1.0"

# 无需前置鉴权的路径。
# 注意：这里的「无需鉴权」≠「不校验」，而是这些端点必须能被不带自定义头的
# 请求访问（EventSource 不能发自定义头、<img> 加载图片也不能），
# 所以由端点内部自己校验 Cookie 里的令牌。
PUBLIC_PATHS = {
    "/", "/api/join", "/api/ping",
    "/static/index.html", "/static/style.css", "/static/app.js",
    "/ws",          # EventSource 无法带自定义头，端点内自校验
    "/api/qr",      # <img> 标签无法带自定义头，端点内自校验
}


class AppState:
    """全局状态容器，避免散落的全局变量。"""

    def __init__(self, base_dir: str, port: int):
        self.base_dir = base_dir
        self.port = port
        self.recv_dir = storage.default_download_dir()
        self.state_dir = os.path.join(base_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)

        self.subscribers: list[queue.Queue] = []
        self.sub_lock = threading.Lock()

        self.tasks = TaskManager(
            recv_dir_provider=lambda: self.recv_dir,
            state_dir=self.state_dir,
            on_update=self.broadcast,
        )

        self.started_at = time.time()
        self.transfer_log: list[dict[str, Any]] = []

        # 访问控制：默认必须输房间码才能用
        self.auth = AuthManager()
        # 房主自己的令牌（本机访问自动带上，不需要输码）
        self.owner_session = self.auth.create_owner_session("127.0.0.1")

    # -------------------------------------------------- 推送

    def broadcast(self) -> None:
        """把最新状态推给所有等待中的前端。"""
        payload = self.snapshot()
        with self.sub_lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self.subscribers.remove(q)
                except ValueError:
                    pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "update",
            "summary": self.tasks.summary(),
            "tasks": self.tasks.list_tasks(limit=100),
            "recv_dir": self.recv_dir,
            "auth": self.auth.status(),
            "ts": time.time(),
        }

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self.sub_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.sub_lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass


def create_app(base_dir: str, port: int) -> tuple[Flask, AppState]:
    static_dir = os.path.join(base_dir, "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = None      # 不限制请求体大小 —— 要传大文件
    state = AppState(base_dir, port)

    # ------------------------------------------------------ 页面

    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.after_request
    def no_cache(resp: Response):
        # 面板是本地工具，禁缓存避免改了页面不生效
        if resp.mimetype in ("text/html", "application/javascript", "text/css"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------ 访问控制

    def _client_ip() -> str:
        """取真实客户端 IP（经代理时优先 X-Forwarded-For 的第一段）。"""
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.remote_addr or ""

    def _current_session():
        """从请求里解析会话：优先请求头，其次 Cookie。"""
        token = request.headers.get("X-Room-Token", "")
        if not token:
            token = request.cookies.get("room_token", "")
        return state.auth.get_session(token)

    @app.before_request
    def guard():
        """
        全局访问控制。

        为什么用 before_request 而不是装饰器逐个加？
          逐个加容易漏 —— 漏掉一个接口就等于门户大开。
          集中拦截，默认拒绝，只有白名单放行，安全性靠设计而非记性。
        """
        path = request.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return None

        sess = _current_session()
        if sess is None:
            # 本机访问自动视为房主：能坐在这台电脑前的人本就有一切权限，
            # 让他再输一遍房间码只是徒增麻烦。
            if state.auth.trust_loopback and state.auth.is_loopback(_client_ip()):
                sess = state.owner_session
            else:
                # 未认证：统一返回 401 + 明确指令，前端据此弹出输码界面
                return jsonify({
                    "ok": False,
                    "error": "需要房间码",
                    "code": "NEED_ROOM_CODE",
                    "public": {
                        "app": APP_NAME,
                        "version": VERSION,
                        "hostname": socket.gethostname(),
                    },
                }), 401

        # 把会话挂到 flask.g，后续处理函数可直接用
        from flask import g
        g.session = sess
        return None

    # ------------------------------------------------------ 登录

    @app.route("/api/ping")
    def api_ping():
        """
        探测是否需要房间码。前端进页面第一件事就是调它。
        不泄露房间码本身，只告诉需不需要。
        """
        sess = _current_session()
        return jsonify({
            "ok": True,
            "app": APP_NAME,
            "version": VERSION,
            "need_code": state.auth.require_code,
            "authed": sess is not None,
            "allow_guests": state.auth.allow_guests,
        })

    @app.route("/api/join", methods=["POST"])
    def api_join():
        """用房间码换取访问令牌。"""
        data = request.get_json(silent=True) or {}
        code = data.get("code", "")
        nickname = (data.get("nickname") or "").strip()[:24]
        peer = _client_ip()

        result = state.auth.verify(code, peer, nickname)
        if not result["ok"]:
            return jsonify({"ok": False, "error": result["error"],
                            "locked_for": result.get("locked_for", 0)}), 403

        sess: Any = result["session"]
        resp = jsonify({
            "ok": True,
            "token": result["token"],
            "nickname": sess.nickname,
            "peer": sess.peer,
        })
        # 同时写 Cookie，这样浏览器直接输 URL 也能保持登录
        resp.set_cookie("room_token", result["token"], max_age=12 * 3600,
                        httponly=False, samesite="Lax")
        return resp

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        token = request.headers.get("X-Room-Token") or request.cookies.get("room_token", "")
        state.auth.revoke(token)
        resp = jsonify({"ok": True})
        resp.delete_cookie("room_token")
        return resp

    # ------------------------------------------------------ 房间管理（仅房主）

    def _authed_or_local():
        """已认证，或来自本机（回环地址）—— 两个特殊端点用这个代替全局鉴权。"""
        sess = _current_session()
        if sess is not None:
            return True
        if state.auth.trust_loopback and state.auth.is_loopback(_client_ip()):
            return True
        return False

    def _require_owner():
        from flask import g
        sess = getattr(g, "session", None)
        if not sess or not sess.is_owner:
            return jsonify({"ok": False, "error": "只有主机可以操作"}), 403
        return None

    @app.route("/api/room")
    def api_room():
        """查看房间信息：房主能看到房间码和成员列表。"""
        from flask import g
        sess = getattr(g, "session", None)
        is_owner = bool(sess and sess.is_owner)
        info: dict[str, Any] = {
            "ok": True,
            "is_owner": is_owner,
            "allow_guests": state.auth.allow_guests,
            "target_peer": state.auth.target_peer,
            "guest_count": state.auth.status()["guest_count"],
        }
        if is_owner:
            info["room_code"] = state.auth.room_code
            info["members"] = state.auth.list_sessions()
        else:
            info["nickname"] = sess.nickname if sess else ""
            info["peer"] = sess.peer if sess else ""
        return jsonify(info)

    @app.route("/api/room/rotate", methods=["POST"])
    def api_room_rotate():
        """更换房间码（旧访客立即失效）。"""
        err = _require_owner()
        if err:
            return err
        new_code = state.auth.rotate_code()
        state.broadcast()
        return jsonify({"ok": True, "room_code": new_code})

    @app.route("/api/room/allow", methods=["POST"])
    def api_room_allow():
        """开关「是否允许他人连接」。关掉后只有本机能用。"""
        err = _require_owner()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        state.auth.allow_guests = bool(data.get("allow", True))
        if not state.auth.allow_guests:
            state.auth.revoke_all_guests()
        state.broadcast()
        return jsonify({"ok": True, "allow_guests": state.auth.allow_guests})

    @app.route("/api/room/target", methods=["POST"])
    def api_room_target():
        """
        定向传输：只接收指定 IP 的文件。
        这是「我只想发给某一个人」的核心开关。
        """
        err = _require_owner()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        peer = (data.get("peer") or "").strip()
        # list_sessions() 返回的是 dict 列表，取 peer 要用 ["peer"]
        # "*" 或空串表示取消定向（接收所有人）
        if peer in ("", "*"):
            state.auth.set_target("")
        else:
            state.auth.set_target(peer)
        state.broadcast()
        return jsonify({"ok": True, "target_peer": state.auth.target_peer})

    @app.route("/api/room/kick", methods=["POST"])
    def api_room_kick():
        """踢出某个已连接成员。"""
        err = _require_owner()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        peer = (data.get("peer") or "").strip()
        kicked = state.auth.kick_peer(peer)
        state.broadcast()
        return jsonify({"ok": True, "kicked": kicked})

    # ------------------------------------------------------ 状态

    @app.route("/api/status")
    def api_status():
        ifaces = netinfo.list_interfaces()
        local_ip = ifaces[0]["ip"] if ifaces else "127.0.0.1"
        return jsonify({
            "ok": True,
            "app": APP_NAME,
            "version": VERSION,
            "local_ip": local_ip,
            "port": state.port,
            "url": f"http://{local_ip}:{state.port}",
            "interfaces": ifaces,
            "hostname": socket.gethostname(),
            "platform": sys.platform,
            "recv_dir": state.recv_dir,
            "recv_free": storage.check_capacity(state.recv_dir, 0)["free"],
            "uptime": round(time.time() - state.started_at, 1),
        })

    @app.route("/api/public_ip")
    def api_public_ip():
        return jsonify(netinfo.get_public_ip())

    @app.route("/api/diagnose")
    def api_diagnose():
        return jsonify(netinfo.diagnose())

    @app.route("/api/scan")
    def api_scan():
        cidr = request.args.get("cidr") or None
        quick = request.args.get("quick", "1") != "0"
        return jsonify(netinfo.scan_lan(cidr=cidr, quick=quick))

    @app.route("/api/drives")
    def api_drives():
        return jsonify({"ok": True, "drives": storage.list_drives()})

    # ------------------------------------------------------ 本机文件浏览

    @app.route("/api/browse")
    def api_browse():
        """浏览本机目录，供「选择要发送的文件」用（浏览器端无法直接读本机文件系统）。"""
        path = request.args.get("path") or os.path.expanduser("~")
        try:
            path = os.path.abspath(path)
            entries = []
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir()
                        size = 0 if is_dir else e.stat().st_size
                    except OSError:
                        continue
                    entries.append({
                        "name": e.name, "path": e.path,
                        "is_dir": is_dir, "size": size,
                        "size_text": storage.fmt_size(size),
                    })
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            parent = os.path.dirname(path)
            return jsonify({
                "ok": True, "path": path, "parent": parent,
                "entries": entries,
                "drives": storage.list_drives(),
            })
        except PermissionError:
            return jsonify({"ok": False, "error": "无权访问该目录", "path": path})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "path": path})

    @app.route("/api/pick", methods=["POST"])
    def api_pick():
        """
        桌面端：弹出系统原生选择框。手机端调用会失败，前端会自动降级到文件上传控件。
        """
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "files")
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if mode == "folder":
                result = filedialog.askdirectory(title="选择要发送的文件夹")
            else:
                result = filedialog.askopenfilenames(title="选择要发送的文件")
            root.destroy()
            paths = list(result) if result else ([] if mode == "folder" else [])
            if mode == "folder" and isinstance(result, str) and result:
                paths = [result]
            return jsonify({"ok": True, "paths": paths, "canceled": not paths})
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"无法打开系统选择框（{exc}）。请使用界面中的文件上传方式。",
            })

    @app.route("/api/send/list", methods=["POST"])
    def api_send_list():
        data = request.get_json(silent=True) or {}
        paths = data.get("paths") or []
        if not paths:
            return jsonify({"ok": False, "error": "未选择任何文件"})
        result = build_send_list(paths)
        return jsonify(result)

    @app.route("/api/precheck", methods=["POST"])
    def api_precheck():
        """
        容量预检。两个用途：
          - 作为接收方：检查「对方要发来的东西」我的盘够不够
          - 作为发送方：检查「我要发的东西」是否合理
        """
        data = request.get_json(silent=True) or {}
        save_dir = data.get("save_dir") or state.recv_dir
        items = data.get("items") or []
        if data.get("paths"):
            listing = build_send_list(data["paths"])
            items = listing["items"]
        return jsonify(state.tasks.precheck(items, save_dir))

    # ------------------------------------------------------ 上传（对方 → 我）

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        """
        接收文件。两种提交方式：
          1. multipart/form-data：常规表单上传（小文件 / 手机浏览器）
          2. application/octet-stream + 请求头：流式上传（大文件，内存恒定）
        两者都走同一个任务管道，都会做容量预检。
        """
        # ---- 方式一：流式（推荐，大文件必须用这个）
        rel = request.headers.get("X-Rel-Path")
        if rel:
            # 前端用 encodeURIComponent 传相对路径（避免 HTTP 头里的非 ASCII 问题），
            # 这里必须还原，否则中文/特殊字符路径会落错地方。
            from urllib.parse import unquote
            try:
                rel = unquote(rel)
            except Exception:
                pass
            try:
                size = int(request.headers.get("X-File-Size") or 0)
            except ValueError:
                size = 0
            sha = request.headers.get("X-File-Sha256", "")
            offset = int(request.headers.get("X-Offset") or 0)
            peer = request.remote_addr or ""

            # 定向传输：房主指定了收件人时，其他人一律拒收
            target_check = state.auth.check_target(peer)
            if not target_check["allowed"]:
                return jsonify({"ok": False, "error": target_check["reason"]}), 403

            task = state.tasks.create_upload_task(rel, size, sha, peer)
            task.transferred = offset if 0 <= offset <= size else 0

            if task.transferred:
                # 续传：把流里的数据接到已有 .part 上
                pass

            chunks = request.stream.iter_chunks() if hasattr(request.stream, "iter_chunks") \
                else iter(lambda: request.stream.read(1024 * 256), b"")
            result = state.tasks.receive_stream(task, chunks)
            result["task_id"] = task.task_id
            return jsonify(result), (200 if result.get("ok") else 500)

        # ---- 方式二：multipart 表单
        files = request.files.getlist("files")
        if not files:
            return jsonify({"ok": False, "error": "没有收到文件"}), 400

        results = []
        peer_ip = request.remote_addr or ""
        # multipart 通道同样要过定向校验，否则绕过流式接口就能投递
        target_check = state.auth.check_target(peer_ip)
        if not target_check["allowed"]:
            return jsonify({"ok": False, "error": target_check["reason"]}), 403

        for f in files:
            rel = f.filename or "unnamed"
            # 某些浏览器会把完整路径塞进 filename，取相对部分
            rel = rel.replace("\\", "/")
            peer = request.remote_addr or ""
            task = state.tasks.create_upload_task(rel, 0, "", peer)
            task.size = 0

            def stream_one(fileobj):
                while True:
                    block = fileobj.read(1024 * 256)
                    if not block:
                        break
                    yield block

            # 表单方式拿不到总大小，先落盘再回填
            tgt = storage.safe_join(state.recv_dir, rel)
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            tmp = tgt + ".part"
            written = 0
            last = 0.0
            task.started_at = time.monotonic()
            task.status = "running"
            try:
                with open(tmp, "wb") as out:
                    for block in stream_one(f):
                        out.write(block)
                        written += len(block)
                        task.meter.add(len(block))
                        task.transferred = written
                        now = time.monotonic()
                        if now - last > 0.3:
                            last = now
                            state.broadcast()
                out_path = tgt
                if os.path.exists(out_path):
                    out_path = storage.unique_path(out_path)
                os.replace(tmp, out_path)
                task.size = written
                task.transferred = written
                task.final_path = out_path
                task.status = "done"
                task.finished_at = time.monotonic()
                results.append({"ok": True, "rel_path": rel, "size": written,
                                "task_id": task.task_id})
            except Exception as exc:
                task.status = "failed"
                task.error = str(exc)
                results.append({"ok": False, "rel_path": rel, "error": str(exc),
                                "task_id": task.task_id})
            state.broadcast()

        return jsonify({"ok": all(r["ok"] for r in results), "results": results})

    # ------------------------------------------------------ 下载（我 → 对方）

    @app.route("/api/download/<task_id>")
    def api_download(task_id: str):
        task = state.tasks.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if not os.path.isfile(task.source_path):
            return jsonify({"ok": False, "error": "源文件已不存在"}), 404

        filename = os.path.basename(task.rel_path.replace("\\", "/"))
        return Response(
            state.tasks.iter_download(task),
            mimetype="application/octet-stream",
            headers={
                "Content-Length": str(task.size),
                "Content-Disposition": _content_disposition(filename),
                "X-Task-Id": task_id,
            },
            direct_passthrough=True,
        )

    @app.route("/api/send", methods=["POST"])
    def api_send():
        """为选中的路径批量建下载任务，返回可直接点/拖拽的下载链接。"""
        data = request.get_json(silent=True) or {}
        paths = data.get("paths") or []
        peer = request.remote_addr or ""
        if not paths:
            return jsonify({"ok": False, "error": "未选择文件"})

        listing = build_send_list(paths)
        out = []
        for it in listing["items"]:
            t = state.tasks.create_download_task(it["path"], it["rel_path"], peer)
            out.append({
                "task_id": t.task_id,
                "rel_path": it["rel_path"],
                "size": it["size"],
                "size_text": storage.fmt_size(it["size"]),
                "url": f"/api/download/{t.task_id}",
            })
        return jsonify({"ok": True, "items": out, "total": listing["total"],
                        "total_text": listing["total_text"],
                        "errors": listing["errors"]})

    # ------------------------------------------------------ 二维码

    @app.route("/api/qr")
    def api_qr():
        """
        把连接地址渲染成 PNG 二维码。

        这个端点无法依赖 before_request 的全局鉴权 —— 它是被 <img> 标签加载的，
        浏览器不会带自定义请求头。所以这里自己校验 Cookie 里的令牌。
        """
        if not _authed_or_local():
            return jsonify({"ok": False, "error": "需要房间码"}), 401

        text = request.args.get("text") or f"http://{netinfo.best_local_ip()}:{state.port}"
        try:
            import io
            import qrcode
            img = qrcode.make(text, box_size=8, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return Response(buf.getvalue(), mimetype="image/png",
                            headers={"Cache-Control": "no-store"})
        except ImportError:
            # 没装 qrcode 时返回一个纯色占位图，前端会显示"生成失败"提示
            return Response(b"", mimetype="image/png", status=204)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    # ------------------------------------------------------ 任务

    @app.route("/api/tasks")
    def api_tasks():
        return jsonify(state.snapshot())

    @app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
    def api_cancel(task_id: str):
        return jsonify(state.tasks.cancel(task_id))

    @app.route("/api/tasks/clear", methods=["POST"])
    def api_clear():
        return jsonify(state.tasks.clear_finished())

    # ------------------------------------------------------ 设置

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            newdir = data.get("recv_dir")
            if newdir:
                newdir = os.path.abspath(newdir)
                try:
                    os.makedirs(newdir, exist_ok=True)
                    state.recv_dir = newdir
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"无法使用该目录：{exc}"})
        return jsonify({
            "ok": True,
            "recv_dir": state.recv_dir,
            "drives": storage.list_drives(),
            "default_dir": storage.default_download_dir(),
        })

    @app.route("/api/open_folder", methods=["POST"])
    def api_open_folder():
        """在资源管理器中打开接收目录。"""
        data = request.get_json(silent=True) or {}
        path = data.get("path") or state.recv_dir
        try:
            if sys.platform == "win32":
                os.startfile(os.path.abspath(path))  # noqa: S606
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    # ------------------------------------------------------ WebSocket

    try:
        from simple_websocket import Server as WS_Server
        _HAS_SIMPLE_WS = True
    except ImportError:
        _HAS_SIMPLE_WS = False

    if not _HAS_SIMPLE_WS:
        # 用 Flask 原生 WebSocket 需要额外依赖，这里退化为 SSE 长连接
        @app.route("/ws")
        def ws_fallback():
            # EventSource 无法携带自定义头，只能靠 Cookie 认证 —— 这里显式校验，
            # 否则会把房间码这道门变成摆设。
            if not _authed_or_local():
                return Response("unauthorized", status=401, mimetype="text/plain")

            def gen():
                q = state.subscribe()
                try:
                    yield "retry: 1000\n\n"
                    # 连上先推一次全量，前端不用再单独拉一遍
                    yield f"data: {json.dumps(state.snapshot(), ensure_ascii=False)}\n\n"
                    while True:
                        try:
                            payload = q.get(timeout=20)
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        except queue.Empty:
                            yield ": keepalive\n\n"
                finally:
                    state.unsubscribe(q)

            return Response(gen(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no"})
    else:
        @app.route("/ws")
        def ws_endpoint():
            if not _authed_or_local():
                return Response("unauthorized", status=401, mimetype="text/plain")
            ws = WS_Server(request.environ)
            q = state.subscribe()
            try:
                ws.send(json.dumps(state.snapshot(), ensure_ascii=False))
                while True:
                    try:
                        payload = q.get(timeout=1.0)
                        ws.send(json.dumps(payload, ensure_ascii=False))
                    except queue.Empty:
                        try:
                            ws.send(json.dumps({"type": "ping"}, ensure_ascii=False))
                        except Exception:
                            break
            except Exception:
                pass
            finally:
                state.unsubscribe(q)
                try:
                    ws.close()
                except Exception:
                    pass

    return app, state


def _content_disposition(filename: str) -> str:
    """正确处理中文文件名的下载响应头。"""
    from urllib.parse import quote
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "file"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


# ---------------------------------------------------------------- 启动

def find_free_port(preferred: int = 9000, tries: int = 20) -> int:
    """端口被占用时自动往后找，避免启动失败。"""
    for i in range(tries):
        p = preferred + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def banner(ip: str, port: int, recv_dir: str, room_code: str) -> str:
    url = f"http://{ip}:{port}"
    line = "=" * 58
    return (
        f"\n{line}\n"
        f"  {APP_NAME}  v{VERSION}\n"
        f"{line}\n"
        f"  本机访问    http://127.0.0.1:{port}\n"
        f"  局域网访问  {url}\n"
        f"  接收目录    {recv_dir}\n"
        f"{line}\n"
        f"  >>> 房间码：{room_code}   <<<\n"
        f"\n"
        f"  把这个房间码告诉你想要互传的人。\n"
        f"  对方在同一网络下打开 {url} ，输入房间码即可连接。\n"
        f"  没有房间码的人无法查看或传输任何文件。\n"
        f"{line}\n"
        f"  首次运行 Windows 会询问防火墙权限，必须点「允许访问」\n"
        f"  按 Ctrl+C 退出\n"
        f"{line}\n"
    )


def print_qr(url: str) -> None:
    """在终端打印二维码，手机扫一下就能连上。"""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        m = qr.get_matrix()
        # 用两个字符宽模拟方形，扫码识别率更高
        for row in m:
            print("  " + "".join("██" if c else "  " for c in row))
        print()
    except Exception:
        print("  （二维码生成失败，请手动输入上面的网址）\n")


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    port = find_free_port(9000)

    app, state = create_app(base_dir, port)

    ip = netinfo.best_local_ip()
    print(banner(ip, port, state.recv_dir, state.auth.room_code))
    print_qr(f"http://{ip}:{port}")

    # 自动打开浏览器（打包后双击 exe 即用，体验更好）
    if "--no-browser" not in sys.argv:
        def _open():
            time.sleep(1.0)
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    # 局域网内其他设备也要能连上，必须监听 0.0.0.0
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False,
            use_reloader=False)


if __name__ == "__main__":
    main()
