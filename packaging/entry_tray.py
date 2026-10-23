"""无黑窗入口：``st-rotator-tray.exe``。

双击 = 直接跑 ``tray`` 子命令（托盘 + 网关 + 控制台），不需要敲任何参数；
带了参数时按普通命令行处理，所以 ``st-rotator-tray.exe --port 8899`` 也成立
（等价于 ``st-rotator tray --port 8899``）。

和 ``entry_cli.py`` 的差别全都源于「没有控制台」这个事实：

1. 没有参数时**必须自己补上 ``tray``**。argparse 要求子命令，不补就会打印用法
   然后退出——而 windowed exe 的 stdout 是 ``None``，用户看到的是「双击图标闪一下
   就没了」，完全无从排查。
2. 参数以 ``-`` 开头时（如 ``--port``）才当成 tray 的选项。
3. 这个 exe 里 stdout/stderr 是 ``None``，任何输出都会消失。所以这里接管
   stderr：换成只保留末尾若干 KB 的环形缓冲，出问题时把缓冲内容弹窗给用户看，
   而不是让他对着空气猜。用环形缓冲而不是无限累加，是因为托盘进程会常驻数天，
   ``-v`` 下日志量不小，攒起来迟早吃满内存。
4. 要求控制台输出的子命令（``status`` / ``check`` 之类）在这个 exe 里没有意义，
   直接提示改用 ``st-rotator.exe``，而不是「跑了但什么都没显示」。
"""

from __future__ import annotations

import sys

#: 捕获的 stderr 上限。只留尾部：用户需要的是最后那条报错，不是全部历史。
_TAIL_LIMIT = 16 * 1024

#: 需要控制台的子命令 → 应改用哪个 exe
_CLI_ONLY = frozenset({"status", "check", "chat", "bench", "subkey", "serve", "ui", "demo"})

#: tray 自己支持的选项（判断「第一个参数是不是子命令」用）
_TRAY_FLAGS = frozenset({
    "-c", "--config", "--host", "--port", "--token", "--browser", "--window-size",
    "--rate-mode", "-q", "--qps", "--log-file", "--log-lines", "-v", "--verbose",
    "-h", "--help",
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


def _normalize(argv: list[str]) -> list[str] | None:
    """把裸调用翻译成完整的 ``tray`` 调用；返回 ``None`` 表示「不启动服务」。"""
    if not argv:
        return ["tray"]
    head = argv[0]
    if head == "tray" or head in _TRAY_FLAGS:
        return ["tray", *argv] if head != "tray" else argv
    return None  # 交给调用方弹窗提示


def _explain(head: str) -> str:
    """给「这个 exe 干不了」的参数一个能照着做的说明。"""
    if head in _CLI_ONLY:
        return (
            f"`{head}` 需要控制台才能看到输出。\n\n"
            f"请改用 st-rotator.exe：\n    st-rotator.exe {head} …\n\n"
            "st-rotator-tray.exe 只负责托盘常驻（双击即启动）。"
        )
    return (
        f"无法识别的参数：{head}\n\n"
        "st-rotator-tray.exe 双击即启动托盘 + 网关 + 控制台；\n"
        "需要其它子命令请用 st-rotator.exe。"
    )


def main() -> int:
    from st_rotator import runtime
    from st_rotator.version import __version__

    argv = sys.argv[1:]
    head = argv[0] if argv else ""
    if head in ("-V", "--version"):
        runtime.notify(f"st-rotator {__version__}", title="st-rotator", error=False)
        return 0

    normalized = _normalize(argv)
    if normalized is None:
        runtime.notify(_explain(head), title="st-rotator")
        return 2

    # windowed exe 里 stderr 是 None：换成环形缓冲，正常时不打扰用户，
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
    except SystemExit as exc:  # argparse 的用法错误走这里
        code = int(exc.code or 0)
    if code not in (0, 130) and capture is not None:
        detail = capture.getvalue().strip()
        if detail:
            runtime.notify(detail, title="st-rotator")
    return code


if __name__ == "__main__":
    sys.exit(main())
