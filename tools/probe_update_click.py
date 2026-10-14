# -*- coding: utf-8 -*-
"""探针（v1.4.43 新增）：右键「检查更新」到底有没有弹框 —— 按产品真实分支跑。

用法：
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/probe_update_click.py
期望输出：三个场景的「用户看到的框」都 **≥1**，退出码 0。
任何场景出现 `0 个框` 就是本轮那个缺陷回来了：点击被在飞检查吞掉，屏幕上什么都不出现。

为什么需要它（而不是只靠用例）：回归用例把网络与弹窗两头都换成假对象，量的是
"分支写对了没有"；这个探针把 `_check_updates_background` / `_handle_update_check`
的**真方法**串起来，只替换最外层的网络与 `show_alert`，回答的是"用户点了这一下，
屏幕上会出现几个框"。v1.4.43 之前它跑出来是 0 —— 既看不见也查不到，正是最该
有东西盯着的那类失效。

三个场景的差别只在"点击距离上一次激活多久"：
  · 场景A 1.0 秒 / 场景C 0.3 秒：右键悬浮窗会把应用切成活跃，活跃又触发一次静默
    补查（v1.4.43 起 60 秒节流），用户接着点就撞进那次在飞检查 —— 历史上必被吞；
  · 场景B 对照：没有后台检查在跑，走全新的手动检查。
网络用可控延迟替代（默认卡满一个往返），因为真实链路在这台机器上忽快忽慢，
延迟固定才能让三次运行可比。
"""

import os
import sys
import tempfile
import threading
import time
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 必须**在任何 sf_* 被 import 之前**改掉日志落点：sf_constants 只在检测到
# "正在跑 unittest" 时才自动把落盘路径挪到临时目录，而这个工具是直接跑的。
# 不这么做，它写出的假失败会混进用户真实的 debug.log（v1.3.67 就是被这件事坑过：
# 一次回归往真日志灌了 1.3 万条假异常，此后那份日志不再能用来判断数据源健康度）。
os.environ.setdefault("SF_LOG_PATH",
                      os.path.join(tempfile.gettempdir(), "sf_probe_update_click.log"))
os.environ.setdefault("SF_CONFIG_PATH",
                      os.path.join(tempfile.gettempdir(), "sf_probe_update_config.json"))
os.environ.setdefault("SF_SINGLE_SOCK",
                      os.path.join(tempfile.gettempdir(), "sf_probe_update.sock"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

APP = QApplication([])          # 必须自己持有引用，否则离屏下进程会中途 abort

import sf_system_alert  # noqa: E402
import sf_update  # noqa: E402
from stock_float_window import FloatWindow  # noqa: E402

NETWORK_DELAY_SEC = 5.0
boxes: list = []    # 「用户看到的框」—— show_alert 每被调用一次记一条
queue: list = []


def _fake_show_alert(*args: Any, **kwargs: Any) -> object:
    boxes.append("%s / %s" % (kwargs.get("title"),
                              kwargs.get("message") or kwargs.get("text") or ""))
    return object()


class Host(QWidget):
    """真 QWidget 宿主：可见反馈那段有 isinstance(self, QWidget) 守卫，替身会整段跳过。

    借来的实现统一标成 Any —— mypy 会把"把 FloatWindow 的方法挂到别的宿主上再调用"
    判成 `Invalid self argument`（misc），这是探针固有的形态，不是产品缺陷。
    """

    _check_updates_background: Any = FloatWindow._check_updates_background
    _handle_update_check: Any = FloatWindow._handle_update_check
    _show_checking_dialog: Any = FloatWindow._show_checking_dialog
    _update_check_stale: Any = FloatWindow._update_check_stale
    _show_update_message: Any = FloatWindow._show_update_message
    _schedule_update_retry: Any = FloatWindow._schedule_update_retry
    _anchor_update_dialog_screen: Any = FloatWindow._anchor_update_dialog_screen

    def _close_update_flow_box(self):
        pass                    # 真实现会碰窗口与 sip.delete，探针不需要

    def _dispose_update_dialog(self, dialog):
        pass

    def _prompt_update(self, update):
        boxes.append("新版确认框 / v%s" % update.version)

    def __init__(self):
        super().__init__()
        self._threads_stopped = False
        self._update_downloading = False
        self._update_checking = False
        self._update_transition_pending = False
        self._update_pending = None
        self._update_manual_wait = False
        self._update_check_aborted = False
        self._update_last_check_at = 0.0
        self._update_check_dialog = None
        self._update_message_dialog = None
        self._update_prompt_dialog = None
        self._update_failure_count = 0
        self._update_retry_timer = None
        self._quitting = False

    def _q_put(self, item):
        queue.append(item)


def run(label, gap_seconds, background=True):
    del boxes[:]
    del queue[:]
    host = Host()
    gate = threading.Event()

    def slow_check(*args: Any, **kwargs: Any) -> None:
        gate.wait(NETWORK_DELAY_SEC)
        raise sf_update.UpdateError("无法连接更新服务")

    # 探针要换的就是最外层那个网络出口；签名写成可变参数，直接赋值即可通过类型检查
    sf_update.check_for_update = slow_check
    if background:
        # 第 1 步：右键悬浮窗 → 应用被激活 → 触发一次静默补查（不弹任何东西）
        host._check_updates_background(manual=False)
    busy = host._update_transition_pending
    time.sleep(gap_seconds)                      # 第 2 步：鼠标移到菜单、按下「检查更新」
    host._check_updates_background(manual=True)  # 第 3 步：这次点击
    gate.set()
    for _ in range(200):                         # 等回包（不靠 sleep 猜时长）
        if queue:
            break
        time.sleep(0.02)
    if queue:
        host._handle_update_check(*queue[0][1])
    print("%-32s 间隔%.1fs 后台在飞=%-5s 用户看到的框=%d %s"
          % (label, gap_seconds, busy, len(boxes), boxes))
    return len(boxes)


def main():
    # 只改模块属性即可：产品代码里的 `import sf_system_alert` 拿到的是同一个模块对象，
    # 所以不必再往 stock_float_window 上贴一份模块名。
    sf_system_alert.show_alert = _fake_show_alert
    print("== 探针：右键「检查更新」有几个框（0 = 点了完全没反应）==")
    results = [
        run("场景A 激活后 1.0 秒点击", 1.0),
        run("场景C 激活后 0.3 秒点击", 0.3),
        run("场景B 无后台检查（对照）", 1.0, background=False),
    ]
    silent = [name for name, count in zip(("场景A", "场景C", "场景B"), results) if count == 0]
    if silent:
        print("!! 这些场景点击后一个框都没有：%s" % "、".join(silent))
        return 1
    print("== 结论：三种时机都给了反馈 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
