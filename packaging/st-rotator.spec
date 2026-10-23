# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（由 ``packaging/build.py`` 调用，不要直接跑）。

为什么用 spec 而不是一堆命令行参数：``--add-data`` / ``version`` 这类选项在命令行上
一长串、还容易漏；spec 里写清楚，后人改起来看得懂。

**一份 spec 出两个 exe**，靠环境变量 ``ST_ROTATOR_VARIANT`` 区分：

- ``cli``  → ``st-rotator.exe``（有控制台，跑所有子命令）
- ``tray`` → ``st-rotator-tray.exe``（无控制台，双击即托盘）

不做成「一个 exe 双模式」是因为 PyInstaller 的 console 标志是**链接期**决定的，
运行时改不了：要么到处留黑窗，要么任何 CLI 输出都看不到。两个 exe 共用同一套
Analysis 结果在 onefile 下也做不到（每个 exe 都要内嵌自己的解包镜像），所以干脆
分两次构建，各 12~15MB。
"""

import os
import sys
from pathlib import Path

#: 仓库根目录（spec 在 packaging/ 下，上一级就是根）
ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
VARIANT = os.environ.get("ST_ROTATOR_VARIANT", "cli")
if VARIANT not in ("cli", "tray"):
    raise SystemExit(f"ST_ROTATOR_VARIANT 只能是 cli 或 tray，收到 {VARIANT!r}")

#: 入口脚本 + 输出名
ENTRY = ROOT / "packaging" / ("entry_tray.py" if VARIANT == "tray" else "entry_cli.py")
NAME = "st-rotator-tray" if VARIANT == "tray" else "st-rotator"

#: 只读资源：首次运行要复制出来的配置模板
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
    # 这一个开关就是两个 exe 的全部差别
    console=(VARIANT == "cli"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "st-rotator.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
