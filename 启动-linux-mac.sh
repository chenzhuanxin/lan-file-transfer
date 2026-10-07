#!/usr/bin/env bash
#
# 局域网文件传输 —— Linux / macOS 启动脚本
#
# 用途：让 Linux、macOS（含黑苹果）作为「房主端」运行本工具。
#      Windows 用户直接用 exe，不需要这个脚本。
#
# 用法：
#   chmod +x start-linux-mac.sh
#   ./start-linux-mac.sh                 正常启动（自动打开浏览器）
#   ./start-linux-mac.sh --no-browser    只启动服务，不打开浏览器
#
# 依赖：Python 3.9+。缺失的包会按「系统安装 → 用户安装 → 虚拟环境」三级降级自动补上。
#
# 退出码：
#   0  正常退出（Ctrl+C）
#   1  环境不满足（无 Python / 装不上依赖 / 缺 venv 模块）
#   2  venv 创建失败
#
# 注意：本脚本刻意不使用 `set -e`。
#   因为「尝试装依赖」天然会失败（比如没装 pip），set -e 会让脚本静默退出，
#   用户什么都看不到。改成显式检查每一步的返回值，失败时给出可操作的提示。

cd "$(dirname "$0")" || {
    echo "✗ 无法进入脚本所在目录"
    exit 1
}

PKGS="flask qrcode pillow psutil"
VENV_DIR=".venv"

echo "=========================================================="
echo "  局域网文件传输 —— Linux / macOS 启动器"
echo "=========================================================="
echo ""

# ---------------------------------------------------------- 1. 找 Python（3.9+）
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "✗ 没找到 Python 3.9 或更高版本。"
    echo ""
    echo "  请先安装："
    echo "    Ubuntu / Debian:  sudo apt install python3 python3-pip python3-venv"
    echo "    Fedora / RHEL:    sudo dnf install python3 python3-pip"
    echo "    Arch / Manjaro:   sudo pacman -S python python-pip"
    echo "    openSUSE:         sudo zypper install python3 python3-pip"
    echo "    macOS:            brew install python3"
    echo "                      （或到 https://www.python.org/downloads/ 下载安装包）"
    exit 1
fi

echo "✓ 使用 Python: $("$PY" --version 2>&1)  ($(command -v "$PY"))"

# ---------------------------------------------------------- 2. 检查依赖完整性
# 必须检查全部四个包 —— 只查 flask 的话，环境里「有 flask 但缺 qrcode」时
# 会被判为依赖齐全，然后启动时崩在 import qrcode 上。
missing=""
for mod in flask qrcode PIL psutil; do
    if ! "$PY" -c "import $mod" >/dev/null 2>&1; then
        missing="$missing $mod"
    fi
done

if [ -z "$missing" ]; then
    echo "✓ 依赖齐全"
else
    echo "! 缺少依赖:$missing"
    echo ""

    # ---------- 三级降级安装 ----------
    installed=0

    # 方式一：系统级 / 用户级 pip install
    echo "  [1/3] 尝试 pip install --user ..."
    if "$PY" -m pip install --user $PKGS >/dev/null 2>&1; then
        installed=1
        echo "        ✓ 安装成功（用户级）"
    else
        echo "        失败"
    fi

    # 方式二：--break-system-packages（PEP 668 管理的发行版，如新版 Ubuntu/Debian）
    if [ "$installed" -eq 0 ]; then
        echo "  [2/3] 尝试 pip install --user --break-system-packages ..."
        if "$PY" -m pip install --user --break-system-packages $PKGS >/dev/null 2>&1; then
            installed=1
            echo "        ✓ 安装成功（用户级，已绕过 PEP 668 限制）"
        else
            echo "        失败"
        fi
    fi

    # 方式三：虚拟环境（最干净，但需要 python3-venv 模块）
    if [ "$installed" -eq 0 ]; then
        echo "  [3/3] 尝试创建虚拟环境 $VENV_DIR ..."

        if [ ! -x "$VENV_DIR/bin/python" ]; then
            # 目录不存在、或存在但是上次失败留下的空壳 —— 两种情况都重建
            [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
            if ! "$PY" -m venv "$VENV_DIR" 2>/dev/null; then
                echo "        ✗ 创建虚拟环境失败"
                echo ""
                echo "  这通常是缺少 venv 模块。请安装后重试："
                echo "    Ubuntu / Debian:  sudo apt install python3-venv"
                echo "    Fedora / RHEL:    sudo dnf install python3-virtualenv"
                exit 2
            fi
        fi

        if "$VENV_DIR/bin/python" -m pip install -q $PKGS 2>/dev/null; then
            PY="$PWD/$VENV_DIR/bin/python"
            installed=1
            echo "        ✓ 安装成功（已装进 $VENV_DIR，不影响系统 Python）"
        else
            echo "        ✗ 虚拟环境里装依赖失败"
        fi
    fi

    if [ "$installed" -eq 0 ]; then
        echo ""
        echo "✗ 三种方式都没能装上依赖。"
        echo ""
        echo "  请手动安装后重试："
        echo "    pip install $PKGS"
        echo ""
        echo "  如果提示 externally-managed-environment（PEP 668），改用："
        echo "    pip install --user --break-system-packages $PKGS"
        echo "  或："
        echo "    python3 -m venv .venv && ./.venv/bin/pip install $PKGS"
        exit 1
    fi
fi

# ---------------------------------------------------------- 3. 二次确认依赖可用
# 安装方式可能装到了别的解释器里，这里用最终选定的 PY 再验一次，
# 免得带着「装完了」的假象跑到 run.py 才崩。
for mod in flask qrcode PIL psutil; do
    if ! "$PY" -c "import $mod" >/dev/null 2>&1; then
        echo ""
        echo "✗ 依赖 $mod 仍不可用（可能装到了别的 Python 环境）。"
        echo "  当前解释器: $PY"
        echo "  请手动执行: $(command -v "$PY" 2>/dev/null || echo "$PY") -m pip install $PKGS"
        exit 1
    fi
done

# ---------------------------------------------------------- 4. 启动
echo ""
echo "----------------------------------------------------------"
echo "  启动中…… 按 Ctrl+C 退出"
echo "----------------------------------------------------------"
echo ""

exec "$PY" run.py "$@"
