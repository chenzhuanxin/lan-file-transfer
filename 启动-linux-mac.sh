#!/usr/bin/env bash
#
# 局域网文件传输 —— Linux / macOS 启动脚本
#
# 用途：让 Linux 或 macOS（含黑苹果）也能作为「发送方/房主」运行本工具。
#      Windows 用户直接用 局域网文件传输.exe，不需要这个脚本。
#
# 用法：
#   chmod +x 启动-linux-mac.sh
#   ./启动-linux-mac.sh
#
# 依赖：python3（3.9+）。缺依赖时会自动尝试安装。

set -e

cd "$(dirname "$0")"

# ---------- 找 Python ----------
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        # 确认版本 >= 3.9
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "✗ 没找到 Python 3.9 或更高版本。"
    echo "  Ubuntu/Debian:  sudo apt install python3 python3-pip"
    echo "  Fedora/RHEL:    sudo dnf install python3 python3-pip"
    echo "  Arch:           sudo pacman -S python python-pip"
    echo "  macOS:          brew install python3"
    exit 1
fi

echo "使用的 Python: $($PY --version 2>&1)"

# ---------- 检查依赖 ----------
if ! "$PY" -c 'import flask' >/dev/null 2>&1; then
    echo ""
    echo "缺少依赖 flask，正在尝试安装……"
    if ! "$PY" -m pip install --user flask qrcode pillow psutil 2>/dev/null; then
        # 有些发行版禁止 --user 或需要 --break-system-packages（PEP 668）
        echo "  常规安装失败，尝试虚拟环境……"
        if [ ! -d ".venv" ]; then
            "$PY" -m venv .venv
        fi
        ./.venv/bin/pip install -q flask qrcode pillow psutil
        PY="./.venv/bin/python"
        echo "  已装到本地虚拟环境 .venv"
    fi
fi

# ---------- 启动 ----------
echo ""
echo "启动中…… 浏览器会自动打开。按 Ctrl+C 退出。"
echo ""
exec "$PY" run.py "$@"
