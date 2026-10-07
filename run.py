# -*- coding: utf-8 -*-
"""
run.py —— 程序入口（打包 exe 用）

单独抽出入口的两个原因：
  1. PyInstaller 需要一个顶层脚本，且要能正确处理「打包后资源路径」
  2. 双击 exe 时用户可能没有控制台权限，需要兜底处理
"""
from __future__ import annotations

import os
import sys
import traceback


def resource_path(relative: str) -> str:
    """
    获取资源真实路径。

    打包后 PyInstaller 会把资源解到 sys._MEIPASS，
    开发时就是脚本所在目录 —— 两种模式都能找到 static/。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


def main() -> int:
    # 把 app 目录加进模块搜索路径，保持「server.py 直接 import netinfo」的写法可用
    app_dir = resource_path("app")
    if not os.path.isdir(app_dir):
        app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    # 打包成 exe 后，_MEIPASS 下没有 app/static，静态文件直接在 MEIPASS/static
    static_probe = resource_path("static")
    base_dir = os.path.dirname(static_probe) if os.path.isdir(static_probe) else app_dir

    try:
        from server import create_app, find_free_port, banner, print_qr, VERSION
        import netinfo
    except ImportError as exc:
        print("依赖缺失：" + str(exc))
        print("请先执行：pip install flask qrcode pillow psutil")
        input("按回车退出…")
        return 1

    port = find_free_port(9000)
    app, state = create_app(base_dir, port)

    ip = netinfo.best_local_ip()
    print(banner(ip, port, state.recv_dir, state.auth.room_code))
    print_qr(f"http://{ip}:{port}")

    if "--no-browser" not in sys.argv:
        import threading
        import time
        import webbrowser

        # 本机自动登录：把房主令牌带在 URL 上，双击 exe 后无需输码
        owner_token = state.owner_session.token
        local_url = f"http://127.0.0.1:{port}/#token={owner_token}"

        def _open():
            time.sleep(1.0)
            try:
                webbrowser.open(local_url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()

    try:
        app.run(host="0.0.0.0", port=port, threaded=True,
                debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception:
        traceback.print_exc()
        input("发生错误，按回车退出…")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
