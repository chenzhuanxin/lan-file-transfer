# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 局域网文件传输 v1.1.0

要点：
  1. static/ 必须一起打包，否则界面 404
  2. 隐藏导入：Flask 的 jinja2、qrcode 的图像后端靠动态导入，静态分析找不到
  3. 排除用不到的重型库（tkinter 除外 —— 原生文件选择框要用），能省 50MB+
"""
import os

ROOT = os.path.abspath(os.getcwd())

a = Analysis(
    ['run.py'],
    pathex=[os.path.join(ROOT, 'app')],
    binaries=[],
    datas=[
        ('app/static/index.html', 'static'),
        ('app/static/style.css', 'static'),
        ('app/static/app.js', 'static'),
    ],
    hiddenimports=[
        'flask', 'jinja2', 'jinja2.ext', 'werkzeug',
        'qrcode', 'qrcode.image.pil', 'qrcode.image.base',
        'PIL', 'PIL.Image',
        'psutil',
        'tkinter', 'tkinter.filedialog',
        'secrets', 'hmac', 'hashlib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'pandas', 'scipy', 'IPython', 'jupyter',
        'pytest', 'sqlalchemy', 'django', 'cv2', 'torch', 'tensorflow',
        'PyQt5', 'PySide2', 'PySide6', 'wx', 'notebook', 'docutils',
        'setuptools', 'pip', 'wheel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='局域网文件传输',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 不开 UPX —— 杀软对 UPX 压缩的 exe 误报率极高
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,              # 保留控制台：显示房间码、连接地址与二维码
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
