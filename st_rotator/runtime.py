"""运行环境适配：源码运行与 PyInstaller 打包 exe 的差异都收在这里。

两种运行方式真正的差别只有三处，但每一处都足以让程序在 exe 里「看起来没反应」：

- **路径**：exe 版没有「项目目录」这个概念。只读资源（如配置模板）在 PyInstaller
  的解包目录 ``sys._MEIPASS``，可写目录是 exe 所在目录；源码版两者都落在项目根 /
  当前目录。混用会让 exe 去读一个根本不存在的模板。
- **控制台**：windowed（无黑窗）exe 的 ``sys.stdout`` / ``sys.stderr`` 是 ``None``，
  任何 ``print`` 都会抛 ``AttributeError``，而且报错信息用户永远看不到。所以这里
  提供 ``install_null_streams`` 兜底，并用 ``notify`` 在没有控制台时改弹 MessageBox。
- **首次运行**：exe 用户手里没有仓库里的 ``config.example.json``，第一次双击必须能
  自动生成配置模板，否则只会得到一句「配置文件不存在」。

这些判断全都集中在本模块，其它模块不要自己写 ``getattr(sys, "frozen", False)``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: 打进 exe 的配置模板文件名（构建时用 ``--add-data`` 放到解包目录根部）
TEMPLATE_NAME = "config.example.json"

#: 默认配置文件名（exe 同目录 / 源码运行的当前目录）
CONFIG_NAME = "config.json"


class _NullStream:
    """无控制台时顶替 ``sys.stdout`` / ``sys.stderr`` 的空写入流。

    只实现 ``print`` 与常见日志库会用到的少数属性，写进去的内容直接丢弃。
    这比在每个 ``print`` 前判断 ``sys.stdout is None`` 靠谱得多 —— 后者漏一处就是
    一个 ``AttributeError``，而 windowed exe 里没有任何地方能看到这个异常。
    """

    encoding = "utf-8"
    errors = "replace"

    def write(self, text: str) -> int:  # noqa: D102 - 标准流协议
        return len(text)

    def flush(self) -> None:  # noqa: D102
        return None

    def isatty(self) -> bool:  # noqa: D102
        return False

    def fileno(self) -> int:  # noqa: D102
        raise OSError("没有可用的控制台")


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


#: 强制「按无控制台处理」。windowed 入口会把标准流换成自己的捕获流，
#: 那样 ``has_console()`` 靠 ``isinstance`` 判断就失效了（换进来的不是 ``_NullStream``），
#: 所以给入口一个显式开关，别让它靠猜。
_FORCE_HEADLESS = False


def force_headless() -> None:
    """声明当前进程没有控制台（由 windowed 入口调用）。"""
    global _FORCE_HEADLESS
    _FORCE_HEADLESS = True


def install_null_streams() -> None:
    """windowed exe 里把 ``None`` 的标准流换成空流，避免 print 崩溃。

    必须在入口脚本的最早处调用：之后的任何 ``print`` / 日志配置都会用到它。
    有控制台时不动任何东西。
    """
    global _FORCE_HEADLESS
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]
        _FORCE_HEADLESS = True
    if sys.stdin is None:
        sys.stdin = _NullStream()  # type: ignore[assignment]


def has_console() -> bool:
    """当前进程有没有真正可用的控制台（决定报错该打印还是该弹窗）。"""
    if _FORCE_HEADLESS:
        return False
    return sys.stderr is not None and not isinstance(sys.stderr, _NullStream)


def resource_dir() -> Path:
    """只读资源目录。

    exe 版是 PyInstaller 的解包目录（``sys._MEIPASS``）；源码版是项目根目录
    （``st_rotator`` 包的上一级）。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """可写目录：exe 所在目录；源码运行时是当前工作目录。

    运行产物（``config.json`` / ``rotator.log`` / ``subkeys.json`` / ``.tray-token``）
    都落在这一层，所以 exe 不能放在没有写权限的目录（例如 ``C:\\Program Files``）。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def default_config_path() -> Path:
    """``-c/--config`` 省略时用的配置路径。"""
    return app_dir() / CONFIG_NAME


def template_path() -> Path:
    """内置配置模板的路径（源码版在项目根，exe 版在解包目录）。"""
    return resource_dir() / TEMPLATE_NAME


def ensure_config(path: str | os.PathLike[str] | None = None) -> tuple[Path, bool]:
    """确保配置文件存在；不存在就生成一份「零 Key」起步配置。

    返回 ``(配置路径, 是否本次新建)``。

    为什么不直接复制 ``config.example.json``
    ----------------------------------------
    模板里的 ``${SENSENOVA_KEY_1}`` 占位符对新用户只会导致启动即报
    「环境变量未设置」，与「双击就能用」的目标冲突。所以首次运行生成的配置
    以模板为底、但把 ``accounts`` 清空：程序带空 Key 池正常启动，
    Key 由用户稍后在控制台里添加（空池请求会得到友好的「Key 池为空」提示）。
    """
    target = Path(path) if path is not None else default_config_path()
    if target.exists():
        return target, False
    data: dict = {}
    template = template_path()
    if template.is_file():
        try:
            loaded = json.loads(template.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["accounts"] = []
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return target, False
    return target, True


def _message_box(title: str, message: str, *, error: bool) -> None:
    """Windows 弹窗；其它平台没有等价的无依赖实现，直接放弃。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        flags = 0x00000010 if error else 0x00000040  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    except Exception:  # pragma: no cover - 弹窗失败也不能影响退出
        pass


def notify(message: str, *, title: str = "st-rotator", error: bool = True) -> None:
    """给用户看一条消息：有控制台就打 stderr，没有控制台（windowed exe）就弹窗。"""
    text = f"错误: {message}" if error else message
    if has_console():
        print(text, file=sys.stderr)
        return
    _message_box(title, text, error=error)
