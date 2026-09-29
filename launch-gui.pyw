r"""双击启动 chrometrans 图形界面 —— 无控制台窗口的入口。

为什么要有这么一个文件：桌面快捷方式指向 venv 里的 pythonw.exe，它**没有控制台**。
没有控制台意味着启动一旦失败（缺依赖、导入报错、Qt 起不来），双击就是「什么都没发生」，
一个字都不留。所以这里先把 stdout/stderr 接到日志再导入并运行 —— 以后出问题去那里看，
而不是回到「界面没出来，也不知道为什么」。

日志落在 %APPDATA%\chrometrans\gui.log，不落在仓库里：仓库根是被 git 跟踪的工作目录，
往里写运行期产物只会多出一堆要 gitignore 的噪音。
"""
import datetime
import os
import sys
import traceback


def _log_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    directory = os.path.join(base, "chrometrans")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return os.path.join(base, "chrometrans-gui.log")   # 建不出来也别因此起不来
    return os.path.join(directory, "gui.log")


LOG = _log_path()
MAX_LOG_BYTES = 1_000_000


def _open_log():
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG_BYTES:
            os.replace(LOG, LOG + ".1")       # 只留一代，别让日志无限长
    except OSError:
        pass                                  # 换不动就接着往后写，不是致命问题
    stream = open(LOG, "a", encoding="utf-8", buffering=1)
    stream.write(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} 启动 ===\n")
    return stream


def main() -> int:
    # 显式 UTF-8：界面里的中文和符号一旦撞上 GBK 控制台会抛 UnicodeEncodeError，
    # 而这里没有控制台可撞 —— 但写文件同样要指定，否则用的是系统默认编码。
    stream = _open_log()
    sys.stdout = sys.stderr = stream
    try:
        from chrometrans.gui.app import main as run_gui
        return run_gui()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        return 1
    finally:
        stream.flush()


if __name__ == "__main__":
    sys.exit(main())
