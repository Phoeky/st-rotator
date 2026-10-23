# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（由 ``packaging/build.py`` 调用，不要直接跑）。

为什么用 spec 而不是一堆命令行参数：``--add-data`` / ``version`` 这类选项在命令行上
一长串、还容易漏；spec 里写清楚，后人改起来看得懂。

**单 exe**：``st-rotator.exe``，windowed（``console=False``，双击不弹黑窗）。
CLI 子命令从终端启动时由入口 ``entry.py`` ``AttachConsole(-1)`` 挂回父控制台，
所以「一个 exe 同时是托盘和命令行工具」不再需要两个构建产物。
"""

from pathlib import Path

#: 仓库根目录（spec 在 packaging/ 下，上一级就是根）
ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入

#: 入口脚本 + 输出名
ENTRY = ROOT / "packaging" / "entry.py"
NAME = "st-rotator"

#: 只读资源：配置模板（文档示例用；首启生成的零 Key 配置以它为底）
DATAS = [(str(ROOT / "config.example.json"), ".")]

#: 明显用不到的大块头，剔掉能省几 MB。保守起见只排「肯定没被 import」的：
#: tkinter（本项目控制台是 Web 页面，不是 tk）、测试与文档工具、pip/setuptools。
EXCLUDES = [
    "tkinter", "_tkinter", "turtle", "turtledemo",
    "unittest", "doctest", "pydoc", "pdb", "test", "lib2to3",
    "setuptools", "pip", "pkg_resources", "wheel",
    "sqlite3", "xml", "xmlrpc", "distutils",
]

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 会拖慢启动、还可能被杀软误报，本项目体积本来就不大，关掉
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # windowed：双击不弹黑窗；CLI 输出靠 entry.py 挂父控制台解决
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "st-rotator.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
