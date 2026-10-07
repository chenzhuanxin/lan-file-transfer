# -*- coding: utf-8 -*-
"""
tasks.py —— 传输任务管理（双向：上传 / 下载）

把「一个正在传输的活儿」抽象成 Task 对象，统一供前端轮询与 WebSocket 推送。

为什么用任务队列而不是直接传：
  1. 一次拖 50 个文件进来，需要排队逐个传，不能并发打爆带宽
  2. 前端要能单独取消/暂停某一个任务
  3. 速度、进度、剩余时间需要统一计算与推送
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

from storage import check_capacity, fmt_size, safe_join, unique_path, DiskFullError
from transfer import (
    CHUNK_SIZE, HashCounter, ReceiveSession, SpeedMeter,
    hash_file, iter_file_chunks, sum_sizes, walk_folder,
)


# 任务状态机
PENDING = "pending"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"


@dataclass
class Task:
    """一个传输任务（单文件粒度）。"""
    task_id: str
    direction: str                      # "upload"（对方传给我） | "download"（我传给对方）
    rel_path: str                       # 相对路径，保留文件夹结构
    size: int = 0
    transferred: int = 0
    status: str = PENDING
    error: str = ""
    sha256: str = ""
    final_path: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    source_path: str = ""               # 下载时：本机源文件路径
    peer: str = ""                      # 对端标识（IP）

    meter: SpeedMeter = field(default_factory=SpeedMeter, repr=False)
    sha256_expect: str = ""             # 上传时：期望校验值

    @property
    def speed(self) -> float:
        if self.status == RUNNING:
            return self.meter.instant
        if self.status == DONE and self.finished_at > self.started_at > 0:
            return self.size / max(self.finished_at - self.started_at, 0.001)
        return 0.0

    @property
    def eta(self) -> float | None:
        """剩余秒数。"""
        if self.status != RUNNING or self.size <= 0:
            return None
        remain = self.size - self.transferred
        sp = self.speed
        if sp <= 1:
            return None
        return remain / sp

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(100.0, self.transferred / self.size * 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "direction": self.direction,
            "rel_path": self.rel_path,
            "name": os.path.basename(self.rel_path.replace("\\", "/")),
            "size": self.size,
            "size_text": fmt_size(self.size),
            "transferred": self.transferred,
            "transferred_text": fmt_size(self.transferred),
            "speed": self.speed,
            "status": self.status,
            "error": self.error,
            "progress": round(self.progress, 2),
            "eta": self.eta,
            "peer": self.peer,
            "final_path": self.final_path,
            "sha256": self.sha256,
        }


class TaskManager:
    """
    任务管理器：管理所有上传/下载任务的生命周期。

    线程安全：所有状态修改在 _lock 内完成。
    """

    def __init__(self, recv_dir_provider: Callable[[], str],
                 state_dir: str, on_update: Callable[[], None] | None = None):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._recv_dir = recv_dir_provider
        self._state_dir = state_dir
        self._on_update = on_update or (lambda: None)
        self._cancels: dict[str, threading.Event] = {}
        os.makedirs(state_dir, exist_ok=True)

    # ---------------------------------------------------------- 查询

    def list_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            ids = self._order[-limit:]
            return [self._tasks[i].to_dict() for i in ids if i in self._tasks]

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def summary(self) -> dict[str, Any]:
        """给前端顶部状态条用的汇总数据。"""
        with self._lock:
            tasks = list(self._tasks.values())
        active = [t for t in tasks if t.status == RUNNING]
        done = [t for t in tasks if t.status == DONE]
        failed = [t for t in tasks if t.status == FAILED]
        up_speed = sum(t.speed for t in active if t.direction == "upload")
        down_speed = sum(t.speed for t in active if t.direction == "download")
        return {
            "active": len(active),
            "pending": len([t for t in tasks if t.status == PENDING]),
            "done": len(done),
            "failed": len(failed),
            "upload_speed": up_speed,
            "download_speed": down_speed,
            "total_tasks": len(tasks),
        }

    # ---------------------------------------------------------- 取消

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if not task:
            return {"ok": False, "error": "任务不存在"}
        ev = self._cancels.get(task_id)
        if ev:
            ev.set()
        with self._lock:
            if task.status in (PENDING, RUNNING, PAUSED):
                task.status = CANCELED
                task.error = "用户取消"
        self._on_update()
        return {"ok": True}

    def clear_finished(self) -> dict[str, Any]:
        with self._lock:
            keep = []
            removed = 0
            for tid in self._order:
                t = self._tasks.get(tid)
                if t and t.status in (DONE, FAILED, CANCELED):
                    del self._tasks[tid]
                    self._cancels.pop(tid, None)
                    removed += 1
                else:
                    keep.append(tid)
            self._order = keep
        self._on_update()
        return {"ok": True, "removed": removed}

    # ---------------------------------------------------------- 预检

    def precheck(self, items: list[dict[str, Any]], save_dir: str) -> dict[str, Any]:
        """
        发送前容量预检 —— 在真正传之前就告诉用户够不够。

        调用方传入 items: [{rel_path, size}]（下载方向的源文件清单）。
        """
        need = sum(int(it.get("size", 0)) for it in items)
        cap = check_capacity(save_dir, need)
        return {
            "ok": cap["ok"],
            "need": need,
            "need_text": fmt_size(need),
            "free": cap["free"],
            "free_text": fmt_size(cap["free"]),
            "shortfall": cap.get("shortfall", 0),
            "shortfall_text": fmt_size(cap.get("shortfall", 0)),
            "message": cap["message"],
            "file_count": len(items),
        }

    # ---------------------------------------------------------- 上传（对方 → 我）

    def create_upload_task(self, rel_path: str, size: int, sha256: str = "",
                           peer: str = "") -> Task:
        task = Task(
            task_id=uuid.uuid4().hex[:12],
            direction="upload",
            rel_path=rel_path.replace("\\", "/"),
            size=int(size),
            sha256_expect=sha256,
            peer=peer,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
            self._cancels[task.task_id] = threading.Event()
        self._on_update()
        return task

    def receive_stream(self, task: Task, chunks) -> dict[str, Any]:
        """
        接收一个文件的数据流并落盘。
        chunks: 可迭代的 bytes 序列（Flask request.stream 已按块产出）。
        """
        cancel_ev = self._cancels.get(task.task_id) or threading.Event()
        recv_dir = self._recv_dir()
        final_path = safe_join(recv_dir, task.rel_path)

        # 预检的目标目录可能还不存在（多层子目录只在写入时才创建），
        # 先向上找到最近的已存在祖先目录再算剩余空间 —— 同一分区结果等价。
        probe_dir = os.path.dirname(final_path)
        while probe_dir and not os.path.isdir(probe_dir):
            parent = os.path.dirname(probe_dir)
            if parent == probe_dir:
                break
            probe_dir = parent
        if not probe_dir or not os.path.isdir(probe_dir):
            probe_dir = recv_dir

        need = task.size - task.transferred
        cap = check_capacity(probe_dir, need)
        if not cap["ok"]:
            with self._lock:
                task.status = FAILED
                task.error = cap["message"]
            self._on_update()
            return {"ok": False, "error": cap["message"]}

        # 目录此时再创建（放在预检之后，避免预检失败却留下空目录）
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        # 断点续传：查找同任务的 .part
        start = 0
        part = final_path + ".part"
        if os.path.exists(part):
            start = min(os.path.getsize(part), task.size)
        # 客户端若声明了偏移量，以客户端为准
        if task.transferred and task.transferred <= task.size:
            start = task.transferred

        sess = ReceiveSession(final_path, task.size, task.sha256_expect, start_offset=start)
        sess.meter = task.meter
        task.started_at = task.started_at or time.monotonic()
        with self._lock:
            task.status = RUNNING
            task.transferred = start
        self._on_update()

        last_push = 0.0
        try:
            for block in chunks:
                if cancel_ev.is_set():
                    sess.abort()
                    with self._lock:
                        task.status = CANCELED
                        task.error = "用户取消"
                    self._on_update()
                    return {"ok": False, "error": "已取消"}
                sess.write(block)
                with self._lock:
                    task.transferred = sess.offset
                now = time.monotonic()
                if now - last_push > 0.3:      # 限流推送，别把前端刷爆
                    last_push = now
                    self._on_update()
        except DiskFullError as exc:
            sess.abort()
            with self._lock:
                task.status = FAILED
                task.error = str(exc)
            self._on_update()
            return {"ok": False, "error": str(exc)}
        except InterruptedError:
            sess.abort()
            with self._lock:
                task.status = CANCELED
            self._on_update()
            return {"ok": False, "error": "已取消"}
        except Exception as exc:
            sess.abort()
            with self._lock:
                task.status = FAILED
                task.error = f"接收失败：{exc}"
            self._on_update()
            return {"ok": False, "error": str(exc)}

        res = sess.finish()
        with self._lock:
            if res["ok"]:
                task.status = DONE
                task.transferred = task.size
                task.final_path = res["path"]
                task.sha256 = res.get("sha256", "")
            else:
                task.status = FAILED
                task.error = res.get("error", "校验失败")
            task.finished_at = time.monotonic()
        self._on_update()
        return res

    # ---------------------------------------------------------- 下载（我 → 对方）

    def create_download_task(self, source_path: str, rel_path: str = "",
                             peer: str = "") -> Task:
        try:
            size = os.path.getsize(source_path)
        except OSError:
            size = 0
        task = Task(
            task_id=uuid.uuid4().hex[:12],
            direction="download",
            rel_path=(rel_path or os.path.basename(source_path)).replace("\\", "/"),
            size=size,
            source_path=source_path,
            peer=peer,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
            self._cancels[task.task_id] = threading.Event()
        self._on_update()
        return task

    def iter_download(self, task: Task):
        """
        产出给 HTTP 响应的字节流（生成器）。
        下载方向不做落盘，所以无需容量预检 —— 空间是消耗在对方那台。
        """
        cancel_ev = self._cancels.get(task.task_id) or threading.Event()
        task.started_at = time.monotonic()
        with self._lock:
            task.status = RUNNING
        self._on_update()

        last_push = 0.0
        try:
            for block in iter_file_chunks(
                task.source_path,
                on_chunk=lambda n: task.meter.add(n),
                cancel=cancel_ev.is_set,
            ):
                with self._lock:
                    task.transferred = task.meter.total
                now = time.monotonic()
                if now - last_push > 0.3:
                    last_push = now
                    self._on_update()
                yield block

            with self._lock:
                task.status = DONE
                task.transferred = task.size
                task.finished_at = time.monotonic()
            self._on_update()
        except InterruptedError:
            with self._lock:
                task.status = CANCELED
                task.error = "用户取消"
            self._on_update()
        except Exception as exc:
            with self._lock:
                task.status = FAILED
                task.error = f"发送失败：{exc}"
            self._on_update()


# ---------------------------------------------------------------- 文件清单

def build_send_list(paths: list[str]) -> dict[str, Any]:
    """
    把用户选中的文件/文件夹展开成待传清单（含相对路径，保留目录结构）。

    这是「选择文件夹」需求的核心：一个文件夹不能当一个文件传，
    必须递归展开成多个文件，每个文件带相对路径，接收端才能重建目录树。
    """
    items: list[dict[str, Any]] = []
    total = 0
    errors: list[str] = []

    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            try:
                size = os.path.getsize(p)
            except OSError as exc:
                errors.append(f"{p}: {exc}")
                continue
            items.append({"rel_path": os.path.basename(p), "size": size, "path": p})
            total += size
        elif os.path.isdir(p):
            folder_name = os.path.basename(p.rstrip("\\/")) or "folder"
            for it in walk_folder(p):
                rel = f"{folder_name}/{it['relative'].split('/', 1)[-1]}" \
                    if "/" in it["relative"] else f"{folder_name}/{it['relative']}"
                items.append({
                    "rel_path": rel.replace("\\", "/"),
                    "size": it["size"],
                    "path": it["path"],
                })
                total += it["size"]
        else:
            errors.append(f"{p}: 路径不存在")

    return {"ok": True, "items": items, "total": total, "errors": errors,
            "total_text": fmt_size(total)}


if __name__ == "__main__":
    import tempfile
    import json

    print("=== 文件清单展开测试（保留目录结构）===")
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "我的项目", "src", "utils"))
        files = {
            "我的项目/readme.md": 500,
            "我的项目/src/main.py": 3000,
            "我的项目/src/utils/helper.py": 1200,
        }
        for rel, size in files.items():
            fp = os.path.join(td, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(b"a" * size)

        lst = build_send_list([os.path.join(td, "我的项目")])
        for it in lst["items"]:
            print(f"  {it['rel_path']:42s} {fmt_size(it['size'])}")
        print(f"  合计 {lst['total_text']}，{len(lst['items'])} 个文件")

    print("\n=== 任务管理测试 ===")
    with tempfile.TemporaryDirectory() as td:
        recv = os.path.join(td, "recv")
        os.makedirs(recv)
        tm = TaskManager(lambda: recv, os.path.join(td, "state"))

        # 预检：够
        print("  预检(10MB):", tm.precheck([{"size": 10 * 1024**2}], recv)["message"])
        # 预检：不够
        print("  预检(10TB):", tm.precheck([{"size": 10 * 1024**4}], recv)["message"])

        # 模拟上传
        t = tm.create_upload_task("test/文件.bin", 5 * 1024 * 1024)
        data = os.urandom(5 * 1024 * 1024)

        def gen():
            for i in range(0, len(data), CHUNK_SIZE):
                yield data[i:i + CHUNK_SIZE]

        res = tm.receive_stream(t, gen())
        print(f"  上传结果 ok={res['ok']} 路径={os.path.basename(res.get('path', ''))}")
        print(f"  任务状态 {tm.get(t.task_id).status} 进度 {tm.get(t.task_id).progress}%")
        print(f"  汇总 {json.dumps(tm.summary(), ensure_ascii=False)}")
