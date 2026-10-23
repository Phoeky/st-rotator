"""无黑窗单 exe 入口：``st-rotator.exe``（唯一产物）。

双击 = 直接跑 ``tray``：网关 + 托盘 + 自动打开控制台。首次运行生成的配置
``accounts`` 为空（零 Key 也能启动），Key 由用户稍后在控制台里添加。

带参数时按普通命令行处理：

- 子命令（``status`` / ``check`` / …）或未知参数 → 原样透传给 argparse，
  用法错误显示完整 help；
- tray 的选项（``--port``、``--port=8899``、``-c`` …）打头 → 自动补 ``tray``
  前缀，``st-rotator.exe --port 8899`` 等价于 ``st-rotator tray --port 8899``；
- ``-V/--version`` → 打印版本号。

windowed exe 的标准流可能是 ``None``（双击启动时没有任何句柄），处理顺序：

1. ``AttachConsole(-1)`` 挂父控制台 —— 覆盖「从终端启动但没继承到句柄」的场景，
   挂上就重开 ``CONOUT$``/``CONIN$``，CLI 子命令从此有输出可见；
2. 仍是 ``None`` 的（纯双击场景）→ 换成尾部环形缓冲 + ``force_headless()``：
   正常时不出声，出错时把缓冲尾部弹窗给用户看。环形缓冲而不是无限累加，
   是因为托盘进程会常驻数天，``-v`` 下日志量不小，攒起来迟早吃满内存。
"""

from __future__ import annotations

import sys

#: 捕获的 stderr 上限。只留尾部：用户需要的是最后那条报错，不是全部历史。
_TAIL_LIMIT = 16 * 1024

#: 第一个参数是这些选项时，等价于「tray 子命令 + 该选项」。
#: 不含 -h/--help：透传给顶层 parser 才能看到全部子命令。
_TRAY_FLAGS = frozenset({
    "-c", "--config", "--host", "--port", "--token", "--browser", "--window-size",
    "--rate-mode", "-q", "--qps", "--log-file", "--log-lines", "-v", "--verbose",
})


class _TailStream:
    """只保留最后 N 字节的写入流，用来在无控制台时兜住报错信息。"""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, limit: int = _TAIL_LIMIT) -> None:
        self._limit = limit
        self._chunks: list[str] = []
        self._size = 0

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._chunks.append(text)
        self._size += len(text)
        # 超出上限就从头丢，直到回到水位以下。丢一半而不是丢一条，
        # 避免每来一行就裁剪一次（托盘长期运行时这是热路径）。
        while self._size > self._limit and len(self._chunks) > 1:
            dropped = self._chunks.pop(0)
            self._size -= len(dropped)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("没有可用的控制台")

    def getvalue(self) -> str:
        return "".join(self._chunks)


def _attach_parent_console() -> None:
    """从终端启动时，把 windowed exe 的标准流接回父控制台。"""
    if sys.stdout is not None and sys.stderr is not None and sys.stdin is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
            return
        # 编码跟着 chcp 走：往 chcp 936 的控制台写 UTF-8 会乱码，反之亦然。
        try:
            cp = int(kernel32.GetConsoleOutputCP())
        except Exception:
            cp = 0
        if cp == 65001:
            enc = "utf-8"
        elif cp > 0:
            enc = f"cp{cp}"
        else:
            enc = "mbcs"
        if sys.stdout is None:
            sys.stdout = open("CONOUT$", "w", encoding=enc, errors="replace")
        if sys.stderr is None:
            sys.stderr = open("CONOUT$", "w", encoding=enc, errors="replace")
        if sys.stdin is None:
            sys.stdin = open("CONIN$", "r", encoding=enc, errors="replace")
    except Exception:
        # 挂不上就算了，走无控制台兜底路径（下面的环形缓冲）
        pass


def _normalize(argv: list[str]) -> list[str]:
    """裸调用 → ``tray``；tray 选项打头 → 补 ``tray`` 前缀；其余原样透传。"""
    if not argv:
        return ["tray"]
    head = argv[0]
    if head == "tray":
        return argv
    # 等号写法（--port=8899）先剥掉再匹配，否则会被当成未知参数
    if head.split("=", 1)[0] in _TRAY_FLAGS:
        return ["tray", *argv]
    return argv  # 子命令 / -h / --version / 未知参数，交给 argparse


def main() -> int:
    from st_rotator import runtime
    from st_rotator.version import __version__

    _attach_parent_console()

    raw_head = sys.argv[1] if len(sys.argv) > 1 else ""
    if raw_head in ("-V", "--version") and sys.stdout is None:
        # 打不出去的版本号等于没跑：没有流时兜底弹窗（正常终端走 argparse 打印）
        runtime.notify(f"st-rotator {__version__}", title="st-rotator", error=False)
        return 0

    normalized = _normalize(sys.argv[1:])

    # 没有标准流（双击 windowed exe）：换成环形缓冲，正常时不打扰用户，
    # 出错时把尾部内容弹出来。
    capture = None
    if sys.stderr is None:
        capture = _TailStream()
        sys.stderr = capture  # type: ignore[assignment]
        # 换了流之后 has_console() 靠 isinstance 已判断不出来，必须显式声明，
        # 否则报错会被 print 进这个缓冲（用户看不见）而不是弹窗。
        runtime.force_headless()
    if sys.stdout is None:
        sys.stdout = _TailStream()  # type: ignore[assignment]

    from st_rotator.cli import main as cli_main

    try:
        code = int(cli_main(normalized))
    except SystemExit as exc:  # argparse 的用法错误 / --help / --version 走这里
        code = int(exc.code or 0)
    if code not in (0, 130) and capture is not None:
        detail = capture.getvalue().strip()
        if detail:
            runtime.notify(detail, title="st-rotator")
    return code


if __name__ == "__main__":
    sys.exit(main())
