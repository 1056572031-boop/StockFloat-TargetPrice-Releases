# -*- coding: utf-8 -*-
"""自动采集层（sf_diag）的判据 —— 验收「以后不用手动接线」这句话成不成立。

判据的形状就是这层的承诺
----------------------
`outputs/诊断导出逐版核查-v1.4.27至v1.4.66-2026-10-12.md` 那 40 版对账之后立的两条：
**搬运**交给机器（新加的状态位自己进包），**立意**交给闸门（`tools/diag_coverage_check.py`）。
所以本文件第一条用例不是「某个字段在不在包里」，而是：造一个导出器从来没听说过的新
模块，在里面写一个新计数器，**什么都不接**，导出里必须有它。这条红了就说明「自动」又
变回一句文档话 —— 而那正是上一轮要治的病。

反向对照同样重要（[[feedback-lock-falsifiability-two-traps]] 那一族：只写正向断言的锁
常常恒真）：

· 名字**不**命中词表的变量不许出现 —— 否则「全都收」会让第一条用例变成恒真；
· callable 不许被调用（边界 1：导出那一下绝不能跑业务）；
· 带 token / password 的键整个不收（边界 2：外发包不许带凭据与身份）；
· 截断与超限必须在包里点名（边界 3：静默截断＝把「很长」伪装成「不见了」）；
· 采集器自己抛错时整包照出、`diagnostics.txt` 的 export_issues 必须有名字
  （AGENTS §5.7 第二条，与 G11/G24 同族）。

窗口一律复用 `DiagnosticsBranchCase`（AGENTS §6 第 14 条：被测窗口只能由带完整隔离桩
的夹具来建，裸 `FloatWindow()` 会把用户机器上的正式版真拉起来）。往 `sys.modules` 里
塞的合成模块都在 `addCleanup` 里摘掉，否则同一进程后面的用例会看见一个不该存在的
「产品模块」。
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_diag
import sf_notify
from tests._base import SFCase
from tests.test_coverage_window_diagnostics import DiagnosticsBranchCase

PROBE_MODULE = "sf_probe_autocollect"
PROBE_OBJECT_MODULE = "sf_probe_objectholder"
_HOME = os.path.expanduser("~")
_SENTINEL = 17          # 一个不会与真实计数撞上的哨兵值（不是版本号，§6 第 1 条无关）


def _install_probe(**values):
    """把一个合成模块塞进 `sys.modules`；调用方负责用 addCleanup 摘掉。"""
    module = types.ModuleType(PROBE_MODULE)
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[PROBE_MODULE] = module
    return module


class _Trap:
    """被调用就留痕的「伪 getter」：用来钉住边界 1。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return "不该被调用"


class AutoCollectCase(DiagnosticsBranchCase):
    """真导一次包，验自动采集层的承诺与三条边界。"""

    def _state(self):
        files = self._export()
        self.assertIn("state.json", files, "自动采集层的文件必须真的在包里")
        return json.loads(files["state.json"]), files

    def _with_probe(self, **values):
        _install_probe(**values)
        self.addCleanup(sys.modules.pop, PROBE_MODULE, None)

    # ---- 承诺本身：新模块的新计数器，零接线进包 ----

    def test_a_brand_new_counter_reaches_the_package_with_no_wiring(self):
        """这条是整个机制的验收：导出器一个字没改，新状态照样读得出来。"""
        self._with_probe(_deliver_fail_count=_SENTINEL,
                         _last_reject_reason="代码不在字典里")
        probe = self._state()[0]["modules"].get(PROBE_MODULE)
        self.assertIsNotNone(probe, "新模块没进包：「以后不用手动接线」这句承诺就只剩闸门那一半了")
        self.assertEqual(probe["_deliver_fail_count"], _SENTINEL)
        self.assertEqual(probe["_last_reject_reason"], "代码不在字典里")

    def test_names_outside_the_table_are_not_collected(self):
        """反向对照：连不命中词表的名字都收，上一条就变成恒真。"""
        self._with_probe(_deliver_fail_count=_SENTINEL,
                         window_title="实时股价", _plain_label="不该出现")
        probe = self._state()[0]["modules"][PROBE_MODULE]
        self.assertIn("_deliver_fail_count", probe)
        self.assertNotIn("window_title", probe, "词表形同虚设：包体积与噪音会一路涨到没人读")
        self.assertNotIn("_plain_label", probe)

    def test_auto_counts_and_curated_counts_are_the_same_number(self):
        """两条通路（人工接线的 notify.json、自动采集的 state.json）必须同源。

        对不上就是各算各的 —— 那种情况下人会开始只信其中一份，另一份白长。
        """
        files = self._export()
        curated = json.loads(files["notify.json"])["counts"]
        auto = self._state()[0]["modules"]["sf_notify"]["_DELIVER_COUNTS"]
        self.assertEqual(auto, curated,
                         "自动采到的通知计数与人工接线的不是同一份账")

    def test_real_product_state_flows_not_just_synthetic(self):
        """合成模块只证明管线通；这条证明**真产品的写法**确实命中词表。"""
        modules = self._state()[0]["modules"]
        for name in ("sf_notify", "sf_universe", "sf_update", "sf_logging"):
            self.assertIn(name, modules, "%s 没被扫到：产品模块的枚举那一层断了" % name)
            self.assertTrue(modules[name], "%s 扫到了却一条状态都没收到，词表与实际写法脱节" % name)

    # ---- 边界 1：只读数据，一个函数都不调 ----

    def test_the_collector_never_calls_anything_it_finds(self):
        """导出发生在用户机器上：状态哪怕长成一个 callable，也不许被调用。"""
        trap = _Trap()
        self._with_probe(_fail_counter=trap, _last_error_state="ok")
        probe = self._state()[0]["modules"][PROBE_MODULE]
        self.assertEqual(trap.calls, 0, "自动层调用了取值函数：取诊断变成了跑业务")
        self.assertNotIn("_fail_counter", probe, "callable 被当成数据收进了包")

    # ---- 边界 2：外发包里不许有家目录、账号名与凭据 ----

    def test_home_directory_and_account_name_are_scrubbed(self):
        secret_path = os.path.join(_HOME, "Library", "Application Support", "StockFloatTargetPrice")
        account = os.environ.get("USER") or ""
        self._with_probe(_last_state_path=secret_path,
                         _fail_reason="目录 %s 打不开" % secret_path)
        body, files = self._state()
        probe = body["modules"][PROBE_MODULE]
        self.assertNotIn(_HOME, files["state.json"], "家目录原样外发：诊断包是要发给别人的")
        self.assertIn("~", probe["_last_state_path"], "换了标记，但目录结构要留着可判代次")
        if account and len(account) >= 3:
            self.assertNotIn(account, json.dumps(probe, ensure_ascii=False),
                             "登录名跟着字符串外发")

    def test_secret_named_keys_are_dropped_entirely(self):
        token_value = "sk-绝不外发的凭据"
        self._with_probe(_api_token=token_value, _deliver_fail_count=1,
                         password_state="hunter2")
        _body, files = self._state()
        probe = files["state.json"]
        self.assertNotIn(token_value, probe)
        self.assertNotIn("hunter2", probe)
        self.assertIn("_deliver_fail_count", json.loads(probe)["modules"][PROBE_MODULE],
                      "拒收要精确到键，别把整块状态一起丢了")

    def test_secret_values_inside_error_text_are_redacted_too(self):
        self._with_probe(_last_error_state="token=do-not-export Authorization: Bearer abc123")
        body, files = self._state()
        value = body["modules"][PROBE_MODULE]["_last_error_state"]
        self.assertNotIn("do-not-export", value)
        self.assertNotIn("abc123", value)
        self.assertIn("redacted", value.casefold())
        self.assertNotIn("do-not-export", files["state.json"])

    # ---- 边界 3：超限要点名，不许静默 ----

    def test_oversized_containers_are_named_not_silently_cut(self):
        big = {"k%d" % i: i for i in range(sf_diag.MAX_ITEMS + 40)}
        long_reason = "x" * (sf_diag.MAX_STR_LEN + 90)
        self._with_probe(_fail_counts=big, _last_fail_reason=long_reason)
        body, _files = self._state()
        notes = " ".join(body["truncated"])
        # 按**具体那一条**状态查，不查模块名：同一份夹具里字符串截断也带着模块名，
        # 只查模块名的话把 dict 溢出那句留痕删掉，用例照样绿（实测踩过）。
        self.assertIn("._fail_counts", notes,
                      "截断了却不点名是哪一条：读包的人会以为这条状态本来就短")
        self.assertIn("._last_fail_reason", notes, "字符串截断同样要点名")
        probe = body["modules"][PROBE_MODULE]
        self.assertLessEqual(len(probe["_fail_counts"]), sf_diag.MAX_ITEMS)
        self.assertLessEqual(len(probe["_last_fail_reason"]), sf_diag.MAX_STR_LEN + 1)

    def test_state_inside_a_product_object_is_counted_not_invented(self):
        """自动层不替人决定要不要展开那个对象，但必须说「这里还有一层没进去」。"""
        holder_type = type("ProbeStore", (), {})
        holder_type.__module__ = PROBE_MODULE
        self._with_probe(_fail_store=holder_type(), _deliver_fail_count=1)
        probe = self._state()[0]["modules"][PROBE_MODULE]
        self.assertNotIn("_fail_store", probe)
        self.assertGreaterEqual(probe.get("_state_inside_objects", 0), 1,
                               "状态藏在产品实例里却没留痕：读包的人会以为这个模块没有状态")

    def test_reexported_classes_are_not_mistaken_for_state(self):
        """反向：`vars(模块)` 里混着**再导来的类**，它们不是状态，也不该冒一条留痕。

        第一版没区分，实测每个模块都冒出一行 `_dropped_non_serializable: 1` ——
        「这里有个类」被写成「这里有条状态没收上来」，读包的人会当真去找。
        """
        class _ReExported:
            pass

        self._with_probe(_error_state=_ReExported, _deliver_fail_count=1)
        probe = self._state()[0]["modules"][PROBE_MODULE]
        self.assertNotIn("_state_inside_objects", probe,
                         "类被当成了「藏了一层的实例」")
        self.assertNotIn("_error_state", probe)

    # ---- 实例爬取：状态住在主窗持有的对象里（sf_runtime 那一族）----

    def test_instance_attributes_of_a_window_held_object_are_collected(self):
        holder_module = types.ModuleType(PROBE_OBJECT_MODULE)
        session_type = type("ProbeSession", (), {})
        session_type.__module__ = PROBE_OBJECT_MODULE
        holder_module.ProbeSession = session_type
        sys.modules[PROBE_OBJECT_MODULE] = holder_module
        self.addCleanup(sys.modules.pop, PROBE_OBJECT_MODULE, None)

        session = session_type()
        session._retry_count = _SENTINEL  # discipline-ok(R5): 被测对象就是"实例里的私有状态名"，公开写法等于没测这一层
        session._last_error = "连接被服务端关掉"  # discipline-ok(R5): 同上，自动采集层读的就是这类名字
        self.win._probe_session = session  # discipline-ok(R5): 把实例挂到被测窗口只有这一处注入点
        self.addCleanup(delattr, self.win, "_probe_session")
        found = self._state()[0]["objects"].get("ProbeSession")
        self.assertIsNotNone(found, "主窗持有的自有对象没被爬进去：「状态住在实例里」的模块就永远读不出来")
        self.assertEqual(found.get("_retry_count"), _SENTINEL)

    # ---- 诚实性：采集器自己坏了必须点名，且不许连累整包 ----

    def test_a_broken_collector_is_named_and_the_package_still_ships(self):
        with mock.patch.object(sf_diag, "collect", side_effect=RuntimeError("采集器炸了")):
            files = self._export()
        self.assertIn("runtime.json", files, "少一份 JSON 不能连累整包")
        issues_line = files["diagnostics.txt"].split("export_issues:")[-1]
        self.assertIn("state.json", issues_line,
                      "取不到值必须点名（AGENTS §5.7 第二条）：缺文件与「一切正常」不许同形")

    def test_state_json_stays_under_the_package_size_guard(self):
        """最坏情况也不能把 `_json_entry` 的 256KB 守卫顶掉（顶掉＝整份文件变成一行报错）。"""
        self._with_probe(**{"_fail_count_%d" % i: i for i in range(400)})
        _body, files = self._state()
        self.assertLess(len(files["state.json"].encode("utf-8")), 256 * 1024)


    def test_dict_keys_are_scrubbed_too(self):
        """复核 F2：值那半边换了、键没换，等于把"这是谁的机器"留在包里。
        字典以路径为键的写法在产品里是真实存在的形态。"""
        secret_path = os.path.join(_HOME, "Library", "Application Support", "StockFloatTargetPrice")
        self._with_probe(_fail_counts={secret_path: 3, os.path.join(_HOME, "Desktop"): 1})
        _body, files = self._state()
        probe = json.loads(files["state.json"])["modules"][PROBE_MODULE]
        keys = list(probe["_fail_counts"])
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertNotIn(_HOME, key, "键里带着家目录原样外发")
            self.assertIn("~", key, "换了标记，但目录结构要留着才能判是哪一代的数据目录")

    def test_non_finite_floats_do_not_make_the_file_invalid_json(self):
        """复核 F3：`NaN` / `Infinity` 是 Python 的 json 扩展，不是 JSON。
        产品里确有 nan 的来源（sf_ui_render.py:1663/1689 的坐标净化），写出去以后
        浏览器与严格解析器直接报错，等于这一份现场对外不可用。"""
        import math
        self._with_probe(_last_gap_px=float("nan"), _avg_ms=math.inf, _retry_count=2)
        _body, files = self._state()
        strict = json.loads(files["state.json"],
                            parse_constant=lambda bad: (_ for _ in ()).throw(
                                ValueError("非 JSON 常量：" + bad)))
        probe = strict["modules"][PROBE_MODULE]
        self.assertIsNone(probe["_last_gap_px"])
        self.assertIsNone(probe["_avg_ms"])
        self.assertEqual(probe["_retry_count"], 2, "只该影响那两条非有限值")
        self.assertIn("_last_gap_px", " ".join(strict["truncated"]),
                       "写成 null 必须在 truncated 里说清是哪一条")

    def test_account_name_is_only_replaced_as_a_whole_word(self):
        """复核 F9：账号名常常是常见英文子串，字面替换会把正文啃掉。

        这条用例本身被复核改过一次：初版拿本机真账号名（`moriarty`）配 "cancelled"
        做金丝雀，两者根本没有子串关系 → 把词边界删掉用例照样绿（恒真判据）。
        现在把账号名换成 `cell`，它确实在 "cancelled" 里，正反向才同时有牙。
        """
        with mock.patch.dict(os.environ, {"USER": "cell", "LOGNAME": "cell"}, clear=False):
            self._with_probe(_fail_reason="用户 cell 的目录打不开，操作已 cancelled")
            _body, files = self._state()
            reason = json.loads(files["state.json"])["modules"][PROBE_MODULE]["_fail_reason"]
        self.assertIn("<user>", reason, "整词账号名必须换掉")
        self.assertNotIn("用户 cell ", reason, "整词那一次确实换了")
        self.assertIn("cancelled", reason, "不该把正文里撞上的片段一起啃掉")

    def test_design_value_module_is_skipped_and_says_so(self):
        """复核 F8：`sf_constants` 一次贡献 58 条必然等于源码的常量，占体积不回答问题。
        排除必须写在包里，否则读的人以为这个模块没有值。"""
        import sf_constants
        body, _files = self._state()
        self.assertNotIn("sf_constants", body["modules"],
                         "设计值整份该跳过（真需要远程确认的阈值由人工接线那几份说）")
        self.assertIn("sf_constants", body["meta"]["excluded_modules"],
                      "刻意排除要看得见，不能静默少一份")
        # 对照：这个模块确实有一堆命中词表的名字（阈值/间隔都算），所以上一条不是
        # "碰巧什么都没扫到"。不断言 APP_VERSION —— §6 第 1 条把那条路堵着。
        hits = [n for n in vars(sf_constants) if sf_diag.is_state_name(n)]
        self.assertTrue(hits, "对照失败：sf_constants 里没有命中词表的名字，上一条判据就成了空转")


class PureCollectorCase(SFCase):
    """不建窗口的三件事：词表与真实写法对齐、身份词表拒收、采集器自己不留静默。"""

    def test_is_state_name_matches_the_shapes_this_repo_actually_uses(self):
        """五个名字全部来自仓库现有代码。缺一条件就说明词表漏了一族，
        而漏收是**静默**的：没人会去问「为什么这条不在包里」。"""
        for name in ("_DELIVER_COUNTS", "_minute_retry_left", "_gap_episodes",
                     "_REJECTS", "_stale_hint_frames"):
            self.assertTrue(sf_diag.is_state_name(name), "%s 该被认成运行态" % name)

    def test_is_state_name_does_not_match_layout_or_text_names(self):
        for name in ("BOTTOM_ROW_ASCENT", "NUM_FONT_FAMILY", "win"):
            self.assertFalse(sf_diag.is_state_name(name), "%s 不是运行态" % name)

    def test_secret_names_are_refused_by_name_not_by_value(self):
        for name in ("_api_token", "password_state", "_user_mail", "_session_cookie",
                     "_api_key_state", "apiKeyState", "passwordState"):
            self.assertTrue(sf_diag.is_secret_name(name), "%s 该整个拒收" % name)
        self.assertFalse(sf_diag.is_secret_name("_deliver_fail_count"))

    def test_collect_names_a_module_it_could_not_read_instead_of_skipping_it(self):
        broken = types.ModuleType("sf_probe_unreadable")
        sys.modules["sf_probe_unreadable"] = broken
        self.addCleanup(sys.modules.pop, "sf_probe_unreadable", None)
        with mock.patch.object(sf_diag, "_module_names", return_value=["sf_probe_unreadable"]), \
                mock.patch.object(sf_diag, "collect_module", side_effect=RuntimeError("读不动")):
            body = sf_diag.collect(None)
        self.assertTrue(any("sf_probe_unreadable" in str(item) for item in body["issues"]),
                        "读不到的模块必须点名；静默跳过等于「这台机器没有状态」那句假话")

    def test_issue_text_is_scrubbed_like_collected_state(self):
        _install_probe(_fail_count=1)
        self.addCleanup(sys.modules.pop, PROBE_MODULE, None)
        with mock.patch.object(sf_diag, "collect_module",
                               side_effect=RuntimeError("cannot read %s token=secret" % _HOME)):
            body = sf_diag.collect(None)
        text = "\n".join(body["issues"])
        self.assertNotIn(_HOME, text)
        self.assertNotIn("secret", text)
        self.assertIn("redacted", text.casefold())

    def test_object_limit_is_reported_instead_of_silently_dropping_children(self):
        holder_type = type("ProbeHolder", (), {})
        holder_type.__module__ = PROBE_OBJECT_MODULE
        owner = type("ProbeOwner", (), {})()
        for index in range(sf_diag.MAX_OBJECTS + 5):
            setattr(owner, "_child_%d" % index, holder_type())
        body = sf_diag.collect(owner)
        self.assertTrue(any("objects" in item for item in body["truncated"]),
                        "达到对象上限必须留下截断证据")

    def test_quoted_secret_values_with_spaces_are_fully_redacted(self):
        needles = sf_diag.identity_needles([])
        text = 'password="first second third" Authorization: Bearer bearer-probe'
        result = sf_diag.scrub_text(text, needles)
        for secret in ("first", "second", "third", "bearer-probe"):
            self.assertNotIn(secret, result)
        self.assertIn("redacted", result.casefold())

    def test_foreign_home_paths_and_query_signatures_are_scrubbed(self):
        result = sf_diag.scrub_text(
            r'/Users/diagnostic-user/Library/x C:\Users\diagnostic-user\logs '
            'https://user:pwd@example.test/q?key=query-key&sign=query-sign&keep=1', ())
        for secret in ("diagnostic-user", "user:pwd@", "query-key", "query-sign"):
            self.assertNotIn(secret, result)
        self.assertIn("example.test", result)
        self.assertNotIn("keep=1", result)

    def test_query_credential_suffixes_and_authorization_schemes_are_scrubbed(self):
        samples = (
            'https://example.test/q?authcode=credential-probe&token_extra=credential-probe&keep=1',
            'Authorization: Basic credential-probe',
            'Authorization: Digest username="credential-probe", nonce="credential-probe"',
            '{"signature": "credential-probe"}',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                result = sf_diag.scrub_text(sample, ())
                self.assertNotIn("credential-probe", result)
                self.assertIn("REDACTED", result)
                if "keep=1" in sample:
                    self.assertNotIn("keep=1", result)

    def test_unsupported_dictionary_key_never_runs_its_string_conversion(self):
        calls = []
        class Key:
            def __str__(self):
                calls.append("called")
                return "business-state"
        _, encoded = sf_diag.encode({Key(): 1, "safe": 2}, (), 0, [], "probe")
        self.assertEqual(calls, [])
        self.assertEqual(encoded["safe"], 2)

    def test_one_encoding_failure_does_not_drop_the_other_module_state(self):
        module = _install_probe(_fail_count=1, _last_error="good-state")
        self.addCleanup(sys.modules.pop, PROBE_MODULE, None)
        real_encode = sf_diag.encode
        def fail_one(value, needles, depth, truncated, path):
            if path.endswith("._fail_count"):
                raise ValueError("bad value")
            return real_encode(value, needles, depth, truncated, path)
        issues = []
        with mock.patch.object(sf_diag, "encode", side_effect=fail_one):
            result = sf_diag.collect_module(module, (), issues, [])
        self.assertEqual(result["_last_error"], "good-state")
        self.assertIn("_fail_count", " ".join(issues))


if __name__ == "__main__":
    unittest.main(verbosity=2)
