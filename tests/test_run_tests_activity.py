"""红线归因在主进程里被真正执行一遍。

为什么单开这个模块：`tools/test_runner.py --self-test` 是被子进程调起的，coverage 看不见它跑到的行，
而改动覆盖率预判（tools/change_predict.py）与关卡都只认主进程的 `.coverage`。新增的归因代码若只在
自检里跑，预判会如实报 predicted_miss —— 那不是误报，是「这段逻辑没有可执行的判据」。
所以这里直接 import 运行器，用临时目录冒充用户数据目录，把每条分支都打到。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import test_runner as TR

try:                                     # 与其余用例一致：两种导入路径都支持
    from _base import SFCase
except ImportError:                      # pragma: no cover
    from tests._base import SFCase



class ActivityCase(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="sf_activity_case_")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "debug.log"

    def append(self, *lines):
        # 每行自带一个换行：写成 join+"\n" 会在末尾多出一个空行，被当成一条真实新增行。
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))


class TestActivityCounting(ActivityCase):
    def test_missing_log_is_named_not_counted_as_zero(self):
        activity = TR.count_activity(None, support_dir=str(self.directory))
        self.assertEqual(activity["status"], "missing")
        self.assertEqual(activity["total"], 0)
        self.assertIn("不存在", TR.activity_report(activity))

    def test_only_lines_appended_after_the_start_are_counted(self):
        self.append("[old] INFO: STARTUP v1.0", "[old] INFO: btn action queued")
        start = TR.user_log_state("debug.log", str(self.directory))
        self.append("[new] INFO: STARTUP v1.4.103", "[new] INFO: first-frame painted",
                    "[new] INFO: reopen called by LaunchServices")
        activity = TR.count_activity(start, support_dir=str(self.directory))
        self.assertEqual(activity["status"], "ok")
        # "reopen handler installed" 是启动时的一次装配，不算失活恢复：这里用的是 reopen called。
        self.assertEqual(activity["counts"],
                         {"启动": 1, "首帧": 1, "点击": 0, "右键菜单": 0, "失活恢复": 1})
        self.assertEqual(activity["total"], 3)

    def test_a_log_created_during_the_run_is_reported_as_created(self):
        self.assertIsNone(TR.user_log_state("debug.log", str(self.directory)))
        self.append("[new] INFO: STARTUP v1.4.103", "[new] INFO: first-frame painted")
        activity = TR.count_activity(None, support_dir=str(self.directory))
        self.assertEqual(activity["status"], "created")
        self.assertEqual(activity["counts"]["启动"], 1)
        self.assertIn("新建", TR.activity_report(activity))

    def test_rotation_is_not_reported_as_a_plain_append(self):
        self.append("[old] INFO: STARTUP v1.0")
        start = TR.user_log_state("debug.log", str(self.directory))
        # 轮转后是另一个文件：inode 变了，大小也可能变小
        rotated_start = {"inode": start["inode"] + 1, "size": start["size"] + 500, "mtime": 0}
        activity = TR.count_activity(rotated_start, support_dir=str(self.directory))
        self.assertEqual(activity["status"], "rotated")
        self.assertIn("轮转", TR.activity_report(activity))
        shrunken = dict(start, size=start["size"] + 500)
        self.append("[new] INFO: STARTUP v1.4.103")
        self.assertEqual(TR.count_activity(shrunken, support_dir=str(self.directory))["status"], "rotated")

    def test_a_log_deleted_during_the_run_is_reported_as_rotated(self):
        self.append("[old] INFO: STARTUP v1.0")
        start = TR.user_log_state("debug.log", str(self.directory))
        self.log.unlink()
        self.assertEqual(TR.count_activity(start, support_dir=str(self.directory))["status"], "rotated")

    def test_unreadable_log_does_not_break_the_run(self):
        self.append("[old] INFO: STARTUP v1.0")
        start = TR.user_log_state("debug.log", str(self.directory))
        self.log.chmod(0)
        self.addCleanup(self.log.chmod, 0o600)
        if os.access(self.log, os.R_OK):      # root 下永远可读，这种机器上本条不成立
            self.skipTest("当前用户能读 0 权限文件，构造不出不可读")
        self.assertEqual(TR.count_activity(start, support_dir=str(self.directory))["status"], "unreadable")
        self.assertIn("读不了", TR.activity_report(TR.count_activity(start, support_dir=str(self.directory))))

    def test_report_names_categories_and_never_quotes_log_text(self):
        self.append("[old] INFO: STARTUP v1.0")
        start = TR.user_log_state("debug.log", str(self.directory))
        self.append("[new] INFO: STARTUP v1.4.103 -- path /Users/someone/config.json")
        report = TR.activity_report(TR.count_activity(start, support_dir=str(self.directory)))
        self.assertIn("启动 1 条", report)
        self.assertIn("共 1 行", report)
        for leaked in ("STARTUP", "/Users/", "config.json", "v1.4.103"):
            self.assertNotIn(leaked, report, "归因说明把日志正文带出来了")

    def test_no_activity_says_it_is_not_the_formal_app(self):
        self.append("[top] INFO: reopen handler installed")
        start = TR.user_log_state("debug.log", str(self.directory))
        self.append("[new] INFO: unrelated line")
        report = TR.activity_report(TR.count_activity(start, support_dir=str(self.directory)))
        self.assertIn("不是正式版写出来的", report)
        self.assertIsNone(TR.activity_report(None))

    def test_read_cap_stops_at_the_first_line_beyond_it(self):
        original = TR.ACTIVITY_READ_CAP
        TR.ACTIVITY_READ_CAP = 40
        self.addCleanup(setattr, TR, "ACTIVITY_READ_CAP", original)
        self.append("x" * 30, "y" * 30, "z" * 30)
        activity = TR.count_activity(None, support_dir=str(self.directory))
        self.assertLessEqual(activity["total"], 2, "超过读取上限后仍在继续读整个文件")


class TestSummaryOutput(ActivityCase):
    def test_hint_line_is_written_only_when_it_exists(self):
        printed = []
        TR.emit_activity_hint(out=printed.append, support_dir=str(self.directory))
        self.assertEqual(printed, [])          # 日志不在时什么都不打
        self.append("[top] INFO: STARTUP v1.4.103")
        TR.emit_activity_hint(out=printed.append, support_dir=str(self.directory))
        self.assertEqual(len(printed), 1)
        self.assertIn("红线", printed[0])

    def test_dirty_detail_lists_files_then_attributes(self):
        self.append("[top] INFO: STARTUP v1.0")
        start = TR.user_log_state("debug.log", str(self.directory))
        self.append("[new] INFO: STARTUP v1.4.103", "[new] INFO: btn action queued")
        printed = []
        TR.emit_dirty_detail(["config.json：大小 1->2"], start, out=printed.append,
                             support_dir=str(self.directory))
        self.assertTrue(printed[0].startswith("!! 用户数据在测试期间被改动"))
        self.assertIn("config.json", printed[1])
        self.assertIn("启动 1 条", printed[2])
        self.assertIn("点击 1 条", printed[2])
        self.assertNotIn("STARTUP", "".join(printed))

    def test_dirty_detail_stops_after_files_when_there_is_no_activity(self):
        self.append("[top] INFO: STARTUP v1.0")
        start = TR.user_log_state("debug.log", str(self.directory))
        printed = []
        TR.emit_dirty_detail(["debug.log：mtime 变了"], start, out=printed.append,
                             support_dir=str(self.directory))
        # 标题 + 文件 + 归因句：没有活动也要说清"不是正式版写出来的"，而不是沉默。
        self.assertEqual(len(printed), 3)
        self.assertIn("不是正式版写出来的", printed[2])


class TestPreRunHint(ActivityCase):
    def test_recent_write_is_named_with_its_age(self):
        self.append("[top] INFO: STARTUP v1.4.103")
        hint = TR.activity_hint("debug.log", now=time.time(), support_dir=str(self.directory))
        self.assertIsNotNone(hint)
        self.assertIn("秒前刚有写入", hint)
        self.assertIn("红线", hint)

    def test_old_write_and_missing_log_stay_silent(self):
        self.append("[top] INFO: STARTUP v1.4.103")
        self.assertIsNone(TR.activity_hint("debug.log", now=time.time() + TR.WINDOW_HINT_SEC + 1,
                                         support_dir=str(self.directory)))
        self.log.unlink()
        self.assertIsNone(TR.activity_hint("debug.log", now=time.time(), support_dir=str(self.directory)))

    def test_clock_skew_into_the_future_stays_silent(self):
        self.append("[top] INFO: STARTUP v1.4.103")
        self.assertIsNone(TR.activity_hint("debug.log", now=time.time() - 3600, support_dir=str(self.directory)))


if __name__ == "__main__":
    unittest.main()
