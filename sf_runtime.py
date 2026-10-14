# -*- coding: utf-8 -*-
"""sf_runtime —— 上帝类拆分第三步（v1.2.41）：运行时调度
（App Nap 省电 / 置顶 / 缩 Dock 运行状态）。

原 FloatWindow 上帝类中运行时节电/置顶状态管理沉入本模块，主窗仅保留薄委托调用，
对外行为 100% 不变。本模块不 import Qt，纯逻辑 + 可注入的系统 API 依赖：

1. App Nap 省电调度（原 _update_nap_policy）
   交易时段通过 NSProcessInfo.beginActivityWithOptions_reason_ 禁止 App Nap，
   保证行情轮询稳定；非交易时段 / 缩到 Dock 时 endActivity_ 恢复默认省电。
   → NapTokenManager：App Nap 令牌生命周期管理（幂等，状态未变化时无系统调用）。
   NSProcessInfo 依赖可注入，便于不依赖 Qt / Foundation 的单测。

2. 缩 Dock ↔ 恢复窗口 的运行状态切换（_minimized 门控 + App Nap 策略）
   → RuntimeState：持有最小化状态与 NapTokenManager，提供
     enter_dock / exit_dock / update_nap / shutdown 状态转换与最小化门控。

3. 置顶 / 降频轮询的间隔决策
   → restore_ui_interval(trading)：恢复窗口后的 UI 看门狗间隔（一律 1000ms；
     trading 参数保留以维持调用点签名与将来分档的可能）。
   （置顶系统调用本身位于 sf_glass.GlassMixin._force_top，v1.2.x 已拆出，此处不重复。）
"""


from sf_constants import APP_DISPLAY_NAME, UI_WATCHDOG_INTERVAL_MS

_ALLOW_IDLE_SLEEP_OPTIONS = 0xEFFFFF


class NapTokenManager(object):
    """App Nap 令牌生命周期管理。

    把关的是"状态未变化时无实际调用开销"：
    - 激活（trading=True）：持有令牌中则跳过，否则 beginActivityWithOptions_reason_
    - 停用（trading=False）：无令牌则跳过，否则 endActivity_ 并回收令牌
    与拆分前 _update_nap_policy 的判定条件逐位一致。
    """

    def __init__(self, ns_process_info=None, activity_options=None,
                 reason=APP_DISPLAY_NAME + " 行情轮询"):
        self._provider = ns_process_info   # 可注入；None 时首次用到才懒加载 Foundation.NSProcessInfo
        self._activity_options = activity_options  # 可注入；None 时取 Foundation.NSActivityUserInitiated
        self._reason = reason
        self._token = None                 # 当前持有的 App Nap 令牌（None=未持有）
        self._failure_count = 0
        self._last_error = ""
        self._last_change_at = 0.0

    # -- 依赖（懒加载，避免模块 import 阶段触碰 Foundation） -------------------
    def _get_info(self):
        if self._provider is None:
            from Foundation import NSProcessInfo
            self._provider = NSProcessInfo.processInfo()
        return self._provider

    def _load_options(self):
        if self._activity_options is None:
            try:
                import Foundation
                self._activity_options = getattr(
                    Foundation, "NSActivityUserInitiatedAllowingIdleSystemSleep",
                    _ALLOW_IDLE_SLEEP_OPTIONS)
            except Exception:
                self._activity_options = _ALLOW_IDLE_SLEEP_OPTIONS
        return self._activity_options

    # -- 状态查询 --------------------------------------------------------------
    def active(self):
        """当前是否持有 App Nap 禁止令牌。"""
        return self._token is not None

    # -- 业务入口 ---------------------------------------------------------------
    def set_trading(self, trading):
        """按交易状态幂等切换 App Nap。返回调用后的令牌（None=未持有）。

        系统 API 失败与拆分前一致：静默降级（不影响行情轮询主流程）。
        """
        try:
            if trading:
                if self._token is None:
                    info = self._get_info()
                    opts = self._load_options()
                    self._token = info.beginActivityWithOptions_reason_(opts, self._reason)
                    self._last_change_at = self._changed_at()
                    self._last_error = ""
            else:
                if self._token is not None:
                    self._get_info().endActivity_(self._token)
                    self._token = None
                    self._last_change_at = self._changed_at()
                    self._last_error = ""
        except Exception as exc:
            self._failure_count += 1
            self._last_error = type(exc).__name__
        return self._token

    @staticmethod
    def _changed_at():
        import time
        return time.time()

    def diagnostics(self):
        return {"active": self.active(), "activity_options": self._load_options(),
                "failure_count": self._failure_count, "last_error": self._last_error,
                "last_change_at": self._last_change_at}

    def stop(self):
        """停止持有（endActivity 回收令牌，幂等），用于退出清理。"""
        return self.set_trading(False)


class RuntimeState(object):
    """运行时节电 / 缩 Dock 状态机。

    持最小化状态 + App Nap 管理器；主窗通过本对象读写 _minimized 门控，
    并按原时序（缩 Dock / 恢复窗口 / 每轮轮询）调用 update_nap 切换省电策略。
    """

    def __init__(self, nap_manager=None):
        self._nap = nap_manager if nap_manager is not None else NapTokenManager()
        self._minimized = False          # 缩小到 Dock 中（防止 show_dock 触发 AppActive 立即恢复窗口）
        self._trading_nap = None         # 最近一次 update_nap 请求方向（可观测，不参与判定）

    # -- 最小化门控 -------------------------------------------------------------
    @property
    def minimized(self):
        """是否处于"缩到 Dock"态。**只是一个标志位**：App Nap 的停用/恢复不在这里做，
        由调用方按原时序自己调 update_nap(False/True)（见 enter_dock / exit_dock 的说明）。
        读它的人（重绘、宽度收尾、置顶复核）都指望它便宜且同步。"""
        return self._minimized

    @minimized.setter
    def minimized(self, v):
        """赋值统一 `bool()` 归一：这个标志对外只认真假。

        实测当前所有调用点传的都是 True/False（主窗的同名 property 转发 + 用例直挂），
        所以这一层归一是**兜底**，不是在迁就某个已知的脏值来源 —— 别据此去找"哪里在传脏值"。
        """
        self._minimized = bool(v)

    def enter_dock(self):
        """缩到 Dock：标记最小化（App Nap 停用由调用方按原时序调 update_nap(False)）。"""
        self._minimized = True
        return self

    def exit_dock(self):
        """恢复窗口：清除最小化标记（App Nap 重启用 update_nap(trading)）。"""
        self._minimized = False
        return self

    @property
    def nap_active(self):
        """当前是否持有 App Nap 禁止令牌。"""
        return self._nap.active()

    @property
    def nap_token(self):
        """当前持有的 App Nap 令牌（None=未持有），兼容主窗旧 _nap_activity 字段。"""
        # 直接透出内部令牌对象本身：endActivity_ 必须传回 begin 时拿到的同一对象。
        return self._nap._token

    # -- App Nap 策略 -----------------------------------------------------------
    def update_nap(self, trading):
        """按交易状态幂等更新 App Nap 策略（原 _update_nap_policy 薄委托）。"""
        self._trading_nap = bool(trading)
        return self._nap.set_trading(bool(trading))

    def shutdown(self):
        """退出清理：回收 App Nap 令牌（幂等）。"""
        return self._nap.stop()

    def diagnostics(self):
        return {"minimized": self._minimized, "nap_requested": self._trading_nap,
                "activity": self._nap.diagnostics()}


def restore_ui_interval(trading):
    """恢复窗口后 UI 兜底看门狗的间隔：一律 1000ms。

    历史：原实现是 `50 if trading else 1000` —— 交易时段要靠 50ms 定时排队列，
    否则数据进来最多等 50ms 才上屏。v1.4.10 起数据入队即发跨线程信号唤醒 _poll，
    定时器只剩兜底巡检职责，恢复窗口后没必要再回到 50ms（见
    sf_quote_loop.ui_poll_interval_ms 的同源说明）。

    trading 参数保留以维持调用点签名与将来分档的可能。
    """
    del trading
    return int(UI_WATCHDOG_INTERVAL_MS)
