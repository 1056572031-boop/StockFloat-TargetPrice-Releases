# -*- coding: utf-8 -*-
"""替换股票第二轮：交易所前缀写错时的模糊搜索、候选省略与键盘、代码规范化。

用户要求：前缀写错（sh000001、bj600519）要支持模糊搜索，搜到的股票、指数都列出来
让用户选。判据落在可观测结果上：候选列表里有什么、有没有自动替换、主窗收到哪个代码。
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QFontMetrics, QKeyEvent
from PyQt6.QtTest import QTest

import sf_config
import sf_editor
import sf_quote
import sf_universe
from tests._base import SFCase

build_index = sf_universe._index  # discipline-ok(R5): 换一份合成字典，不读文件、不联网

ROWS = [("sz000001", "平安银行"), ("sh600519", "贵州茅台"),
        ("bj920185", "贝特瑞"), ("sz300750", "宁德时代")]


class UniverseDigitsCase(SFCase):
    def setUp(self):
        super().setUp()
        for name, value in (("_DISABLED", False), ("_rows", []),
                            ("_by_code", {}), ("_by_name", {})):
            self.track_patch(mock.patch.object(sf_universe, name, value))
        build_index(ROWS)

    def test_match_digits_ignores_the_exchange_prefix(self):
        self.assertEqual(sf_universe.match_digits("000001"), [("sz000001", "平安银行")])
        self.assertEqual(sf_universe.match_digits("6005"), [("sh600519", "贵州茅台")])
        self.assertEqual(sf_universe.match_digits("0", limit=1), [("sz000001", "平安银行")])

    def test_match_digits_returns_nothing_without_digits_or_when_disabled(self):
        self.assertEqual(sf_universe.match_digits(""), [])
        with mock.patch.object(sf_universe, "_DISABLED", True):
            self.assertEqual(sf_universe.match_digits("000001"), [])

    def test_prefixed_digits_accepts_any_number_of_digits(self):
        cases = {"sh6005": ("sh", "6005"), "SZ 0000": ("sz", "0000"), "6005": ("", "6005"),
                 "ｂｊ９２０": ("bj", "920"), "sh600519": ("sh", "600519")}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sf_universe.prefixed_digits(text), expected)
        for text in ("贵州", "", "sh", "6005a"):
            with self.subTest(text=text):
                self.assertIsNone(sf_universe.prefixed_digits(text))

    def test_code_shape_accepts_the_suffix_spelling_brokers_copy(self):
        """券商软件复制出来的 600519.SH 不能被当成名称去搜。"""
        cases = {"600519.SH": ("sh", "600519"), "600519.sz": ("sz", "600519"),
                 "sh.600519": ("sh", "600519"), "SH600519.SH": ("sh", "600519")}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sf_universe.code_shape(text), expected)
        self.assertIsNone(sf_universe.code_shape("sz600519.sh"), "前后交易所不一致不是代码")



SMARTBOX = ('v_hint="sh~000001~上证指数~szzs~ZS^sz~000001~平安银行~payh~GP-A'
            '^jj~000001~华夏成长混合~hxcz~KJ^hk~00001~长和~ch~GP^sz~000001~short"')


class SearchByDigitsCase(SFCase):
    """search_by_digits：同号的股票、指数、ETF/LOF 都列出来，不收场外基金。"""

    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(sf_universe, "_DISABLED", True))
        # track_patch 返回的是补丁对象，桩本身要在启动后从模块上取。
        self.track_patch(mock.patch.object(sf_quote, "http_get", return_value=SMARTBOX))
        self.track_patch(mock.patch.object(sf_quote, "eastmoney_search_bj", return_value=[]))
        self.http = sf_quote.http_get
        self.bj = sf_quote.eastmoney_search_bj

    def test_lists_the_index_and_the_stock_with_the_same_digits(self):
        got = sf_quote.search_by_digits("000001")
        self.assertEqual(got, [("sh000001", "上证指数"), ("sz000001", "平安银行")])

    def test_the_typed_prefix_goes_first(self):
        self.assertEqual([c for c, _ in sf_quote.search_by_digits("000001", "sz")],
                         ["sz000001", "sh000001"])

    def test_only_bare_digits_are_sent_to_the_interface(self):
        """腾讯不认带前缀的写法（sh000001 查回 N），请求里只能有数字。"""
        sf_quote.search_by_digits("000001", "sh")
        self.assertIn("q=000001&", self.http.call_args.args[0])
        self.assertNotIn("sh000001", self.http.call_args.args[0])

    def test_invalid_digits_never_reach_the_network(self):
        for digits in ("", None, "٦٠٠", "60a", "sh600"):
            with self.subTest(digits=digits):
                self.assertEqual(sf_quote.search_by_digits(digits), [])
        self.http.assert_not_called()

    def test_falls_back_to_eastmoney_when_the_interface_has_nothing(self):
        """北交所不在腾讯里，腾讯没结果再问东财。"""
        self.http.return_value = 'v_hint="N"'
        self.bj.return_value = [("bj920185", "贝特瑞"), ("bj920999", "不相干")]
        self.assertEqual(sf_quote.search_by_digits("920185"), [("bj920185", "贝特瑞")])

    def test_falls_back_to_the_local_dictionary_when_offline(self):
        self.http.side_effect = OSError("offline")
        with mock.patch.object(sf_universe, "ensure_ready"), \
                mock.patch.object(sf_universe, "_DISABLED", False), \
                mock.patch.object(sf_universe, "_rows", []), \
                mock.patch.object(sf_universe, "_by_code", {}), \
                mock.patch.object(sf_universe, "_by_name", {}):
            build_index(ROWS)
            self.assertEqual(sf_quote.search_by_digits("000001"),
                             [("sz000001", "平安银行")])

    def test_the_dictionary_is_loaded_before_the_offline_fallback_reads_it(self):
        """刚启动就断网：字典还在磁盘上，没装载就去读等于什么都列不出。"""
        self.http.side_effect = OSError("offline")
        with mock.patch.object(sf_universe, "ensure_ready") as ready:
            sf_quote.search_by_digits("000001")
        ready.assert_called_once_with()

    def test_results_are_deduplicated_and_capped(self):
        rows = "^".join("sh~%06d~名称%d~x~GP-A" % (i, i) for i in range(20))
        self.http.return_value = 'v_hint="%s^sh~000000~重复~x~GP-A"' % rows
        with mock.patch.object(sf_quote, "SEARCH_MAX_RESULTS", 5):
            got = sf_quote.search_by_digits("0")
        self.assertEqual(len(got), 5)
        self.assertEqual(len({c for c, _ in got}), 5)



class ResolveCodeInputCase(SFCase):
    """resolve_code_input：写错前缀要找出同号候选；条数交给窗口决定（一条替换，多条列出）。"""

    CANDIDATES = [("sh000001", "上证指数"), ("sz000001", "平安银行")]

    def setUp(self):
        super().setUp()
        self.rejects = []
        self.track_patch(mock.patch.object(sf_universe, "note_reject", self.rejects.append))
        self.track_patch(mock.patch.object(sf_editor, "search_by_digits", return_value=self.CANDIDATES))
        self.by_digits = sf_editor.search_by_digits
        self.track_patch(mock.patch.object(sf_editor, "verify_bare_code", return_value=[]))
        self.verify = sf_editor.verify_bare_code

    def test_wrong_prefix_looks_up_every_same_number_candidate(self):
        got = sf_editor.resolve_code_input("sz600519")
        self.assertEqual(got, self.CANDIDATES)
        self.by_digits.assert_called_once_with("600519", "sz")
        self.verify.assert_not_called()

    def test_wrong_prefix_with_nothing_found_is_recorded(self):
        self.by_digits.return_value = []
        self.assertEqual(sf_editor.resolve_code_input("bj600519"), [])
        self.assertEqual(self.rejects, ["prefix_mismatch"])

    def test_unfinished_code_with_a_prefix_is_a_fuzzy_search_too(self):
        self.assertEqual(sf_editor.resolve_code_input("sh6005"), self.CANDIDATES)
        self.by_digits.assert_called_once_with("6005", "sh")
        self.by_digits.return_value = []
        self.assertEqual(sf_editor.resolve_code_input("sh6005"), [])
        self.assertEqual(self.rejects, ["prefix_no_match"])

    def test_unfinished_code_without_a_prefix_is_not_handled_here(self):
        for text in ("6005", "茅台", ""):
            with self.subTest(text=text):
                self.assertEqual(sf_editor.resolve_code_input(text), [])
        self.by_digits.assert_not_called()

    def test_a_correct_and_confirmed_code_is_returned_as_is(self):
        self.verify.return_value = [("sh600519", "贵州茅台")]
        self.assertEqual(sf_editor.resolve_code_input("sh600519"), [("sh600519", "贵州茅台")])
        self.by_digits.assert_not_called()

    def test_a_code_the_quote_rejects_falls_back_to_same_number_candidates(self):
        """000300 在深市不存在，只有沪市是指数；按同号候选处理，不直接判无此代码。"""
        self.by_digits.return_value = [("sh000300", "沪深300")]
        self.assertEqual(sf_editor.resolve_code_input("000300"), [("sh000300", "沪深300")])
        self.by_digits.assert_called_once_with("000300", "sz")

    def test_a_code_nobody_knows_stays_not_found(self):
        self.by_digits.return_value = []
        self.assertEqual(sf_editor.resolve_code_input("sh999999"), [])

    def test_input_kind_helper(self):
        for text in ("600519", "sh600519", "sh6005", "600519.SH", "ｓｈ６００"):
            with self.subTest(text=text):
                self.assertTrue(sf_editor._is_code_input(text))  # discipline-ok(R5): 路由判定本身就是被测行为
        for text in ("6005", "茅台", "", "ST金正"):
            with self.subTest(text=text):
                self.assertFalse(sf_editor._is_code_input(text))  # discipline-ok(R5): 同上



class EditorPrefixFlowCase(SFCase):
    """编辑窗：前缀写错后的整条流程，以及候选的省略、键盘操作。"""

    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def setUp(self):
        super().setUp()
        self.applied = []
        self.track_patch(mock.patch.object(sf_editor.SearchThread, "start", lambda thread: thread.run()))
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready"))
        self.track_patch(mock.patch.object(sf_universe, "match_confident", return_value=[]))
        self.track_patch(mock.patch.object(sf_universe, "note_reject"))
        self.track_patch(mock.patch.object(sf_editor, "log_swallow"))
        self.track_patch(mock.patch.object(sf_quote, "http_get", side_effect=OSError("测试不联网")))
        self.track_patch(mock.patch.object(sf_quote, "eastmoney_search_bj", return_value=[]))
        self.track_patch(mock.patch.object(
            sf_quote, "fetch_quote",
            side_effect=lambda code: (_ for _ in ()).throw(sf_quote.QuoteDataError("无此代码"))))
        self.track_patch(mock.patch.object(
            sf_editor, "search_by_digits",
            return_value=[("sh000001", "上证指数"), ("sz000001", "平安银行")]))

    def make_editor(self, code="sh600519", width=200):
        ed = sf_editor.EditorWindow(code, self.applied.append, QPoint(50, 50), width)
        ed.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.track_widget(ed)
        ed.show()
        self.app.processEvents()
        return ed

    def candidates(self, ed):
        return [ed.cand_box.itemAt(i).widget() for i in range(ed.cand_box.count())]

    def test_wrong_prefix_lists_stock_and_index_and_applies_nothing(self):
        """用户要求：前缀写错要模糊搜索，股票和指数都列出来让他选。"""
        ed = self.make_editor("sh600519")
        ed.entry.setText("sz000001")          # 这个前缀是对的：直接是平安银行
        ed.entry.setText("sh000001")
        ed.do_search()
        labels = [b.text() for b in self.candidates(ed)]
        self.assertEqual(len(labels), 2)
        self.assertIn("上证指数", labels[0])
        self.assertIn("平安银行", labels[1])
        self.assertEqual(self.applied, [], "只列出来，不替用户选")

    def test_choosing_a_candidate_applies_that_exact_code(self):
        ed = self.make_editor("sh600519")
        ed.entry.setText("sh000001")
        ed.do_search()
        self.candidates(ed)[0].click()
        self.assertEqual(self.applied, ["sh000001"])

    def test_a_single_candidate_is_applied_directly_without_asking(self):
        """用户要求：只搜出一条时直接替换，写错前缀的模糊搜索也一样。"""
        sf_editor.search_by_digits.return_value = [("sh000300", "沪深300")]
        ed = self.make_editor("sh600519")
        ed.entry.setText("sz000300")
        ed.do_search()
        self.assertEqual(self.applied, ["sh000300"])
        self.assertEqual(self.candidates(ed), [])

    def test_a_single_wrong_prefix_stock_is_applied_directly(self):
        sf_editor.search_by_digits.return_value = [("sh600519", "贵州茅台")]
        ed = self.make_editor("sz000001")
        ed.entry.setText("bj600519")
        ed.do_search()
        self.assertEqual(self.applied, ["sh600519"])

    def test_a_single_candidate_that_is_already_shown_asks_nothing_and_says_so(self):
        sf_editor.search_by_digits.return_value = [("sh600519", "贵州茅台")]
        ed = self.make_editor("sh600519")
        ed.entry.setText("sz600519")
        ed.do_search()
        self.assertEqual(self.applied, [])
        self.assertIn("已在显示中", ed.hint.text())

    def test_nothing_found_for_a_wrong_prefix_says_so(self):
        sf_editor.search_by_digits.return_value = []
        ed = self.make_editor("sh600519")
        ed.entry.setText("bj600519")
        ed.do_search()
        self.assertIn("未找到该代码", ed.hint.text())
        self.assertEqual(self.candidates(ed), [])

    def test_a_confirmed_single_result_still_applies(self):
        """没写错前缀的唯一结果同样直接替换。"""
        sf_quote.fetch_quote.side_effect = lambda code: {"name": "样例", "code": code}
        ed = self.make_editor("sh600519")
        ed.entry.setText("000002")
        ed.do_search()
        self.assertEqual(self.applied, ["sz000002"])

    def test_suffix_spelling_is_applied_like_the_prefix_spelling(self):
        sf_quote.fetch_quote.side_effect = lambda code: {"name": "样例", "code": code}
        ed = self.make_editor("sh600519")
        ed.entry.setText("000002.SZ")
        ed.do_search()
        self.assertEqual(self.applied, ["sz000002"])

    def test_long_names_are_elided_but_the_code_stays_visible(self):
        sf_editor.search_by_digits.return_value = [
            ("sh510300", "沪深300ETF华泰柏瑞超长名称" * 2), ("sh510310", "沪深300ETF华泰柏瑞超长名称" * 2)]
        ed = self.make_editor("sh600519", width=184)
        ed.entry.setText("sh510300")
        ed.entry.setText("sz510300")
        ed.do_search()
        buttons = self.candidates(ed)
        self.assertEqual(len(buttons), 2)
        for button, code in zip(buttons, ("sh510300", "sh510310")):
            self.assertTrue(button.text().endswith(code), "代码必须完整：%r" % button.text())
            self.assertIn("…", button.text())
            self.assertIn("超长名称", button.toolTip(), "完整名称放在悬停提示里")
            width = QFontMetrics(button.font()).horizontalAdvance(button.text())
            self.assertLessEqual(width, button.width(), "省略后仍超宽：%r" % button.text())

    def test_label_helper_keeps_short_names_and_shrinks_to_the_code_alone(self):
        self.assertEqual(sf_editor._candidate_label("平安银行", "sz000001", 300),  # discipline-ok(R5): 省略规则本身是被测行为
                         "平安银行  sz000001")
        self.assertEqual(sf_editor._candidate_label("很长的名称", "sz000001", 1), "sz000001")  # discipline-ok(R5): 同上

    def listed(self, count=3):
        sf_editor.search_by_digits.return_value = [
            ("sh6%05d" % i, "候选%d" % i) for i in range(count)]
        ed = self.make_editor("sh600519")
        ed.entry.setText("sz600001")
        ed.do_search()
        # 线程在测试里同步执行，按钮刚进布局还没显示；真机上回包走事件队列，
        # 用户按键时布局早已生效。不可见的控件拿不到焦点，所以先让事件循环转一轮。
        self.app.processEvents()
        self.assertEqual(len(self.candidates(ed)), count)
        ed.entry.setFocus()
        return ed

    def test_down_and_up_move_focus_between_the_entry_and_candidates(self):
        ed = self.listed(3)
        buttons = self.candidates(ed)
        steps = []
        for _ in range(4):                                  # 多按一次：停在最后一条，不越界
            QTest.keyClick(ed.entry if not steps else ed.focusWidget(), Qt.Key.Key_Down)
            steps.append(buttons.index(ed.focusWidget()))
        self.assertEqual(steps, [0, 1, 2, 2])
        for expected in (1, 0):
            QTest.keyClick(ed.focusWidget(), Qt.Key.Key_Up)
            self.assertIs(ed.focusWidget(), buttons[expected])
        QTest.keyClick(ed.focusWidget(), Qt.Key.Key_Up)
        self.assertIs(ed.focusWidget(), ed.entry, "再往上回到输入框")

    def test_return_on_a_focused_candidate_chooses_it(self):
        ed = self.listed(2)
        QTest.keyClick(ed.entry, Qt.Key.Key_Down)
        QTest.keyClick(ed.focusWidget(), Qt.Key.Key_Return)
        self.assertEqual(self.applied, ["sh600000"])

    def test_arrow_keys_do_nothing_when_there_are_no_candidates(self):
        ed = self.make_editor("sh600519")
        ed.entry.setFocus()
        QTest.keyClick(ed.entry, Qt.Key.Key_Down)
        self.assertIs(ed.focusWidget(), ed.entry)

    def test_other_keys_on_a_candidate_keep_the_default_behaviour(self):
        ed = self.listed(2)
        button = self.candidates(ed)[0]
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(self.applied, ["sh600000"], "空格仍是按钮的默认触发键")

    def test_escape_still_closes_the_editor(self):
        ed = self.listed(2)
        QTest.keyClick(ed, Qt.Key.Key_Escape)
        self.assertTrue(getattr(ed, "_closed", False))  # discipline-ok(R5): _closed 没有公开读口，产品里也是 getattr 读


class ConfigCanonicalCase(SFCase):
    """配置里的代码读回：全角收成 ASCII；600519 与 sh600519 是同一只。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sf_prefix_fuzzy_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.track_patch(mock.patch.object(sf_config, "CONFIG_PATH", self.cfg))
        self.track_patch(mock.patch.object(sf_config, "CONFIG_BACKUP_PATH",
                                           os.path.join(self.tmp, "config.backup.json")))
        self.track_patch(mock.patch.object(sf_config, "log_line", lambda *a, **k: None))

    def write(self, **fields):
        data = {"code": "sh600519", "x": 100, "y": 100, "h": 224, "bg": "glass"}
        data.update(fields)
        with open(self.cfg, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)

    def alert(self, code, target=70.0):
        return {"code": code, "target": target, "direction": "above",
                "session_date": "2099-12-31", "state": "pending"}

    def test_fullwidth_code_is_repaired_on_read(self):
        """旧版本把全角代码原样写进过配置，读回后行情永远是 "--"。"""
        self.write(code="６００５１９")
        self.assertEqual(sf_config.load_config()[0], "sh600519")

    def test_plain_ascii_codes_are_left_alone(self):
        for code in ("600519", "sh600519", "SH600519", "bj920185"):
            with self.subTest(code=code):
                self.write(code=code)
                self.assertEqual(sf_config.load_config()[0], code)

    def test_bare_and_prefixed_spellings_are_the_same_stock_for_alerts(self):
        """顶层是裸码 600519，切走再切回 sh600519，目标价不能读不回来。"""
        self.write(code="600519", alerts=[self.alert("600519")])
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1)
        self.assertEqual(len(sf_config.load_alerts("600519")), 1)
        self.assertEqual(sf_config.load_alerts("sz000001"), [])

    def test_legacy_entries_without_a_code_follow_the_canonical_owner(self):
        self.write(code="600519", alerts=[{k: v for k, v in self.alert("x").items() if k != "code"}])
        self.assertEqual(len(sf_config.load_alerts("sh600519")), 1)
        self.assertEqual(sf_config.load_alerts("sz000001"), [])

    def test_saving_under_the_prefixed_code_replaces_the_bare_entries(self):
        self.write(code="600519", alerts=[self.alert("600519", 70.0)])
        self.assertTrue(sf_config.save_alerts([self.alert("sh600519", 80.0)], code="sh600519"))
        with open(self.cfg, encoding="utf-8") as handle:
            stored = json.load(handle)["alerts"]
        self.assertEqual([a["target"] for a in stored], [80.0], "同一只股票不该留下两份")

    def test_other_stocks_entries_survive_a_save_for_the_canonical_stock(self):
        self.write(code="600519", alerts=[self.alert("sz000001", 11.0)])
        self.assertTrue(sf_config.save_alerts([self.alert("sh600519", 80.0)], code="600519"))
        with open(self.cfg, encoding="utf-8") as handle:
            stored = json.load(handle)["alerts"]
        self.assertEqual(sorted(str(a["code"]) for a in stored), ["sh600519", "sz000001"])


class SeedPrefixCase(SFCase):
    """首屏快照：同号不同交易所（平安银行 / 上证指数）不能互相预填。"""

    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()
        cls.tmp = tempfile.mkdtemp(prefix="sf_seed_prefix_")

    def setUp(self):
        super().setUp()
        import datetime
        import stock_float_window as window_module
        from tests._base import fixed_clock
        self.m = window_module
        clock = fixed_clock(datetime.datetime(2026, 9, 12, 16, 30),
                            module_names=("sf_quote", "stock_float_window"))
        clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)
        env = {"SF_LAST_QUOTE_PATH": os.path.join(self.tmp, "last_quote.json"),
               "SF_SEED_FIRST_SCREEN": "0"}
        self.track_patch(mock.patch.dict(os.environ, env))
        if os.path.exists(env["SF_LAST_QUOTE_PATH"]):
            os.remove(env["SF_LAST_QUOTE_PATH"])

    def make_window(self, code):
        m = self.m
        for name in ("_fetch_quote", "_fetch_minute"):
            self.track_patch(mock.patch.object(m.FloatWindow, name, lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None))
        self.track_patch(mock.patch.object(m, "load_config",
                                           return_value=(code, 100, 100, 224, "translucent")))
        self.track_patch(mock.patch.object(m, "save_config", lambda *a, **k: None))
        self.track_patch(mock.patch.object(sf_quote, "http_get", lambda *a, **k: ""))
        self.track_patch(mock.patch.object(m, "start_icon_worker", lambda: None))
        win = m.FloatWindow()
        self.track_widget(win)
        win.running = False
        for timer in ("timer", "top_timer", "_anim_timer", "_flash_timer"):
            try:
                getattr(win, timer).stop()
            except Exception:
                pass
        return win

    def seeded(self, window_code, snapshot_code):
        sf_config.save_last_quote(
            snapshot_code, {"name": "样例", "code": "000001", "price": "11.54", "pct": "1.0",
                            "high": "11.6", "low": "11.4"}, [("0930", 11.3, None)], "2026-09-12")
        win = self.make_window(window_code)
        os.environ["SF_SEED_FIRST_SCREEN"] = "1"
        win._seed_first_screen()  # discipline-ok(R5): 首屏预填没有公开入口，被测对象就是它
        return bool(getattr(win, "_seeded_from_cache", False))  # discipline-ok(R5): 预填结果就是被测行为

    def test_snapshot_of_the_stock_is_not_used_for_the_index_with_the_same_number(self):
        self.assertFalse(self.seeded("sh000001", "sz000001"))

    def test_snapshot_of_the_same_security_is_used(self):
        self.assertTrue(self.seeded("sz000001", "sz000001"))

    def test_legacy_bare_snapshot_code_is_still_accepted(self):
        """旧快照里的代码可能没有前缀，不能因为多了这道校验把它们全判成别家的。"""
        self.assertTrue(self.seeded("sz000001", "000001"))
