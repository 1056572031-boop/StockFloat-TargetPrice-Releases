# -*- coding: utf-8 -*-
"""「目标价」sf_calendar.py 单测：2026 内置 / >2026 后台拉取 / 降级 / 毒缓存防护。"""
import datetime
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import sf_calendar

# v1.3.68：统一轮询器（见 TESTS_AUDIT_2026-09-14.md 之 P1-4）——
# 取代本文件里三处 "手写 for + break + sleep"，超时不再静默通过。
# 两种 import 形态都要能活：discover 时 tests/ 在 sys.path（顶层模块形态），
# 以包方式 `python -m unittest tests.test_calendar` 时则是 tests._base。
try:
    from _base import wait_for
except ImportError:  # pragma: no cover - 仅在包模式导入时走这里
    from tests._base import wait_for


def _reset():
    """清空所有进程内日历状态（缓存 / 已拉取 / 重试冷却 / 在途）。"""
    sf_calendar._month_cache.clear()
    sf_calendar._fetched_months.clear()
    sf_calendar._inflight.clear()
    sf_calendar._next_retry_at.clear()
    sf_calendar._month_sync_status.clear()


class _CalendarTestBase(unittest.TestCase):
    """把磁盘缓存目录重定向到临时目录，避免测试污染真实用户目录。

    同时禁止真实网络：_warm_async 会在**后台线程**里调 http_get，
    若某条用例漏 mock，请求会在用例结束后才发出，既拖慢测试，
    又会污染全局连接池计数（曾让 test_network_mock 出现与日历无关的失败）。
    """

    def setUp(self):
        _reset()
        self._tmp = tempfile.mkdtemp(prefix="sf-cal-test-")
        self._cache_patch = mock.patch.object(sf_calendar, "_CACHE_DIR", self._tmp)
        self._cache_patch.start()

        def _no_net(*_a, **_k):
            raise AssertionError("测试期间禁止真实网络请求（漏了 mock）")

        self._net_patch = mock.patch.object(sf_calendar, "http_get",
                                            side_effect=_no_net)
        self._net_patch.start()

    def wait_idle(self, timeout=5.0):
        """等待后台预热线程跑完（必须在停止 patch 之前，否则会漏到真实网络）。

        v1.3.68：原先自己 `while time.time() < end: ... time.sleep(0.01)`，
        超时后**静默返回**，外面完全看不出"其实没等到"。改走统一轮询器，
        超时会打印现场，且不再重复实现同一套循环。
        """
        return wait_for(lambda: not sf_calendar._inflight, timeout=timeout,
                        msg="日历后台预热线程未在 %.1fs 内结束" % timeout)

    def tearDown(self):
        # v1.4.60（复核 F14）：原先是"等不到只打印一行，然后照样撤桩"。撤桩等于把在飞
        # 线程放回真实世界 —— 网络桩一停，它真的去访问交易所接口（本轮复核时就实测到
        # 这一行）；缓存桩一停，它往用户真实的日历缓存目录里写。所以：等到才撤，
        # 等不到就把两个桩的撤除排到收尾（addCleanup 是后进先出，先撤桩再删临时目录），
        # 并且**判红**，不再静默通过。
        idle = self.wait_idle()
        if idle:
            self._net_patch.stop()
            self._cache_patch.stop()
            shutil.rmtree(self._tmp, ignore_errors=True)
        else:
            self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
            self.addCleanup(self._cache_patch.stop)
            self.addCleanup(self._net_patch.stop)
        _reset()
        self.assertTrue(idle, "日历后台预热线程 5 秒内没走完 —— 见上：此时桩必须延后撤除")


class TestDiskCalendarRecovery(_CalendarTestBase):
    def test_disk_cache_is_confirmed_after_process_restart(self):
        """已成功落盘的官方日历在重启后仍可直接用于目标价判定。"""
        ym = "2027-01"
        path = sf_calendar._month_cache_path(ym)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"holidays": ["2027-01-01", "2027-01-02"]}, f)
        self.assertTrue(sf_calendar._load_month_cache(ym))
        self.assertIn(ym, sf_calendar._fetched_months)
        self.assertFalse(sf_calendar.is_trading_day_confirmed(datetime.date(2027, 1, 1)))
        self.assertTrue(sf_calendar.is_trading_day_confirmed(datetime.date(2027, 1, 4)))


class TestCalendar2026(_CalendarTestBase):
    """2026 路径应完全命中内置 TRADING_HOLIDAYS，不发网络请求。"""
    def test_next_around_national_day(self):
        # 9-30 周三 → 跳过国庆 10-01~10-07 → 10-08 开市
        self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 9, 30)),
                         datetime.date(2026, 10, 8))
    def test_next_spring_festival(self):
        self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 2, 14)),
                         datetime.date(2026, 2, 24))
        self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 2, 18)),
                         datetime.date(2026, 2, 24))
    def test_is_holiday_2026(self):
        self.assertTrue(sf_calendar.is_holiday(datetime.date(2026, 10, 1)))
        self.assertTrue(sf_calendar.is_holiday(datetime.date(2026, 2, 18)))
        self.assertFalse(sf_calendar.is_holiday(datetime.date(2026, 2, 24)))
        self.assertFalse(sf_calendar.is_holiday(datetime.date(2026, 9, 4)))
    def test_no_network_for_2026(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               autospec=True) as m:
            sf_calendar.next_trading_day(datetime.date(2026, 9, 30))
            sf_calendar.is_holiday(datetime.date(2026, 10, 1))
            m.assert_not_called()


class TestNoBlockingNetwork(_CalendarTestBase):
    """R32-2：is_holiday / next_trading_day 在调用线程上绝不能发网络请求。

    调用点位于 UI 线程（目标价「立即生效」→ make_alert → _session_date_for），
    同步 http_get 会让界面卡住最长约 16 秒（timeout=8 × retries=1）。
    """

    @staticmethod
    def _spy_out_of_thread(fn):
        """执行 fn，返回「拉取动作发生在哪些线程」；等待后台线程跑完。"""
        main_ident = threading.get_ident()
        seen = []

        def _spy(ym):
            seen.append(threading.get_ident())
            return None

        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               side_effect=_spy):
            fn()
        # v1.3.68：手写 for+break+sleep 换成统一轮询，超时不再静默通过
        wait_for(lambda: bool(seen), timeout=2.0, msg="后台补齐线程未执行")
        return main_ident, seen

    def test_is_holiday_never_fetches_on_calling_thread(self):
        main_ident, seen = self._spy_out_of_thread(
            lambda: sf_calendar.is_holiday(datetime.date(2027, 3, 5)))
        self.assertTrue(seen, "缓存缺失时应触发后台补齐")
        self.assertNotIn(main_ident, seen, "拉取不得发生在调用线程上")

    def test_next_trading_day_never_fetches_on_calling_thread(self):
        main_ident, seen = self._spy_out_of_thread(
            lambda: sf_calendar.next_trading_day(datetime.date(2026, 12, 31)))
        self.assertTrue(seen, "缓存缺失时应触发后台补齐")
        self.assertNotIn(main_ident, seen, "拉取不得发生在调用线程上")

    def test_past_year_never_fetches(self):
        """历史年份不发请求（数据已过期且无意义）。"""
        with mock.patch.object(sf_calendar, "_fetch_month_holidays") as m:
            self.assertFalse(sf_calendar.is_holiday(datetime.date(2025, 10, 1)))
            m.assert_not_called()


class TestBackgroundFetch(_CalendarTestBase):
    """>2026：后台线程拉取，成功后下次调用即命中缓存。"""

    def test_warm_async_fills_cache(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value={"2027-01-01"}):
            t = sf_calendar._warm_async("2027-01")
            self.assertIsNotNone(t)
            t.join(timeout=5)
        self.assertTrue(sf_calendar.is_holiday(datetime.date(2027, 1, 1)))
        self.assertIn("2027-01", sf_calendar._fetched_months)

    def test_warm_async_persists_to_disk(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value={"2027-01-01", "2027-01-04"}):
            t = sf_calendar._warm_async("2027-01")
            t.join(timeout=5)
        path = os.path.join(self._tmp, "calendar_2027-01.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(set(data["holidays"]), {"2027-01-01", "2027-01-04"})

    def test_after_warm_next_trading_day_respects_holiday(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value={"2027-01-01"}):
            t = sf_calendar._warm_async("2027-01")
            t.join(timeout=5)
        # 12-31 周四 → 跳过 01-01(假)/01-02(六)/01-03(日) → 01-04(一)
        self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 12, 31)),
                         datetime.date(2027, 1, 4))

    def test_degrade_before_warm(self):
        """缓存未就绪时降级为「仅按周末判断」，不报错。"""
        self.assertFalse(sf_calendar.is_holiday(datetime.date(2027, 1, 1)))
        self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 12, 31)),
                         datetime.date(2027, 1, 1))

    def test_future_weekday_is_unconfirmed_until_official_calendar_arrives(self):
        """目标价不能将未来工作日缓存缺失误判为已确认交易日。"""
        self.assertIsNone(sf_calendar.is_trading_day_confirmed(datetime.date(2027, 1, 1)))
        sf_calendar._fetched_months.add("2027-01")
        # 已同步月份的最终结论必须使用官方休市结果，而非此前的降级缓存。
        with mock.patch.object(sf_calendar, "is_holiday", return_value=True):
            self.assertFalse(sf_calendar.is_trading_day_confirmed(datetime.date(2027, 1, 1)))

    def test_sync_status_transitions_without_network_on_read(self):
        """状态查询只读且不阻塞；后台完成后由 syncing 转为 ready。"""
        self.assertEqual(sf_calendar.calendar_sync_status(["2027-01"])["2027-01"], "idle")
        started = threading.Event()
        release = threading.Event()

        def _delayed_fetch(_ym):
            started.set()
            release.wait(timeout=5)
            return {"2027-01-01"}

        with mock.patch.object(sf_calendar, "_fetch_month_holidays", side_effect=_delayed_fetch):
            t = sf_calendar._warm_async("2027-01")
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(sf_calendar.calendar_sync_status(["2027-01"])["2027-01"], "syncing")
            release.set()
            t.join(timeout=5)
        self.assertEqual(sf_calendar.calendar_sync_status(["2027-01"])["2027-01"], "ready")

    def test_warm_async_skips_when_inflight(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value={"2027-01-01"}) as m:
            # v1.4.60（复核 F14 的真根因）：这里"假装 2027-01 正在拉取"是直接往
            # _inflight 里塞一个月份，而它**不是线程写的、没人会摘掉它**。
            # tearDown 等的是 `_inflight` 变空，于是这条用例每次都白等满 5 秒超时 ——
            # 而超时原先不判红，这件事就一直藏在"全量绿但每次多花 5 秒"里。
            # 谁种下的状态谁收掉，而且必须在 tearDown 之前收（addCleanup 排在 tearDown
            # 之后，等于没收）。
            sf_calendar._inflight.add("2027-01")
            try:
                self.assertIsNone(sf_calendar._warm_async("2027-01"))
                m.assert_not_called()
            finally:
                sf_calendar._inflight.discard("2027-01")   # discipline-ok(R5): 在飞标记只有这个集合，模拟"已有线程"没有公开入口


class TestEmptyResponseNotCached(_CalendarTestBase):
    """R32-1 毒缓存防护：官方未发布月份返回 0 行，绝不能当成「无休市日」。

    否则会把「无数据」落盘永久生效 —— 2027 全年春节/元旦都被判成交易日，
    且重启后也不会重新拉取。
    """

    def test_fetch_returns_none_on_zero_rows(self):
        body = json.dumps({"data": []})
        with mock.patch.object(sf_calendar, "http_get", return_value=body):
            self.assertIsNone(sf_calendar._fetch_month_holidays("2027-01"))

    def test_fetch_returns_none_on_blank_body(self):
        with mock.patch.object(sf_calendar, "http_get", return_value=""):
            self.assertIsNone(sf_calendar._fetch_month_holidays("2027-01"))

    def test_fetch_returns_none_on_bad_json(self):
        with mock.patch.object(sf_calendar, "http_get", return_value="<html>502</html>"):
            self.assertIsNone(sf_calendar._fetch_month_holidays("2027-01"))

    def test_empty_result_not_persisted(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value=set()) as m:
            sf_calendar._ensure_month("2027-01")
        self.assertNotIn("2027-01", sf_calendar._fetched_months)
        self.assertFalse(os.path.exists(os.path.join(self._tmp, "calendar_2027-01.json")))
        m.assert_called_once()

    def test_empty_result_allows_retry_after_cooldown(self):
        """拉取失败不应永久标记，冷却期过后允许重试。"""
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value=None) as m:
            sf_calendar._ensure_month("2027-01")
            self.assertEqual(m.call_count, 1)
            # 冷却期内不重试
            sf_calendar._ensure_month("2027-01")
            self.assertEqual(m.call_count, 1)
            self.assertNotIn("2027-01", sf_calendar._fetched_months)
            # 冷却期过后可重试
            sf_calendar._next_retry_at["2027-01"] = 0.0
            sf_calendar._ensure_month("2027-01")
            self.assertEqual(m.call_count, 2)

    def test_real_szse_semantics_jybz_zero_is_closed(self):
        """锁定深交所字段语义：jybz == "0" 表示休市（防后人改反）。"""
        body = json.dumps({"data": [
            {"jybz": "1", "jyrq": "2027-03-01"},
            {"jybz": "0", "jyrq": "2027-03-02"},
        ]})
        with mock.patch.object(sf_calendar, "http_get", return_value=body):
            hol = sf_calendar._fetch_month_holidays("2027-03")
        self.assertEqual(hol, {"2027-03-02"})


class TestNextTradingDayBounded(_CalendarTestBase):
    """R32-3：日历数据异常（恒为休市）时不得在调用线程死循环。"""

    def test_bounded_when_every_day_is_holiday(self):
        """数据异常（恒为休市）时：不死循环，扫描窗口内降级返回第一个工作日。"""
        with mock.patch.object(sf_calendar, "_MAX_SCAN_DAYS", 50), \
             mock.patch.object(sf_calendar, "is_holiday", return_value=True):
            # 2027-01-01 是窗口内第一个工作日（周五）
            self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 12, 31)),
                             datetime.date(2027, 1, 1))

    def test_bounded_when_holiday_data_missing(self):
        """缓存全空 + 拉取恒失败：仍要在有限步内返回，不得挂死。"""
        with mock.patch.object(sf_calendar, "_MAX_SCAN_DAYS", 20), \
             mock.patch.object(sf_calendar, "is_holiday", return_value=True):
            got = sf_calendar.next_trading_day(datetime.date(2027, 2, 26))
        self.assertIsInstance(got, datetime.date)
        self.assertLess(got.weekday(), 5)

    def test_normal_path_unaffected(self):
        self.assertEqual(sf_calendar.next_trading_day(datetime.date(2026, 9, 30)),
                         datetime.date(2026, 10, 8))


class TestPrefetch(_CalendarTestBase):
    def test_prefetch_ignores_invalid_and_warms_valid(self):
        with mock.patch.object(sf_calendar, "_fetch_month_holidays",
                               return_value={"2027-02-08"}) as m:
            sf_calendar.prefetch_months_async(["2027-02", None, "", 123])
            # v1.3.68：同上，超时带现场信息，不再"没等到也照样往下走"
            wait_for(lambda: "2027-02" in sf_calendar._fetched_months,
                     timeout=2.0, msg="2027-02 未被后台预热填充")
        self.assertEqual(m.call_count, 1)
        self.assertTrue(sf_calendar.is_holiday(datetime.date(2027, 2, 8)))

    def test_prefetch_empty_iterable_is_noop(self):
        sf_calendar.prefetch_months_async(None)
        sf_calendar.prefetch_months_async([])
        self.assertEqual(sf_calendar._inflight, set())


class TestFutureHolidayRouting(_CalendarTestBase):
    """R43：2027+ 休市日必须被 market_status / _is_trading_time 识别。

    此前 sf_quote 只用内置 2026 TRADING_HOLIDAYS，2027 春节会被误判为交易日；
    v1.2.80 改为统一走 sf_calendar.is_holiday（未来月表走缓存、不在调用线程发网络）。
    """

    @staticmethod
    def _fake_2027_02(*_a, **_k):
        # 2027 春节（假想休市日）+ 正常工作日
        rows = [{"jyrq": "2027-02-17", "jybz": "0"},
                {"jyrq": "2027-02-18", "jybz": "0"},
                {"jyrq": "2027-02-09", "jybz": "1"}]
        return json.dumps({"data": rows})

    def test_is_holiday_future_via_cache(self):
        with mock.patch.object(sf_calendar, "http_get",
                               side_effect=self._fake_2027_02):
            sf_calendar._ensure_month("2027-02")
        self.assertTrue(sf_calendar.is_holiday(datetime.date(2027, 2, 17)))
        self.assertFalse(sf_calendar.is_holiday(datetime.date(2027, 2, 9)))

    def test_quote_routing_sees_future_holiday(self):
        with mock.patch.object(sf_calendar, "http_get",
                               side_effect=self._fake_2027_02):
            sf_calendar._ensure_month("2027-02")
        import sf_quote
        tz = datetime.timezone(datetime.timedelta(hours=8))
        # 春节当天 10:00 → 应判为休市（非交易时段）
        self.assertFalse(sf_quote._is_trading_time(
            datetime.datetime(2027, 2, 17, 10, 0, tzinfo=tz)))
        # 普通交易日 10:00 → 应判为交易中
        self.assertTrue(sf_quote._is_trading_time(
            datetime.datetime(2027, 2, 9, 10, 0, tzinfo=tz)))
        # market_status 文案也应是「休市」
        self.assertEqual(sf_quote.market_status(
            datetime.datetime(2027, 2, 17, 10, 0, tzinfo=tz))[0], "休市")


if __name__ == "__main__":
    unittest.main()


class TestFetchFailureIsAudible(_CalendarTestBase):
    """S5（v1.4.58）：四种失败原因必须各留一行日志。

    旧实现是一整句 `try: … except Exception: return None` 零日志 —— 界面上只剩
    一个 degraded 角标，事后完全查不到"这个月为什么永远拉不到"（网络挂了？
    jybz/jyrq 字段改名？官方换了响应形状？），而它的后果是**法定假日被当交易日**：
    目标价在假日被清空、提醒口径全错。按 §6 第 9 条：兜异常可以，静默不行。
    """

    def _fetch(self, side_effect=None, returns=None):
        with mock.patch.object(sf_calendar, "http_get",
                               side_effect=side_effect, return_value=returns), \
             mock.patch.object(sf_calendar, "log_line") as lg, \
             mock.patch.object(sf_calendar, "log_line_throttled") as lt:
            out = sf_calendar._fetch_month_holidays("2027-03")   # discipline-ok(R5): 同上：调的是宿主上绑的产品真方法
        return out, [c.args[0] for c in lg.call_args_list], lt.call_count

    def test_network_failure_names_the_reason(self):
        out, msgs, _ = self._fetch(side_effect=OSError("connection reset"))
        self.assertIsNone(out)
        self.assertTrue(any("网络" in m for m in msgs), "网络失败要说清是网络：%s" % msgs)

    def test_empty_body_names_the_reason(self):
        out, msgs, _ = self._fetch(returns="")
        self.assertIsNone(out)
        self.assertTrue(any("响应为空" in m for m in msgs), msgs)

    def test_non_json_body_is_quoted(self):
        out, msgs, _ = self._fetch(returns="<html>gateway</html>")
        self.assertIsNone(out)
        self.assertTrue(any("不是合法 JSON" in m and "gateway" in m for m in msgs),
                        "报错要带上响应开头，否则换数据源时无从判断：%s" % msgs)

    def test_rows_without_holidays_reports_shape_change(self):
        body = json.dumps({"data": [{"jybz": "1", "jyrq": "2027-03-02"}]})
        out, msgs, _ = self._fetch(returns=body)
        self.assertIsNone(out, "一行休市日都没有 = 字段改名，不能当成「该月不休市」")
        self.assertTrue(any("形态异常" in m for m in msgs), msgs)

    def test_unpublished_month_is_not_a_warning_storm(self):
        """官方未发布（0 行）是常态：走节流，不占 WARNING。"""
        out, msgs, throttled = self._fetch(returns=json.dumps({"data": []}))
        self.assertIsNone(out)
        self.assertEqual(msgs, [], "0 行不该刷 WARNING：%s" % msgs)
        self.assertEqual(throttled, 1, "但必须留一条可查的 INFO")

    def test_success_logs_no_failure_reason(self):
        body = json.dumps({"data": [{"jybz": "0", "jyrq": "2027-03-05"}]})
        out, msgs, throttled = self._fetch(returns=body)
        self.assertEqual(out, {"2027-03-05"})
        self.assertEqual((msgs, throttled), ([], 0), "成功路径不许刷失败日志")
