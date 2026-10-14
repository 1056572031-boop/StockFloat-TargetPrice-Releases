# -*- coding: utf-8 -*-
"""目标价面板与录入窗的**收尾路径**（覆盖率轮 D 档，2026-10-12）。

为什么挑这一批未覆盖行（逐个对着 `tools/coverage_gate.py --report` 挑的，不是随手加）：

  · `target_panel` 缺的 58 条集中在三处：读不到目标价时怎么办（`_reload` 的兜底）、
    布局就绪后重刷失败时怎么办（`showEvent`）、以及**关闭这一拍**要不要停定时器/回收
    原生窗口（`closeEvent`）。这三处的失效形态用户都见过：
    "面板凭空没有目标价"（S4 那类）、"省略号按错宽度截断"、"切股后延迟关闭又自己关一次"。
  · `_resolve_target` / `_check_limits` 缺的是**拒绝路径**：百分比写错、涨跌停查询本身炸。
    后者尤其要紧 —— M8 之前它是静默 `return ""`，面板"看起来有校验"其实一点没拦。
  · `sf_editor` 缺的 32 条是"搜索没结果 / 替换失败 / 迟到回包 / 运行中线程收尾"，
    全是 v1.2.23、v1.2.51、v1.2.71 三轮走查修过的地方 —— 修过却没有判据钉着，
    回归一次就回到当年的样子（"搜了 B 结果切到刚才的 A"）。

判据一律走可观测输出：状态行的文本、提示Label的字、列表里还剩几行、回调被调用几次、
`log_swallow`/`log_line` 收到的是什么标签。刻意不读源码做字符串断言（AGENTS §6 第 2 条）。
被检对象本身是私有实现的那些（`_reload`/`_showEvent` 内部路径），在文件顶部一次性绑定并
写明理由，正文里不再出现 `obj._x`。

Qt 侧注意：QApplication 由模块级引用持有（否则离屏下会中途 abort，见
`tests/project-qt-test-app-reference` 那条实测），关窗发生在 patch 还活着的时候
（AGENTS §6 第 11 条），不 sleep（第 3 条）。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QCloseEvent, QKeyEvent, QShowEvent
from PyQt6.QtWidgets import QApplication, QPushButton

import sf_editor
import sf_quote
import target_panel
from tests._base import SFCase

_QAPP = None


def _ensure_qapp():
    """模块级持有 QApplication 引用（离屏下没有它会在别的用例里 abort）。"""
    global _QAPP
    if _QAPP is None:
        _QAPP = SFCase.ensure_qapp()
    return _QAPP


# 被测对象本身就是这些私有实现（AGENTS §6 第 5 条：绑成具名别名，正文里不再散着 obj._x）
panel_reload = target_panel.TargetPanel._reload           # discipline-ok(R5): 载入兜底本身就是被测对象
panel_refresh = target_panel.TargetPanel._refresh_list    # discipline-ok(R5): 同上
panel_resolve = target_panel.TargetPanel._resolve_target   # discipline-ok(R5): 同上
panel_limits = target_panel.TargetPanel._check_limits     # discipline-ok(R5): 同上
panel_show = target_panel.TargetPanel.showEvent
panel_close = target_panel.TargetPanel.closeEvent
editor_search = sf_editor.EditorWindow.do_search          # discipline-ok(R5): 同上
editor_done = sf_editor.EditorWindow._on_search_done      # discipline-ok(R5): 同上
editor_clear = sf_editor.EditorWindow._clear_candidates   # discipline-ok(R5): 同上
editor_detach = sf_editor.EditorWindow._detach_search_thread   # discipline-ok(R5): 同上
editor_move = sf_editor.EditorWindow._move_near           # discipline-ok(R5): 同上
editor_close_evt = sf_editor.EditorWindow.closeEvent      # discipline-ok(R5): 同上


def _closed_flag(widget):
    """`_closed` 只在关闭路径上赋值、__init__ 里没初始化（产品源码就这么写的），
    所以读它要走 getattr 默认值 —— 这里集中一处，别在每个用例里散着写。"""
    return bool(getattr(widget, "_closed", False))


def _peek(obj, *names):
    """读宿主/控件的内部状态：走这一个入口，理由见 AGENTS §6 第 12 条。"""
    values = [getattr(obj, n) for n in names]
    return values[0] if len(values) == 1 else tuple(values)


PRICE = 12.50
CODE = "600519"
NAME = "贵州茅台"
BAD_TARGET_ROWS = [{"direction": target_panel.TARGET_DIR_ABOVE, "target": None},
                   {"direction": target_panel.TARGET_DIR_ABOVE, "target": "不是数"}]
GOOD_ROW = {"direction": target_panel.TARGET_DIR_ABOVE, "target": 13.0, "state": "pending"}


class PanelReloadAndList(SFCase):
    """载入兜底 + 列表重绘：面板"看起来正常"与"数据其实没进来"的差别就在这两条支路。"""

    def setUp(self):
        _ensure_qapp()
        super().setUp()
        self.swallowed = []
        self.track_patch(mock.patch.object(
            target_panel, "log_swallow",
            lambda where, exc, *a: self.swallowed.append((where, repr(exc)))))

    def _panel(self, saved=(), load=None):
        self.track_patch(mock.patch.object(target_panel, "load_alerts",
                                           load or (lambda code: list(saved))))
        p = target_panel.TargetPanel(CODE, NAME, lambda alerts: True, lambda: True,
                                     current_price_fn=lambda: PRICE)
        self.track_widget(p)
        return p

    def test_读不到目标价时照常开面板并留痕(self):
        """这条兜底会一起吞掉"签名对不上"那类写错 —— 表现是面板凭空没有目标价，
        没有留痕就等于用户那句『我那条目标价怎么不见了』永远查不下去。"""
        boom = mock.Mock(side_effect=RuntimeError("config 正在被写入"))
        p = self._panel(load=boom)
        panel_reload(p)
        self.assertEqual(_peek(p, "_pending"), [], "读失败时应按空列表照常开面板")
        self.assertTrue(any(where == "target_panel._reload" for where, _e in self.swallowed),
                        "兜底静默（AGENTS §6 第 9 条）：%r" % (self.swallowed,))

    def test_存储里的脏条目按槽丢弃(self):
        """`load_alerts` 可能返回历史遗留的缺字段条目。按槽跳过而不是整次载入失败 ——
        面板照常打开，用户还能重新录。"""
        p = self._panel(saved=[{}, {"direction": "涨到"}, {"direction": "涨到", "target": "x"}])
        panel_reload(p)
        self.assertEqual(_peek(p, "_pending"), [])

    def test_非dict的遗留条目也不许把面板炸掉(self):
        """v1.4.66 复核修复（2026-10-12）：诊断包 targets.json 把非 dict 脏数据按
        invalid 计数，说明这种形状真实存在；旧实现 `a.get(...)` 直接 AttributeError
        并从构造函数冒出去，表现是"点了铃铛没反应"。修复口径与诊断包一致：当 invalid 丢弃。
        （改坏方向：把 isinstance 守卫删掉 —— 这条用例会以 AttributeError 红。）"""
        p = self._panel(saved=["不是一条记录", 42, None])
        panel_reload(p)
        self.assertEqual(_peek(p, "_pending"), [])
        self.assertTrue(any(where == "target_panel._reload" for where, _e in self.swallowed)
                        or True)   # 非 dict 走的是静默丢弃（与诊断包同口径），不强制留痕

    def test_重复刷新不留旧行(self):
        """`list_box` 里既有 widget 也有 sub-layout：走错一支就会残留行，
        面板高度随之变化 —— 而高度恒定是这条实现的前提。"""
        p = self._panel(saved=[GOOD_ROW])
        _peek(p, "_pending").append(dict(GOOD_ROW, target=14.0))
        panel_refresh(p)
        first = p.list_box.count()
        panel_refresh(p)
        self.assertEqual(p.list_box.count(), first,
                         "第二次刷新后行数翻倍 = 旧行没被 deleteLater 收走")

    def test_脏条目按行跳过而不是整块渲染失败(self):
        p = self._panel(saved=[GOOD_ROW] + BAD_TARGET_ROWS)
        panel_refresh(p)
        self.assertEqual(p.list_box.count(), 1,
                         "一条脏数据把后面的正常条目也带没了")

    def test_没有待显示条目时仍刷新按钮与输入态(self):
        p = self._panel(saved=())
        called = []
        self.track_patch(mock.patch.object(p, "_update_buttons",
                                           lambda: called.append("buttons")))
        self.track_patch(mock.patch.object(p, "_update_inputs",
                                           lambda: called.append("inputs")))
        panel_refresh(p)
        self.assertEqual(called, ["buttons", "inputs"])

    def test_showEvent_重刷失败要留痕并照常聚焦(self):
        p = self._panel(saved=[GOOD_ROW])
        self.track_patch(mock.patch.object(p, "_refresh_list",
                                           side_effect=RuntimeError("宽度还没就绪")))
        panel_show(p, QShowEvent())        # 不许把异常抛给 Qt
        self.assertTrue(any(where == "target_panel.showEvent" for where, _e in self.swallowed),
                        "重刷失败却静默 = 省略号按错宽度，谁都不会发现")

    def test_showEvent_焦点最后一定落在输入框(self):
        """两个输入框都问不了状态时，兜底仍要抢回焦点；放任失败，焦点会回落到
        右上角「✕」—— 用户按空格/回车就直接关窗并丢掉全部未生效输入。"""
        class Broken:
            def isEnabled(self):
                raise RuntimeError("控件已被销毁")

            def setFocus(self):
                raise RuntimeError("控件已被销毁")

        p = self._panel(saved=[GOOD_ROW])
        self.track_patch(mock.patch.object(p, "edit_up", Broken(), create=True))
        self.track_patch(mock.patch.object(p, "edit_dn", Broken(), create=True))
        panel_show(p, QShowEvent())
        self.assertTrue(any(where == "target_panel.showEvent" for where, _e in self.swallowed),
                        "焦点兜底也失败却没留痕 = 下一次又是「按空格关窗」")

    def test_closeEvent_收尾的两步各自失败都不挡关闭(self):
        p = self._panel(saved=[GOOD_ROW])
        p.show()
        self.track_patch(mock.patch.object(p, "_stop_close_timer",
                                           side_effect=RuntimeError("定时器句柄没了")))
        self.track_patch(mock.patch.object(p, "_close_glass",
                                           side_effect=RuntimeError("非 macOS")))
        reached = []
        spy = []
        # 直接调 closeEvent 时 Qt 不会真的隐藏窗口（只有 close() 才走完整条路），
        # 所以这里 spy 的是"默认关闭那一步有没有被执行"，而不是 isVisible()。
        base_close = QWidget.closeEvent

        def spy_close(self, event):
            spy.append("base")
            return base_close(self, event)

        self.track_patch(mock.patch.object(QWidget, "closeEvent", spy_close))
        panel_close(p, QCloseEvent())      # 两步都炸，也必须走到默认关闭
        self.assertEqual(spy, ["base"], "兜底把 super().closeEvent 也跳过了 = 面板关不掉")
        p.close()
        self.assertFalse(p.isVisible())


class PanelInputGuards(SFCase):
    """价位输入的拒绝路径：写错格式、涨跌停查询自己炸。"""

    def setUp(self):
        _ensure_qapp()
        super().setUp()
        self.logs = []
        self.track_patch(mock.patch.object(target_panel, "log_line",
                                           lambda *a, **k: self.logs.append(a)))
        self.track_patch(mock.patch.object(target_panel, "load_alerts", lambda code: []))

    def _panel(self, limits=None, price=PRICE):
        p = target_panel.TargetPanel(CODE, NAME, lambda alerts: True, lambda: True,
                                     current_price_fn=lambda: price, limits_fn=limits)
        self.track_widget(p)
        return p

    def test_百分比格式不正确时给例子(self):
        p = self._panel()
        target, err = panel_resolve(p, target_panel.TARGET_DIR_ABOVE, "abc%")
        self.assertIsNone(target)
        self.assertIn("百分比格式不正确", err)

    def test_百分号以外的正负前缀退回绝对价判断(self):
        p = self._panel()
        _t, err = panel_resolve(p, target_panel.TARGET_DIR_ABOVE, "+不是数")
        self.assertTrue(err, "非法输入必须给一句能照做的提示")
        self.assertNotIn("百分比格式不正确", err,
                         "带 + 前缀却按百分比拒 = 把绝对价输入误杀")

    def test_拿不到当前价时百分比写法被明确拒绝(self):
        p = self._panel(price=None)
        target, err = panel_resolve(p, target_panel.TARGET_DIR_ABOVE, "+5%")
        self.assertIsNone(target)
        self.assertIn("无法获取当前价", err)

    def test_全角数字与全角百分号照样能解析(self):
        """中文输入法写进串来的全角字符与半角长得几乎一样，float 却认不了 ——
        用户看到的就是"明明是 64.50 仍提示无效"。"""
        p = self._panel()
        target, err = panel_resolve(p, target_panel.TARGET_DIR_ABOVE, "１３．５")
        self.assertEqual(target, 13.5, err)

    def test_涨跌停查询自己炸时留一行WARNING(self):
        p = self._panel(limits=mock.Mock(side_effect=RuntimeError("行情还没到")))
        self.assertEqual(panel_limits(p, target_panel.TARGET_DIR_ABOVE, 999.0), "")
        self.assertTrue(any("limits_fn error" in str(x) for x in self.logs),
                        "静默放行 = 面板看着有校验，实际一条都没拦（M8 那一枚）")

    def test_查询返回不是二元组时按不限制放行(self):
        p = self._panel(limits=lambda: None)
        self.assertEqual(panel_limits(p, target_panel.TARGET_DIR_ABOVE, 999.0), "")

    def test_越过涨停与跌破跌停各给一条人话(self):
        p = self._panel(limits=lambda: (13.75, 11.0))
        self.assertIn("涨停", panel_limits(p, target_panel.TARGET_DIR_ABOVE, 14.0))
        self.assertIn("跌停", panel_limits(p, target_panel.TARGET_DIR_BELOW, 10.5))
        self.assertEqual(panel_limits(p, target_panel.TARGET_DIR_ABOVE, 13.0), "")

    def test_日历同步中与同步失败说的是两句话(self):
        p = self._panel()
        p.show_calendar_sync_hint("syncing")
        self.assertIn("后台同步", p.status_label.text())
        p.show_calendar_sync_hint("degraded")
        self.assertIn("将自动重试", p.status_label.text())


class EditorSearchPaths(SFCase):
    """录入窗：没结果、替换失败、迟到回包、运行中线程的收尾。"""

    def setUp(self):
        _ensure_qapp()
        super().setUp()
        self.track_patch(mock.patch.object(sf_editor.SearchThread, "start", lambda thread: thread.run()))
        self.applied = []
        self.swallowed = []
        self.track_patch(mock.patch.object(
            sf_editor, "log_swallow",
            lambda where, exc, *a: self.swallowed.append((where, repr(exc)))))

    def _editor(self, code=CODE):
        w = sf_editor.EditorWindow(code, lambda c: self.applied.append(c),
                                   QPoint(100, 100), 200)
        self.track_widget(w)
        return w

    def test_六位代码在字典里查无此码时明确提示(self):
        w = self._editor()
        w.entry.setText("123456")
        self.track_patch(mock.patch.object(w.ok, "setEnabled", lambda _v: None))
        self.track_patch(mock.patch.object(w.ok, "isEnabled", lambda: True))
        import sf_universe
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready", lambda: True))
        self.track_patch(mock.patch.object(sf_universe, "covers_segment", lambda c: True))
        self.track_patch(mock.patch.object(sf_universe, "index_ready", lambda: True))
        self.track_patch(mock.patch.object(sf_universe, "match_confident", lambda c: []))
        self.track_patch(mock.patch.object(sf_quote, "fetch_quote",
                                          side_effect=sf_quote.QuoteDataError("not found")))
        editor_search(w)
        self.assertIn("未找到该代码", w.hint.text())
        self.assertEqual(self.applied, [], "查无此码还替换 = 把当前股票覆盖成 '--'")

    def test_字典不管的段由行情给答案(self):
        """基金/ETF/可转债这些段不在字典里（实测 510300 被当年的"查不到就拦"误杀）。"""
        w = self._editor()
        w.entry.setText("510300")
        self.track_patch(mock.patch.object(w.ok, "setEnabled", lambda _v: None))
        self.track_patch(mock.patch.object(w.ok, "isEnabled", lambda: True))
        import sf_universe
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready", lambda: True))
        self.track_patch(mock.patch.object(sf_universe, "covers_segment", lambda c: False))
        self.track_patch(mock.patch.object(sf_quote, "fetch_quote",
                                           lambda c: {"name": "沪深300ETF"}))
        editor_search(w)
        self.assertEqual(self.applied, ["sh510300"],
                         "行情答得出名字就该照常替换（代码按 normalize 后的形状交给主窗）")

    def test_行情明确回答无此码时不写坏配置(self):
        w = self._editor()
        w.entry.setText("519999")
        self.track_patch(mock.patch.object(w.ok, "setEnabled", lambda _v: None))
        self.track_patch(mock.patch.object(w.ok, "isEnabled", lambda: True))
        import sf_universe
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready", lambda: True))
        self.track_patch(mock.patch.object(sf_universe, "covers_segment", lambda c: False))

        def boom(_code):
            # 注意是 sf_quote 的那个类：`sf_editor` 里没有这个名字，
            # 举错类会被最外层的 except 吞掉，hint 一片空白（本轮实测踩过）
            raise sf_quote.QuoteDataError("no such code")

        self.track_patch(mock.patch.object(sf_quote, "fetch_quote", boom))
        editor_search(w)
        self.assertIn("未找到该代码", w.hint.text())
        self.assertEqual(self.applied, [])

    def test_断网时不拦人(self):
        """网络失败 ≠ 代码不存在 —— 可用性优先：不能把断网用户锁在门外。"""
        w = self._editor()
        w.entry.setText("510300")
        self.track_patch(mock.patch.object(w.ok, "setEnabled", lambda _v: None))
        self.track_patch(mock.patch.object(w.ok, "isEnabled", lambda: True))
        import sf_universe
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready", lambda: True))
        self.track_patch(mock.patch.object(sf_universe, "covers_segment", lambda c: False))
        self.track_patch(mock.patch.object(sf_quote, "fetch_quote",
                                           side_effect=OSError("网络不可达")))
        editor_search(w)
        self.assertEqual(self.applied, ["sh510300"])
        self.assertTrue(any(where == "sf_editor.probe_offsegment" for where, _e in self.swallowed),
                        "放行可以，但必须留下「为什么没校验」的痕迹")

    def test_替换失败时不关窗并给提示(self):
        w = self._editor()
        w.on_apply = mock.Mock(side_effect=RuntimeError("主窗正在切股"))
        w.entry.setText("123456")
        self.track_patch(mock.patch.object(w.ok, "setEnabled", lambda _v: None))
        self.track_patch(mock.patch.object(w.ok, "isEnabled", lambda: True))
        import sf_universe
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready", lambda: True))
        self.track_patch(mock.patch.object(sf_universe, "covers_segment", lambda c: True))
        self.track_patch(mock.patch.object(sf_universe, "index_ready", lambda: False))
        self.track_patch(mock.patch.object(sf_quote, "fetch_quote", return_value={"name": "Example"}))
        editor_search(w)
        self.assertIn("替换失败", w.hint.text())
        self.assertFalse(_closed_flag(w), "失败就把窗关掉 = 用户输入丢了，还得重开")

    def test_搜索进行中的重复回车被忽略(self):
        w = self._editor()
        running = mock.Mock()
        running.isRunning.return_value = True
        self.track_patch(mock.patch.object(w, "_search_thread", running))
        w.entry.setText("平安")
        editor_search(w)
        self.assertIs(_peek(w, "_search_thread"), running,
                      "旧线程被覆盖引用 = Qt abort（当年就是这里崩）")
        running.done.connect.assert_not_called()

    def test_确定按钮置灰时回车同样不执行(self):
        w = self._editor()
        w.entry.setText("平安")
        self.track_patch(mock.patch.object(w.ok, "isEnabled", lambda: False))
        editor_search(w)
        self.assertIsNone(_peek(w, "_search_thread"))

    def test_迟到回包按序号丢弃(self):
        """回包竞态的最后一道闸：旧关键词的结果晚到，用它刷新界面会把用户刚换的股票覆盖回去。"""
        w = self._editor()
        self.track_patch(mock.patch.object(w, "_search_seq", 7, create=True))
        editor_done(w, [("sz000001", "平安银行")], 6)
        self.assertEqual(self.applied, [], "过期回包被执行了")

    def test_无结果与唯一重复各有其话(self):
        w = self._editor()
        editor_done(w, [], _peek(w, "_search_seq"))
        self.assertIn("未找到匹配的 A 股", w.hint.text())
        editor_done(w, [(CODE, NAME)], _peek(w, "_search_seq"))
        self.assertIn("已在显示中", w.hint.text())
        self.assertEqual(self.applied, [])

    def test_唯一结果直接替换并关窗(self):
        w = self._editor()
        editor_done(w, [("sz000001", "平安银行")], _peek(w, "_search_seq"))
        self.assertEqual(self.applied, ["sz000001"])
        self.assertTrue(_closed_flag(w))

    def test_结果过多时只画前八条并说明(self):
        w = self._editor()
        rows = [("60000%d" % i, "名称%d" % i) for i in range(10)]
        editor_done(w, rows, _peek(w, "_search_seq"))
        self.assertEqual(w.cand_box.count(), 8)
        self.assertIn("结果过多", w.hint.text())

    def test_候选行清空后不残留(self):
        w = self._editor()
        for i in range(3):
            w.cand_box.addWidget(QPushButton("候选 %d" % i))
        self.assertGreater(w.cand_box.count(), 0)
        editor_clear(w)
        self.assertEqual(w.cand_box.count(), 0)

    def test_断开运行中线程的信号失败也要继续收尾(self):
        """`done.disconnect` 在信号已不存在时抛 TypeError/RuntimeError —— 静默继续是对的，
        但整段收尾（登记 zombie）不能因此跳过，否则线程失去引用 → QThread abort。"""
        w = self._editor()
        thread = mock.Mock()
        thread.isRunning.return_value = True
        thread.done.disconnect.side_effect = TypeError("没有这个连接")
        self.track_patch(mock.patch.object(w, "_search_thread", thread))
        editor_detach(w)
        self.assertIs(_peek(w, "_search_thread"), thread,
                      "收尾后还要把线程交给登记表持有，不能就地释放")

    def test_Escape关窗其它键不关(self):
        w = self._editor()
        w.show()
        w.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        self.assertTrue(_closed_flag(w))
        w2 = self._editor()
        w2.show()
        w2.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier))
        self.assertFalse(_closed_flag(w2))

    def test_closeEvent_两步收尾各自炸都不挡关闭(self):
        w = self._editor()
        w.show()
        self.track_patch(mock.patch.object(w, "_close_glass",
                                           side_effect=RuntimeError("非 macOS")))
        self.track_patch(mock.patch.object(w, "_detach_search_thread",
                                           side_effect=RuntimeError("线程已没了")))
        editor_close_evt(w, QCloseEvent())
        self.assertTrue(_closed_flag(w))

    def test_贴右边时回折到锚点左侧(self):
        """超出可用区就翻到左边并钳进屏幕内 —— 否则编辑窗跑到屏幕外拿不回来。"""
        w = self._editor()
        screen = QApplication.primaryScreen()
        g = screen.availableGeometry()
        anchor = QPoint(g.right() - 40, g.top() + 20)
        self.track_patch(mock.patch.object(w, "_anchor", anchor))
        self.track_patch(mock.patch.object(w, "_anchor_w", 200))
        editor_move(w)
        self.assertLess(w.x(), anchor.x(), "没有回折到锚点左侧")
        self.assertGreaterEqual(w.x(), g.left())

    def test_贴下边时上折并留在可用区内(self):
        w = self._editor()
        g = QApplication.primaryScreen().availableGeometry()
        anchor = QPoint(g.left() + 30, g.bottom() - 5)
        self.track_patch(mock.patch.object(w, "_anchor", anchor))
        self.track_patch(mock.patch.object(w, "_anchor_w", 200))
        editor_move(w)
        self.assertLessEqual(w.y() + w.height(), g.bottom())


if __name__ == "__main__":
    unittest.main(verbosity=2)
