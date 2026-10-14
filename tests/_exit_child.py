# -*- coding: utf-8 -*-
"""真 cocoa 退出链路的子进程替身（被 tests/test_v1420_exit_teardown.py 调起）。

为什么必须真 cocoa、必须子进程：v1.4.18 在 iMac 上的 SIGSEGV 发生在 Qt 的 cocoa
插件里 —— QApplication.quit() 被转成 -[NSApplication terminate:] → exit()，排队的
deleteLater 直到 C++ 静态析构阶段才冲刷，那时销毁带 QWindow 的下载进度框就踩空指针
（QThreadStorageData::get ← QOpenGLContext::currentContext ← QSurface::~QSurface）。
离屏插件（offscreen）走的是另一套平台后端，复现不了；同进程建真窗口又会冲掉整个
测试会话（见 tests/_alert_child.py 同样的取舍）。所以放子进程，父用例只看标记与退出码。

模式：
    probe               —— 预检本机有没有窗口服务
    update_download_quit —— 线上崩溃原样：下载完成 → 收口进度框 → quit()
    plain_quit          —— 常规退出（没有进度框），确认修复没把正常路径改坏
"""

import os
import pathlib
import sys
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

# 崩溃只在 cocoa 后端出现：父套件用 offscreen，这里必须显式覆盖回来。
os.environ["QT_QPA_PLATFORM"] = "cocoa"


def _marker(key, value):
    print("%s=%s" % (key, value), flush=True)


def _fake_http_get(url, **kw):
    return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'


def _build_window():
    """造一个真实 FloatWindow，并把网络/落盘/图标线程全部挡掉。"""
    import sf_quote
    import sf_system_alert
    import stock_float_window as m

    patches = [
        patch.object(m, "load_config",
                     return_value=("sh600519", -12000, -12000, 200, "translucent")),
        patch.object(m, "save_config", lambda *a, **k: None),
        patch.object(sf_quote, "http_get", _fake_http_get),
        patch.object(m, "start_icon_worker", lambda: None),
        patch.object(m.FloatWindow, "_force_top", lambda self: None),
        # 强制走 Qt 自绘兜底框 —— 线上崩的就是它（原生进度框没有 Qt 析构面）
        patch.object(sf_system_alert, "show_progress_alert", lambda **kw: None),
    ]
    for p in patches:
        p.start()
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    win = m.FloatWindow()
    win.show()
    win.move(-12000, -12000)      # 别在用户屏幕上闪一下
    return app, win, m


def run_probe():
    app, win, m = _build_window()
    _marker("probe", "ok")
    _marker("visible", win.isVisible())
    win._shutdown_threads()
    win.close()
    return 0


def _run_in_event_loop(prepare, action):
    """必须在 app.exec() 里退出：崩溃的成因就是"事件循环正在跑时被 terminate:"。

    直接在 exec() 之前调 quit() 只会置一个 quitNow 标志然后原样返回，
    既不走 terminate: 也不复现崩溃 —— 那样这条用例会永远绿，等于没测。
    prepare(win) 在 exec() 之前把现场搭好（进度框必须先上屏并完成映射，
    Qt 才会为它建出真正的 QWindow；线上崩溃就是这个形态）。
    """
    from PyQt6.QtCore import QTimer
    app, win, m = _build_window()
    prepare(win)
    QTimer.singleShot(1500, lambda: action(win))
    app.exec()
    _marker("exec_returned", "True")
    return 0


def _open_box(win):
    box = win._open_download_progress_box()
    _marker("box", type(box).__name__)
    try:
        box.move(-12000, -12000)      # 别在用户屏幕上闪一下
    except Exception:
        pass


def _download_then_quit(win):
    import sf_update
    update = sf_update.UpdateInfo("9.9.9", "回归用说明",
                                  "https://example.invalid/a.zip", 8, "a" * 64)
    _marker("quit_called", "True")
    # 走真实分发链：队列消费函数自己会 _close_update_flow_box() 然后 quit()。
    # 安装交接必须假掉：真去启动安装助手会改动 /Applications。
    with patch.object(sf_update, "launch_installer", lambda *a, **k: True):
        win._handle_update_download(update, "/tmp/does-not-exist.zip", None)


def run_update_download_quit():
    """线上崩溃原样：下载完成事件抵达 → 收口进度框 → quit() → 退出进程。"""
    return _run_in_event_loop(_open_box, _download_then_quit)


def run_plain_quit():
    """常规退出：没有进度框，也不允许崩。"""
    return _run_in_event_loop(lambda win: None,
                              lambda win: (_marker("quit_called", "True"),
                                           win.quit()))


_MODES = {
    "probe": run_probe,
    "update_download_quit": run_update_download_quit,
    "plain_quit": run_plain_quit,
}


def main(argv):
    mode = argv[1] if len(argv) > 1 else "probe"
    fn = _MODES.get(mode)
    if fn is None:
        _marker("error", "unknown mode %s" % mode)
        return 2
    try:
        return fn()
    except BaseException as exc:                       # noqa: BLE001
        import traceback
        _marker("error", repr(exc))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
