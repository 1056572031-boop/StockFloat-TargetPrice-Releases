# -*- coding: utf-8 -*-
"""实时股价 第1轮 BUG 走查修复回归测试。

覆盖项（对应本轮走查修复，风格对齐 test_bugfix_148/151）：
    R1-1  sf_quote.get_last_quote_source() 实时读取最近成功行情来源（消除导入快照）；
    R1-2  _export_diagnostics 的 meta 经 getter 实时读取数据源/HTTP 状态（源码级防回归）；
    R1-3  _apply_quote 的动画调优开关名为 anim_params（原 anim_tune 不在开关表，恒 False，
          导致微小直赋/130ms/200ms 调优从未生效）；
    R1-4  _chart_minute_offset 对带冒号时间串（备用源 "09:30"）防御归一，
          不再退化按序号均分（v1.2.2 缺陷复活防护）；
    R1-5  EditorWindow.closeEvent 统一收尾：不走 _close 的关闭路径（开关式关闭/退出）
          同样断开 done 信号并把运行中搜索线程登记进模块级 zombie 表，
          杜绝 QThread 运行中析构/迟到信号投递已删除对象导致的 abort；
    R1-6  公开窗口行为为基础，私有 CGS 作为第三方系统全屏兼容补充。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import inspect
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint,  QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

import sf_editor
import sf_glass
import sf_quote
import sf_ui_render


# =====================================================================
# R1-1：行情来源 getter 实时读取
# =====================================================================
class TestQuoteSourceGetter(unittest.TestCase):
    def test_getter_reflects_latest_source(self):
        sf_quote._LAST_QUOTE_SOURCE = "sina"
        try:
            self.assertEqual(sf_quote.get_last_quote_source(), "sina")
            sf_quote._LAST_QUOTE_SOURCE = "tencent"
            self.assertEqual(sf_quote.get_last_quote_source(), "tencent",
                             "getter 必须每次读取最新来源")
        finally:
            sf_quote._LAST_QUOTE_SOURCE = None


# =====================================================================
# R1-2：诊断导出经 getter 实时读取（源码级）
# =====================================================================
class TestDiagnosticsMetaGetters(unittest.TestCase):
    def test_export_diagnostics_uses_getters(self):
        import stock_float_window as m
        src = inspect.getsource(m.FloatWindow._export_diagnostics)
        self.assertIn("get_last_quote_source()", src,
                      "诊断导出必须经 getter 实时读取行情来源")
        self.assertIn("get_last_http_status()", src,
                      "诊断导出必须经 getter 实时读取 HTTP 状态")
        self.assertNotIn("_LAST_QUOTE_SOURCE or", src,
                         "不得再使用导入时快照变量")

    def test_main_module_no_snapshot_import(self):
        import stock_float_window as m
        src = inspect.getsource(m)
        self.assertNotIn("from sf_quote import _LAST_QUOTE_SOURCE", src,
                         "主窗不得再导入 _LAST_QUOTE_SOURCE 快照")
        self.assertNotIn("from sf_network import _LAST_HTTP_STATUS", src,
                         "主窗不得再导入 _LAST_HTTP_STATUS 快照")


# =====================================================================
# R1-3：动画调优开关名纠正（源码级）
# =====================================================================
class TestAnimParamsFlagName(unittest.TestCase):
    def test_apply_quote_uses_anim_params(self):
        import stock_float_window as m
        src = inspect.getsource(m.FloatWindow._apply_quote)
        self.assertIn('feature_enabled("anim_params")', src,
                      "动画调优开关必须为 anim_params（在 _FEATURE_DEFAULTS 内）")
        self.assertNotIn('feature_enabled("anim_tune")', src,
                         "anim_tune 不在开关表，恒 False，不得再使用")


# =====================================================================
# R1-4：分时 X 轴换算对带冒号时间串的防御
# =====================================================================
class TestMinuteOffsetColonTolerance(unittest.TestCase):
    def test_colon_format_maps_same_as_plain(self):
        f = sf_ui_render._chart_minute_offset
        # 备用源（东财）"HH:MM" 与主源 "HHMM" 换算结果必须一致
        self.assertEqual(f("09:30", 0, 3), f("0930", 0, 3))
        self.assertEqual(f("0930", 0, 3), 0)        # 开盘 → 0
        self.assertEqual(f("10:00", 1, 3), 30)      # 上午段
        self.assertEqual(f("11:30", 2, 3), 120)     # 午休点
        self.assertEqual(f("13:00", 3, 6), 120)     # 下午开盘
        self.assertEqual(f("15:00", 5, 6), 240)     # 收盘

    def test_colon_format_never_falls_back_to_index(self):
        """带冒号串绝不能走 except 的按序号均分回退（与序号值区分验证）。"""
        f = sf_ui_render._chart_minute_offset
        # 若退化回退，返回值 = i*240/(n-1)，与真实分钟偏移不同
        self.assertEqual(f("10:00", 0, 10), 30)
        self.assertEqual(f("14:00", 0, 10), 120 + 60)


# =====================================================================
# R1-5：编辑器 closeEvent 统一收尾（不走 _close 的关闭路径）
# =====================================================================
class _StubSearchThread(QObject):
    """SearchThread 桩：可连接的 done/finished 信号，不启动真实线程。"""

    done = pyqtSignal(list)
    finished = pyqtSignal()

    def __init__(self, running=True):
        super().__init__()
        self._running = running

    def isRunning(self):
        return self._running

    def start(self):
        pass


# v1.4.33 复核第 2 项：原来这里是一个只有 x()/y() 的鸭子类型替身。
# `EditorWindow._move_near()` 会把锚点交给 `QGuiApplication.screenAt(...)`，
# 传非 QPoint 会抛 TypeError，而那段外面有兜异常 —— 于是**整段"按所在屏幕
# 定位 + 越界回折 + 钳制"的逻辑一次也没执行过**，只在日志里留下一条
# `SWALLOWED sf_editor._move_near`，看起来像产品缺陷。改成真 QPoint 之后，
# 这条路径是真的在跑，下面的用例也就能断言定位结果了。


class TestEditorCloseEventRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make_editor(self):
        applied = []
        ed = sf_editor.EditorWindow("sh600519", lambda c: applied.append(c),
                                    QPoint(0, 0), 200)
        return ed, applied

    def test_move_near_lands_inside_the_screen_and_clear_of_the_card(self):
        """`_move_near` 真的执行完（不再被 TypeError 短路）：位置必须可用且不重叠。

        守的是 AGENTS 里那条"静默兜异常会让功能永久失效而没人知道"：
        以前这个替身让整段定位逻辑空转，测试却照样绿。
        """
        from PyQt6.QtGui import QGuiApplication
        ed, _ = self._make_editor()
        # 这两个内部成员没有公开入口：锚点宽度由宿主窗口注入，定位函数本身
        # 就是被测对象（AGENTS §6 第 5 条的"确有必要"情形，按规矩就地标注）。
        ed._anchor_w = 184          # discipline-ok(R5): 锚点宽度无公开写入口
        ed._move_near()             # discipline-ok(R5): 被测函数即此私有方法
        g = (QGuiApplication.screenAt(QPoint(0, 0))
             or QGuiApplication.primaryScreen()).availableGeometry()
        x, y = ed.x(), ed.y()
        self.assertGreaterEqual(x, g.left() + 8, "定位越出屏幕左边界")
        self.assertGreaterEqual(y, g.top() + 8, "定位越出屏幕上边界")
        self.assertLessEqual(x + ed.width(), g.right() - 8 + ed.width(),
                             "回折逻辑没生效：窗口右缘跑到屏幕外")
        self.assertGreater(x, 184 + 12 - 1,
                           "没有落在锚点右侧（184 宽 + 12 间距）说明整段被跳过")

    def tearDown(self):
        # 清理 zombie 表中的桩，避免污染其他用例
        sf_editor._zombie_threads[:] = [
            t for t in sf_editor._zombie_threads
            if not isinstance(t, _StubSearchThread)]

    def test_bare_close_registers_running_thread(self):
        """直接 close()（开关式关闭/退出路径，不经 _close）也必须安全收尾。"""
        ed, _ = self._make_editor()
        st = _StubSearchThread(running=True)
        ed._search_thread = st
        st.done.connect(ed._on_search_done)
        ed.close()   # 不走 _close：closeEvent 统一收尾
        self.assertIn(st, sf_editor._zombie_threads,
                      "运行中线程必须登记进模块级 zombie 表（防运行中析构 abort）")
        self.assertEqual(st.receivers(st.done), 0,
                         "close 后 done 信号必须已断开（防投递已删除对象）")

    def test_close_event_source_routes_detach(self):
        src = inspect.getsource(sf_editor.EditorWindow.closeEvent)
        self.assertIn("_detach_search_thread", src,
                      "closeEvent 必须复用幂等的线程收尾逻辑")

    def test_prune_never_drops_running_threads_over_soft_limit(self):
        class Running:
            def isRunning(self):
                return True
        old = list(sf_editor._zombie_threads)
        try:
            running = [Running() for _ in range(sf_editor._ZOMBIE_THREADS_MAX + 2)]
            sf_editor._zombie_threads[:] = running
            sf_editor._prune_zombie_threads()
            self.assertEqual(sf_editor._zombie_threads, running)
        finally:
            sf_editor._zombie_threads[:] = old

    def test_close_idempotent_detach(self):
        """_close 后再 close（closeEvent 二次收尾）不得重复登记/崩溃。"""
        ed, _ = self._make_editor()
        st = _StubSearchThread(running=True)
        ed._search_thread = st
        st.done.connect(ed._on_search_done)
        ed._close()
        count_after_close = sf_editor._zombie_threads.count(st)
        self.assertEqual(count_after_close, 1)
        # WA_DeleteOnClose 下 _close 已 close()，此处仅验证收尾方法幂等
        ed._detach_search_thread()
        self.assertEqual(sf_editor._zombie_threads.count(st), 1,
                         "收尾必须幂等，不得重复登记")


# =====================================================================
# R1-6：公开窗口行为（行为与源码边界）
# =====================================================================
class TestPublicSpaceApiGuardrails(unittest.TestCase):

    def test_public_behavior_combines_required_apple_flags(self):
        class _AppKit:
            NSWindowCollectionBehaviorCanJoinAllSpaces = 1
            NSWindowCollectionBehaviorStationary = 16
            NSWindowCollectionBehaviorFullScreenAuxiliary = 256

        with patch.dict(sys.modules, {"AppKit": _AppKit}):
            behavior = sf_glass.public_overlay_collection_behavior()
        self.assertEqual(behavior, 273,
                         "公开组合必须覆盖所有桌面、Mission Control 与全屏辅助窗口")

    def test_main_overlay_path_keeps_public_behavior_and_cgs_fallback(self):
        src = inspect.getsource(sf_glass)
        self.assertIn("public_overlay_collection_behavior()", src)
        self.assertIn("CGSAddWindowsToSpaces", src)
        self.assertIn("CGSGetActiveSpace", src)
        self.assertIn("CGSMainConnectionID", src)
        self.assertIn("_CGS_REPROBE_INTERVAL", src)

    def test_cgs_failure_is_non_fatal_and_retries_later(self):
        """私有 CGS 不可用时：不抛异常、连续失败后暂停 60 秒、到点重新计数。

        故障注入点必须是 `sf_glass._cgs_libs`（本模块唯一的库句柄入口），
        **不能**再 patch `ctypes.cdll.LoadLibrary`：v1.4.10 起句柄被进程内缓存
        （_CGS_LIBS），只要前面任何用例真的加载过一次，LoadLibrary 就再也不被调用，
        这个 patch 会静默失效 —— 实测随机顺序下本用例因此误红
        （`_cgs_ok` 为 True：私有路径其实成功了）。守的是"不可用时的退避行为"，
        不是"有没有人去 dlopen"。
        """
        class _Window:
            def windowNumber(self):
                return 42

        host = type("_Host", (), {
            "_cgs_ok": None,
            "_cgs_fail": 0,
            "_cgs_disabled_at": 0.0,
        })()
        with patch("sf_glass._cgs_libs", side_effect=OSError("unavailable")):
            for _ in range(3):
                sf_glass.GlassMixin._force_join_active_space(host, _Window())
        self.assertFalse(host._cgs_ok)
        host._cgs_disabled_at = 0.0
        with patch("sf_glass.time.monotonic", return_value=61.0), \
             patch("sf_glass._cgs_libs", side_effect=OSError("unavailable")):
            sf_glass.GlassMixin._force_join_active_space(host, _Window())
        self.assertEqual(host._cgs_fail, 1, "达到重探间隔后必须重新开始计数")


if __name__ == "__main__":
    unittest.main(verbosity=2)
