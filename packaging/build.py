"""构建 Windows 免安装 exe 与发布包。

用法（在仓库根目录）::

    python -m venv .venv
    .venv\\Scripts\\pip install pyinstaller
    .venv\\Scripts\\python packaging\\build.py

产物全部落在 ``release/``：

- ``st-rotator-<版本>-win64.zip``   ← 发布用（两个 exe + 示例配置 + README + LICENSE）
- ``st-rotator-<版本>-source.zip``  ← 源码包（供其它系统自行运行）
- ``st-rotator.exe`` / ``st-rotator-tray.exe`` ← 单独留一份方便本地测试

脚本自身不依赖 PyInstaller 之外的任何第三方包，也不假设 PATH 里有 ``pyinstaller``
（用 ``sys.executable -m PyInstaller``，确保用的是当前解释器装的那份）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = ROOT / "release"

#: 打包成 zip 时放进根目录的文件（exe 除外，单独加）
EXTRA_FILES = ["config.example.json", "README.md", "LICENSE"]


def read_version() -> str:
    """从 ``st_rotator/version.py`` 读版本号，避免和程序内版本脱节。"""
    sys.path.insert(0, str(ROOT))
    from st_rotator.version import __version__

    return __version__


def make_icon() -> Path:
    """用项目自带的托盘图标渲染器生成 exe 图标。

    复用 ``st_rotator.trayicons`` 而不是另找一张 png：托盘图标本来就是这套配色，
    exe 图标和托盘图标一致，用户才不会觉得「装错程序了」。顺带也不用引入 Pillow。
    """
    sys.path.insert(0, str(ROOT))
    from st_rotator.trayicons import COLOR_HEALTHY, ICON_SIZES, build_ico

    target = PACKAGING / "st-rotator.ico"
    target.write_bytes(build_ico(ICON_SIZES, COLOR_HEALTHY))
    return target


def write_version_info(version: str, variant: str) -> Path:
    """生成 PyInstaller 的 ``version_info.txt``。

    没有它，exe 右键「属性 → 详细信息」里版本号、产品名全是空白，用户和杀软都
    分不清这是什么程序。内容就是 VS_VERSIONINFO 结构的字面描述，PyInstaller
    会编译进 PE 资源段。

    ``OriginalFilename`` 必须跟着 variant 走：两个 exe 同名会让人在任务管理器里
    认错进程，也让「文件被替换」类告警对不上号。
    """
    exe_name = "st-rotator-tray.exe" if variant == "tray" else "st-rotator.exe"
    parts = (version.split(".") + ["0", "0", "0"])[:4]
    nums = ", ".join(str(int(p)) for p in parts)
    text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({nums}),
    prodvers=({nums}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Phoeky'),
      StringStruct('FileDescription', 'st-rotator 多账户多 Key 轮换工具'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'st-rotator'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', '{exe_name}'),
      StringStruct('ProductName', 'st-rotator'),
      StringStruct('ProductVersion', '{version}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    target = PACKAGING / "version_info.txt"
    target.write_text(text, encoding="utf-8")
    return target


def run_pyinstaller(variant: str) -> None:
    """按 variant 构建一个 exe。"""
    env = dict(os.environ, ST_ROTATOR_VARIANT=variant)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(PACKAGING / "st-rotator.spec"),
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
    ]
    print(f"\n=== 构建 {variant} ===")
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=env)


def make_release_zip(version: str) -> Path:
    """把两个 exe + 说明文件打成发布 zip。"""
    RELEASE.mkdir(parents=True, exist_ok=True)
    target = RELEASE / f"st-rotator-{version}-win64.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for exe in ("st-rotator.exe", "st-rotator-tray.exe"):
            path = DIST / exe
            if not path.is_file():
                raise SystemExit(f"构建产物缺失: {path}")
            zf.write(path, exe)
        for name in EXTRA_FILES:
            path = ROOT / name
            if path.is_file():
                zf.write(path, name)
    return target


def make_source_zip(version: str) -> Path:
    """打源码包（排除构建产物、虚拟环境、本地配置）。

    用 ``git archive`` 更省事，但要求工作区干净且 git 可用；这里手写过滤规则，
    保证「改了没提交也能出包」——发布前临时改 README 的场景很常见。
    """
    RELEASE.mkdir(parents=True, exist_ok=True)
    target = RELEASE / f"st-rotator-{version}-source.zip"
    skip_dirs = {
        ".git", ".buildenv", ".venv", "dist", "build", "release",
        "__pycache__", ".tray-icons", ".idea", ".vscode",
    }
    skip_files = {
        "config.json", "rotator.log", "subkeys.json", ".tray-token",
        "python-path.txt", "st-rotator.ico", "version_info.txt",
    }
    skip_suffix = (".pyc", ".pyo", ".lnk", ".spec.bak", ".qoder.site")
    prefix = f"st-rotator-{version}"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if any(part in skip_dirs for part in rel.parts):
                continue
            if not path.is_file() or path.name in skip_files or path.name.endswith(skip_suffix):
                continue
            # 构建时生成的图标属于「产物」，源码包不留；spec 会在构建时重新生成
            zf.write(path, f"{prefix}/{rel.as_posix()}")
    return target


def main() -> int:
    version = read_version()
    print(f"st-rotator {version} → 构建 Windows exe")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "错误: 当前解释器没装 PyInstaller。\n"
            f"  {sys.executable} -m pip install pyinstaller",
            file=sys.stderr,
        )
        return 1

    make_icon()
    for variant in ("cli", "tray"):
        write_version_info(version, variant)
        run_pyinstaller(variant)

    payload = make_release_zip(version)
    source = make_source_zip(version)
    for item in (payload, source):
        print(f"  {item}  ({item.stat().st_size / 1024 / 1024:.1f} MB)")

    manifest = {
        "version": version,
        "artifacts": [
            {"file": p.name, "size": p.stat().st_size, "sha256": _sha256(p)}
            for p in (payload, source)
        ],
    }
    (RELEASE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n完成，产物在 {RELEASE}")
    return 0


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
