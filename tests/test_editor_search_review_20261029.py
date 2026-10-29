# -*- coding: utf-8 -*-
"""替换股票搜索的 2026-10-29 走查修复判据（每项缺陷至少一条，改坏必红）。

覆盖：显式交易所前缀不被偷换成别的股票、名称里的英文字母与全角字符能本地命中、
联网失败不缓存、ETF/LOF 名称可搜但不挤掉股票、全角代码不进配置、带前缀代码走行情核验、
提示行不被截断、候选收起后窗口收回高度。

判据都落在可观测结果上（返回值、传给主窗的代码、提示文字宽度、窗口高度），
不读源码做字符串断言。
"""
import os
import sys
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFontMetrics

import sf_editor
import sf_quote
import sf_universe
from tests._base import SFCase

build_index = sf_universe._index  # discipline-ok(R5): 换一份合成字典，不读文件、不联网

ROWS = [
    ("sh600519", "贵州茅台"), ("sz000001", "平安银行"), ("sz000100", "TCL科技"),
    ("sz002470", "ST金正"), ("sz000622", "*ST恒立"), ("sz000725", "京东方Ａ"),
    ("sz000858", "五 粮 液"), ("sh601398", "工商银行"), ("sz300750", "宁德时代"),
    ("bj920185", "贝特瑞"),
]
BODY_STOCK = 'v_hint="sh~600519~贵州茅台~gzmt~GP-A"'
MANY = [("sh6%05d" % i, "候选%02d" % i) for i in range(12)]


class DictionaryCase(SFCase):
    """本地字典匹配：显式前缀、折叠、精确名称。"""

    def setUp(self):
        super().setUp()
        for name, value in (("_DISABLED", False), ("_rows", []),
                            ("_by_code", {}), ("_by_name", {})):
            self.track_patch(mock.patch.object(sf_universe, name, value))
        build_index(ROWS)

    def test_explicit_exchange_prefix_is_respected(self):
        """sh000001 是上证指数，不能悄悄变成深市的平安银行（缺陷 1）。"""
        self.assertEqual(sf_universe.match_confident("sz000001"), [("sz000001", "平安银行")])
        for wrong in ("sh000001", "bj600519", "sz600519", "SH000001", " sh 000001 "):
            with self.subTest(keyword=wrong):
                self.assertEqual(sf_universe.match_confident(wrong), [])

    def test_bare_digits_still_try_every_exchange(self):
        self.assertEqual(sf_universe.match_confident("600519"), [("sh600519", "贵州茅台")])
        self.assertEqual(sf_universe.match_confident("920185"), [("bj920185", "贝特瑞")])

    def test_names_with_letters_match_in_any_case(self):
        """字典里有 389 只名称含英文字母（缺陷 7）；搜索词转小写后必须仍能对上。"""
        cases = {"TCL科技": "sz000100", "tcl科技": "sz000100", "ST金正": "sz002470",
                 "st金正": "sz002470", "*ST恒立": "sz000622", "*st恒立": "sz000622",
                 "TCL": "sz000100"}
        for keyword, code in cases.items():
            with self.subTest(keyword=keyword):
                self.assertEqual(sf_universe.match_confident(keyword)[0][0], code)
                self.assertEqual(sf_universe.match_fuzzy(keyword, 5)[0][0], code)

    def test_returned_name_keeps_the_original_spelling(self):
        """折叠只用于比较；界面显示的仍是东财给的原名，不能变成小写。"""
        self.assertEqual(sf_universe.match_confident("tcl科技"), [("sz000100", "TCL科技")])
        self.assertEqual(sf_universe.match_confident("京东方a"), [("sz000725", "京东方Ａ")])

    def test_fullwidth_input_matches_halfwidth_names_and_codes(self):
        self.assertEqual(sf_universe.match_confident("京东方Ａ")[0][0], "sz000725")
        self.assertEqual(sf_universe.match_confident("６００５１９")[0][0], "sh600519")
        self.assertEqual(sf_universe.match_confident("ｓｚ０００００１")[0][0], "sz000001")

    def test_spaced_dictionary_names_are_matched_without_spaces(self):
        self.assertEqual(sf_universe.match_confident("五粮液")[0][0], "sz000858")
        self.assertEqual(sf_universe.match_confident("五 粮 液")[0][0], "sz000858")

    def test_a_full_name_is_not_swallowed_by_longer_names_containing_it(self):
        """精确名称先于"包含且唯一"：全名被别的名称包含时仍应命中全名。"""
        build_index(ROWS + [("sz000011", "平安"), ("sz000012", "平安银行控股")])
        self.assertEqual(sf_universe.match_confident("平安"), [("sz000011", "平安")])


class CodeShapeCase(SFCase):
    def test_complete_codes_in_every_spelling(self):
        cases = {"600519": ("", "600519"), "sh600519": ("sh", "600519"),
                 "SZ000001": ("sz", "000001"), "６００５１９": ("", "600519"),
                 " bj 920185 ": ("bj", "920185"), "ｓｈ６００５１９": ("sh", "600519")}
        for text, shape in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sf_universe.code_shape(text), shape)

    def test_things_that_are_not_complete_codes(self):
        """位数不对、混了字母、非 ASCII 数字，一律不是代码（改走名称搜索）。"""
        for text in ("60051", "6005190", "sh60051", "abc123", "贵州茅台", "", None,
                     "٦٠٠٥١٩", "hk00700", "sh600519.sz"):
            with self.subTest(text=text):
                self.assertIsNone(sf_universe.code_shape(text))


class SearchOrchestrationCase(SFCase):
    """search_stocks：缓存只记真实答复、ETF/LOF 候选、关键词折叠。字典停用，只测联网侧。"""

    def setUp(self):
        super().setUp()
        self.track_patch(mock.patch.object(sf_universe, "_DISABLED", True))
        self.track_patch(mock.patch.object(sf_quote, "_TEST_ENV", False))  # discipline-ok(R5): 缓存在单测环境默认短路，测缓存必须打开
        self.track_patch(mock.patch.dict(sf_quote._SEARCH_CACHE, clear=True))  # discipline-ok(R5): 隔离缓存内容
        self.track_patch(mock.patch.object(sf_quote, "eastmoney_search_bj", return_value=[]))

    def stub_http(self, *responses):
        """把 http_get 换成按顺序吐响应的桩，返回这个桩以便数调用次数。"""
        self.track_patch(mock.patch.object(sf_quote, "http_get", side_effect=list(responses)))
        return sf_quote.http_get

    def test_a_failed_network_search_is_not_cached(self):
        """断网那一次的空结果不能让同一个词在 5 分钟内一直搜不到（缺陷 2）。"""
        http = self.stub_http(OSError("offline"), BODY_STOCK)
        self.assertEqual(sf_quote.search_stocks("茅台"), [])
        self.assertEqual(sf_quote.search_stocks("茅台"), [("sh600519", "贵州茅台")])
        self.assertEqual(http.call_count, 2)

    def test_an_empty_answer_is_not_cached_either(self):
        """东财兜底静默失败时，腾讯的空答复也分不清"确实没有"，同样不缓存。"""
        http = self.stub_http('v_hint="N"', BODY_STOCK)
        self.assertEqual(sf_quote.search_stocks("茅台"), [])
        self.assertEqual(sf_quote.search_stocks("茅台"), [("sh600519", "贵州茅台")])
        self.assertEqual(http.call_count, 2)

    def test_a_real_answer_is_still_cached(self):
        http = self.stub_http(BODY_STOCK, OSError("不该再发请求"))
        first = sf_quote.search_stocks("茅台")
        self.assertEqual(sf_quote.search_stocks("茅台"), first)
        self.assertEqual(http.call_count, 1)

    def test_etf_names_are_searchable_when_no_stock_matches(self):
        """搜「沪深300ETF」要有候选（缺陷 6）；指数同属第二队，场外基金不收。"""
        body = ('v_hint="sh~510300~沪深300ETF~hs300etf~ETF^sz~159919~沪深300ETF~hs300~ETF'
                '^jj~110026~易方达创业板联接~yfd~KJ^sh~000300~沪深300~hs300~ZS^sz~161725~白酒LOF~bj~LOF"')
        self.stub_http(body)
        got = sf_quote.search_stocks("沪深300ETF")
        self.assertEqual([c for c, _ in got], ["sh510300", "sz159919", "sh000300", "sz161725"])

    def test_funds_do_not_crowd_out_a_stock_match(self):
        """搜"茅台"不该混进一堆茅台主题 ETF，否则唯一命中自动替换变成要多点一次。"""
        self.stub_http('v_hint="sh~600519~贵州茅台~gzmt~GP-A^sh~512690~酒ETF~jetf~ETF"')
        self.assertEqual(sf_quote.search_stocks("茅台"), [("sh600519", "贵州茅台")])

    def test_board_suffixed_stock_types_are_still_stocks(self):
        self.stub_http('v_hint="sh~688981~中芯国际~zxgj~GP-A-KCB^sz~300750~宁德时代~ndsd~GP-A-CYB"')
        self.assertEqual([c for c, _ in sf_quote.search_stocks("芯")], ["sh688981", "sz300750"])

    def test_stocks_and_funds_are_each_capped_and_parsing_stops_when_both_are_full(self):
        """腾讯一次返回很多行时，股票和基金各自封顶，且不会因为基金多而挤掉股票。"""
        cap = sf_quote.SEARCH_MAX_RESULTS
        rows = ["sz~%06d~基金%03d~jj~ETF" % (i, i) for i in range(cap + 10)]
        rows += ["sh~%06d~股票%03d~gp~GP-A" % (600000 + i, i) for i in range(cap + 10)]
        rows += ["sh~688999~末尾的股票~mw~GP-A"]
        self.stub_http('v_hint="%s"' % "^".join(rows))
        got = sf_quote.search_stocks("多")
        self.assertEqual(len(got), cap)
        self.assertTrue(all(code.startswith("sh6") for code, _ in got), "有股票命中时基金不进候选")
        self.assertNotIn(("sh688999", "末尾的股票"), got, "超过上限的行不再收")

    def test_bonds_and_b_shares_on_the_exchanges_are_not_candidates(self):
        """沪深交易所上的债券、B 股既不是股票也不是 ETF/指数，不进候选。

        只放这两类行：若过滤被去掉，它们会变成第二队候选而返回非空，用例当场变红。
        """
        self.stub_http('v_hint="sh~113001~某转债~zz~ZQ^sz~200002~万科B~wkb~GP-B"')
        self.assertEqual(sf_quote.search_stocks("转债"), [])

    def test_fullwidth_keyword_is_folded_before_it_is_sent(self):
        http = self.stub_http(BODY_STOCK)
        sf_quote.search_stocks("ＴＣＬ　科技")
        sent = http.call_args.args[0]
        self.assertIn("q=" + urllib.parse.quote("tcl科技"), sent)


class VerifyBareCodeCase(SFCase):
    """verify_bare_code：形状、前缀核对、全角收成 ASCII 之后才碰行情。"""

    def setUp(self):
        super().setUp()
        self.rejects = []
        self.track_patch(mock.patch.object(sf_universe, "ensure_ready"))
        self.track_patch(mock.patch.object(sf_universe, "match_confident", return_value=[]))
        self.track_patch(mock.patch.object(sf_universe, "note_reject", self.rejects.append))
        self.track_patch(mock.patch.object(sf_editor, "log_swallow"))
        self.fetch = mock.Mock(side_effect=lambda code: {"name": "样例", "code": code})
        self.track_patch(mock.patch.object(sf_quote, "fetch_quote", self.fetch))

    def test_prefix_that_contradicts_the_code_range_is_refused_without_probing(self):
        """sh000001 / bj600519 / sz510300：前缀和代码段对不上，明确判无此代码（缺陷 1）。"""
        for keyword in ("sh000001", "bj600519", "sz510300", "SH000001"):
            with self.subTest(keyword=keyword):
                self.assertEqual(sf_editor.verify_bare_code(keyword), [])
        self.fetch.assert_not_called()
        self.assertEqual(set(self.rejects), {"prefix_mismatch"})

    def test_matching_prefix_missing_from_the_dictionary_is_confirmed_by_quote(self):
        """sh510300 前缀对得上、字典里没有（ETF 段），由行情答复。"""
        self.assertEqual(sf_editor.verify_bare_code("sh510300"), [("sh510300", "样例")])
        self.fetch.assert_called_once_with("sh510300")

    def test_fullwidth_digits_reach_the_probe_as_ascii(self):
        """全角数字不能原样去取行情，更不能原样进配置（缺陷 3）。"""
        self.assertEqual(sf_editor.verify_bare_code("６００５１９"), [("sh600519", "样例")])
        self.fetch.assert_called_once_with("sh600519")

    def test_offline_fullwidth_input_is_still_applied_as_ascii(self):
        """断网时宽松放行，但放行的必须是规范化后的 ASCII 代码。"""
        self.fetch.side_effect = OSError("offline")
        self.assertEqual(sf_editor.verify_bare_code("６００５１９"), [("sh600519", "sh600519")])

    def test_wrong_shapes_never_reach_the_probe(self):
        for keyword in ("12345", "1234567", "abc", "٦٠٠٥١٩"):
            with self.subTest(keyword=keyword):
                self.assertEqual(sf_editor.verify_bare_code(keyword), [])
        self.fetch.assert_not_called()
        self.assertEqual(set(self.rejects), {"invalid_code_shape"})

    def test_dictionary_hit_skips_the_probe_and_keeps_its_name(self):
        sf_universe.match_confident.return_value = [("sh600519", "贵州茅台")]
        self.assertEqual(sf_editor.verify_bare_code("sh600519"), [("sh600519", "贵州茅台")])
        self.fetch.assert_not_called()


CARD_W = 184   # 主窗收窄时的实际宽度（与 test_v1434_hint_gap 同口径），提示行可用宽度由它推出


class EditorFlowCase(SFCase):
    """编辑窗：全角输入、前缀错配、候选收起后收回高度、提示行不被截断。"""

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
        # 编辑窗被改坏时可能误走名称搜索，那条路会真联网；判据不该依赖外部网络。
        self.track_patch(mock.patch.object(sf_quote, "http_get", side_effect=OSError("测试不联网")))
        self.track_patch(mock.patch.object(sf_quote, "eastmoney_search_bj", return_value=[]))
        self.fetch = mock.Mock(side_effect=lambda code: {"name": "样例", "code": code})
        self.track_patch(mock.patch.object(sf_quote, "fetch_quote", self.fetch))

    def make_editor(self, code="sh600519"):
        ed = sf_editor.EditorWindow(code, self.applied.append, QPoint(50, 50), CARD_W)
        ed.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.track_widget(ed)
        ed.show()
        self.app.processEvents()
        return ed

    def deliver(self, ed, keyword, results):
        """发起一次搜索并让它带着给定结果回包（不真的起线程）。"""
        with mock.patch.object(sf_editor.SearchThread, "start"):
            ed.entry.setText(keyword)
            ed.do_search()
            thread = ed._search_thread  # discipline-ok(R5): 回包要带搜索序号，只能取这条线程
            thread.done.emit(results, thread.seq)
        self.app.processEvents()

    def test_current_code_in_other_spellings_keeps_confirm_disabled(self):
        ed = self.make_editor("sh600519")
        for text in ("６００５１９", "SH600519", " sh 600519 ", "ｓｈ６００５１９"):
            with self.subTest(text=text):
                ed.entry.setText(text)
                self.assertFalse(ed.ok.isEnabled())

    def test_fullwidth_code_is_applied_as_ascii(self):
        """全角状态敲出的代码，主窗收到的必须是规范化后的 ASCII 代码（缺陷 3）。"""
        ed = self.make_editor("sh600519")
        ed.entry.setText("０００００１")
        ed.do_search()
        self.assertEqual(self.applied, ["sz000001"])
        self.fetch.assert_called_once_with("sz000001")

    def test_mismatched_prefix_reports_not_found_and_applies_nothing(self):
        """输入 sh000001 不能换成平安银行，也不能走到取行情（缺陷 1）。"""
        ed = self.make_editor("sh600519")
        ed.entry.setText("sh000001")
        ed.do_search()
        self.assertEqual(self.applied, [])
        self.assertIn("未找到该代码", ed.hint.text())
        self.fetch.assert_not_called()

    def test_window_returns_to_its_compact_height_after_candidates_go_away(self):
        """候选收起后窗口要缩回内容高度，不留一大片空白（缺陷 5）。"""
        ed = self.make_editor()
        compact = ed.height()
        self.deliver(ed, "银行", MANY)
        self.assertGreater(ed.height(), compact)
        ed.entry.setText("银")
        self.app.processEvents()
        self.assertEqual(ed.height(), ed.sizeHint().height())
        self.assertEqual(ed.height(), compact)

    def test_window_also_shrinks_when_the_next_search_finds_nothing(self):
        ed = self.make_editor()
        compact = ed.height()
        self.deliver(ed, "银行", MANY)
        self.deliver(ed, "不存在的名字", [])
        self.assertEqual(ed.height(), compact)
        self.assertEqual(ed.cand_box.count(), 0)

    def test_every_hint_fits_the_hint_line_at_the_narrow_card_width(self):
        """提示行只有一行高、不换行；任何文案超宽都会被截断（缺陷 4）。"""
        ed = self.make_editor()
        seen = {}
        self.deliver(ed, "银行", MANY)
        seen["too_many"] = ed.hint.text()
        self.deliver(ed, "sh000001", [])
        seen["code_not_found"] = ed.hint.text()
        self.deliver(ed, "不存在的名字", [])
        seen["name_not_found"] = ed.hint.text()
        self.deliver(ed, "茅台", [("sh600519", "贵州茅台")])
        seen["duplicate"] = ed.hint.text()
        ed.on_apply = mock.Mock(side_effect=RuntimeError("主窗正在切股"))
        self.deliver(ed, "宁德", [("sz300750", "宁德时代")])
        seen["apply_failed"] = ed.hint.text()
        with mock.patch.object(sf_editor.SearchThread, "start"):
            ed.entry.setText("宁德时代")
            ed.do_search()
        seen["searching"] = ed.hint.text()
        metrics = QFontMetrics(ed.hint.font())
        for name, text in seen.items():
            with self.subTest(hint=name):
                self.assertTrue(text, "这条路径应当给出提示")
                self.assertLessEqual(metrics.horizontalAdvance(text), ed.hint.width(),
                                     "提示被截断：%r" % text)

    def test_prefixed_code_missing_from_the_dictionary_is_confirmed_by_quote(self):
        """带前缀的 ETF 代码在线上搜索里是查不到的（腾讯返回 N），只能由行情核验（缺陷 6）。"""
        ed = self.make_editor("sh600519")
        ed.entry.setText("sh510300")
        ed.do_search()
        self.assertEqual(self.applied, ["sh510300"])
        self.fetch.assert_called_once_with("sh510300")
