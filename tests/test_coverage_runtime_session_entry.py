"""运行会话账本与它在主程序里的接线：每条失败路径都要留下可查的事实。

这批判据守的是"上次没正常退出 → 这次问一次"这条链：账本读写、退出钩子、
提示与答案回写、首帧/健康采样把阶段推进。它们的失效形态都是**用户看不见**
（要么再也不问，要么每次都问），所以断言一律落在账本文件内容与留痕站点上。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sf_glass
import sf_run_session as ledger_module
import sf_startup_health as health
import sf_system_alert
import sf_ui_render
import sf_update
from PyQt6.QtWidgets import QApplication, QWidget
from sf_run_session import (PHASE_FRAME_READY, PHASE_LAUNCHING, PHASE_UPDATE_HANDOFF, PROMPT_DISMISSED,
                            RuntimeSessionLedger, SessionLedgerError)
from tests._base import SFCase


def record(**overrides):
    """一条形状完整的会话记录；校验只看形状，字段值由每个用例自己改坏一处。"""
    value = {"session_id": "s" * 32, "started_at": "2026-10-24T00:00:00+00:00",
             "app_version": "fixture", "bundle_id": "com.skyrocket.app", "pid": 4242,
             "phase": "healthy", "prompt_state": "none", "prompt_count": 0,
             "update_handoff_id": None, "expected_update_version": None, "ended_at": None}
    value.update(overrides)
    return value


def ledger_file(current, pending):
    return {"schema_version": ledger_module.LEGACY_JSON_SCHEMA_VERSION, "current": current, "pending": pending}


def temp_dir(case, prefix):
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir="/tmp")
    case.addCleanup(temporary.cleanup)
    return Path(temporary.name)


class OsProxy:
    """只给一个模块换掉 os 的某个函数：全进程改 os 会波及同一棵树里的其他线程。"""

    def __init__(self, real, **overrides):
        self.__dict__["_real"] = real
        self.__dict__["_overrides"] = overrides

    def __getattr__(self, name):
        overrides = self.__dict__["_overrides"]
        if name in overrides:
            return overrides[name]
        return getattr(self.__dict__["_real"], name)


def failing(name, calls):
    def _raise(*_args, **_kwargs):
        calls.append(name)
        raise OSError("fixture")

    return _raise


class LedgerStorageFailureTests(SFCase):
    """写坏账本时的三种现场：事务失败回滚、文件不是数据库、连挪都挪不动。

    单槽 JSON 时代这些用例盯的是临时件清理与 os.replace；换成 SQLite 之后失败形态变成
    「事务没提交」「文件根本打不开」，要守的事没变：**上一次会话的事实不能变成半截，
    而账本坏掉绝不能挡在窗口起来之前**。
    """

    def setUp(self):
        super().setUp()
        self.root = temp_dir(self, "run-session-failure-")
        self.path = self.root / "run-session.sqlite3"
        self.ledger = RuntimeSessionLedger(self.path)

    def seeded(self, session_id):
        self.ledger.begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1234,
                          session_id=session_id, started_at="2026-10-24T00:00:00+00:00")

    def test_a_rejected_write_rolls_back_and_leaves_the_previous_session_readable(self):
        self.seeded("a" * 32)
        with self.assertRaises(sqlite3.IntegrityError):
            # app_version 给 None：NOT NULL 当场拒绝。失败必须是整笔回滚，不许留下半条会话。
            self.ledger.begin(app_version=None, bundle_id="com.skyrocket.app", pid=1234,
                              session_id="b" * 32, started_at="2026-10-24T00:00:01+00:00")
        self.assertEqual([row["session_id"] for row in self.ledger.unfinished()], ["a" * 32])
        self.assertEqual(self.ledger.current()["session_id"], "a" * 32)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["run-session.sqlite3"],
                         "回滚之后数据目录里不许多出 -journal / 临时件")

    def test_an_unusable_ledger_file_is_set_aside_and_startup_continues(self):
        """截断或别的程序占了这个文件名：整份改名挪走（可查），新账本照常开工。"""
        self.path.write_bytes(b"{not a database at all")
        self.assertIsNone(self.begin_after_corruption())
        self.assertEqual(self.ledger.current()["phase"], PHASE_LAUNCHING)
        kept = sorted(item.name for item in self.root.iterdir())
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0], "run-session.sqlite3")
        self.assertTrue(kept[1].startswith("run-session.sqlite3.corrupt-"),
                        "坏文件必须还在（唯一改名），不能被「顺手清理」掉")
        self.assertIn(b"not a database", next(self.root.glob("run-session.sqlite3.corrupt-*")).read_bytes())

    def begin_after_corruption(self):
        return self.ledger.begin(app_version="fixture", bundle_id="com.skyrocket.app",
                                 pid=1234, session_id="a" * 32, started_at="2026-10-24T00:00:00+00:00")

    def test_a_ledger_with_malformed_legacy_json_is_ignored_without_blocking_startup(self):
        legacy = self.path.with_name("run-session.json")
        legacy.write_text("{", encoding="utf-8")
        self.assertIsNone(self.begin_after_corruption())
        self.assertTrue(legacy.exists())

    def test_a_ledger_with_valid_legacy_json_is_migrated_and_preserved(self):
        legacy = self.path.with_name("run-session.json")
        current = record(session_id="l" * 32, phase=PHASE_FRAME_READY)
        legacy.write_text(json.dumps(ledger_file(current, None)), encoding="utf-8")
        self.assertEqual(self.begin_after_corruption().session_id, "l" * 32)
        self.assertEqual(len(list(self.root.glob("run-session.json.migrated-*"))), 1)

        self.path.write_bytes(b"garbage")
        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        with self.assertRaises(SessionLedgerError) as raised:
            self.begin_after_corruption()
        self.assertIn("cannot be set aside", str(raised.exception))

    def test_a_ledger_that_cannot_be_created_at_all_is_named(self):
        self.path.write_bytes(b"garbage")
        with mock.patch.object(self.ledger, "_create", return_value=None) as create:  # discipline-ok(R5): 造「两次都建不起来」只能拦这一层
            with self.assertRaises(SessionLedgerError) as raised:
                self.begin_after_corruption()
        self.assertEqual(create.call_count, 2)
        self.assertIn("cannot be created", str(raised.exception))

    def test_malformed_rows_are_quarantined_when_a_new_session_starts(self):
        """坏行不能永久遮住后来正常写入的会话。"""
        self.seeded("a" * 32)
        import sqlite3
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE session_events SET phase='nonsense'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ledger_module.MalformedLedger):
            self.ledger.current()
        self.assertIsNone(self.begin_after_corruption())
        self.assertEqual(self.ledger.current()["phase"], PHASE_LAUNCHING)
        self.assertEqual(len(list(self.root.glob("run-session.sqlite3.corrupt-*"))), 1)


class RowValidationTests(SFCase):
    """行校验的两条形状守卫：直接从数据库渲染出来的行也照样要过这一关。

    `_row()` 按固定列名 zip 出字典，正常路径不可能给出「不是字典」或「列不对」的输入，
    所以这两条分支只能按纯函数直接证 —— 它们防的是以后有人改了列名却忘了改校验。
    """

    def test_only_a_complete_and_typed_record_passes(self):
        good = record()
        self.assertTrue(ledger_module._valid_session(good))  # discipline-ok(R5): 形状守卫没有公开入口，只能按纯函数验
        missing = dict(good)
        del missing["started_at"]
        broken = {"不是字典": "session-id", "空值": None, "少一列": missing,
                  "多一列": dict(good, stray=1), "pid 是字符串": dict(good, pid="4242"),
                  "phase 不认识": dict(good, phase="sleeping")}
        for label, value in broken.items():
            with self.subTest(shape=label):
                self.assertFalse(ledger_module._valid_session(value), label)  # discipline-ok(R5): 同上

    def test_an_assessment_with_no_readable_current_row_gives_no_verdict(self):
        """读不出"上一次是哪次"时不许猜「它崩了」：没有结论就返回 None。

        这条判据原先由 JSON 形状用例顺带覆盖；换成数据库之后那条路没了，
        守卫本身还在 —— 判据必须跟着搬到新形状上，不能因为"没人跑到"就当它不需要。
        """
        self.assertIsNone(RuntimeSessionLedger.assess_previous(None))
        self.assertIsNone(RuntimeSessionLedger.assess_previous({"current": None, "pending": None}))
        self.assertIsNone(RuntimeSessionLedger.assess_previous({"current": "半条记录", "pending": None}))
        pending_only = {"current": None, "pending": record(session_id="p" * 32, phase=PHASE_FRAME_READY)}
        verdict = RuntimeSessionLedger.assess_previous(pending_only)
        self.assertEqual((verdict.session_id, verdict.category, verdict.should_prompt),
                         ("p" * 32, "post_frame_unfinished", True))

class LedgerReadSideEffectTests(SFCase):
    """只读路径零副作用 —— 这条是整档被「用户数据被改动」红线拒收换来的实测教训。

    诊断导出会问账本「上一次会话怎么样」，那一刻程序可能正在写它。读必须真的只是读：
    不建目录、不建库、不产生 -wal/-shm、不改目录修改时间。
    """

    def setUp(self):
        super().setUp()
        self.root = temp_dir(self, "run-session-read-only-")

    def footprint(self, directory):
        if not directory.is_dir():
            return None
        return (directory.stat().st_mtime_ns, sorted(item.name for item in directory.iterdir()))

    def test_reading_a_ledger_that_does_not_exist_creates_nothing(self):
        missing = self.root / "nested"
        ledger = RuntimeSessionLedger(missing / "run-session.sqlite3")
        before = self.footprint(self.root)
        self.assertIsNone(ledger.current())
        self.assertIsNone(ledger.pending())
        self.assertEqual(ledger.unfinished(), [])
        self.assertEqual(self.footprint(self.root), before)
        self.assertFalse(missing.exists(), "读路径不许为了「看一下」就建出目录")

    def test_reading_an_existing_ledger_leaves_the_directory_untouched(self):
        path = self.root / "run-session.sqlite3"
        RuntimeSessionLedger(path).begin(app_version="fixture", bundle_id="com.skyrocket.app",
                                         pid=1234, session_id="a" * 32,
                                         started_at="2026-10-24T00:00:00+00:00")
        before = self.footprint(self.root)
        self.assertTrue(path.read_bytes(), "写过一次的账本不该是空文件")
        ledger = RuntimeSessionLedger(path)
        self.assertEqual(ledger.current()["session_id"], "a" * 32)
        self.assertEqual(ledger.pending(), None)
        self.assertEqual(self.footprint(self.root), before)

    def test_reading_with_the_directory_present_still_creates_no_file(self):
        """目录已经在了、库还没有：读一次不许把库建出来（写入才是建库的那一步）。"""
        path = self.root / "run-session.sqlite3"
        ledger = RuntimeSessionLedger(path)
        before = self.footprint(self.root)
        self.assertEqual(before[1], [])
        self.assertIsNone(ledger.current())
        self.assertIsNone(ledger.pending())
        self.assertEqual(ledger.unfinished(), [])
        self.assertEqual(self.footprint(self.root), before)

    def test_a_ledger_that_cannot_be_opened_is_named_instead_of_reported_as_empty(self):
        """读不出来要说「读不出来」，不能写成「这台机器没有账本」。"""
        path = self.root / "run-session.sqlite3"
        RuntimeSessionLedger(path).begin(app_version="fixture", bundle_id="com.skyrocket.app",
                                        pid=1234, session_id="a" * 32,
                                        started_at="2026-10-24T00:00:00+00:00")
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        ledger = RuntimeSessionLedger(path)
        with self.assertRaises(sqlite3.OperationalError):
            ledger.current()
        with self.assertRaises(sqlite3.OperationalError):
            ledger.unfinished()

    def test_only_the_missing_table_is_read_as_no_ledger(self):
        """「库里还没这张表」才算没有账本；别的查询错误必须照实抛出。"""
        path = self.root / "run-session.sqlite3"
        import sqlite3 as _sqlite3
        connection = _sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE session_events(x)")
            connection.commit()
        finally:
            connection.close()
        with mock.patch.object(ledger_module, "_snapshot",
                               side_effect=sqlite3.OperationalError("disk I/O error")) as snap:  # discipline-ok(R5): 造「表在但查询失败」只能拦渲染这一层
            with self.assertRaises(sqlite3.OperationalError):
                RuntimeSessionLedger(path).current()
            self.assertEqual(snap.call_count, 1)

    def test_a_valid_database_without_the_table_is_answered_as_no_ledger(self):
        import sqlite3
        path = self.root / "run-session.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE unrelated(x)")
            connection.commit()
        finally:
            connection.close()
        ledger = RuntimeSessionLedger(path)
        self.assertIsNone(ledger.current())
        self.assertEqual(ledger.unfinished(), [])


class PendingSessionsAreNeverLostTests(SFCase):
    """单槽账本的真正缺陷：两次「没正常退出」挤在一起，前一次会被后一次抹掉。"""

    def setUp(self):
        super().setUp()
        self.path = temp_dir(self, "run-session-multi-") / "run-session.sqlite3"

    def ledger(self):
        return RuntimeSessionLedger(self.path)

    def start(self, session_id, phase=None):
        ledger = self.ledger()
        ledger.begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1234,
                     session_id=session_id, started_at="2026-10-24T00:00:00+00:00")
        if phase == PHASE_FRAME_READY:
            ledger.mark_frame_ready(session_id)
        return ledger

    def test_two_unasked_sessions_are_both_remembered(self):
        self.start("a" * 32, PHASE_FRAME_READY)
        self.start("b" * 32, PHASE_FRAME_READY)
        self.assertEqual(sorted(row["session_id"] for row in self.ledger().unfinished()),
                         sorted(["a" * 32, "b" * 32]))

    def test_the_older_unasked_session_is_asked_first_and_the_newer_one_survives(self):
        """两次都没正常退出：先问更早的那次（与单槽时代一致），答完之后后一次**还在**。

        单槽 JSON 在这里会把 b 覆盖掉 —— 旧 pending 占着那一个槽位，等再启动时
        上一轮 current 已经是别人，b 的事实就再也读不出来了。
        """
        self.start("a" * 32, PHASE_FRAME_READY)
        self.start("b" * 32, PHASE_FRAME_READY)
        first = self.ledger().begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1234,
                                    session_id="c" * 32, started_at="2026-10-24T00:00:01+00:00")
        self.assertEqual((first.session_id, first.category, first.should_prompt),
                         ("a" * 32, "post_frame_unfinished", True))
        # 还没人答过 a：再启动一次仍然先问最早的那次（不是换成最新的），
        # 这一步才真正区分「按顺序排队」与「每次挑最新」。
        again = self.ledger().begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1234,
                                    session_id="d" * 32, started_at="2026-10-24T00:00:02+00:00")
        self.assertEqual(again.session_id, "a" * 32)
        self.ledger().mark_prompt("a" * 32, PROMPT_DISMISSED)
        later = self.ledger().begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=1234,
                                    session_id="e" * 32, started_at="2026-10-24T00:00:03+00:00")
        self.assertEqual((later.session_id, later.category), ("b" * 32, "post_frame_unfinished"))

    def test_a_second_process_beginning_does_not_erase_the_first_sessions_milestones(self):
        first = self.start("a" * 32, PHASE_FRAME_READY)
        self.assertTrue(self.ledger().current()["session_id"] == "a" * 32)
        self.ledger().begin(app_version="fixture", bundle_id="com.skyrocket.app", pid=9999,
                            session_id="b" * 32, started_at="2026-10-24T00:00:03+00:00")
        row = next(item for item in first.unfinished() if item["session_id"] == "a" * 32)
        self.assertEqual(row["phase"], PHASE_FRAME_READY)
        self.assertEqual(row["app_version"], "fixture")


class LedgerAdvanceGuardTests(SFCase):
    def setUp(self):
        super().setUp()
        self.path = temp_dir(self, "run-session-guard-") / "run-session.json"
        self.ledger = RuntimeSessionLedger(self.path)
        self.ledger.begin(app_version="fixture", bundle_id="com.skyrocket.app",
                          pid=1234, session_id="a" * 32, started_at="2026-10-24T00:00:00+00:00")

    def test_advancing_an_unknown_session_is_refused(self):
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_frame_ready("b" * 32)

    def test_prompting_an_unknown_state_is_refused(self):
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_prompt("a" * 32, "answered")

    def test_prompting_without_a_ledger_is_refused(self):
        self.path.unlink()
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_prompt("a" * 32, "dismissed")

    def test_prompting_a_session_the_ledger_does_not_know_is_refused(self):
        with self.assertRaises(SessionLedgerError):
            self.ledger.mark_prompt("c" * 32, "dismissed")


class RuntimeSessionEntryWiringTests(SFCase):
    """主程序里那三个具名入口：账本坏掉时程序照样起来。"""

    def setUp(self):
        super().setUp()
        import stock_float_window as m
        self.m = m
        self.root = temp_dir(self, "entry-session-")
        self.track_patch(mock.patch.object(m, "LOG_PATH", str(self.root / "debug.log")))
        self.swallow = mock.Mock()
        self.track_patch(mock.patch.object(m, "log_swallow", self.swallow))
        self.line = mock.Mock()
        self.track_patch(mock.patch.object(m, "log_line", self.line))

    def ledger(self, **overrides):
        session = mock.Mock(spec=RuntimeSessionLedger)
        session.current.return_value = record(**overrides)
        session.begin.return_value = None
        return session

    def test_a_healthy_ledger_returns_the_new_session_and_no_history_to_ask(self):
        session, session_id, previous = self.m._begin_runtime_session()  # discipline-ok(R5): 登记会话只在主程序入口发生，没有别的公开入口
        self.assertIsInstance(session, RuntimeSessionLedger)
        self.assertEqual(session.current()["session_id"], session_id)
        self.assertIsNone(previous)
        self.m._mark_runtime_session_clean(session, session_id)  # discipline-ok(R5): 退出钩子的本体就是这个函数
        self.assertEqual(session.current()["phase"], "clean_exit")
        self.assertFalse(self.swallow.called)

    def test_a_broken_ledger_is_named_in_the_log_and_never_blocks_startup(self):
        with mock.patch.object(self.m, "RuntimeSessionLedger", side_effect=OSError("disk full")):
            result = self.m._begin_runtime_session()  # discipline-ok(R5): 要验的就是「账本坏了也照样起来」，只能直接调入口
        self.assertEqual(result, (None, None, None))
        self.swallow.assert_called_once()
        self.assertEqual(self.swallow.call_args.args[0], "stock_float_window.runtime_session.begin")

    def test_a_ledger_without_a_current_record_yields_no_session_id(self):
        blank = self.ledger()
        blank.current.return_value = None
        with mock.patch.object(self.m, "RuntimeSessionLedger", return_value=blank):
            session, session_id, previous = self.m._begin_runtime_session()  # discipline-ok(R5): 同上
        self.assertIs(session, blank)
        self.assertIsNone(session_id)
        self.assertIsNone(previous)

    def test_a_clean_exit_failure_is_recorded_not_raised(self):
        boom = mock.Mock(spec=RuntimeSessionLedger)
        boom.mark_clean_exit.side_effect = RuntimeError("ledger vanished")
        self.m._mark_runtime_session_clean(boom, "a" * 32)  # discipline-ok(R5): 要验的是失败留痕，只能直接调
        self.swallow.assert_called_once()
        self.assertEqual(self.swallow.call_args.args[0],
                         "stock_float_window.runtime_session.clean_exit")

    def prompt_once(self, session, *, answer=None, dialog=object()):
        """跑一次真实提示函数，返回 (展示参数, 窗口记账)。"""
        shown = {}
        opened = []

        def show_alert(**kwargs):
            shown.update(kwargs)
            return dialog

        win = SimpleNamespace(_queue_feedback_open=lambda: opened.append("feedback"))
        with mock.patch.object(sf_system_alert, "show_alert", side_effect=show_alert), \
                mock.patch.object(sf_system_alert, "bundled_icon_path", return_value=""):
            self.m._offer_unfinished_session_feedback(win, session, "a" * 32)  # discipline-ok(R5): 提示由启动后 2.2 秒定时器触发，用例必须直接调这一个
        if answer is not None:
            shown["on_result"](answer)
        return shown, opened

    def test_answering_describe_opens_the_feedback_flow_and_is_written_back(self):
        seen = []
        session = mock.Mock(spec=RuntimeSessionLedger)
        session.mark_prompt.side_effect = lambda *args: seen.append(args)
        shown, opened = self.prompt_once(session, answer="描述问题")
        self.assertEqual(shown["title"], "反馈问题")
        self.assertEqual(shown["buttons"], ("暂不", "描述问题"))
        self.assertEqual(opened, ["feedback"])
        self.assertEqual(seen, [("a" * 32, "deferred"), ("a" * 32, "opened_feedback")])

    def test_declining_only_records_the_refusal(self):
        seen = []
        session = mock.Mock(spec=RuntimeSessionLedger)
        session.mark_prompt.side_effect = lambda *args: seen.append(args)
        self.prompt_once(session, answer="暂不")
        self.assertEqual(seen[-1], ("a" * 32, "dismissed"))

    def test_a_missing_system_dialog_is_said_out_loud_without_marking_the_session(self):
        """弹窗建不出来 = 没问过；下次启动必须仍能提示。"""
        session = mock.Mock(spec=RuntimeSessionLedger)
        self.prompt_once(session, dialog=None)
        self.assertIn("unfinished-session prompt unavailable",
                      [call.args[0] for call in self.line.call_args_list])
        session.mark_prompt.assert_not_called()

    def test_a_missing_system_dialog_leaves_the_session_eligible_on_next_launch(self):
        """创建系统框失败后，SQLite 账本仍须保留同一条异常会话待下次提示。"""
        path = temp_dir(self, "entry-prompt-retry-") / "run-session.sqlite3"
        session = RuntimeSessionLedger(path)
        session.begin(app_version="fixture", bundle_id="com.skyrocket.app",
                      pid=1234, session_id="a" * 32, started_at="2026-10-24T00:00:00+00:00")
        session.mark_frame_ready("a" * 32)
        previous = session.begin(app_version="fixture", bundle_id="com.skyrocket.app",
                                 pid=1235, session_id="b" * 32, started_at="2026-10-24T00:01:00+00:00")
        self.assertTrue(previous.should_prompt)
        self.assertEqual(previous.session_id, "a" * 32)
        with mock.patch.object(sf_system_alert, "show_alert", return_value=None), \
                mock.patch.object(sf_system_alert, "bundled_icon_path", return_value=""):
            self.m._offer_unfinished_session_feedback(SimpleNamespace(), session, previous.session_id)  # discipline-ok(R5): 定时器最终调用私有提示入口，必须验证创建失败不消耗账本。
        resumed = session.begin(app_version="fixture", bundle_id="com.skyrocket.app",
                                pid=1236, session_id="c" * 32, started_at="2026-10-24T00:02:00+00:00")
        self.assertTrue(resumed.should_prompt)
        self.assertEqual(resumed.session_id, "a" * 32)

    def test_a_prompt_write_back_failure_does_not_escape_into_qt(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        session.mark_prompt.side_effect = [None, RuntimeError("disk full"), None]
        _shown, opened = self.prompt_once(session, answer="描述问题")
        self.assertEqual(opened, ["feedback"], "回写失败不该把已经打开的反馈流程吞掉")
        self.swallow.assert_called_once()
        self.assertEqual(self.swallow.call_args.args[0],
                         "stock_float_window.runtime_session.prompt_answer")

    def test_a_deferred_write_failure_skips_the_dialog_entirely(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        session.mark_prompt.side_effect = OSError("ledger busy")
        _shown, _opened = self.prompt_once(session)
        self.swallow.assert_called_once()
        self.assertEqual(self.swallow.call_args.args[0], "stock_float_window.runtime_session.prompt")

    def test_update_handoff_marks_the_session_so_the_next_launch_does_not_call_it_a_crash(self):
        path = temp_dir(self, "entry-handoff-") / "run-session.json"
        session = RuntimeSessionLedger(path)
        session.begin(app_version="fixture", bundle_id="com.skyrocket.app",
                      pid=1234, session_id="a" * 32, started_at="2026-10-24T00:00:00+00:00")
        session.mark_frame_ready("a" * 32)
        quit_calls, messages = [], []
        host = SimpleNamespace(_runtime_session=session, _runtime_session_id="a" * 32,  # discipline-ok(R5): 更新 handoff 的私有会话接口无公开替身
                               quit=lambda: quit_calls.append("quit"),
                               _show_update_message=lambda *args: messages.append(args))
        newer = ".".join(("9", "9", "9"))
        update = SimpleNamespace(archive_sha256="a" * 64, version=newer)
        with mock.patch.object(sf_update, "launch_installer") as launch:
            self.m.FloatWindow._launch_downloaded_update(host, update, "fixture.zip")  # discipline-ok(R5): 交接打点长在更新启动方法内部，替身宿主只能直调
        launch.assert_called_once()
        self.assertEqual(quit_calls, ["quit"])
        self.assertEqual(messages, [])
        self.assertEqual(session.current()["phase"], PHASE_UPDATE_HANDOFF)

    def test_update_handoff_ledger_failure_reclaims_started_helper(self):
        host = SimpleNamespace(_runtime_session=mock.Mock(spec=RuntimeSessionLedger),
                               _runtime_session_id="a" * 32, quit=lambda: None,  # discipline-ok(R5): handoff 接线只通过私有宿主属性传递会话身份
                               _show_update_message=lambda *args: None)
        process = mock.Mock()
        launch = sf_update._InstallerLaunch(["/bin/true"], process, "a" * 32,  # discipline-ok(R5): 构造带回收句柄的私有 helper 包装器
                                            Path("context"), Path("helper"))
        host._runtime_session.mark_update_handoff.side_effect = OSError("disk full")  # discipline-ok(R5): 私有会话协作者仅用于构造账本失败现场
        update = SimpleNamespace(archive_sha256="a" * 64, version="9.9.9")
        with mock.patch.object(sf_update, "launch_installer", return_value=launch), \
             mock.patch.object(sf_update, "handoff_context_path", return_value=Path("/tmp/no-context")), \
             mock.patch.object(sf_update, "handoff_ack_path", return_value=Path("/tmp/no-ack")):
            self.m.FloatWindow._launch_downloaded_update(host, update, "fixture.zip")  # discipline-ok(R5): 只能从窗口私有更新接线验证 helper 回收
        process.terminate.assert_called_once()


class StartupHealthSessionTests(SFCase):
    """健康采样把会话标成 healthy 并确认 OTA 交接；确认失败不外抛。"""

    def setUp(self):
        super().setUp()
        self.app = self.ensure_qapp()
        self.window = self.track_widget(QWidget())
        self.track_patch(mock.patch.object(sys, "frozen", True, create=True))
        self.track_patch(mock.patch.object(sf_glass, "native_cocoa_active", return_value=True))
        self.track_patch(mock.patch.object(health, "STARTUP_DELAY_MS", 0))
        self.track_patch(mock.patch.object(health, "capture", return_value={"status": "passed"}))
        self.swallow = mock.Mock()
        self.track_patch(mock.patch("sf_logging.log_swallow", self.swallow))

    def sampled(self):
        QApplication.processEvents()
        return health.diagnostics(self.window)["startup"]["status"] == "passed"

    def wire(self, session):
        vars(self.window)["_runtime_session"] = session  # discipline-ok(R5): 健康采样只从窗口私有接线取会话
        vars(self.window)["_runtime_session_id"] = "a" * 32  # discipline-ok(R5): 同上

    def test_a_sampled_start_marks_the_session_healthy_and_acks_the_handoff(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        self.wire(session)
        with mock.patch.object(sf_update, "acknowledge_pending_handoff", return_value=True) as ack:
            health.schedule(self.window)
            self.assertTrue(self.wait_for(self.sampled))
        session.mark_healthy.assert_called_once_with("a" * 32)
        self.assertEqual(ack.call_args.kwargs["session_id"], "a" * 32)
        self.assertEqual(ack.call_args.kwargs["handoff_id"], None)
        self.assertFalse(self.swallow.called)

    def test_failed_health_does_not_advance_the_session_or_ack(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        self.wire(session)
        self.track_patch(mock.patch.object(health, "capture", return_value={"status": "failed"}))
        with mock.patch.object(sf_update, "acknowledge_pending_handoff", return_value=True) as ack:
            health.schedule(self.window)
            self.window._startup_health_timer.timeout.emit()  # discipline-ok(R5): 直接触发已登记的单次采样信号，避免等待 Qt 事件时钟
            QApplication.processEvents()
        self.assertEqual(self.window._startup_health["status"], "failed")  # discipline-ok(R5): 健康采样结果只通过窗口私有状态可观测
        session.mark_healthy.assert_not_called()
        ack.assert_not_called()

    def test_a_sessionless_start_says_nothing_to_the_ledger(self):
        """没有会话号时一个 mark 都不许发 —— 否则会拿别人的会话号写坏别人的账。"""
        with mock.patch.object(sf_update, "acknowledge_pending_handoff", return_value=True) as ack:
            health.schedule(self.window)
            self.assertTrue(self.wait_for(self.sampled))
        ack.assert_called_once()
        self.assertEqual(ack.call_args.kwargs["session_id"], "")

    def test_a_failing_handoff_ack_is_named_and_never_breaks_the_sample(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        self.wire(session)
        with mock.patch.object(sf_update, "acknowledge_pending_handoff",
                               side_effect=OSError("staging gone")):
            health.schedule(self.window)
            self.assertTrue(self.wait_for(self.sampled))
        session.mark_healthy.assert_called_once_with("a" * 32)
        self.assertTrue(self.sampled())
        self.assertIn("sf_startup_health.handoff_ack",
                      [call.args[0] for call in self.swallow.call_args_list])


class FirstFrameSessionTests(SFCase):
    """首帧锚点顺手把会话推进到 frame_ready；推不动只留痕，不许影响绘制。"""

    def setUp(self):
        super().setUp()
        import sf_quote
        import stock_float_window as m
        self.m = m
        self.app = self.ensure_qapp()
        self.root = temp_dir(self, "first-frame-")
        self.track_patch(mock.patch.object(m, "load_config",
                                           return_value=("sh600519", 100, 100, 200, "translucent")))
        self.track_patch(mock.patch.object(m, "save_config", lambda *a, **k: None))
        self.track_patch(mock.patch.object(sf_quote, "http_get", lambda *a, **k: ""))
        self.track_patch(mock.patch.object(m, "start_icon_worker", lambda: None))
        self.track_patch(mock.patch.object(m, "CONFIG_PATH", str(self.root / "config.json")))
        self.track_patch(mock.patch.object(m, "CONFIG_BACKUP_PATH", str(self.root / "config.bak")))
        self.track_patch(mock.patch.object(m, "LAST_QUOTE_PATH", str(self.root / "last_quote.json")))
        self.track_patch(mock.patch.object(m, "LOG_PATH", str(self.root / "debug.log")))
        self.track_patch(mock.patch("sf_logging.LOG_PATH", str(self.root / "debug.log")))
        self.track_patch(mock.patch.object(m.FloatWindow, "_force_top", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_quote_loop_wrapper", lambda self: None))
        self.track_patch(mock.patch.object(m.FloatWindow, "_minute_loop_wrapper", lambda self: None))
        self.swallow = mock.Mock()
        self.track_patch(mock.patch.object(sf_ui_render, "log_swallow", self.swallow))

    def tearDown(self):
        for widget in reversed(self._sf_widgets):
            widget._exit_persisted = True  # discipline-ok(R5): closeEvent 写盘幂等标记只能从私有状态设定。
            widget.close()
        super().tearDown()

    def painted_window(self, session):
        win = self.track_widget(self.m.FloatWindow())
        win.running = False  # discipline-ok(R5): 夹具要停帧驱动，这个开关没有公开入口
        win._wake.set()  # discipline-ok(R5): 同上，行情循环的唤醒闸只有内部句柄
        for timer in (win.timer, win.top_timer, win._anim_timer,  # discipline-ok(R5): 夹具停表
                      win._flash_timer):  # discipline-ok(R5): 帧驱动与动画定时器只有私有句柄
            timer.stop()
        vars(win)["_runtime_session"] = session  # discipline-ok(R5): 首帧接线只能通过窗口私有属性构造
        vars(win)["_runtime_session_id"] = "a" * 32  # discipline-ok(R5): 同上
        win.show()
        QApplication.processEvents()
        win._sf_first_frame_logged = False  # discipline-ok(R5): 锚点一次性，重新装填才看得见因果
        return win

    def test_a_real_paint_advances_the_session_to_frame_ready(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        win = self.painted_window(session)
        win.grab()
        QApplication.processEvents()
        self.assertTrue(session.mark_frame_ready.called, "画完一帧却没推进会话 = 下次启动会被判成没起来")
        for call_args in session.mark_frame_ready.call_args_list:
            self.assertEqual(call_args.args, ("a" * 32,))
        self.assertFalse(self.swallow.called)

    def test_a_session_that_refuses_the_phase_is_logged_and_the_frame_still_paints(self):
        session = mock.Mock(spec=RuntimeSessionLedger)
        session.mark_frame_ready.side_effect = SessionLedgerError("ledger gone")
        win = self.painted_window(session)
        win.grab()
        QApplication.processEvents()
        self.assertIn("sf_ui_render.first_frame_session",
                      [call.args[0] for call in self.swallow.call_args_list])
        self.assertTrue(win.isVisible())


class HandoffAckRecoveryTests(SFCase):
    """OTA 交接上下文：问不出答案要单独说，坏掉的一条不许吞掉整轮。"""

    def setUp(self):
        super().setUp()
        self.root = temp_dir(self, "handoff-ack-")
        self.track_patch(mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(self.root)))

    def context(self, token):
        return self.root / ("handoff-%s.context.json" % token)

    def test_a_corrupt_context_is_refused_without_touching_the_file(self):
        token = "a" * 32
        context = sf_update.write_handoff_context(token, expected_version="fixture")
        context.write_text("{", encoding="utf-8")
        self.assertFalse(sf_update.acknowledge_pending_handoff(version="fixture", handoff_id=token))
        self.assertEqual(context.read_text(encoding="utf-8"), "{")

    def test_a_context_that_cannot_be_statated_is_answered_as_no_handoff(self):
        """上下文文件在扫描中途消失：这不是"没有交接"，但只能给 False 并留下这一轮。"""
        ghost = self.context("b" * 32)
        ghost.symlink_to(self.root / "already-gone.json")
        self.assertFalse(sf_update.acknowledge_pending_handoff(version="fixture"))

    def test_a_malformed_context_is_skipped_and_the_valid_one_still_acks(self):
        token = "a" * 32
        sf_update.write_handoff_context(token, expected_version="fixture")
        broken = self.context("b" * 32)
        broken.write_text("{", encoding="utf-8")
        stamp = time.time() + 60          # 坏的那条排在最前面被读到
        os.utime(broken, (stamp, stamp))
        self.assertTrue(sf_update.acknowledge_pending_handoff(version="fixture",
                                                              session_id="probe",
                                                              handoff_id=token))
        ack = json.loads(sf_update.handoff_ack_path(token).read_text(encoding="utf-8"))
        self.assertEqual(ack["handoff_id"], token)
        self.assertEqual(ack["session_id"], "probe")
        self.assertTrue(broken.exists(), "读不出来的上下文只许跳过，不许被当成处理掉")
