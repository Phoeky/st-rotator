"""控制台版入口：``st-rotator.exe``。

对应源码运行的 ``python -m st_rotator``。保留控制台窗口，因为 ``serve`` / ``bench`` /
``status`` 这些子命令的输出本来就是给终端看的，重定向到文件后照样能用。

PyInstaller 的分析起点是脚本而不是包，所以这里必须有一层真实入口：直接拿
``st_rotator/__main__.py`` 当入口的话，``__name__ == "__main__"`` 成立但包内相对
导入会失败（``__main__.py`` 靠 ``python -m`` 注入的 ``__package__`` 才能用相对导入）。
"""

from __future__ import annotations

import sys


def main() -> int:
    from st_rotator.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
