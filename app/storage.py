# -*- coding: utf-8 -*-
"""
storage.py —— 磁盘空间与路径处理

职责：
  1. 列出所有磁盘分区及剩余空间
  2. 传输前容量预检（核心需求：容量超标提示）
  3. 传输中的写满捕获（预检通过但中途被别的程序占满）
  4. 路径安全清洗（防止目录穿越攻击）
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import string
from typing import Any


def _drive_label(path: str) -> str:
    """给分区起个友好名字。跨平台。"""
    path = os.path.abspath(path)
    if sys.platform == "win32":
        drive = os.path.splitdrive(path)[0]
        if drive:
            return f"{drive}\\"
        return path
    # macOS：外接卷挂在 /Volumes/<名字>
    if sys.platform == "darwin" and path.startswith("/Volumes/"):
        parts = path.split("/")
        if len(parts) > 2:
            return parts[2]
    # Linux：根分区显示为「/」，其它挂载点显示其名字
    if path == "/":
        return "/（根分区）"
    return path


def list_drives() -> list[dict[str, Any]]:
    """
    列出所有可写磁盘分区。
    返回 [{label, path, total, used, free, free_pct, is_default}]

    跨平台：
      Windows —— GetLogicalDrives 位掩码
      macOS   —— / 加 /Volumes/* 下的每个卷
      Linux   —— / 加 /media、/mnt、/run/media 下的挂载点
    """
    drives: list[str] = []

    if sys.platform == "win32":
        # 用 GetLogicalDrives 位掩码，比遍历 A-Z 快且准确
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if bitmask & (1 << i):
                    drives.append(f"{letter}:\\")
        except Exception:
            drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    else:
        # 类 Unix：根分区永远有
        drives = ["/"]
        if sys.platform == "darwin":
            # macOS：外接硬盘、U盘、dmg 都挂在 /Volumes
            vol = "/Volumes"
            if os.path.isdir(vol):
                for name in sorted(os.listdir(vol)):
                    p = os.path.join(vol, name)
                    # 跳过系统卷（Macintosh HD 实际就是 /），避免重复
                    if os.path.ismount(p) or os.path.isdir(p):
                        drives.append(p)
        else:
            # Linux：常见可移动介质挂载点
            for base in ("/media", "/mnt", "/run/media"):
                if not os.path.isdir(base):
                    continue
                for name in sorted(os.listdir(base)):
                    p = os.path.join(base, name)
                    if os.path.isdir(p):
                        drives.append(p)

    result: list[dict[str, Any]] = []
    default_dir = default_download_dir()

    for d in drives:
        # 只保留「已就绪、可写」的分区（跳过光驱、未插卡的读卡器）
        if not os.path.exists(d):
            continue
        try:
            usage = shutil.disk_usage(d)
        except (OSError, PermissionError):
            continue
        if usage.total == 0:
            continue
        # 可写测试：在分区根建临时文件（失败则跳过，如 C:\ 需要管理员）
        result.append({
            "label": _drive_label(d),
            "path": d,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "free_pct": round(usage.free / usage.total * 100, 1),
            "is_default": os.path.abspath(default_dir).lower().startswith(
                os.path.abspath(d).lower()
            ),
        })

    result.sort(key=lambda x: (not x["is_default"], x["path"]))
    return result


def default_download_dir() -> str:
    """
    默认接收目录：用户「下载」文件夹下的 LANFileTransfer，
    避免直接散落到桌面/下载根目录。
    """
    if sys.platform == "win32":
        base = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
    target = os.path.join(base, "LANFileTransfer")
    try:
        os.makedirs(target, exist_ok=True)
    except Exception:
        target = base
    return target


def check_capacity(target_dir: str, need_bytes: int, safety_margin: float = 0.02) -> dict[str, Any]:
    """
    容量预检 —— 本工具的核心保护之一。

    safety_margin: 额外预留比例（默认 2%），避免刚好写满导致系统异常。

    返回 {ok, free, need, shortfall, message}
    """
    try:
        target_dir = os.path.abspath(target_dir)
        if not os.path.isdir(target_dir):
            return {
                "ok": False, "free": 0, "need": need_bytes, "shortfall": need_bytes,
                "message": f"目录不存在：{target_dir}",
            }
        usage = shutil.disk_usage(target_dir)
    except Exception as exc:
        return {
            "ok": False, "free": 0, "need": need_bytes, "shortfall": need_bytes,
            "message": f"无法读取磁盘信息：{exc}",
        }

    required = int(need_bytes * (1 + safety_margin))
    free = usage.free
    if free >= required:
        return {
            "ok": True, "free": free, "need": need_bytes, "shortfall": 0,
            "message": f"空间充足：需要 {fmt_size(need_bytes)}，可用 {fmt_size(free)}",
        }

    return {
        "ok": False,
        "free": free,
        "need": need_bytes,
        "shortfall": required - free,
        "message": (
            f"空间不足：需要 {fmt_size(need_bytes)}（含 2% 余量），"
            f"目标盘仅剩 {fmt_size(free)}，还差 {fmt_size(required - free)}"
        ),
    }


class DiskFullError(OSError):
    """传输中途磁盘写满。"""


def safe_join(base_dir: str, relative: str) -> str:
    """
    安全拼接路径，阻止 ../../ 目录穿越。
    多文件传输时文件名来自网络，必须清洗。
    """
    base_abs = os.path.abspath(base_dir)

    # 去掉 Windows 非法字符与路径分隔符
    cleaned = relative.replace("\\", "/")
    parts = []
    for seg in cleaned.split("/"):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        # 清理 Windows 文件名非法字符
        seg = re.sub(r'[<>:"|?*\x00-\x1f]', "_", seg)
        # 去掉 Windows 保留名
        if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", seg, re.I):
            seg = "_" + seg
        parts.append(seg)

    if not parts:
        parts = ["unnamed"]

    result = os.path.abspath(os.path.join(base_abs, *parts))

    # 最终校验：结果必须仍在 base 内
    if not result.lower().startswith(base_abs.lower()):
        raise ValueError(f"非法路径（疑似目录穿越）：{relative}")

    return result


def unique_path(path: str) -> str:
    """若文件已存在，自动加 (1)(2)... 后缀，避免覆盖用户已有文件。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{root} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1
        if i > 9999:
            raise OSError("无法生成唯一文件名")


def fmt_size(n: float) -> str:
    """人类可读的大小。"""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def fmt_speed(bytes_per_sec: float) -> str:
    return fmt_size(bytes_per_sec) + "/s"


if __name__ == "__main__":
    print("=== 磁盘分区 ===")
    for d in list_drives():
        print(f"  {d['label']:8s} 剩余 {fmt_size(d['free']):>10s} "
              f"({d['free_pct']}%) 总 {fmt_size(d['total'])} 默认={d['is_default']}")
    print("\n=== 默认接收目录 ===", default_download_dir())
    print("\n=== 容量预检测试 ===")
    print(check_capacity(default_download_dir(), 5 * 1024**3))
    print(check_capacity(default_download_dir(), 5000 * 1024**3))
    print("\n=== 路径安全测试 ===")
    for p in ["a/b.txt", "../../../etc/passwd", "CON.txt", "a<b>c?.txt", ""]:
        print(f"  {p!r:28s} -> {safe_join(default_download_dir(), p)}")
