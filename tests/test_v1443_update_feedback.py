# -*- coding: utf-8 -*-
"""v1.4.43 检查更新的可见性契约（本轮四条改动各守一条，全部只看公开行为）。

为什么这一轮专门给它写用例：修复前用离屏探针按真实分支跑过，
「右键→应用被激活→点检查更新」这条序列下**用户看到的弹窗数量是 0**，
而日志里一行都不留 —— 既看不见、也查不到，正是最该被用例钉住的那类缺陷。

守的五件事：
  1. 手动点击永不静默：撞上在飞检查时不重复发请求，但点击由那次回包兑现；
  2. 已缓存新版优先：点击直接弹确认，不看任何在飞状态；
  3. 正在下载时点击要说清在跑什么；
  4. 激活补查加节流（用墙钟，唤醒后仍要查）；
  5. 取清单的超时预算覆盖实测首字节耗时，瞬时失败可重试、内容不对不重试，
     且每一次检查都必须在日志里留一行结果。

写法规避：不断言私有成员的状态（点击有没有被记账，用「后续回包有没有弹框」
来证明），不 sleep 后断言，不写死版本号。被检函数本身是 FloatWindow 的私有
方法 —— 不构造完整主窗（读配置、起线程、抢单实例 socket）就只能按方法直接调，
所以把这类引用**集中在模块级这四处**豁免，用例正文一律用别名。
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_constants  # noqa: E402
import sf_update  # noqa: E402
import stock_float_window  # noqa: E402
from sf_constants import UPDATE_RESUME_MIN_GAP_SEC  # noqa: E402
from stock_float_window import FloatWindow  # noqa: E402
from tests._base import SFCase  # noqa: E402

_check_bg = FloatWindow._check_updates_background  # discipline-ok(R5): 被测对象本身就是私有方法，豁免集中此一处
_handle_result = FloatWindow._handle_update_check  # discipline-ok(R5): 同上，用例正文只引用这里的别名
_handle_download = FloatWindow._handle_update_download  # discipline-ok(R5): 同上
_prompt = FloatWindow._prompt_update  # discipline-ok(R5): 同上
_handle_prompt_answer = FloatWindow._handle_update_prompt_answer  # discipline-ok(R5): 同上
_resume_check = FloatWindow._check_updates_after_resume  # discipline-ok(R5): 同上
_show_checking = FloatWindow._show_checking_dialog  # discipline-ok(R5): 同上
_show_previous_result = FloatWindow._show_previous_update_result  # discipline-ok(R5): 同上

_NEW = sf_update.UpdateInfo(
    "9.9.9", "本轮说明",
    "https://github.com/o/r/releases/download/v/a.zip", 8, "a" * 64)

_QAPP = None


def _ensure_qapp():
    """QApplication 必须由模块级引用持有，否则离屏下会中途 abort（同 test_update.py 的做法）。"""
    global _QAPP
    if _QAPP is None:
        from PyQt6.QtWidgets import QApplication
        _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


def _fake(prompts=None, messages=None, feedback=None, downloads=None, **overrides):
    """只装配被检函数用到的成员；记录点都用列表，不去读被检对象的内部状态。"""
    base = {
        "_threads_stopped": False,
        "_update_checking": False,
        "_update_downloading": False,
        "_update_transition_pending": False,
        "_update_pending": None,
        "_update_ready_archive": None,
        "_update_ready_version": None,
        "_update_prompt_when_ready": False,
        "_ready_update_archive": lambda self, value: None,
        "_update_check_dialog": None,
        "_update_last_check_at": 0.0,
        # 本文件的用例都在测"点击被在飞检查接住"，一律把这次检查当作**还活着**；
        # 过期自愈那条由 tests/test_v1444_update_stale_cancel.py 专门测。
        "_update_check_stale": lambda self: False,
        "_update_failure_count": 0,
        "_update_retry_timer": None,
        "_prompt_update": lambda self, value: (
            prompts.append(value) if prompts is not None else None),
        "_show_update_message": lambda self, *a, **k: (
            messages.append(a) if messages is not None else None),
        "_show_checking_dialog": lambda self: (
            feedback.append("checking") if feedback is not None else None),
        "_download_update_background": lambda self, value, **kwargs: (
            downloads.append((value, kwargs)) if downloads is not None else None),
        "_close_update_flow_box": lambda self: None,
        "_schedule_update_retry": lambda self: None,
    }
    base.update(overrides)
    return type("FakeWindow", (), base)()


_IN_FLIGHT = {"_update_checking": True, "_update_transition_pending": True}


class ClickNeverSilentTest(SFCase):
    def test_click_during_inflight_check_is_answered_by_that_check(self):
        """撞上在飞检查：不重复发请求，但点击必须由那次回包兑现成确认框。"""
        prompts, feedback = [], []
        host = _fake(prompts=prompts, feedback=feedback, **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread") as thread:
            _check_bg(host, manual=True)
        thread.assert_not_called()
        self.assertEqual(feedback, ["checking"], "点击必须立刻有可见反馈")
        _handle_result(host, False, _NEW, None)
        self.assertEqual(prompts, [_NEW],
                         "用户点过：有新版本就直接弹确认，不再静默缓存")

    def test_the_answer_is_consumed_only_once(self):
        """一次点击只兑一次：兑现之后的静默回包不得再莫名弹框。"""
        prompts, messages = [], []
        host = _fake(prompts=prompts, messages=messages, **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)
        _handle_result(host, False, _NEW, None)
        _handle_result(host, False, None, None)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(messages, [])

    def test_inflight_result_reports_latest_version_when_user_clicked(self):
        messages, feedback = [], []
        host = _fake(messages=messages, feedback=feedback, **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)
        _handle_result(host, False, None, None)
        self.assertEqual(messages, [("检查更新", "当前已是最新版本。")])

    def test_inflight_result_reports_failure_when_user_clicked(self):
        messages, retries = [], []
        host = _fake(messages=messages,
                     _schedule_update_retry=lambda self: retries.append("retry"),
                     **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)
        _handle_result(host, False, None, sf_update.UpdateError("无法连接更新服务"))
        self.assertEqual(len(messages), 1, "失败也要照实汇报")
        self.assertEqual(retries, ["retry"], "失败后仍要照常排退避补查")

    def test_silent_result_downloads_without_showing_a_confirmation_first(self):
        """自动检查不先抢焦点，发现新版后直接后台下载，完成后再提示。"""
        prompts, messages, feedback, downloads = [], [], [], []
        host = _fake(prompts=prompts, messages=messages, feedback=feedback,
                     downloads=downloads)
        _handle_result(host, False, _NEW, None)
        self.assertEqual((prompts, messages, feedback), ([], [], []))
        self.assertEqual(downloads, [(_NEW, {
            "prompt_when_ready": True, "show_progress": False})])

    def test_silent_check_never_opens_a_checking_dialog(self):
        """静默自检（启动 15 秒 / 激活补查 / 6 小时复查 / 退避补查）一律不给框。

        这条是本轮改动过程中**自己写出来的回归**：把可见反馈抽成公共方法之后，
        漏掉 manual 判断就会让看盘时被抢焦点。加强过的既有用例也逮到过同一次。
        """
        feedback = []
        host = _fake(feedback=feedback)
        with patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=False)
        self.assertEqual(feedback, [])

    def test_cached_offer_wins_over_inflight_guard(self):
        """已缓存新版时点击立刻弹确认，哪怕此刻正有检查在飞。"""
        prompts = []
        host = _fake(prompts=prompts, _update_pending=_NEW, **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread") as thread:
            _check_bg(host, manual=True)
        thread.assert_not_called()
        self.assertEqual(prompts, [_NEW])

    def test_click_while_downloading_says_what_is_running(self):
        messages = []
        host = _fake(messages=messages, _update_downloading=True)
        with patch("stock_float_window.threading.Thread") as thread:
            _check_bg(host, manual=True)
        thread.assert_not_called()
        self.assertEqual(len(messages), 1, "下载中点击也要讲清楚，不能毫无反应")

    def test_download_completion_shows_ready_to_restart_prompt(self):
        prompts = []
        host = _fake(prompts=prompts, _update_downloading=True,
                     _update_prompt_when_ready=True)
        _handle_download(host, _NEW, "/tmp/Skyrocket-9.9.9.zip", None)
        self.assertEqual(prompts, [_NEW])

    def test_ready_prompt_uses_restart_button_and_keeps_release_notes(self):
        import queue
        import sf_system_alert
        captured = {}

        def show_alert(**kwargs):
            captured.update(kwargs)
            return object()

        host = type("ReadyPromptHost", (), {
            "_quitting": False,
            "_update_prompt_dialog": None,
            "_dispose_update_dialog": lambda self, dialog: None,
            "_anchor_update_dialog_screen": lambda self: None,
            "_ready_update_archive": lambda self, value: "/tmp/Skyrocket-9.9.9.zip",
            "q": queue.Queue(),
        })()
        with patch.object(sf_system_alert, "show_alert", show_alert), \
                patch.object(sf_system_alert, "bundled_icon_path", return_value=None):
            _prompt(host, _NEW)
        self.assertEqual(captured["buttons"], ("暂不更新", "更新并重启应用"))
        self.assertEqual(captured["default"], "更新并重启应用")
        self.assertEqual(captured["cancel"], "暂不更新")
        self.assertIn("本轮说明", captured["info"])

    def test_deferring_ready_update_does_not_launch_and_restart_uses_cached_zip(self):
        events = []
        host = type("ReadyAnswerHost", (), {
            "_quitting": False,
            "_update_prompt_dialog": object(),
            "_update_pending": _NEW,
            "_dispose_update_dialog": lambda self, dialog: None,
            "_ready_update_archive": lambda self, value: "/tmp/Skyrocket-9.9.9.zip",
            "_launch_downloaded_update": lambda self, update, archive: (
                events.append((update, archive))),
            "_download_update_background": lambda self, *args, **kwargs: (
                events.append(("redownload", args, kwargs))),
        })()
        _handle_prompt_answer(host, "暂不更新")
        self.assertEqual(events, [])
        _handle_prompt_answer(host, "更新并重启应用")
        self.assertEqual(events, [(_NEW, "/tmp/Skyrocket-9.9.9.zip")])

    def test_repeated_clicks_do_not_stack_checking_dialogs(self):
        """真 QWidget 宿主：连点两次只弹一张「正在检查更新」，不叠第二张。

        这一条只能拿真的 QWidget 验：可见反馈那段有 isinstance(self, QWidget) 的
        守卫（宿主是测试替身或没有 Qt 环境时不该动界面），用替身会整段跳过等于没测。
        状态与借用的方法都写在类体里 —— 写成 host.属性 = … 就是在读私有成员。
        """
        _ensure_qapp()
        import sf_system_alert
        calls = []

        def fake_show_alert(**kw):
            calls.append(kw.get("text"))
            return object()

        class Host(stock_float_window.QWidget):
            _threads_stopped = False
            _update_checking = True
            _update_transition_pending = True
            _update_downloading = False
            _update_pending = None
            _update_check_dialog = None
            _update_last_check_at = 0.0
            _update_check_stale = lambda self: False   # 测"不叠框"，这次检查当作还活着
            _anchor_update_dialog_screen = lambda self: None
            _check_updates_background = _check_bg
            _show_checking_dialog = _show_checking

        host = Host()
        self.addCleanup(host.deleteLater)
        with patch.object(sf_system_alert, "show_alert", fake_show_alert), \
             patch.object(sf_system_alert, "bundled_icon_path", return_value=None), \
             patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)
            _check_bg(host, manual=True)
        self.assertEqual(calls, ["正在检查更新，请稍候…"], "重复点击不得叠第二张框")


class ResumeThrottleTest(SFCase):
    def _started(self, since_last):
        from PyQt6.QtCore import Qt
        started = []
        host = _fake(_update_last_check_at=time.time() - since_last,
                     _check_updates_background=lambda self: started.append("check"))
        _resume_check(host, Qt.ApplicationState.ApplicationActive)
        return started

    def test_activation_inside_gap_is_ignored(self):
        """一个间隔内的连续激活不该各发一次网络请求。"""
        self.assertEqual(self._started(UPDATE_RESUME_MIN_GAP_SEC / 2.0), [])

    def test_activation_after_gap_checks(self):
        self.assertEqual(self._started(UPDATE_RESUME_MIN_GAP_SEC + 1.0), ["check"])

    def test_wake_from_long_sleep_still_checks(self):
        """节流用墙钟：monotonic 在 macOS 睡眠期间不前进，用它会把唤醒补查误吞。"""
        self.assertEqual(self._started(3600.0), ["check"])


class ManifestNetworkBudgetTest(SFCase):
    def test_timeout_covers_measured_first_byte_latency(self):
        # 数字来源：本机 2026-10-08 对 UPDATE_MANIFEST_URL 实测 6 次，首字节
        # 7.38 / 7.70 / 7.94 / 8.04 / 8.25 秒，另有 1 次 12 秒无响应。
        # 旧预算硬编码 5 秒 —— 在这条链路上每次检查都必失败。
        self.assertGreaterEqual(sf_constants.UPDATE_MANIFEST_TIMEOUT_SEC, 12)

    def test_transient_failure_retries_once_then_succeeds(self):
        calls = []

        def flaky(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise sf_update.UpdateError("无法连接更新服务")
            return b"manifest-bytes"

        with patch.object(sf_update, "fetch_manifest", flaky), \
             patch.object(sf_update, "verify_manifest", lambda raw: _NEW), \
             patch.object(sf_update, "is_newer", lambda *a, **k: True):
            got = sf_update.check_for_update("1.0.0")
        self.assertIs(got, _NEW)
        self.assertEqual(len(calls), sf_constants.UPDATE_MANIFEST_ATTEMPTS)
        self.assertTrue(all(t >= 12 for t in calls), "重试沿用放宽后的预算")

    def test_persistent_failure_still_raises(self):
        def always_fail(timeout=None):
            raise sf_update.UpdateError("无法连接更新服务")

        with patch.object(sf_update, "fetch_manifest", always_fail), \
             patch.object(sf_update, "verify_manifest") as verify:
            with self.assertRaises(sf_update.UpdateError):
                sf_update.check_for_update("1.0.0")
            verify.assert_not_called()

    def test_untampered_content_is_never_fetched_twice(self):
        """验签失败是「内容不对」而不是「链路抖动」：重试只会白跑一次网络。"""
        fetches = []

        def ok(timeout=None):
            fetches.append(1)
            return b"manifest-bytes"

        with patch.object(sf_update, "fetch_manifest", ok), \
             patch.object(sf_update, "verify_manifest",
                          side_effect=sf_update.UpdateError("更新清单签名校验失败")):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.check_for_update("1.0.0")
        self.assertEqual(len(fetches), 1)


class PreviousInstallResultTest(SFCase):
    def test_non_success_results_are_shown_and_consumed_once(self):
        shown = []
        host = type("Host", (), {
            "_show_update_message": lambda self, *args: shown.append(args),
        })()
        results = [
            {"status": "rolled_back"},
            {"status": "rollback_failed"},
            {"status": "failed"},
        ]
        with patch.object(sf_update, "consume_install_result",
                          side_effect=results + [None]):
            for _ in results:
                _show_previous_result(host)
        self.assertEqual(len(shown), 3)
        self.assertIn("已自动保留原版本", shown[0][1])
        self.assertIn("自动恢复原版本失败", shown[1][1])
        self.assertEqual(shown[2], ("更新未完成", "更新未完成，当前版本未受影响。"))

    def test_installed_or_missing_result_is_silent(self):
        shown = []
        host = type("Host", (), {
            "_show_update_message": lambda self, *args: shown.append(args),
        })()
        with patch.object(sf_update, "consume_install_result",
                          side_effect=[{"status": "installed"}, None]):
            _show_previous_result(host)
            _show_previous_result(host)
        self.assertEqual(shown, [])

    def test_read_failure_is_logged_and_does_not_escape(self):
        lines = []
        host = type("Host", (), {
            "_show_update_message": lambda self, *args: self,
        })()
        with patch.object(sf_update, "consume_install_result",
                          side_effect=OSError("bad result")), \
                patch.object(stock_float_window, "log_line",
                             lambda msg, *args: lines.append(msg)):
            _show_previous_result(host)
        self.assertTrue(any("update result read failed" in line for line in lines))


class CheckTraceabilityTest(SFCase):
    def test_every_check_outcome_is_logged(self):
        """此前只有失败才写日志，成功与「点击被复用」全是空白 —— 本轮 bug 就这么被藏住。"""
        lines = []
        with patch.object(stock_float_window, "log_line",
                          lambda msg, *a, **k: lines.append(msg)):
            _handle_result(_fake(), True, None, None)
        self.assertTrue(any("update check done" in line for line in lines),
                        "每次检查都要留下一行可查的结果")

    def test_reused_click_is_logged(self):
        lines = []
        host = _fake(**_IN_FLIGHT)
        with patch.object(stock_float_window, "log_line",
                          lambda msg, *a, **k: lines.append(msg)), \
             patch("stock_float_window.threading.Thread"):
            _check_bg(host, manual=True)
        self.assertTrue(any("reused in-flight" in line for line in lines),
                        "点击被复用这件事本身也要可查")


class CheckingDialogIsNotAPrerequisiteTest(SFCase):
    """「正在检查」框只是提示：它出错不能拖垮检查本身。

    用户机器上的实录：安装包在程序运行中被换掉，惰性 import 系统弹窗模块时
    归档读出坏数据（zlib "Error -3 incorrect header check"），异常一路冒出，
    检查没发起，在飞标记又留在真值，直到被判过期才放开。
    """

    def _run_manual_check(self, show_checking):
        """跑一次手动检查，返回 (线程启动次数, 被留痕的 (位置, 异常) 列表, 宿主)。"""
        swallowed = []
        host = _fake(_show_checking_dialog=show_checking)
        with patch("stock_float_window.threading.Thread") as thread, \
             patch.object(stock_float_window, "log_swallow",
                          lambda where, exc, *a, **k: swallowed.append((where, exc))):
            _check_bg(host, manual=True)
        return thread.return_value.start.call_count, swallowed, host

    def test_dialog_failure_still_starts_the_check_and_leaves_a_trace(self):
        import zlib
        failure = zlib.error("Error -3 while decompressing data: incorrect header check")

        def broken_dialog(self):
            raise failure

        started, swallowed, _host = self._run_manual_check(broken_dialog)
        self.assertEqual(started, 1, "等待框抛错也必须照常发起检查")
        self.assertEqual(swallowed, [("sf_update_flow.show_checking_dialog", failure)],
                         "失败要留一条带位置的痕迹，不许静默吞掉")

    def test_flags_stay_consistent_after_a_failed_dialog(self):
        """框失败后在飞状态与"检查已发起"一致：第二次点击被接住，回包放开后能重来。"""
        import zlib

        def broken_dialog(self):
            raise zlib.error("incorrect header check")

        _started, _swallowed, host = self._run_manual_check(broken_dialog)
        with patch("stock_float_window.threading.Thread") as thread:
            _check_bg(host, manual=False)
        thread.assert_not_called()      # 检查确实在飞，没有另起第二个请求

        _handle_result(host, False, None, None)   # 回包到达，状态位放开
        with patch("stock_float_window.threading.Thread") as thread:
            _check_bg(host, manual=False)
        self.assertEqual(thread.return_value.start.call_count, 1,
                         "回包之后必须能再发起检查，不能被框的失败锁死")

    def test_dialog_failure_on_a_click_that_reuses_the_inflight_check_is_traced_not_raised(self):
        import zlib
        failure = zlib.error("Error -3 while decompressing data: incorrect header check")
        swallowed = []

        def broken_dialog(self):
            raise failure

        host = _fake(_show_checking_dialog=broken_dialog, **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread") as thread, \
             patch.object(stock_float_window, "log_swallow",
                          lambda where, exc, *a, **k: swallowed.append((where, exc))):
            _check_bg(host, manual=True)          # 不抛
        thread.assert_not_called()                # 仍然复用在飞的检查，不另起请求
        self.assertEqual(swallowed, [("sf_update_flow.show_checking_dialog", failure)])
        # 点击已记在账上：那次检查回包时照常兑现成「当前已是最新版本」。
        messages = []
        host = _fake(messages=messages, _show_checking_dialog=broken_dialog, **_IN_FLIGHT)
        with patch("stock_float_window.threading.Thread"), \
             patch.object(stock_float_window, "log_swallow", lambda *a, **k: None):
            _check_bg(host, manual=True)
        _handle_result(host, False, None, None)
        self.assertEqual(messages, [("检查更新", "当前已是最新版本。")])

    def test_working_dialog_behaves_as_before(self):
        feedback = []
        swallowed = []
        host = _fake(feedback=feedback)
        with patch("stock_float_window.threading.Thread") as thread, \
             patch.object(stock_float_window, "log_swallow",
                          lambda where, exc, *a, **k: swallowed.append((where, exc))):
            _check_bg(host, manual=True)
        self.assertEqual(feedback, ["checking"])
        self.assertEqual(thread.return_value.start.call_count, 1)
        self.assertEqual(swallowed, [], "框正常时不应有任何留痕")


if __name__ == "__main__":
    unittest.main()
