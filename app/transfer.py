# -*- coding: utf-8 -*-
"""
transfer.py —— 传输引擎

职责：
  1. 速度计量（1 秒滑动窗口，避免跳数）
  2. 分块流式传输（8MB 块，内存占用恒定，支持超大文件）
  3. 断点续传（记录偏移量，中断后从断点继续）
  4. SHA256 完整性校验
  5. 接收端落盘（含磁盘写满捕获）

设计原则：与 Web 框架解耦，可单测。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Iterator

CHUNK_SIZE = 8 * 1024 * 1024          # 8MB 块：在千兆网下平衡吞吐与内存
SPEED_WINDOW = 1.0                    # 速度滑动窗口（秒）


# ============================================================ 速度计量

class SpeedMeter:
    """
    滑动窗口速度计。

    为什么不用「总量 / 总耗时」？因为那个值在传输全程几乎不动，
    用户看到的进度条速度是"累计平均值"，无法反映实时快慢。
    滑动窗口给的是"最近 1 秒真实速度"，体验好得多。
    """

    def __init__(self, window: float = SPEED_WINDOW):
        self.window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self._total = 0
        self._start = time.monotonic()
        self._peak = 0.0

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._total = 0
            self._start = time.monotonic()
            self._peak = 0.0

    def add(self, nbytes: int) -> None:
        """记一笔已传输字节数。"""
        now = time.monotonic()
        with self._lock:
            self._total += nbytes
            self._samples.append((now, nbytes))
            # 丢弃窗口外的样本
            cutoff = now - self.window
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    @property
    def instant(self) -> float:
        """瞬时速度（字节/秒），最近 1 秒窗口。"""
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window
            win = [(t, n) for t, n in self._samples if t >= cutoff]
            if len(win) < 2:
                # 样本太少时回退到累计均值，避免显示 0
                elapsed = max(now - self._start, 0.001)
                return self._total / elapsed if self._total else 0.0
            span = win[-1][0] - win[0][0]
            if span <= 0:
                return 0.0
            total = sum(n for _, n in win)
            speed = total / span
            self._peak = max(self._peak, speed)
            return speed

    @property
    def average(self) -> float:
        with self._lock:
            elapsed = max(time.monotonic() - self._start, 0.001)
            return self._total / elapsed if self._total else 0.0

    @property
    def peak(self) -> float:
        self.instant  # 刷新峰值
        return self._peak

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


# ============================================================ 断点续传

class ResumeStore:
    """
    断点记录：把「已成功写入的字节数、临时文件路径」持久化到磁盘。

    为什么需要：传 10GB 文件时网络抖动、电脑休眠很常见。
    没有断点记录就得从头再来，用户会直接放弃这个工具。

    存续传状态用 .part 文件 + .resume 元数据文件。
    """

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _meta_path(self, task_key: str) -> str:
        safe = hashlib.md5(task_key.encode("utf-8")).hexdigest()
        return os.path.join(self.state_dir, f"{safe}.resume")

    def part_path(self, task_key: str) -> str:
        safe = hashlib.md5(task_key.encode("utf-8")).hexdigest()
        return os.path.join(self.state_dir, f"{safe}.part")

    def get_offset(self, task_key: str) -> int:
        """读取上次中断时的偏移量与临时文件实际大小（取较小值保证一致）。"""
        meta = self._meta_path(task_key)
        if not os.path.exists(meta):
            return 0
        try:
            with open(meta, "r", encoding="utf-8") as f:
                recorded = int(f.read().strip() or 0)
        except Exception:
            return 0
        part = self.part_path(task_key)
        if not os.path.exists(part):
            return 0
        actual = os.path.getsize(part)
        # 若记录值大于实际值（说明上次写元数据时崩溃），以实际值为准
        return min(recorded, actual)

    def set_offset(self, task_key: str, offset: int) -> None:
        with self._lock:
            try:
                with open(self._meta_path(task_key), "w", encoding="utf-8") as f:
                    f.write(str(offset))
            except Exception:
                pass

    def clear(self, task_key: str) -> None:
        for p in (self._meta_path(task_key), self.part_path(task_key)):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ============================================================ 哈希校验

class HashCounter:
    """流式计算 SHA256，边传边算，不额外读盘。"""

    def __init__(self):
        self._h = hashlib.sha256()
        self._size = 0

    def update(self, data: bytes) -> None:
        self._h.update(data)
        self._size += len(data)

    def hexdigest(self) -> str:
        return self._h.hexdigest()

    @property
    def size(self) -> int:
        return self._size


def hash_file(path: str, progress: Callable[[int, int], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> str:
    """计算文件 SHA256（用于发送前校验告知接收端）。"""
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel and cancel():
                raise InterruptedError("用户取消")
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            h.update(block)
            done += len(block)
            if progress:
                progress(done, total)
    return h.hexdigest()


# ============================================================ 发送端

def iter_file_chunks(path: str, start: int = 0, chunk_size: int = CHUNK_SIZE,
                     on_chunk: Callable[[int], None] | None = None,
                     cancel: Callable[[], bool] | None = None) -> Iterator[bytes]:
    """
    流式读取文件，从 start 偏移开始。
    内存占用 = chunk_size，传 100GB 文件也不会爆内存。
    """
    with open(path, "rb") as f:
        if start:
            f.seek(start)
        while True:
            if cancel and cancel():
                raise InterruptedError("用户取消传输")
            block = f.read(chunk_size)
            if not block:
                break
            if on_chunk:
                on_chunk(len(block))
            yield block


# ============================================================ 接收端

class ReceiveSession:
    """
    单个文件的接收会话。

    用法：
        s = ReceiveSession(path, total_size, expected_sha256)
        s.write(chunk)          # 写数据，内部自动处理磁盘写满
        s.finish()              # 校验 + 原子重命名
    """

    def __init__(self, final_path: str, total_size: int,
                 expected_sha256: str = "", start_offset: int = 0):
        self.final_path = final_path
        self.total_size = total_size
        self.expected_sha256 = expected_sha256
        self.part_path = final_path + ".part"
        self.offset = start_offset
        self.hash = HashCounter()
        self.meter = SpeedMeter()
        self._fp = None
        self.error = ""

        os.makedirs(os.path.dirname(self.final_path) or ".", exist_ok=True)

        if start_offset and os.path.exists(self.part_path):
            # 续传：保留已有部分，继续追加
            self._fp = open(self.part_path, "ab")
            # 已写入部分也需要参与哈希 —— 重读一遍
            if expected_sha256:
                with open(self.part_path, "rb") as f:
                    left = start_offset
                    while left > 0:
                        blk = f.read(min(CHUNK_SIZE, left))
                        if not blk:
                            break
                        self.hash.update(blk)
                        left -= len(blk)
        else:
            self.offset = 0
            self._fp = open(self.part_path, "wb")

    def write(self, data: bytes) -> None:
        try:
            self._fp.write(data)
            self.offset += len(data)
            self.hash.update(data)
            self.meter.add(len(data))
        except OSError as exc:
            # 磁盘写满 / 权限问题：明确抛出，让上层给用户可读提示
            err_no = getattr(exc, "errno", None)
            if err_no == 28 or "space" in str(exc).lower():
                self.error = "磁盘空间已满，传输中止"
                raise DiskFullError(self.error) from exc
            self.error = f"写入失败：{exc}"
            raise

    def finish(self) -> dict[str, Any]:
        """收尾：关闭文件 → 校验 → 原子重命名。"""
        if self._fp:
            self._fp.flush()
            os.fsync(self._fp.fileno())   # 确保数据真正落盘，不只是进系统缓存
            self._fp.close()
            self._fp = None

        actual_sha = self.hash.hexdigest() if self.expected_sha256 else ""

        # 完整性校验
        if self.expected_sha256 and actual_sha != self.expected_sha256:
            return {
                "ok": False,
                "error": "文件校验失败，数据可能损坏",
                "expected": self.expected_sha256,
                "actual": actual_sha,
            }

        # 只有校验通过才把 .part 改成正式文件，避免半成品污染目录
        target = self.final_path
        if os.path.exists(target):
            from storage import unique_path
            target = unique_path(target)
        os.replace(self.part_path, target)

        return {
            "ok": True, "path": target,
            "sha256": actual_sha, "size": self.offset,
        }

    def abort(self) -> None:
        """用户取消：保留 .part 以便后续续传，只关文件句柄。"""
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None


# ============================================================ 文件夹打包

def walk_folder(root: str, cancel: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """
    递归遍历文件夹，返回文件清单（相对路径 + 大小）。
    跳过符号链接，避免无限递归。
    """
    root = os.path.abspath(root)
    items: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if cancel and cancel():
            break
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, os.path.dirname(root))
            items.append({
                "name": name,
                "path": full,
                "relative": rel.replace("\\", "/"),
                "size": size,
            })
    return items


def sum_sizes(paths: list[str]) -> dict[str, Any]:
    """
    统计待传文件总大小 —— 容量预检的输入。
    文件夹会递归展开。
    """
    total = 0
    file_count = 0
    folder_count = 0
    errors: list[str] = []

    for p in paths:
        if os.path.isfile(p):
            try:
                total += os.path.getsize(p)
                file_count += 1
            except OSError as exc:
                errors.append(f"{p}: {exc}")
        elif os.path.isdir(p):
            folder_count += 1
            for it in walk_folder(p):
                total += it["size"]
                file_count += 1

    return {
        "total_bytes": total,
        "file_count": file_count,
        "folder_count": folder_count,
        "errors": errors,
    }


if __name__ == "__main__":
    import tempfile
    from storage import fmt_size, fmt_speed

    print("=== 速度计测试 ===")
    m = SpeedMeter()
    for _ in range(10):
        m.add(4 * 1024 * 1024)
        time.sleep(0.1)
    print(f"  瞬时 {fmt_speed(m.instant)}  平均 {fmt_speed(m.average)}  总量 {fmt_size(m.total)}")

    print("\n=== 分块读写 + 校验测试 ===")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "源文件.bin")
        payload = os.urandom(9 * 1024 * 1024 + 12345)  # 跨越 1 个块边界
        with open(src, "wb") as f:
            f.write(payload)

        expect = hash_file(src)
        print(f"  源文件 {fmt_size(len(payload))} SHA256={expect[:16]}...")

        dst = os.path.join(td, "out", "接收文件.bin")
        sess = ReceiveSession(dst, len(payload), expect)
        for blk in iter_file_chunks(src):
            sess.write(blk)
        res = sess.finish()
        print(f"  接收完成 ok={res['ok']} 大小={fmt_size(res.get('size', 0))}")
        print(f"  目标文件存在={os.path.exists(dst)}")
        with open(dst, "rb") as f:
            print(f"  内容一致={f.read() == payload}")

    print("\n=== 断点续传测试 ===")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "big.bin")
        payload = os.urandom(20 * 1024 * 1024)
        with open(src, "wb") as f:
            f.write(payload)
        expect = hash_file(src)

        dst = os.path.join(td, "recv.bin")
        # 第一段：只传一半就中断
        sess = ReceiveSession(dst, len(payload), expect)
        sent = 0
        for blk in iter_file_chunks(src):
            sess.write(blk)
            sent += len(blk)
            if sent >= 10 * 1024 * 1024:
                break
        sess.abort()
        part_size = os.path.getsize(dst + ".part")
        print(f"  中断时已写入 {fmt_size(part_size)}")

        # 第二段：从断点续传
        sess2 = ReceiveSession(dst, len(payload), expect, start_offset=part_size)
        for blk in iter_file_chunks(src, start=part_size):
            sess2.write(blk)
        res = sess2.finish()
        print(f"  续传完成 ok={res['ok']} SHA 校验={'通过' if res['ok'] else '失败'}")
        with open(dst, "rb") as f:
            print(f"  内容一致={f.read() == payload}")

    print("\n=== 文件夹统计测试 ===")
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "sub", "deep"))
        for name, size in [("a.txt", 100), ("sub/b.txt", 2000), ("sub/deep/c.bin", 3_000_000)]:
            p = os.path.join(td, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x" * size)
        info = sum_sizes([td])
        print(f"  {info['file_count']} 个文件, {info['folder_count']} 个文件夹, "
              f"总计 {fmt_size(info['total_bytes'])}")
