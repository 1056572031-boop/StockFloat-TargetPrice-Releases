# -*- coding: utf-8 -*-
"""`tools/desktop_folder_sync.py` 的行为判据（桌面分发目录跟着发版走）。

为什么在主进程直调：链上自检测试是起子进程跑 `--self-test` 的，子进程不计入 `.coverage`
（AGENTS §6 第 10 条），新工具会把链上档拖低。这里直调同一批函数，既是判据也把账记回主进程。

守的四件事：
  1. 版本绑定件、冻结件、同步文档三类各判各的，任何一类种错都必须红。
  2. 发布件本身不对时，拒绝给文档盖章（盖章会把错误写成对账）。
  3. 只改文档的对账段，正文一个字不动；空跑不覆盖改前的备份。
  4. 自检与 --check 只读：不碰真实的桌面分发目录。
"""
import contextlib
import io
import os
import runpy
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import desktop_folder_sync as DFS  # noqa: E402

try:
    from _base import SFCase                     # noqa: E402
except ImportError:                              # 以包形式被 discover 导入时
    from tests._base import SFCase               # noqa: E402


def run_main(argv):
    """调 main 并吞掉输出；参数错误的 SystemExit 折算成退出码。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = DFS.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class FixtureCase(SFCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="sf_dfs_test_")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.folder, self.repo = DFS.make_fixture(self.tmp)
        self.base = ["--dir", str(self.folder), "--repo", str(self.repo), "--version", DFS.VER]

    def refresh(self, *extra):
        return run_main(self.base + ["--refresh", "--backup-dir", str(self.tmp / "bak")] + list(extra))


class CommandLineShape(SFCase):
    """链上工具的参数形状规矩：未知参数与缺少动作一律退 2，绝不落到默认动作。"""

    def test_unknown_argument_and_missing_action_exit_two(self):
        for argv in (["--bogus"], [], ["--adopt", "x.md"], ["--check", "--refresh"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv)[0], 2)

    def test_adopt_without_refresh_is_refused(self):
        self.assertEqual(run_main(["--check", "--adopt", "x.md"])[0], 2)

    def test_self_test_passes_and_never_touches_the_real_desktop(self):
        real = Path(DFS.DESKTOP_OUTPUT_DIR)
        before = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        code, out, _ = run_main(["--self-test"])
        after = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        self.assertEqual(code, 0, out)
        self.assertEqual(before, after, "自检不许改动真实的桌面分发目录")


class CheckAndRefreshFlow(FixtureCase):
    def test_unregistered_document_is_a_note_not_a_failure(self):
        code, out, _ = run_main(self.base + ["--check"])
        self.assertEqual(code, 0, out)
        self.assertIn("notes.md", out)

    def test_adopt_then_check_is_green_and_leaves_body_untouched(self):
        self.assertEqual(self.refresh("--adopt", "notes.md")[0], 0)
        self.assertEqual(run_main(self.base + ["--check"])[0], 0)
        text = (self.folder / "notes.md").read_text(encoding="utf-8")
        for line in DFS.DOC_BODY.splitlines():
            if line.strip():
                self.assertIn(line, text)
        self.assertIn(DFS.BEGIN, text)

    def test_refresh_without_adopt_does_not_touch_an_unregistered_document(self):
        before = (self.folder / "notes.md").read_bytes()
        self.assertEqual(self.refresh()[0], 0)
        self.assertEqual((self.folder / "notes.md").read_bytes(), before)

    def test_refresh_refuses_when_the_release_zip_is_wrong(self):
        self.assertEqual(self.refresh("--adopt", "notes.md")[0], 0)
        (self.folder / ("Skyrocket-%s-macos.zip" % DFS.VER)).write_bytes(b"tampered")
        DFS.edit_doc(self.folder, "对账版本：" + DFS.VER, "对账版本：0.0.1")
        stale = (self.folder / "notes.md").read_bytes()
        self.assertEqual(self.refresh()[0], 1)
        self.assertEqual((self.folder / "notes.md").read_bytes(), stale)

    def test_stale_reference_exits_three_and_is_listed_not_rewritten(self):
        self.assertEqual(self.refresh("--adopt", "notes.md")[0], 0)
        DFS.append_doc(self.folder, "\n见 `tools/gone.py`\n")
        code, out, _ = self.refresh()
        self.assertEqual(code, 3, out)
        self.assertIn("tools/gone.py", out)
        self.assertTrue((self.folder / "notes.md").read_text(encoding="utf-8").rstrip().endswith("`tools/gone.py`"))

    def test_frozen_manifest_must_not_be_refreshed(self):
        name = next(iter(DFS.FROZEN))
        (self.folder / name).write_text('{"version": "%s"}' % DFS.VER, encoding="utf-8")
        code, out, _ = run_main(self.base + ["--check"])
        self.assertEqual(code, 1, out)
        self.assertIn(name, out)

    def test_old_release_leftovers_are_flagged(self):
        (self.folder / "Skyrocket-V0.0.1.dmg").write_bytes(b"old")
        code, out, _ = run_main(self.base + ["--check"])
        self.assertEqual(code, 1, out)
        self.assertIn("Skyrocket-V0.0.1.dmg", out)

    def test_missing_directory_is_a_failure(self):
        code, _, err = run_main(["--dir", str(self.tmp / "nope"), "--repo", str(self.repo), "--check"])
        self.assertEqual(code, 1)
        code, _, err = run_main(["--dir", str(self.tmp / "nope"), "--repo", str(self.repo), "--refresh",
                                 "--backup-dir", str(self.tmp / "bak")])
        self.assertEqual(code, 1)
        self.assertIn("不存在", err)


class ReleaseChainWiring(SFCase):
    """守的是不可逆 shell 调用的顺序，没有可调用的行为可断言（整条跑就是真发版）。"""

    def setUp(self):
        super().setUp()
        self.source = (ROOT / "build_release.sh").read_text(encoding="utf-8")
        self.call = '"$VENV_PY" tools/desktop_folder_sync.py --refresh'

    def test_sync_runs_after_publish_and_push_inside_the_publishing_branch(self):
        at = self.source.index(self.call)
        self.assertEqual(self.source.count(self.call), 1)
        self.assertGreater(at, self.source.index("tools/publish_update_release.py --assets"))
        self.assertGreater(at, self.source.index('git -C "$PROJ" push -q origin'))
        self.assertLess(at, self.source.index("== 本机模式：不生成/发布 OTA"))

    def test_failure_is_reported_in_the_final_verdict_not_swallowed(self):
        self.assertIn('DESKTOP_NOTE=""', self.source)
        self.assertIn('if [ -n "${DESKTOP_NOTE:-}" ]; then', self.source)
        self.assertLess(self.source.index(self.call), self.source.index('if [ -n "${DESKTOP_NOTE:-}" ]; then'))


class ManifestAndFrozenFailures(FixtureCase):
    """清单或冻结件读不出来时，必须是一条说得清的失败，而不是异常或默默放行。"""

    def problems(self):
        return "\n".join(DFS.audit(self.folder, DFS.VER, self.repo).problems)

    def test_missing_manifest_is_reported_and_stops_the_zip_comparison(self):
        (self.folder / DFS.CURRENT_JSON).unlink()
        report = DFS.audit(self.folder, DFS.VER, self.repo)
        self.assertEqual(report.code(), 1)
        text = "\n".join(report.problems)
        self.assertIn("缺少 " + DFS.CURRENT_JSON, text)
        self.assertIn("无法核对 ZIP", text)

    def test_manifest_that_is_not_json_is_named_as_unreadable(self):
        (self.folder / DFS.CURRENT_JSON).write_text("{not json", encoding="utf-8")
        text = self.problems()
        self.assertIn("读不出来", text)
        self.assertIn("无法核对 ZIP", text)

    def test_manifest_that_is_not_an_object_is_a_failure(self):
        (self.folder / DFS.CURRENT_JSON).write_text("[]", encoding="utf-8")
        self.assertIn("无法核对 ZIP", self.problems())

    def test_unreadable_frozen_manifest_is_a_failure(self):
        name = next(iter(DFS.FROZEN))
        for label, content in (("not json", "{not json"), ("not an object", "[]")):
            with self.subTest(label=label):
                (self.folder / name).write_text(content, encoding="utf-8")
                self.assertIn(name + " 读不出来", self.problems())

    def test_frozen_pin_equal_to_the_current_release_asks_for_review(self):
        pinned = next(iter(DFS.FROZEN.values()))
        report = DFS.Report()
        DFS.check_frozen(self.folder, pinned, report)
        self.assertTrue(any("登记表需要复核" in p for p in report.problems), report.problems)


class DocSyncRefusal(FixtureCase):
    def test_sync_doc_refuses_unregistered_and_broken_documents_untouched(self):
        doc = self.folder / "notes.md"
        original = doc.read_bytes()
        backup = self.tmp / "bak"
        self.assertEqual(DFS.sync_doc(doc, lambda body: "x", backup, False), "refused")
        self.assertEqual(doc.read_bytes(), original)
        broken = original + (DFS.BEGIN + "\n").encode("utf-8")
        doc.write_bytes(broken)
        self.assertEqual(DFS.sync_doc(doc, lambda body: "x", backup, True), "refused")
        self.assertEqual(doc.read_bytes(), broken)
        self.assertFalse(backup.exists(), "拒绝时不该留下备份目录")


class SelfTestMachinery(FixtureCase):
    """自检本身也要能失败：它红的时候必须说清楚红在哪、并返回非零。"""

    def test_tally_records_only_the_failed_expectations(self):
        tally = DFS.Tally()
        tally.expect("good", True)
        tally.expect("bad", False)
        self.assertEqual((tally.total, tally.failed), (2, ["bad"]))

    def test_self_test_turns_red_and_names_the_planted_failure(self):
        def planted(tally):
            tally.expect("planted failure", False)

        err = io.StringIO()
        with mock.patch.object(DFS, "selftest_edges", planted), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = DFS.self_test()
        self.assertEqual(code, 1)
        self.assertIn("planted failure", err.getvalue())
        self.assertIn("自检失败", err.getvalue())

    def test_fixture_helpers_fail_loudly_when_their_premise_is_wrong(self):
        with self.assertRaises(RuntimeError):
            DFS.edit_doc(self.folder, "这段文字不在夹具里", "x")
        with tempfile.TemporaryDirectory(prefix="sf_dfs_test_") as raw, \
                mock.patch.object(DFS, "refresh", return_value=1):
            with self.assertRaises(RuntimeError):
                DFS.adopted_fixture(Path(raw))

    def test_running_the_script_directly_exits_with_the_self_test_result(self):
        with mock.patch.object(sys, "argv", ["desktop_folder_sync.py", "--self-test"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(ROOT / "tools" / "desktop_folder_sync.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
