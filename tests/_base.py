# -*- coding: utf-8 -*-
"""v1.3.68：测试公用底座（沿用标准 unittest，只是把重复件收拢进来）。

由来（TESTS_AUDIT_2026-09-14.md 之 P0-3 / P1-4）：72 个测试文件里有
**47 处**各自写 `QApplication.instance() or QApplication([])`、**0 处**做清理，
跨模块共享字体度量缓存、剪贴板、事件队列，导致"单独跑绿、全量红"这类
不可复现的失败（`tests/test_v165.py` 的注释本身就承认了顺序差异）。
另外 **14 处** `time.sleep(...)` 是在猜"异步多久能完成"，机器一忙就超阈值。

本模块只提供三样东西，**不改造任何存量用例**（批量重写风险远大于收益）：

  1. `wait_for(cond, timeout)` —— 轮询等到条件满足，取代猜时长的 sleep；
     超时时把最后一次条件状态和上下文一起报出来，失败信息自带线索。
  2. `SFCase` —— 新用例的基类：复用 QApplication（不再每个文件各写一遍），
     并在 tearDown 关掉本用例显式登记的窗口（不碰别人建的窗口）。
  3. `fixed_clock(...)` —— 注入"现在几点"的时钟，取代写死 `datetime(2026,2,18)`
     这类会随年份/节假日表失效的断言。

约束：**做加法不做减法**。存量用例不继承 SFCase 也照样能跑，新写的建议继承。
"""
import contextlib
import time
import unittest


def wait_for(cond, timeout=2.0, tick=0.01, msg=""):
    """轮询等待 `cond()` 为真，返回 True；超时返回 False。

    取代 `time.sleep(0.05)` 之后立刻断言的写法 —— 那种写法在快机器上是浪费，
    在慢机器/高负载下是随机失败。轮询写法两头都不吃亏。

    用法：
        self.assertTrue(wait_for(lambda: m.call_count == 1), "通知应被投递一次")
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = cond()
        except Exception as exc:          # 断言类异常也算"条件还不成立"
            last = "异常: %r" % (exc,)
        if last:
            return True
        time.sleep(tick)
    if msg:
        # 超时信息必须自带现场，否则又要花十几分钟去猜"到底卡在哪"
        print("\n  [wait_for 超时 %.1fs] %s｜最后一次条件结果=%r" % (timeout, msg, last))
    return False


def wait_until_false(cond, timeout=2.0, tick=0.01, msg=""):
    """`wait_for` 的反向版本：等到条件为假。用于"不该发生"的负向断言。

    比 `sleep(0.05)` + `assertEqual(call_count, 0)` 更可信：它证明的是
    "我在 timeout 内一直观察，始终没有发生"，而不是"我睡了一小觉没看到"。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            val = cond()
        except Exception as exc:
            val = "异常: %r" % (exc,)
        if not val:
            return True
        time.sleep(tick)
    if msg:
        print("\n  [wait_until_false 超时 %.1fs] %s｜最后一次条件结果=%r" % (timeout, msg, val))
    return False


class SFCase(unittest.TestCase):
    """新用例建议继承的基类。

    提供的能力刻意保持克制 —— Qt 在离屏环境下的析构很敏感（历史上出现过
    show() 之后 processEvents() 直接 SIGSEGV），所以这里**不做**全进程级别的
    Qt 事件泵或 widget 清扫，只关掉用例自己显式登记的窗口。
    """

    def setUp(self):
        self._sf_widgets = []
        self._sf_patches = []

    def tearDown(self):
        # closeEvent may persist state, so close widgets while their isolation
        # patches are still active.  The marker keeps delayed offscreen close
        # events from saving a second time after this method returns.
        for w in reversed(self._sf_widgets):
            try:
                w._exit_persisted = True  # discipline-ok(R5): closeEvent 幂等标记没有公开入口。
                w.close()
            except Exception:
                pass
        self._sf_widgets = []
        for p in reversed(self._sf_patches):
            try:
                p.stop()
            except Exception:
                pass
        self._sf_patches = []

    # ---- 登记式辅助：只管自己创建的东西 ----
    def track_widget(self, w):
        """登记窗口，tearDown 时自动关闭（避免跨用例残留窗口堆叠）。"""
        self._sf_widgets.append(w)
        return w

    def track_patch(self, p):
        """登记 patch，tearDown 时自动 stop（忘记 stop 是 Mock 泄漏的头号原因）。"""
        p.start()
        self._sf_patches.append(p)
        return p

    @classmethod
    def ensure_qapp(cls):
        """复用全局 QApplication —— 与存量用例同口径，不再各写一遍。"""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        return app

    def wait_for(self, cond, timeout=2.0, tick=0.01, msg=""):
        return wait_for(cond, timeout=timeout, tick=tick, msg=msg)

    def wait_until_false(self, cond, timeout=2.0, tick=0.01, msg=""):
        return wait_until_false(cond, timeout=timeout, tick=tick, msg=msg)


@contextlib.contextmanager
def fixed_clock(dt=None, module_names=("sf_quote", "sf_scheduler")):
    """把"现在几点"固定住 —— 取代写死日期的断言。

    背景：9 处测试用 `datetime(2026, 2, 18)` 之类固定日期判断"是否交易时段"，
    一旦节假日表更新或跨年，这类断言会失效，而报错完全看不出是日期依赖。

    用法：
        with fixed_clock(datetime.datetime(2026, 2, 18, 10, 0)):
            self.assertEqual(sf_quote.market_status()[0], "交易中")

    注意：只 patch 传入模块里名为 `_beijing_now` 的函数（项目统一入口），
    模块不存在或无该函数时静默跳过 —— 辅助工具不应把用例拖垮。
    """
    import datetime
    import sys
    from unittest import mock

    now = dt
    if isinstance(now, (tuple, list)):
        now = datetime.datetime(*now)
    patches = []
    for name in module_names:
        mod = sys.modules.get(name)
        if mod is None or not hasattr(mod, "_beijing_now"):
            continue
        patches.append(mock.patch.object(mod, "_beijing_now",
                                         return_value=now))
    for p in patches:
        p.start()
    try:
        yield now
    finally:
        for p in reversed(patches):
            p.stop()
