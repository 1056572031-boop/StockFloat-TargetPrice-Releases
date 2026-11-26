"""release_resume：发布后续跑与收口日志。离线，不访问网络、不碰真实发布树。"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._base import SFCase
from tools import quality_evidence as evidence
from tools import release_resume as resume

REPO = Path(__file__).resolve().parents[1]
# 样本版本用构造值（AGENTS §6 第 1 条：用例不对版本号下断言）。
VERSION = ".".join(("7", "8", "9"))
HEAD = "a" * 40
# 样本里的“发布开始”取一个固定的过去时刻，不依赖现在几点；夹具文件的修改时间都在它之后。
STARTED = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
APP = Path("/Applications/Skyrocket.app")
HARD = ("6.0-public-release", "6.1-download-entry", "6.2-render-guide", "6.2-check-guide", "8-final-smoke")
SOFT = ("7-push", "7.1-desktop")
PUBLISH_LOG = "\n".join((
    "已安装: " + str(APP),
    "版本: " + VERSION,
    "published_archive=Skyrocket-" + VERSION + "-macos.zip",
    "   " + resume.LIVE_MANIFEST_MARK + "，发版可以宣布成功",
))


class FakeRunner:
    """按命令行里出现的片段决定退出码；也可以让某条命令抛 OSError 或超时。"""

    def __init__(self, rcs=None, raises=None):
        self.rcs = rcs or {}
        self.raises = raises or {}
        self.calls = []

    def __call__(self, argv, **kwargs):
        line = " ".join(argv)
        self.calls.append((line, kwargs))
        for fragment, exc in self.raises.items():
            if fragment in line:
                raise exc
        if argv[:2] == ["git", "rev-list"]:
            return SimpleNamespace(returncode=0, stdout=b"0\n")
        rc = next((code for fragment, code in self.rcs.items() if fragment in line), 0)
        return SimpleNamespace(returncode=rc, stdout=("输出 " + line + "\n").encode())

    def commands(self):
        return [line for line, _ in self.calls]


class _TempCase(SFCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="sf_resume_test_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "tree"
        self.evidence = Path(temp.name) / "evidence"
        self.root.mkdir()
        (self.root / "sf_constants.py").write_text('APP_VERSION = "%s"\n' % VERSION, encoding="utf-8")
        snapshot = mock.patch("release_state.source_snapshot", return_value={"head": HEAD})
        self.addCleanup(snapshot.stop)
        snapshot.start()
        index = mock.patch.object(resume, "verify_index")
        self.addCleanup(index.stop)
        index.start()
        guide = mock.patch.object(resume, "verify_guide_render")
        self.addCleanup(guide.stop)
        guide.start()
        self.steps = resume.plan("py", VERSION, "feat/x", APP)

    def write_results(self, rcs=None, ahead="0\n", smoke=True):
        """造一份“各步骤都跑过”的证据目录；rcs 里的值覆盖默认的 0。"""
        self.evidence.mkdir(exist_ok=True)
        for name, _, _ in self.steps:
            code = (rcs or {}).get(name, 0)
            (self.evidence / ("rest-%s.rc" % name)).write_text("%s\n" % code, encoding="utf-8")
            body = "日志 " + name + "\n"
            if name == "8-final-smoke" and smoke:
                body += "== 结论: 冒烟通过 ==\n"
            (self.evidence / ("rest-%s.log" % name)).write_text(body, encoding="utf-8")
        ahead_file = self.evidence / resume.AHEAD_FILE
        if ahead is None:
            ahead_file.unlink(missing_ok=True)       # 同一目录被多个子用例复用，不能留着上一轮的文件
        else:
            ahead_file.write_text(ahead, encoding="utf-8")

    def write_publish_log(self, text=PUBLISH_LOG, name="publish.log"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_state(self, stage=resume.PENDING_STAGE, version=VERSION, head=HEAD, history=True,
                    source=None):
        path = self.root / "state.json"
        entries = [{"from": "candidate_ready", "to": resume.PENDING_STAGE, "at": STARTED.isoformat()}]
        source = source or {"head": head}
        path.write_text(json.dumps({"stage": stage, "version": version, "source": source,
                                    "history": entries if history else []}), encoding="utf-8")
        return path


class PlanTests(_TempCase):
    def test_hard_and_soft_steps_follow_the_script(self):
        # 失败即停与否照脚本：6.1/6.2/公开发布/最终复验失败 exit 1；推送与桌面对账只记结果。
        self.assertEqual([name for name, hard, _ in self.steps if hard], list(HARD))
        self.assertEqual([name for name, hard, _ in self.steps if not hard], list(SOFT))

    def test_order_and_commands_match_build_release_script(self):
        # 守的是顺序与命令行，而不是行为：脚本改了顺序或参数而这里没跟着改，续跑就会和脚本分叉，
        # 又没有可调用行为可断言（真跑就是真发布），所以只能读脚本文本，逐项在“上一项之后”找。
        source = (REPO / "build_release.sh").read_text(encoding="utf-8")
        # 6.1 必须带自动出处：删掉这个环境变量，发版部署又会变成没有提交号的黑盒。
        markers = ["tools/public_release.py --publish", "SF_DEPLOY_PROVENANCE=auto node deploy-pages.mjs", "tools/render_install_guide.py",
                   "tools/render_install_guide.py --check", 'push -q origin "HEAD:refs/heads/$SRC_BRANCH"',
                   "tools/desktop_folder_sync.py --refresh --version", "tools/smoke_frozen_app.py"]
        self.assertEqual(len(markers), len(self.steps))
        at = 0
        for marker, (name, _, _) in zip(markers, self.steps):
            at = source.find(marker, at)
            self.assertGreater(at, -1, "脚本里在上一步之后找不到 %s（%s）" % (marker, name))
            at += 1
        smoke = self.steps[-1][2]
        self.assertIn(" ".join(smoke[3:]), source)       # --platform cocoa --timeout 30

    def test_publish_step_exists_only_when_a_quality_record_is_given_and_comes_first(self):
        record = Path("/records/quality.json")
        # 无质量记录的基础计划不包含 OTA 续跑；绑定质量记录时才插入新步骤。
        self.assertNotIn("6-publish-ota", [name for name, _, _ in self.steps])
        with_publish = resume.plan("py", VERSION, "feat/x", APP, record)
        self.assertEqual([name for name, _, _ in with_publish][0], "6.0-public-release")
        self.assertEqual([name for name, _, _ in with_publish][1], resume.PUBLISH_STEP)
        self.assertEqual([name for name, _, _ in with_publish][2:], [name for name, _, _ in self.steps][1:])
        name, hard, argv = with_publish[1]
        self.assertEqual((name, hard), (resume.PUBLISH_STEP, True))
        self.assertEqual(argv[:2], ["/usr/bin/env", "SF_QUALITY_RECORD=" + str(record)])
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--assets") + 1], "build/update_assets/V" + VERSION)
        self.assertEqual(argv[argv.index("--public-assets") + 1], "build/public_assets/V" + VERSION)
        source = (REPO / "build_release.sh").read_text(encoding="utf-8")
        publish_at = source.find("tools/publish_update_release.py")
        self.assertGreater(publish_at, -1)
        self.assertLess(publish_at, source.find("node deploy-pages.mjs"), "脚本里第 6 步在 6.1 之前")
        # 下载入口的读回要核对 DMG、源码 ZIP、校验清单都已在发行页：它们由公开发布步骤上传，必须先于读回。
        public_at = source.find('tools/public_release.py --publish')
        self.assertGreater(public_at, -1, "找不到公开发布调用")
        self.assertLess(public_at, source.find("node deploy-pages.mjs"), "公开发布必须在下载入口读回之前")

    def test_every_release_asset_is_uploaded_before_the_download_entry_is_read_back(self):
        # 下载入口的线上读回要求 DMG、源码 ZIP、校验清单都已在发行页；v1.4.109 与 v1.4.110 两次都因为
        # 公开发布排在读回之后而在读回处停住。这里守的是计划里的顺序，不是命令行文本。
        names = [name for name, _, _ in self.steps]
        self.assertLess(names.index("6.0-public-release"), names.index("6.1-download-entry"))
        self.assertLess(names.index("6.1-download-entry"), names.index("6.2-render-guide"))
        self.assertLess(names.index("6.2-render-guide"), names.index("6.2-check-guide"))

    def test_app_version_is_read_from_constants_without_importing_it(self):
        self.assertEqual(resume.app_version(self.root), VERSION)
        (self.root / "sf_constants.py").write_text("OTHER = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "APP_VERSION"):
            resume.app_version(self.root)


class ScriptTextTests(SFCase):
    """工具补出的两行必须和脚本真实打印的逐字一致（读脚本文本拦不住措辞漂移，所以真跑收尾段）。"""

    def run_closing(self, directory, sync_note):
        source = (REPO / "build_release.sh").read_text(encoding="utf-8")
        timing = source.split("# BEGIN step_timing", 1)[1].split("# END step_timing", 1)[0]
        block = source[source.index("# ---- 8)"):]
        root = Path(directory)
        env = dict(os.environ, LOCAL_ONLY="0", PROJ=str(root), VERSION=VERSION, APP_BUNDLE="Skyrocket.app",
                   DO_INSTALL="1", INSTALL_DIR=str(APP), VENV_PY="/usr/bin/true",
                   SRC_BRANCH="feat/fixture", SYNC_NOTE=sync_note)
        done = subprocess.run(["/bin/bash", "-c", "set -eu\n" + timing + "\n" + block], env=env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.splitlines()

    def test_header_and_closing_line_equal_what_the_script_prints(self):
        with tempfile.TemporaryDirectory(prefix="sf_resume_script_") as directory:
            for sync_note in ("", "源码未同步"):
                with self.subTest(sync_note=sync_note):
                    lines = self.run_closing(directory, sync_note)
                    self.assertIn(resume.step_header(APP), lines)
                    self.assertIn(resume.closing_line(VERSION, sync_note), lines)


class RunStepsTests(_TempCase):
    def run_it(self, runner, **kwargs):
        out = []
        code = resume.run_steps(self.root, self.evidence, self.steps, "feat/x", runner=runner,
                                out=out.append, **kwargs)
        return code, out

    def test_dry_run_changes_nothing(self):
        runner = FakeRunner()
        code, out = self.run_it(runner, apply=False)
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [])
        self.assertTrue(all(line.startswith("预演") for line in out), out)
        self.assertFalse(self.evidence.exists(), "预演不许创建证据目录")

    def test_apply_runs_every_step_in_order_and_records_each_result(self):
        runner = FakeRunner()
        with mock.patch.dict(os.environ, {"SF_LANES": "4", "SF_SKIP_COVERAGE": "1", "KEEP_ME": "yes"}):
            code, _ = self.run_it(runner, apply=True, timeout=123.0)
        self.assertEqual(code, 0)
        self.assertEqual({kwargs["timeout"] for _, kwargs in runner.calls}, {123.0})
        names = [name for name, _, _ in self.steps]
        for name in names:
            self.assertEqual(resume.step_rc(self.evidence, name), 0)
            self.assertIn("输出", (self.evidence / ("rest-%s.log" % name)).read_text(encoding="utf-8"))
        self.assertEqual((self.evidence / resume.AHEAD_FILE).read_text(encoding="utf-8"), "0\n")
        commands = runner.commands()
        self.assertEqual(len(commands), len(names) + 1)           # 多出的一条是推送后的落后提交数
        self.assertIn("rev-list", commands[names.index("7-push") + 1])
        for _, kwargs in runner.calls:
            self.assertNotIn("SF_LANES", kwargs["env"])
            self.assertNotIn("SF_SKIP_COVERAGE", kwargs["env"])
            self.assertEqual(kwargs["env"].get("KEEP_ME"), "yes")
            self.assertEqual(kwargs["cwd"], self.root)

    def test_source_change_between_steps_stops_before_next_command(self):
        runner = FakeRunner()
        checks = mock.Mock(side_effect=(None, ValueError("源码或文档已变化")))
        with self.assertRaisesRegex(ValueError, "源码或文档"):
            resume.run_steps(self.root, self.evidence, self.steps, "feat/x", apply=True,
                             runner=runner, out=lambda _line: None, verify_source=checks)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(resume.step_rc(self.evidence, self.steps[0][0]), 0)
        self.assertIsNone(resume.step_rc(self.evidence, self.steps[1][0]))

    def test_a_failing_hard_step_stops_the_run_and_later_steps_do_not_start(self):
        runner = FakeRunner(rcs={"--check": 3})
        code, out = self.run_it(runner, apply=True)
        self.assertEqual(code, 3)
        self.assertEqual(resume.step_rc(self.evidence, "6.2-check-guide"), 3)
        # 排在它前面的步骤照常跑完；排在它后面的（推送、桌面对账、最终冷启动）一个都不许开始。
        for earlier in ("6.0-public-release", "6.1-download-entry", "6.2-render-guide"):
            self.assertEqual(resume.step_rc(self.evidence, earlier), 0, earlier)
        for later in ("7-push", "7.1-desktop", "8-final-smoke"):
            self.assertIsNone(resume.step_rc(self.evidence, later), later)
        self.assertTrue(any("按脚本口径停在这里" in line for line in out), out)

    def test_a_failing_soft_step_is_recorded_and_the_run_goes_on(self):
        runner = FakeRunner(rcs={"push": 1})
        code, _ = self.run_it(runner, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(resume.step_rc(self.evidence, "7-push"), 1)
        self.assertEqual(resume.step_rc(self.evidence, "8-final-smoke"), 0)

    def test_steps_that_already_succeeded_are_skipped_unless_redone(self):
        self.write_results()
        runner = FakeRunner()
        code, out = self.run_it(runner, apply=True)
        self.assertEqual((code, runner.calls), (0, []))
        self.assertEqual(len([line for line in out if line.startswith("跳过")]), len(self.steps))
        code, _ = self.run_it(runner, apply=True, redo=("8-final-smoke",))
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("smoke_frozen_app", runner.commands()[0])

    def test_a_step_that_cannot_start_or_times_out_is_a_failure_not_a_crash(self):
        cases = (
            (OSError("missing tool"), resume.OS_ERROR_RC, "无法执行"),
            (subprocess.TimeoutExpired("x", 1, output=b"partial-output"), resume.TIMEOUT_RC, "超时"),
        )
        for exc, expected_rc, expected_text in cases:
            with self.subTest(exc=type(exc).__name__):
                runner = FakeRunner(raises={"deploy-pages": exc})
                code, _ = self.run_it(runner, apply=True)
                self.assertEqual(code, expected_rc)
                log = (self.evidence / "rest-6.1-download-entry.log").read_text(encoding="utf-8")
                self.assertIn(expected_text, log)
                if expected_rc == resume.TIMEOUT_RC:
                    self.assertIn("partial-output", log, "被杀前已经打印的部分要留在日志里")
                    self.assertIn(resume.STEP_TIMEOUT_ENV, log, "超时日志要说明怎么调")
                for leftover in self.evidence.glob("rest-*"):
                    leftover.unlink()

    def test_a_timeout_is_told_apart_from_a_command_that_could_not_start(self):
        self.assertNotEqual(resume.TIMEOUT_RC, resume.OS_ERROR_RC)

    def test_a_failed_step_is_rerun_without_redo_while_succeeded_ones_stay_skipped(self):
        self.write_results(rcs={"6.2-check-guide": 3})
        runner = FakeRunner()
        code, _ = self.run_it(runner, apply=True)
        self.assertEqual(code, 0)
        commands = runner.commands()
        self.assertIn("--check", commands[0], "失败过的步骤要自己重跑，不需要 --redo")
        self.assertTrue(all("render_install_guide.py --check" not in c for c in commands[1:]))
        self.assertEqual(resume.step_rc(self.evidence, "6.2-check-guide"), 0)
        self.assertEqual(len([c for c in commands if "git rev-list" in c]), 0, "成功过的推送不该重跑")

    def test_redo_names_that_are_not_in_the_plan_are_refused_before_anything_runs(self):
        self.write_results()
        runner = FakeRunner()
        with self.assertRaises(ValueError) as caught:
            self.run_it(runner, apply=True, redo=("8-final-smok",))
        self.assertIn("8-final-smok", str(caught.exception))
        self.assertIn("8-final-smoke", str(caught.exception), "要把可选的步骤名列出来")
        self.assertEqual(runner.calls, [])

    def test_the_old_exit_code_is_withdrawn_before_a_redo_so_an_interrupt_cannot_keep_it(self):
        self.write_results()
        runner = FakeRunner(raises={"smoke_frozen_app": KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            self.run_it(runner, apply=True, redo=("8-final-smoke",))
        self.assertIsNone(resume.step_rc(self.evidence, "8-final-smoke"),
                          "重做被打断后，上一次的 0 不能继续替这一步作证")
        ok, problems = resume.compose(self.root, self.evidence, [self.write_publish_log()], APP,
                                      self.root / "never.log", STARTED)
        self.assertFalse(ok)
        self.assertIn("8-final-smoke", " ".join(problems))
        self.assertFalse((self.root / "never.log").exists())

    def test_interrupted_ota_redo_cannot_be_omitted_from_composed_log(self):
        self.write_results()
        ota_rc = self.evidence / ("rest-%s.rc" % resume.PUBLISH_STEP)
        ota_log = self.evidence / ("rest-%s.log" % resume.PUBLISH_STEP)
        ota_rc.write_text("0\n", encoding="utf-8")
        ota_log.write_text("原来的成功日志\n", encoding="utf-8")
        steps = resume.plan("py", VERSION, "feat/x", APP, Path("record"))
        runner = FakeRunner(raises={"publish_update_release.py": KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            resume.run_steps(self.root, self.evidence, steps, "feat/x", apply=True,
                             redo=(resume.PUBLISH_STEP,), runner=runner)
        self.assertFalse(ota_rc.exists())
        out = self.root / "interrupted-ota.log"
        ok, problems = resume.compose(self.root, self.evidence, [self.write_publish_log()], APP, out, STARTED)
        self.assertFalse(ok, "被中断的 OTA 重做不能被合成日志忽略")
        self.assertIn(resume.PUBLISH_STEP, " ".join(problems))
        self.assertFalse(out.exists())

    def test_inflight_marker_overrides_old_success_and_forces_retry(self):
        self.write_results()
        marker = self.evidence / "rest-8-final-smoke.inflight"
        marker.write_text("started", encoding="utf-8")
        runner = FakeRunner()
        code, _ = self.run_it(runner, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("smoke_frozen_app", runner.commands()[0])
        self.assertFalse(marker.exists())

    def test_interrupted_push_readback_cannot_reuse_old_ahead_result(self):
        self.write_results(ahead="0\n")
        runner = FakeRunner(raises={"git rev-list": KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            self.run_it(runner, apply=True, redo=("7-push",))
        out = self.root / "interrupted-push.log"
        ok, problems = resume.compose(self.root, self.evidence, [self.write_publish_log()], APP, out, STARTED)
        self.assertFalse(ok, "推送后的读回中断时不能复用旧的同步结果")
        self.assertIn("7-push", " ".join(problems))
        self.assertFalse(out.exists())

    def test_a_dry_run_does_not_withdraw_any_exit_code(self):
        self.write_results()
        self.run_it(FakeRunner(), apply=False, redo=("8-final-smoke",))
        self.assertEqual(resume.step_rc(self.evidence, "8-final-smoke"), 0)

    def test_release_step_environment_removes_inherited_git_routing(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/wrong/repo", "GIT_WORK_TREE": "/wrong/tree",
                                         "GIT_INDEX_FILE": "/wrong/index", "KEEP_ME": "yes"}):
            runner = FakeRunner()
            resume.execute(runner, ["git", "push"], self.root, 10)
        env = runner.calls[0][1]["env"]
        self.assertFalse(any(key.startswith("GIT_") for key in env))
        self.assertEqual(env["KEEP_ME"], "yes")

    def test_the_default_runner_really_runs_a_command_and_captures_its_output(self):
        steps = [("echo", True, ["/bin/sh", "-c", "echo hello-from-step"])]
        out = []
        code = resume.run_steps(self.root, self.evidence, steps, "feat/x", apply=True, out=out.append)
        self.assertEqual(code, 0)
        self.assertIn("hello-from-step", (self.evidence / "rest-echo.log").read_text(encoding="utf-8"))
        self.assertEqual(resume.step_rc(self.evidence, "echo"), 0)

    def age_evidence(self, seconds_before_start):
        """把证据目录里所有文件的修改时间退到“本次发布开始”之前。"""
        stamp = STARTED.timestamp() - seconds_before_start
        for path in self.evidence.glob("rest-*"):
            os.utime(path, (stamp, stamp))

    def test_evidence_older_than_this_publish_is_rerun_instead_of_skipped(self):
        self.write_results()
        self.age_evidence(3600)
        runner = FakeRunner()
        code, out = self.run_it(runner, apply=True, since=STARTED)
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls) - 1, len(self.steps), "旧发布留下的 0 不能替这次发布作证，每一步都要重跑")
        self.assertEqual(len([line for line in out if line.startswith("重跑")]), len(self.steps))
        self.assertEqual([line for line in out if line.startswith("跳过")], [])

    def test_evidence_from_this_publish_is_still_skipped(self):
        self.write_results()                       # 夹具文件的修改时间是“现在”，晚于固定的过去时刻 STARTED
        runner = FakeRunner()
        code, out = self.run_it(runner, apply=True, since=STARTED)
        self.assertEqual((code, runner.calls), (0, []))
        self.assertEqual(len([line for line in out if line.startswith("跳过")]), len(self.steps))

    def test_a_step_with_an_exit_code_but_no_log_is_rerun_when_freshness_is_checked(self):
        self.write_results()
        (self.evidence / "rest-6.2-render-guide.log").unlink()
        runner = FakeRunner()
        code, _ = self.run_it(runner, apply=True, since=STARTED)
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("render_install_guide.py", runner.commands()[0])

    def test_without_a_start_time_old_behaviour_is_kept(self):
        self.write_results()
        self.age_evidence(3600)
        runner = FakeRunner()
        code, _ = self.run_it(runner, apply=True)
        self.assertEqual((code, runner.calls), (0, []))

    def test_the_publish_step_gets_its_own_larger_time_budget(self):
        record = Path("/records/quality.json")
        steps = resume.plan("py", VERSION, "feat/x", APP, record)
        runner = FakeRunner()
        out = []
        with mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: ""}):
            code = resume.run_steps(self.root, self.evidence, steps, "feat/x", runner=runner,
                                    out=out.append, apply=True, timeout=123.0)
        self.assertEqual(code, 0)
        budgets = {line: kwargs["timeout"] for line, kwargs in runner.calls}
        publish = [value for line, value in budgets.items() if "publish_update_release.py" in line]
        others = {value for line, value in budgets.items() if "publish_update_release.py" not in line}
        self.assertEqual(publish, [resume.PUBLISH_TIMEOUT_DEFAULT])
        self.assertEqual(others, {123.0}, "其余步骤仍然用通用预算")
        # 上传一次 + 读回最多四次，每次最长 900 秒（publish_update_release 的上传超时默认值），再留余量。
        self.assertGreaterEqual(resume.PUBLISH_TIMEOUT_DEFAULT, 5 * 900.0 + 900.0)

    def test_the_publish_budget_follows_a_larger_general_budget_and_an_explicit_setting_wins(self):
        with mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: ""}):
            self.assertEqual(resume.timeout_for_step(resume.PUBLISH_STEP, 9999.0), 9999.0)
            self.assertEqual(resume.timeout_for_step("7-push", 9999.0), 9999.0)
        with mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: "1000"}):
            self.assertEqual(resume.timeout_for_step(resume.PUBLISH_STEP, 9999.0), 1000.0)
            self.assertEqual(resume.timeout_for_step("7-push", 9999.0), 9999.0)


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ProcessGroupTests(_TempCase):
    """超时与打断时整组进程都要死；用真进程验证，因为“只杀直接子进程”在替身上看不出区别。"""

    SCRIPT = "sleep 300 & echo $!; wait"

    def start_args(self):
        return dict(cwd=self.root, env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def grandchild_pid(self, text):
        return int(text.split()[0])

    def test_a_timeout_kills_the_grandchildren_too_and_keeps_the_partial_output(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            resume.run_group(["/bin/sh", "-c", self.SCRIPT], timeout=2.0, **self.start_args())
        pid = self.grandchild_pid(caught.exception.output.decode())
        self.addCleanup(lambda: process_alive(pid) and os.kill(pid, 9))
        self.assertTrue(self.wait_until_false(lambda: process_alive(pid), timeout=5.0),
                        "孙进程在超时后必须一起被杀掉")

    def test_execute_turns_that_timeout_into_the_timeout_exit_code(self):
        rc, text = resume.execute(resume.run_group, ["/bin/sh", "-c", self.SCRIPT], self.root, 2.0)
        pid = self.grandchild_pid(text)
        self.addCleanup(lambda: process_alive(pid) and os.kill(pid, 9))
        self.assertEqual(rc, resume.TIMEOUT_RC)
        self.assertIn("超时", text)
        self.assertTrue(self.wait_until_false(lambda: process_alive(pid), timeout=5.0))

    def test_an_interrupt_kills_the_whole_group_and_is_not_swallowed(self):
        class InterruptingProc:
            """委托给真进程，只在第一次 communicate 时抛 KeyboardInterrupt。

            不继承 subprocess.Popen：测试运行器会把 subprocess.Popen 换成一个拦截函数（弹窗守卫），
            在那里 class X(subprocess.Popen) 直接 TypeError，而单独跑 unittest 时没有这层守卫，会漏过去。"""

            def __init__(self, real):
                self.real = real
                self.interrupted = False

            @property
            def pid(self):
                return self.real.pid

            def poll(self):
                return self.real.poll()

            def communicate(self, *args, **kwargs):
                if not self.interrupted:
                    self.interrupted = True
                    # 先等孙进程把自己的号码打印出来，保证被杀的确实是一棵已经成形的进程树。
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and not self.real.stdout.peek(1):
                        time.sleep(0.01)
                    raise KeyboardInterrupt()
                return self.real.communicate(*args, **kwargs)

        proc_ref = {}

        def spying_popen(*args, **kwargs):
            proc_ref["proc"] = InterruptingProc(subprocess.Popen(*args, **kwargs))
            return proc_ref["proc"]

        with self.assertRaises(KeyboardInterrupt):
            resume.run_group(["/bin/sh", "-c", self.SCRIPT], timeout=30.0, popen=spying_popen,
                             **self.start_args())
        proc = proc_ref["proc"]
        self.assertTrue(self.wait_until_false(lambda: proc.poll() is None, timeout=5.0),
                        "打断之后直接子进程也必须已经退出")
        try:
            os.killpg(proc.pid, 0)
            group_alive = True
        except ProcessLookupError:
            group_alive = False
        if group_alive:
            os.killpg(proc.pid, 9)
        self.assertFalse(group_alive, "打断之后整个进程组都必须已经被杀")

    def test_a_group_that_already_exited_is_not_an_error_when_the_kill_comes_late(self):
        class RacingProc:
            pid = 4321

            def communicate(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(["fake"], timeout, output=b"done")
                return b"done", None

        with mock.patch.object(resume.os, "killpg", side_effect=ProcessLookupError) as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                resume.run_group(["fake"], timeout=5.0, popen=lambda *args, **kwargs: RacingProc(),
                                 **self.start_args())
        kill.assert_called_once_with(4321, resume.signal.SIGKILL)

    def test_a_normal_run_returns_status_and_output_like_subprocess_run(self):
        done = resume.run_group(["/bin/sh", "-c", "echo out; exit 4"], timeout=10.0, **self.start_args())
        self.assertEqual((done.returncode, done.stdout.strip()), (4, b"out"))

    # 一个一次性的子 Python：在里面跑 run_group，由测试从外面给它发真信号。不能在测试进程自己身上发
    # SIGTERM：处理器万一没装上，死的就是整个测试运行器。
    SIGNAL_CHILD = r'''
import os, signal, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools import release_resume as resume
pidfile, ready = sys.argv[2], sys.argv[3]
signal.signal(signal.SIGHUP, signal.SIG_DFL)      # 父进程若是 nohup 起的，会把“忽略”继承下来；这里不依赖启动方式
original = signal.getsignal(signal.SIGTERM)
def announce():
    while signal.getsignal(signal.SIGTERM) == original:   # 处理器被换掉，才算“已经可以被打断”
        time.sleep(0.01)
    Path(ready).write_text("ready")
threading.Thread(target=announce, daemon=True).start()
script = "sleep 300 & echo $! > " + pidfile + "; wait"
try:
    resume.run_group(["/bin/sh", "-c", script], cwd=sys.argv[1], env=dict(os.environ),
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
except resume.ProcessGroupInterrupted as exc:
    print("INTERRUPTED", exc.args[0])
    print("RESTORED", signal.getsignal(signal.SIGTERM) == original)
    sys.exit(7)
'''

    def run_child_and_signal(self, signum):
        pidfile, ready = self.root / "grandchild.pid", self.root / "ready"
        child = subprocess.Popen([sys.executable, "-c", self.SIGNAL_CHILD, str(REPO), str(pidfile), str(ready)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())

        def grandchild():
            text = pidfile.read_text().strip() if pidfile.exists() else ""
            return int(text) if text.isdigit() else 0

        # 正常情况下一两秒内就绪；上限取 20 秒是为了让“处理器没装上”这种失败在两个子用例合计
        # 40 秒内以断言失败收场，而不是被运行器的单用例 60 秒超时掐掉。
        up = self.wait_for(lambda: ready.exists() and grandchild(), timeout=20.0)
        if not up:
            child.kill()
            self.fail("子进程没有在限时内把处理器装好并起出孙进程：" + child.communicate()[0])
        pid = grandchild()
        self.addCleanup(lambda: process_alive(pid) and os.kill(pid, 9))
        os.kill(child.pid, signum)
        output, _ = child.communicate(timeout=30)
        return child.returncode, output, pid

    def test_a_termination_signal_kills_the_whole_group_and_restores_the_handlers(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                rc, output, pid = self.run_child_and_signal(signum)
                self.assertEqual(rc, 7, "没有走到清理分支就被信号直接带走了：" + output)
                self.assertIn("INTERRUPTED %d" % int(signum), output)
                self.assertIn("RESTORED True", output, "处理器必须在清理之后还原")
                self.assertTrue(self.wait_until_false(lambda: process_alive(pid), timeout=5.0),
                                "被信号打断后孙进程必须一起被杀，不能留着继续写线上")
                for leftover in ("ready", "grandchild.pid"):
                    (self.root / leftover).unlink(missing_ok=True)

    def test_the_installed_handler_turns_a_signal_into_the_cleanup_path_in_process(self):
        # 上面那条用真信号的用例跑在子进程里，覆盖率看不见它；这里在本进程里直接调用已装上的处理器，
        # 证明“处理器 → 异常 → 杀整组 → 原样抛出”这条链本身成立，不依赖子进程。
        class Proc:
            pid = 4321
            returncode = None

            def communicate(self, timeout=None):
                if timeout is not None:
                    signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)    # 就像收到了一次挂起信号
                return b"", None

        # 运行器若是 nohup 起的，SIGHUP 原本是“忽略”，处理器就不会装；先置成默认，不让用例依赖启动方式。
        previous = signal.signal(signal.SIGHUP, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        with mock.patch.object(resume.os, "killpg") as kill:
            with self.assertRaises(resume.ProcessGroupInterrupted) as caught:
                resume.run_group(["fake"], timeout=5.0, popen=lambda *args, **kwargs: Proc(), **self.start_args())
        self.assertEqual(caught.exception.args, (signal.SIGHUP,))
        kill.assert_called_once_with(4321, signal.SIGKILL)

    def test_handlers_exist_only_while_waiting_and_are_restored_afterwards(self):
        seen = {}

        class Proc:
            pid = 4321
            returncode = 0

            def communicate(self, timeout=None):
                seen["term"] = signal.getsignal(signal.SIGTERM)
                seen["hup"] = signal.getsignal(signal.SIGHUP)
                return b"", None

        previous = signal.signal(signal.SIGHUP, signal.SIG_DFL)      # 不依赖运行器是不是 nohup 起的
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
        resume.run_group(["fake"], timeout=5.0, popen=lambda *args, **kwargs: Proc(), **self.start_args())
        self.assertTrue(callable(seen["term"]) and callable(seen["hup"]), "等待期间必须装着转成异常的处理器")
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)), before)

    def test_a_signal_the_user_chose_to_ignore_stays_ignored(self):
        seen = {}

        class Proc:
            pid = 4321
            returncode = 0

            def communicate(self, timeout=None):
                seen["hup"] = signal.getsignal(signal.SIGHUP)
                seen["term"] = signal.getsignal(signal.SIGTERM)
                return b"", None

        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        resume.run_group(["fake"], timeout=5.0, popen=lambda *args, **kwargs: Proc(), **self.start_args())
        self.assertEqual(seen["hup"], signal.SIG_IGN, "nohup 之类明确忽略的信号不能被改成“会终止”")
        self.assertTrue(callable(seen["term"]))
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_running_off_the_main_thread_does_not_crash_and_still_returns_output(self):
        result = {}

        def work():
            try:
                done = resume.run_group(["/bin/sh", "-c", "echo from-thread"], timeout=10.0, **self.start_args())
                result["out"] = done.stdout.strip()
            except Exception as exc:                          # 只要抛了就是缺陷，交给下面的断言报告
                result["exc"] = exc

        worker = threading.Thread(target=work)
        worker.start()
        worker.join(20.0)
        self.assertNotIn("exc", result, result)
        self.assertEqual(result.get("out"), b"from-thread")

    def test_an_unreadable_exit_code_file_counts_as_not_done(self):
        self.evidence.mkdir()
        (self.evidence / "rest-6.1-download-entry.rc").write_text("abc\n", encoding="utf-8")
        self.assertIsNone(resume.step_rc(self.evidence, "6.1-download-entry"))


class ComposeTests(_TempCase):
    def compose(self, publish_logs=None, out_name="combined.log"):
        logs = publish_logs if publish_logs is not None else [self.write_publish_log()]
        out = self.root / out_name
        ok, problems = resume.compose(self.root, self.evidence, logs, APP, out, STARTED)
        return ok, problems, out

    def test_complete_evidence_writes_a_log_that_satisfies_the_post_release_checker(self):
        self.write_results()
        source = self.write_publish_log()
        ok, problems, out = self.compose([source])
        self.assertEqual((ok, problems), (True, []))
        body = out.read_text(encoding="utf-8")
        self.assertEqual([m for m in evidence.publish_log_markers(APP, VERSION) if m not in body], [])
        self.assertIn(resume.closing_line(VERSION), body)
        self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), body)
        self.assertIn("不是 build_release.sh 一次连续运行的输出", body)
        self.assertIn("推送后落后远端提交数：0", body)

    def test_each_failed_or_missing_required_step_blocks_the_log_and_is_named(self):
        for name in HARD:
            for rc in (None, 5):
                with self.subTest(step=name, rc=rc):
                    self.write_results(rcs={name: rc if rc else 0})
                    if rc is None:
                        (self.evidence / ("rest-%s.rc" % name)).unlink()
                    ok, problems, out = self.compose(out_name="blocked-%s-%s.log" % (name, rc))
                    self.assertFalse(ok)
                    self.assertIn(name, " ".join(problems))
                    self.assertFalse(out.exists(), "条件不满足时不许落盘")

    def test_a_hard_step_with_only_a_zero_exit_code_cannot_supply_a_closing_line(self):
        self.write_results()
        for name in HARD:
            with self.subTest(step=name):
                (self.evidence / ("rest-%s.log" % name)).unlink()
                ok, problems, out = self.compose(out_name="missing-log-%s.log" % name)
                self.assertFalse(ok)
                self.assertIn(name, " ".join(problems))
                self.assertIn("没有它的步骤日志", " ".join(problems))
                self.assertFalse(out.exists(), "缺日志时不许写带收尾语的合成发布日志")
                self.write_results()

    def test_the_log_is_refused_when_the_live_manifest_was_never_read_back(self):
        self.write_results()
        source = self.write_publish_log(PUBLISH_LOG.replace(resume.LIVE_MANIFEST_MARK, "（读回缺失）"))
        ok, problems, out = self.compose([source])
        self.assertFalse(ok)
        self.assertIn(resume.LIVE_MANIFEST_MARK, " ".join(problems))
        self.assertFalse(out.exists())

    def test_markers_that_only_the_source_logs_can_supply_are_not_invented(self):
        # 已安装/版本/published_archive 只能来自原始发布日志；缺了就拒绝，不替它补。
        self.write_results()
        for marker in ("已安装: " + str(APP), "版本: " + VERSION, "published_archive=Skyrocket-" + VERSION):
            with self.subTest(marker=marker):
                source = self.write_publish_log(PUBLISH_LOG.replace(marker, "（缺）"), "no-%d.log" % len(marker))
                ok, problems, out = self.compose([source], "missing-%d.log" % len(marker))
                self.assertFalse(ok)
                self.assertIn("仍缺少", " ".join(problems))
                self.assertFalse(out.exists())

    def test_a_smoke_log_without_the_verdict_cannot_supply_the_verdict_line(self):
        self.write_results(smoke=False)
        ok, problems, out = self.compose()
        self.assertFalse(ok)
        self.assertIn("冒烟通过", " ".join(problems))
        self.assertFalse(out.exists())

    def test_the_verdict_must_come_from_the_final_smoke_log_not_from_an_earlier_one(self):
        # 候选阶段的冒烟（第 3.6 步）也会在原始发布日志里留下同一句；它证明不了最终启动复验跑过。
        self.write_results(smoke=False)
        source = self.write_publish_log(PUBLISH_LOG + "\n== " + resume.SMOKE_VERDICT + " ==")
        ok, problems, out = self.compose([source])
        self.assertFalse(ok)
        self.assertIn(resume.SMOKE_STEP, " ".join(problems))
        self.assertFalse(out.exists())

    def test_marker_lines_found_only_in_a_step_log_do_not_count_as_source_evidence(self):
        self.write_results()
        step_log = self.evidence / "rest-7.1-desktop.log"
        step_log.write_text("已安装: " + str(APP) + "\n", encoding="utf-8")
        source = self.write_publish_log(PUBLISH_LOG.replace("已安装: " + str(APP), "（缺）"))
        ok, problems, out = self.compose([source])
        self.assertFalse(ok)
        self.assertIn("来源日志仍缺少", " ".join(problems))
        self.assertFalse(out.exists())

    def test_trailing_whitespace_of_tool_output_is_dropped_but_the_hash_stays_the_originals(self):
        # wrangler 的表格、git 的 remote 提示带行尾空白，而提交钩子不接受带行尾空白的文件：
        # 合成日志要入库归档，所以附入时去掉；哈希仍是来源文件原始字节的，对得回去。
        self.write_results()
        (self.evidence / "rest-6.1-download-entry.log").write_text("Binding   \nremote: \t\n\n\n", encoding="utf-8")
        source = self.write_publish_log(PUBLISH_LOG + "   \n")
        original = (self.evidence / "rest-6.1-download-entry.log").read_bytes()
        ok, problems, out = self.compose([source])
        self.assertEqual((ok, problems), (True, []))
        body = out.read_text(encoding="utf-8")
        self.assertEqual([n for n, line in enumerate(body.split("\n"), 1) if line != line.rstrip()], [])
        self.assertNotIn("\n\n\n\n", body)
        self.assertIn(hashlib.sha256(original).hexdigest(), body)
        self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), body)
        self.assertEqual([m for m in evidence.publish_log_markers(APP, VERSION) if m not in body], [])

    def write_publish_step(self, rc=0, text=""):
        (self.evidence / ("rest-%s.rc" % resume.PUBLISH_STEP)).write_text("%s\n" % rc, encoding="utf-8")
        (self.evidence / ("rest-%s.log" % resume.PUBLISH_STEP)).write_text(text, encoding="utf-8")

    def test_a_publish_step_the_tool_ran_supplies_the_lines_only_the_publisher_prints(self):
        self.write_results()
        thin = "\n".join(line for line in PUBLISH_LOG.splitlines()
                         if not line.startswith("published_archive=") and resume.LIVE_MANIFEST_MARK not in line)
        source = self.write_publish_log(thin)
        ok, problems, _ = self.compose([source], "without-step.log")
        self.assertFalse(ok)
        self.write_publish_step(0, "published_archive=Skyrocket-%s-macos.zip\n   %s\n" % (VERSION, resume.LIVE_MANIFEST_MARK))
        ok, problems, out = self.compose([source], "with-step.log")
        self.assertEqual((ok, problems), (True, []))
        body = out.read_text(encoding="utf-8")
        self.assertIn("6-publish-ota=0", body)
        self.assertIn("步骤 6-publish-ota", body)

    def test_a_failed_publish_step_blocks_the_log_and_is_named(self):
        self.write_results()
        self.write_publish_step(5, PUBLISH_LOG)
        ok, problems, out = self.compose()
        self.assertFalse(ok)
        self.assertIn(resume.PUBLISH_STEP, " ".join(problems))
        self.assertFalse(out.exists())

    def test_without_a_publish_step_record_nothing_about_it_is_required(self):
        self.write_results()
        ok, problems, _ = self.compose()
        self.assertEqual((ok, problems), (True, []))

    def test_a_stale_publish_step_log_is_refused_like_any_other_evidence(self):
        self.write_results()
        self.write_publish_step(0, PUBLISH_LOG)
        long_ago = (STARTED - datetime.timedelta(days=1)).timestamp()
        os.utime(self.evidence / ("rest-%s.log" % resume.PUBLISH_STEP), (long_ago, long_ago))
        ok, problems, _ = self.compose()
        self.assertFalse(ok)
        self.assertIn("rest-%s.log" % resume.PUBLISH_STEP, " ".join(problems))

    def test_a_new_required_line_in_the_checker_is_not_papered_over_by_the_tool(self):
        # 验收校验器哪天多要一行（措辞或清单变了）而本工具没跟着改：合成后的复核必须拒绝，
        # 不能写出一份过不了 post-release 的“完整”日志。
        self.write_results()
        real = resume.evidence.publish_log_markers       # 工具自己 import 的那一份（与 tools.quality_evidence 不是同一个模块对象）

        def with_extra(app, version):
            return real(app, version) + ["发布后新增的必备行 " + version]

        with mock.patch.object(resume.evidence, "publish_log_markers", side_effect=with_extra):
            ok, problems, out = self.compose()
        self.assertFalse(ok)
        self.assertIn("发布后新增的必备行", " ".join(problems))
        self.assertFalse(out.exists())

    def test_evidence_older_than_the_publish_start_is_refused_and_named(self):
        # 证据目录被复用时，旧版本留下的退出码与日志不能沿用到这次发布。
        long_ago = (STARTED - datetime.timedelta(days=1)).timestamp()
        for victim in ("rest-8-final-smoke.rc", "rest-6.0-public-release.log", resume.AHEAD_FILE, "publish.log"):
            with self.subTest(victim=victim):
                self.write_results()
                source = self.write_publish_log()
                target = source if victim == "publish.log" else self.evidence / victim
                os.utime(target, (long_ago, long_ago))
                ok, problems, out = self.compose([source], "stale-%s.log" % victim)
                self.assertFalse(ok)
                self.assertIn(victim, " ".join(problems))
                self.assertFalse(out.exists())
        self.write_results()
        ok, problems, _ = self.compose(out_name="fresh.log")
        self.assertEqual((ok, problems), (True, []))

    def test_unsynced_source_is_carried_into_the_closing_line(self):
        for label, kwargs in (("落后三笔", dict(ahead="3\n")), ("推送失败", dict(rcs={"7-push": 1})),
                              ("没记录", dict(ahead=None))):
            with self.subTest(case=label):
                self.write_results(**kwargs)
                ok, _, out = self.compose(out_name="unsynced-%s.log" % label)
                self.assertTrue(ok)
                self.assertIn(resume.closing_line(VERSION, "源码未同步"), out.read_text(encoding="utf-8"))
        self.write_results(ahead=None)
        ok, _, out = self.compose(out_name="no-ahead.log")
        self.assertIn("推送后落后远端提交数：未记录", out.read_text(encoding="utf-8"))

    def test_an_existing_output_is_never_overwritten(self):
        self.write_results()
        ok, _, out = self.compose()
        self.assertTrue(ok)
        with self.assertRaisesRegex(ValueError, "不覆盖"):
            resume.compose(self.root, self.evidence, [self.root / "publish.log"], APP, out, STARTED)


class StateTests(_TempCase):
    """状态文件必须描述 --root 这棵树：版本与提交都对得上，否则拒绝（防止拿 A 的状态去操作 B 棵树）。"""

    def load(self, **kwargs):
        with mock.patch.object(resume, "git_output", return_value=HEAD):
            return resume.load_state(self.write_state(**kwargs), self.root, (resume.PENDING_STAGE,))

    def test_a_state_that_matches_the_tree_is_accepted(self):
        self.assertEqual(self.load()["version"], VERSION)

    def test_same_head_with_changed_source_identity_is_refused(self):
        source = {"head": HEAD, "gate_fingerprint": "original", "gate_input_count": 12,
                  "workspace_token": "original-workspace", "docs": {"README.md": "original-doc"}}
        for field, changed in (("gate_fingerprint", "changed"), ("gate_input_count", 13),
                               ("workspace_token", "changed-workspace"),
                               ("docs", {"README.md": "changed-doc"})):
            with self.subTest(field=field):
                current = dict(source, **{field: changed})
                path = self.write_state(source=source)
                with mock.patch.object(resume, "git_output", return_value=HEAD), \
                        mock.patch("release_state.source_snapshot", return_value=current):
                    with self.assertRaisesRegex(ValueError, "源码或文档"):
                        resume.load_state(path, self.root, (resume.PENDING_STAGE,), require_snapshot=True)

    def test_another_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, "不是同一次发布"):
            self.load(version=".".join(("1", "0", "0")))

    def test_another_commit_is_refused(self):
        with self.assertRaisesRegex(ValueError, "不是同一次发布"):
            self.load(head="b" * 40)

    def test_a_stage_outside_the_allowed_ones_is_refused(self):
        with self.assertRaisesRegex(ValueError, resume.PENDING_STAGE):
            self.load(stage="candidate_ready")

    def test_publish_start_is_the_transition_into_pending(self):
        state = json.loads(self.write_state().read_text(encoding="utf-8"))
        self.assertEqual(resume.publish_started_at(state), STARTED)
        with self.assertRaisesRegex(ValueError, "没有进入"):
            resume.publish_started_at({"history": []})

    def test_git_output_reads_the_real_head_of_the_given_tree_and_ignores_inherited_git_routing(self):
        repo = self.root / "repo"
        repo.mkdir()
        env = evidence.gate_stamp.git_env()
        for argv in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.test",
                                      "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", *argv], cwd=repo, env=env, check=True, capture_output=True, timeout=30)
        expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
                                  capture_output=True, text=True, timeout=30).stdout.strip()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "nowhere")}):
            self.assertEqual(resume.git_output(repo, "rev-parse", "HEAD"), expected)


class BindingContextTests(SFCase):
    def test_missing_or_changed_binding_refuses_before_compose(self):
        from release_state import reference

        for case in ("observations", "quality", "manifest", "quality_changed", "manifest_changed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="sf_resume_binding_") as temp:
                root = Path(temp)
                record = root / "quality.json"
                manifest = record.with_suffix(".manifest.json")
                record.write_text("original quality", encoding="utf-8")
                manifest.write_text("original manifest", encoding="utf-8")
                state = {"version": VERSION, "source": {"head": HEAD},
                         "history": [{"to": resume.PENDING_STAGE, "at": STARTED.isoformat()}],
                         "observations": {"publish_quality": reference(record),
                                          "publish_manifest": reference(manifest)}}
                if case == "observations":
                    del state["observations"]
                elif case in ("quality", "manifest"):
                    del state["observations"]["publish_" + case]
                elif case == "quality_changed":
                    record.write_text("changed quality", encoding="utf-8")
                else:
                    manifest.write_text("changed manifest", encoding="utf-8")
                state_path = root / "state.json"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                message = "缺少质量绑定" if case in ("observations", "quality", "manifest") else "未绑定本轮发布状态或已发生变化"
                with self.assertRaisesRegex(ValueError, message):
                    resume.binding_context(root, state_path, state)
                output = root / "combined.log"
                argv = ["compose", "--root", str(root), "--state", str(state_path),
                        "--evidence", str(root / "evidence"), "--publish-log", str(root / "publish.log"),
                        "--out", str(output)]
                with mock.patch.object(resume, "load_state", return_value=state), \
                        mock.patch.object(resume, "compose") as compose, \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(resume.main(argv), 2)
                self.assertIn(message, errors.getvalue())
                compose.assert_not_called()
                self.assertFalse(output.exists())
                self.assertFalse((root / "evidence").exists())


class GuideRenderPreflightTests(SFCase):
    def test_matching_render_is_read_only_and_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="sf_resume_guide_") as temp:
            root = Path(temp)
            (root / "docs").mkdir()
            guide = root / "docs/INSTALL.md"
            guide.write_bytes(b"frozen guide\n")
            result = SimpleNamespace(returncode=0, stdout=b"frozen guide\n\n", stderr=b"")
            with mock.patch.object(resume.subprocess, "run", return_value=result) as run:
                resume.verify_guide_render(root)
            self.assertEqual(guide.read_bytes(), b"frozen guide\n")
            self.assertEqual(run.call_args.args[0][-1], "--print")
            result.stdout = b"different guide\n\n"
            with mock.patch.object(resume.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(ValueError, "安装说明"):
                    resume.verify_guide_render(root)
            self.assertEqual(guide.read_bytes(), b"frozen guide\n")

    def test_render_failure_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="sf_resume_guide_") as temp:
            result = SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed")
            with mock.patch.object(resume.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(ValueError, "渲染预检失败"):
                    resume.verify_guide_render(Path(temp))


class RealSourceIdentityTests(SFCase):
    def test_noncanonical_frozen_ledger_rejects_published_reformatting(self):
        self._exercise_ledger_transition(noncanonical=True)

    def test_published_ledger_transition_allows_run_and_compose_but_not_tampering(self):
        self._exercise_ledger_transition(noncanonical=False)

    def _exercise_ledger_transition(self, *, noncanonical):
        import release_state
        from release_sequence import mark_published

        with tempfile.TemporaryDirectory(prefix="sf_resume_ledger_") as temp:
            root = Path(temp).resolve()
            env = evidence.gate_stamp.git_env()
            env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test")
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                               capture_output=True, text=True)
            git("init", "-q")
            for name in release_state.DOCS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            (root / "sf_constants.py").write_text(
                'APP_VERSION = "%s"\nRELEASE_SEQUENCE = 2\n' % VERSION, encoding="utf-8")
            (root / "fixture.py").write_text("value = 1\n", encoding="utf-8")
            (root / ".gitignore").write_text("build/\n*.lock\n", encoding="utf-8")
            ledger = root / "tools/release_sequence_ledger.json"
            ledger.parent.mkdir()
            original = {"schema": 1, "releases": [
                {"version": "7.8.8", "sequence": 1, "status": "published"},
                {"version": VERSION, "sequence": 2, "status": "allocated"}]}
            ledger.write_text(json.dumps(original, indent=None if noncanonical else 2) + "\n", encoding="utf-8")
            ledger.chmod(0o644)
            git("add", "--", ".")
            git("commit", "-qm", "fixture")
            state_path = root / "build/state.json"
            state = release_state.init_state(root, state_path, VERSION)
            head_patch = mock.patch.object(resume, "git_output", side_effect=lambda _root, *args:
                                          state["source"]["head"] if args == ("rev-parse", "HEAD") else "fixture")
            head_patch.start()
            self.addCleanup(head_patch.stop)
            record = root / "build/quality.json"
            record.write_text("{}", encoding="utf-8")
            record.with_suffix(".manifest.json").write_text("{}", encoding="utf-8")
            state.update(stage=resume.PENDING_STAGE,
                         history=[{"to": resume.PENDING_STAGE, "at": STARTED.isoformat()}],
                         observations={"publish_quality": release_state.reference(record),
                                       "publish_manifest": release_state.reference(record.with_suffix(".manifest.json"))})
            state_path.write_text(json.dumps(state), encoding="utf-8")
            common = ["--root", str(root), "--state", str(state_path), "--evidence", str(root / "build/evidence")]
            def offline(_runner, command, _cwd, _timeout):
                if "tools/publish_update_release.py" in command:
                    mark_published(ledger, VERSION, 2)
                if command[:2] == ["git", "rev-list"]:
                    return 0, "0\n"
                if "tools/smoke_frozen_app.py" in command:
                    return 0, resume.SMOKE_VERDICT + "\n"
                return 0, "offline\n"
            if noncanonical:
                mark_published(ledger, VERSION, 2)
                self.assertNotEqual(ledger.read_bytes(), (json.dumps(original) + "\n").encode())
                with self.assertRaisesRegex(ValueError, "源码或文档"):
                    resume.load_state(state_path, root, (resume.PENDING_STAGE,), require_snapshot=True)
                return
            with mock.patch.object(resume, "verify_guide_render"), \
                    mock.patch.object(resume, "execute", side_effect=offline), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(resume.main(["run", *common, "--apply"]), 0, errors.getvalue())
                self.assertEqual(resume.main(["run", *common, "--apply"]), 0, errors.getvalue())
            published = ledger.read_bytes()
            log = root / "build/log"
            log.write_text(PUBLISH_LOG, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(resume.main(["compose", *common, "--publish-log", str(log),
                                              "--out", str(root / "build/out")]), 0, errors.getvalue())
            self.assertIn(resume.closing_line(VERSION), (root / "build/out").read_text(encoding="utf-8"))
            # The transition check reads the committed ledger as text first; fail only the
            # subsequent binary read, after proving the same snapshot is otherwise valid.
            self.assertNotEqual(state["source"], release_state.source_snapshot(root))
            self.assertTrue(resume.evidence.published_ledger_transition(root, VERSION, 2))
            resume.verify_snapshot(root, state)
            real_run = subprocess.run
            failed_shows = []

            def fail_binary_ledger_show(argv, **kwargs):
                if (argv == ["git", "show", "HEAD:tools/release_sequence_ledger.json"]
                        and not kwargs.get("text", False)):
                    failed_shows.append(argv)
                    return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"injected failure")
                return real_run(argv, **kwargs)

            blocked_out = root / "build/blocked-out"
            with mock.patch.object(resume.subprocess, "run", side_effect=fail_binary_ledger_show), \
                    mock.patch.object(resume, "verify_guide_render") as guide, \
                    mock.patch.object(resume, "run_steps") as run_steps, \
                    mock.patch.object(resume, "compose") as compose:
                with self.assertRaisesRegex(ValueError, "源码或文档"):
                    resume.verify_snapshot(root, state)
                for command in ("run", "compose"):
                    argv = [command, *common]
                    if command == "run":
                        argv.append("--apply")
                    else:
                        argv += ["--publish-log", str(log), "--out", str(blocked_out)]
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(resume.main(argv), 2, errors.getvalue())
                    self.assertIn("源码或文档", errors.getvalue())
                guide.assert_not_called()
                run_steps.assert_not_called()
                compose.assert_not_called()
            self.assertEqual(len(failed_shows), 3)
            self.assertFalse(blocked_out.exists())
            # The legitimate working-tree ledger transition must not exempt other staged files.
            (root / "fixture.py").write_text("staged change\n", encoding="utf-8")
            git("add", "--", "fixture.py")
            (root / "fixture.py").write_text("value = 1\n", encoding="utf-8")
            for command in ("run", "compose"):
                argv = [command, *common]
                if command == "compose":
                    argv += ["--publish-log", str(log), "--out", str(root / "build/out")]
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(resume.main(argv), 2)
                    self.assertIn("索引", errors.getvalue())
            git("add", "--", "fixture.py")
            for tamper in ("other_record", "sequence", "version", "source", "format", "mode"):
                with self.subTest(tamper=tamper):
                    data = json.loads(published)
                    if tamper == "other_record":
                        data["releases"][0]["status"] = "allocated"
                    elif tamper == "sequence":
                        data["releases"][1]["sequence"] = 3
                    elif tamper == "version":
                        data["releases"][1]["version"] = "7.8.10"
                    elif tamper == "source":
                        (root / "fixture.py").write_text("changed\n", encoding="utf-8")
                    elif tamper == "mode":
                        ledger.chmod(0o600)
                    else:
                        ledger.write_bytes(published + b" ")
                    if tamper in ("other_record", "sequence", "version"):
                        ledger.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    for command in ("run", "compose"):
                        argv = [command, *common]
                        if command == "compose":
                            argv += ["--publish-log", str(root / "build/log"), "--out", str(root / "build/out")]
                        with contextlib.redirect_stderr(io.StringIO()) as errors:
                            self.assertEqual(resume.main(argv), 2, (tamper, command, errors.getvalue()))
                            self.assertIn("源码或文档", errors.getvalue())
                    ledger.write_bytes(published)
                    ledger.chmod(0o644)
                    (root / "fixture.py").write_text("value = 1\n", encoding="utf-8")
            self.assertEqual(resume.load_state(state_path, root, (resume.PENDING_STAGE,),
                                               require_snapshot=True)["version"], VERSION)

    def test_run_and_compose_reject_index_drift(self):
        import release_state

        with tempfile.TemporaryDirectory(prefix="sf_resume_index_") as temp:
            root = Path(temp).resolve()
            env = evidence.gate_stamp.git_env()
            env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test")
            def git(*args, input=None):
                return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                                      capture_output=True, text=True, input=input).stdout.strip()
            git("init", "-q")
            for name in release_state.DOCS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            (root / "sf_constants.py").write_text('APP_VERSION = "%s"\n' % VERSION, encoding="utf-8")
            target = root / "fixture.py"
            target.write_text("value = 1\n", encoding="utf-8")
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            git("add", "--", ".")
            git("commit", "-qm", "fixture")
            state_path = root / "build/state.json"
            state = release_state.init_state(root, state_path, VERSION)
            state["stage"] = resume.PENDING_STAGE
            state_path.write_text(json.dumps(state), encoding="utf-8")
            blob = git("hash-object", "-w", "--stdin", input="value = 2\n")
            original_blob = git("rev-parse", "HEAD:fixture.py")
            common = ["--root", str(root), "--state", str(state_path),
                      "--evidence", str(root / "build/evidence")]
            for drift in ("blob", "mode", "unmerged"):
                with self.subTest(drift=drift):
                    if drift == "blob":
                        git("update-index", "--cacheinfo", "100644", blob, "fixture.py")
                    elif drift == "mode":
                        git("update-index", "--cacheinfo", "100755", original_blob, "fixture.py")
                    else:
                        git("update-index", "--index-info", input=(
                            "0 " + "0" * len(original_blob) + "\tfixture.py\n"
                            "100644 " + original_blob + " 1\tfixture.py\n"
                            "100644 " + blob + " 2\tfixture.py\n"))
                    for command in ("run", "compose"):
                        argv = [command, *common]
                        if command == "compose":
                            argv += ["--publish-log", str(root / "build/log"), "--out", str(root / "build/out")]
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                            self.assertEqual(resume.main(argv), 2, (drift, command, errors.getvalue()))
                            self.assertIn("索引", errors.getvalue())
                    git("update-index", "--cacheinfo", "100644", original_blob, "fixture.py")

    def test_run_rejects_dirty_source_docs_and_new_build_inputs(self):
        import release_state

        with tempfile.TemporaryDirectory(prefix="sf_resume_identity_") as temp:
            root = Path(temp).resolve()
            env = evidence.gate_stamp.git_env()
            env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test")
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                                      capture_output=True, text=True)
            git("init", "-q")
            for name in release_state.DOCS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            (root / "sf_constants.py").write_text('APP_VERSION = "%s"\n' % VERSION, encoding="utf-8")
            (root / "fixture.py").write_text("value = 1\n", encoding="utf-8")
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            git("add", "--", ".")
            git("commit", "-qm", "fixture")
            state_path = root / "build" / "state.json"
            release_state.init_state(root, state_path, VERSION)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["stage"] = resume.PENDING_STAGE
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(resume.load_state(state_path, root, (resume.PENDING_STAGE,),
                                               require_snapshot=True)["version"], VERSION)
            for name in ("fixture.py", "README.md", "new_input.py"):
                path = root / name
                original = path.read_bytes() if path.exists() else None
                with self.subTest(path=name):
                    path.write_text("changed\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "源码或文档"):
                        resume.load_state(state_path, root, (resume.PENDING_STAGE,), require_snapshot=True)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)


class TimeoutTests(SFCase):
    def test_default_override_and_bad_values(self):
        with mock.patch.dict(os.environ, {resume.STEP_TIMEOUT_ENV: ""}):
            self.assertEqual(resume.step_timeout(), resume.STEP_TIMEOUT_DEFAULT)
        with mock.patch.dict(os.environ, {resume.STEP_TIMEOUT_ENV: "4321"}):
            self.assertEqual(resume.step_timeout(), 4321.0)
        for junk in ("abc", "5", "-1"):
            with self.subTest(value=junk), mock.patch.dict(os.environ, {resume.STEP_TIMEOUT_ENV: junk}):
                with self.assertRaises(ValueError):
                    resume.step_timeout()

    def test_publish_budget_default_override_and_bad_values(self):
        with mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: ""}):
            self.assertEqual(resume.publish_step_timeout(), resume.PUBLISH_TIMEOUT_DEFAULT)
        with mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: "7200"}):
            self.assertEqual(resume.publish_step_timeout(), 7200.0)
        for junk in ("abc", "899", "-1"):
            with self.subTest(value=junk), mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: junk}):
                with self.assertRaises(ValueError):
                    resume.publish_step_timeout()


class MainTests(_TempCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(resume, "git_output", side_effect=self.fake_git):
            code = resume.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def fake_git(root, *args):
        return "feat/x" if "--abbrev-ref" in args else HEAD

    def common(self, command, **state):
        from release_state import reference
        state_path = self.write_state(**state)
        record = self.root / "bound-quality.json"
        manifest = record.with_suffix(".manifest.json")
        record.write_text("{}", encoding="utf-8")
        manifest.write_text("{}", encoding="utf-8")
        value = json.loads(state_path.read_text(encoding="utf-8"))
        value["observations"] = {"publish_quality": reference(record),
                                 "publish_manifest": reference(manifest)}
        state_path.write_text(json.dumps(value), encoding="utf-8")
        return [command, "--root", str(self.root), "--evidence", str(self.evidence),
                "--state", str(state_path)]

    def bind_results(self, argv):
        state_path = Path(argv[argv.index("--state") + 1])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        binding = resume.binding_context(self.root, state_path, state)
        steps = resume.plan(str(self.root / ".venv/bin/python3"), VERSION, "feat/x", APP,
                            Path(binding["quality"]["path"]))
        for name, _, command in steps:
            if resume.step_rc(self.evidence, name) is not None:
                data = resume.binding_data(self.evidence, name, command, binding)
                (self.evidence / ("rest-%s.binding.json" % name)).write_text(json.dumps(data), encoding="utf-8")

    def test_run_refuses_a_version_that_is_not_published_and_pending(self):
        code, _, err = self.run_main(*self.common("run", stage="complete"))
        self.assertEqual(code, 2)
        self.assertIn(resume.PENDING_STAGE, err)

    def test_run_refuses_a_state_that_belongs_to_another_tree_before_planning_anything(self):
        for state in (dict(version=".".join(("1", "0", "0"))), dict(head="b" * 40)):
            with self.subTest(state=sorted(state)):
                code, out, err = self.run_main(*self.common("run", **state), "--apply")
                self.assertEqual(code, 2)
                self.assertIn("不是同一次发布", err)
                self.assertNotIn("执行", out)
                self.assertFalse(self.evidence.exists())

    def test_run_with_a_quality_record_previews_the_publish_step_first(self):
        record = self.root / "quality.json"
        record.write_text("{}", encoding="utf-8")
        code, out, _ = self.run_main(*self.common("run"), "--quality-record", str(record))
        self.assertEqual(code, 0)
        self.assertEqual(out.count("预演"), len(self.steps) + 1)
        self.assertLess(out.index("publish_update_release.py"), out.index("deploy-pages"))
        self.assertIn("SF_QUALITY_RECORD=" + str(record.resolve()), out)
        self.assertFalse(self.evidence.exists())

    def test_run_rejects_guide_render_drift_before_any_release_step(self):
        with mock.patch.object(resume, "verify_guide_render", side_effect=ValueError("安装说明与冻结内容不一致")), \
                mock.patch.object(resume, "run_steps") as steps:
            code, _, err = self.run_main(*self.common("run"), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("安装说明", err)
        steps.assert_not_called()

    def test_run_rejects_source_drift_before_any_step(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                argv = self.common("run") + (["--apply"] if apply else [])
                with mock.patch("release_state.source_snapshot", return_value={"head": HEAD, "changed": True}), \
                        mock.patch.object(resume, "run_steps") as steps:
                    code, _, err = self.run_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn("源码或文档", err)
                steps.assert_not_called()

    def test_main_source_guard_rechecks_snapshot_before_each_step(self):
        argv = self.common("run") + ["--apply"]
        with mock.patch.object(resume, "run_steps", return_value=0) as steps:
            code, _, _ = self.run_main(*argv)
        self.assertEqual(code, 0)
        guard = steps.call_args.kwargs["verify_source"]
        guard()
        with mock.patch("release_state.source_snapshot", return_value={"head": HEAD, "changed": True}):
            with self.assertRaisesRegex(ValueError, "源码或文档"):
                guard()

    def test_quality_binding_is_rechecked_between_actual_steps(self):
        state_args = self.common("run") + ["--apply"]
        record = self.root / "bound-quality.json"
        calls = []
        def run_steps(root, directory, steps, branch, *, verify_source, **kwargs):
            verify_source()
            calls.append("first")
            record.write_text('{"changed":true}', encoding="utf-8")
            verify_source()
            calls.append("second")
            return 0
        with mock.patch.object(resume, "run_steps", side_effect=run_steps):
            code, _, err = self.run_main(*state_args)
        self.assertEqual(code, 2)
        self.assertIn("质量", err)
        self.assertEqual(calls, ["first"])

    def test_apply_rejects_unbound_quality_record_before_any_release_step(self):
        record = self.root / "quality.json"
        record.write_text("{}", encoding="utf-8")
        with mock.patch.object(resume, "run_steps") as steps:
            code, _, err = self.run_main(*self.common("run"), "--quality-record", str(record), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("质量", err)
        steps.assert_not_called()

    def test_apply_accepts_exact_bound_record_and_rejects_changed_manifest(self):
        from release_state import reference
        record = self.root / "quality.json"
        manifest = record.with_suffix(".manifest.json")
        record.write_text("{}", encoding="utf-8")
        manifest.write_text("{}", encoding="utf-8")
        state_path = self.write_state()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["observations"] = {"publish_quality": reference(record),
                                 "publish_manifest": reference(manifest)}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        argv = ["run", "--root", str(self.root), "--evidence", str(self.evidence),
                "--state", str(state_path), "--quality-record", str(record), "--apply"]
        with mock.patch.object(resume, "run_steps", return_value=0) as steps:
            code, _, _ = self.run_main(*argv)
            self.assertEqual(code, 0)
            steps.assert_called_once()
            steps.reset_mock()
            manifest.write_text('{"changed":true}', encoding="utf-8")
            code, _, _ = self.run_main(*argv)
            self.assertEqual(code, 2)
            steps.assert_not_called()

    def test_apply_without_quality_argument_still_validates_bound_record(self):
        from release_state import reference
        record = self.root / "quality.json"
        manifest = record.with_suffix(".manifest.json")
        record.write_text("{}", encoding="utf-8")
        manifest.write_text("{}", encoding="utf-8")
        state_path = self.write_state()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["observations"] = {"publish_quality": reference(record),
                                 "publish_manifest": reference(manifest)}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        manifest.write_text('{"changed":true}', encoding="utf-8")
        argv = ["run", "--root", str(self.root), "--evidence", str(self.evidence),
                "--state", str(state_path), "--apply"]
        with mock.patch.object(resume, "run_steps") as steps:
            code, _, _ = self.run_main(*argv)
        self.assertEqual(code, 2)
        steps.assert_not_called()

    def test_apply_rejects_nonobject_quality_bindings(self):
        state_path = self.write_state()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["observations"] = []
        state_path.write_text(json.dumps(state), encoding="utf-8")
        argv = ["run", "--root", str(self.root), "--evidence", str(self.evidence),
                "--state", str(state_path), "--apply"]
        with mock.patch.object(resume, "run_steps") as steps:
            code, _, err = self.run_main(*argv)
        self.assertEqual(code, 2)
        self.assertIn("质量绑定", err)
        steps.assert_not_called()

    def test_apply_without_explicit_record_includes_bound_ota_step(self):
        with mock.patch.object(resume, "run_steps", return_value=0) as steps:
            code, _, _ = self.run_main(*self.common("run"), "--apply")
        self.assertEqual(code, 0)
        plan = steps.call_args.args[2]
        self.assertIn(resume.PUBLISH_STEP, [item[0] for item in plan])

    def test_run_refuses_a_quality_record_that_is_not_a_file(self):
        code, _, err = self.run_main(*self.common("run"), "--quality-record", str(self.root / "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("--quality-record", err)

    def test_run_hands_the_redo_names_and_the_configured_timeout_to_the_step_runner(self):
        with mock.patch.object(resume, "run_steps", return_value=0) as run_steps, \
                mock.patch.dict(os.environ, {resume.STEP_TIMEOUT_ENV: "4321"}):
            code, _, _ = self.run_main(*self.common("run"), "--apply", "--redo", "8-final-smoke",
                                       "--redo", "7-push")
        self.assertEqual(code, 0)
        kwargs = run_steps.call_args.kwargs
        self.assertEqual(kwargs["redo"], ["8-final-smoke", "7-push"])
        self.assertEqual(kwargs["timeout"], 4321.0)
        self.assertTrue(kwargs["apply"])

    def test_run_refuses_a_misspelled_redo_name_with_exit_two_and_runs_nothing(self):
        self.write_results()
        runner = FakeRunner()
        with mock.patch.object(resume, "run_group", runner):
            code, out, err = self.run_main(*self.common("run"), "--apply", "--redo", "8-final-smok")
        self.assertEqual(code, 2)
        self.assertIn("8-final-smok", err)
        self.assertNotIn("执行", out)

    def test_every_probe_and_network_switch_is_scrubbed_from_the_step_environment(self):
        # 名单在这里再写一遍，而不是引用 SCRUBBED_ENV：引用自己等于没测，删掉一项也不会红。
        must_scrub = ("SF_GUARD_PROBE", "SF_LOCK_PROBE", "SF_LOCK_PROBE_DIR", "SF_PUBLIC_REGRESSION",
                      "SF_SKIP_COVERAGE", "SF_RANDOM_ORDER", "SF_LANES", "SF_REJECT_NETWORK",
                      "SF_TEST_REJECT_NETWORK")
        with mock.patch.dict(os.environ, {name: "1" for name in must_scrub}):
            env = resume.clean_env()
        self.assertEqual([name for name in must_scrub if name in env], [])

    def test_run_refuses_a_bad_timeout_setting_instead_of_falling_back(self):
        with mock.patch.dict(os.environ, {resume.STEP_TIMEOUT_ENV: "abc"}):
            code, _, err = self.run_main(*self.common("run"))
        self.assertEqual(code, 2)
        self.assertIn(resume.STEP_TIMEOUT_ENV, err)

    def test_run_refuses_a_bad_publish_budget_before_any_step_starts(self):
        with mock.patch.dict(os.environ, {resume.PUBLISH_TIMEOUT_ENV: "abc"}), \
                mock.patch.object(resume, "run_steps") as run_steps:
            code, _, err = self.run_main(*self.common("run"), "--apply")
        self.assertEqual(code, 2)
        self.assertIn(resume.PUBLISH_TIMEOUT_ENV, err)
        run_steps.assert_not_called()

    def test_run_hands_the_publish_start_time_to_the_step_runner(self):
        with mock.patch.object(resume, "run_steps", return_value=0) as run_steps:
            code, _, _ = self.run_main(*self.common("run"), "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(run_steps.call_args.kwargs["since"], STARTED)

    def test_a_termination_signal_during_a_step_exits_with_128_plus_the_signal_number(self):
        with mock.patch.object(resume, "run_steps", side_effect=resume.ProcessGroupInterrupted(signal.SIGTERM)):
            code, _, err = self.run_main(*self.common("run"), "--apply")
        self.assertEqual(code, 128 + int(signal.SIGTERM))
        self.assertIn("SIGTERM", err)
        self.assertIn("重跑", err)

    def test_run_without_apply_only_previews_and_creates_nothing(self):
        code, out, _ = self.run_main(*self.common("run"))
        self.assertEqual(code, 0)
        self.assertEqual(out.count("预演"), len(self.steps))
        self.assertIn("HEAD:refs/heads/feat/x", out)
        self.assertFalse(self.evidence.exists())

    def test_compose_reports_what_is_missing_and_exits_non_zero(self):
        source = self.write_publish_log()
        code, _, err = self.run_main(*self.common("compose"), "--publish-log", str(source),
                                     "--out", str(self.root / "o.log"))
        self.assertEqual(code, 1)
        self.assertIn("没有记录", err)

    def test_run_does_not_skip_unbound_fresh_successes(self):
        self.write_results()
        runner = FakeRunner()
        with mock.patch.object(resume, "execute", side_effect=lambda runner, argv, cwd, timeout:
                               (0, "offline result\n")) as execute:
            code, _, _ = self.run_main(*self.common("run"), "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(any("tools/public_release.py" in call.args[1] for call in execute.call_args_list),
                        "无绑定的旧成功不能作为本次发布的成功")

    def test_actual_run_bindings_allow_resume_and_reject_tampering(self):
        argv = self.common("run") + ["--apply"]
        def offline(_runner, command, _cwd, _timeout):
            if command[:2] == ["git", "rev-list"]:
                return 0, "0\n"
            if "tools/smoke_frozen_app.py" in command:
                return 0, resume.SMOKE_VERDICT + "\n"
            return 0, "offline\n"
        with mock.patch.object(resume, "execute", side_effect=offline) as execute:
            self.assertEqual(self.run_main(*argv)[0], 0)
            execute.reset_mock()
            self.assertEqual(self.run_main(*argv)[0], 0)
            self.assertEqual(execute.call_count, 0)
            source = self.write_publish_log()
            compose_args = ["compose", *argv[1:-1], "--publish-log", str(source),
                            "--out", str(self.root / "bound.log")]
            self.assertEqual(self.run_main(*compose_args)[0], 0)
            log = self.evidence / "rest-6.0-public-release.log"
            log.write_text("changed\n", encoding="utf-8")
            compose_args[-1] = str(self.root / "tampered.log")
            self.assertNotEqual(self.run_main(*compose_args)[0], 0)
            self.assertEqual(self.run_main(*argv)[0], 0)
            self.assertTrue(any("tools/public_release.py" in call.args[1] for call in execute.call_args_list))

    def test_compose_rejects_unbound_fresh_step_evidence(self):
        self.write_results()
        source = self.write_publish_log()
        target = self.root / "unbound.log"
        code, _, _ = self.run_main(*self.common("compose"), "--publish-log", str(source),
                                   "--out", str(target))
        self.assertNotEqual(code, 0)
        self.assertFalse(target.exists())

    def test_compose_rejects_source_drift(self):
        self.write_results()
        source = self.write_publish_log()
        target = self.root / "drift.log"
        argv = self.common("compose") + ["--publish-log", str(source), "--out", str(target)]
        self.bind_results(argv)
        with mock.patch("release_state.source_snapshot", return_value={"head": HEAD, "changed": True}):
            code, _, _ = self.run_main(*argv)
        self.assertNotEqual(code, 0)
        self.assertFalse(target.exists())

    def test_compose_accepts_the_state_after_acceptance_too(self):
        self.write_results()
        source = self.write_publish_log()
        argv = self.common("compose", stage=resume.COMPLETE_STAGE)
        self.bind_results(argv)
        code, _, _ = self.run_main(*argv, "--publish-log", str(source), "--out", str(self.root / "late.log"))
        self.assertEqual(code, 0)

    def test_compose_success_prints_the_output_path(self):
        self.write_results()
        source = self.write_publish_log()
        target = self.root / "o.log"
        argv = self.common("compose")
        self.bind_results(argv)
        arguments = (*argv, "--publish-log", str(source), "--out", str(target))
        code, out, _ = self.run_main(*arguments)
        self.assertEqual(code, 0)
        self.assertIn(str(target), out)
        code, _, err = self.run_main(*arguments)
        self.assertEqual(code, 2)
        self.assertIn("不覆盖", err)

    def test_script_entry_point_returns_the_exit_code(self):
        # 状态文件不存在：入口把它当作参数问题返回 2（重点是 __main__ 那一行真的把返回码交给了进程）。
        argv = ["release_resume.py", "compose", "--root", str(self.root), "--evidence", str(self.evidence),
                "--state", str(self.root / "missing.json"), "--publish-log", str(self.write_publish_log()),
                "--out", str(self.root / "o.log")]
        with mock.patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            runpy.run_path(str(REPO / "tools" / "release_resume.py"), run_name="__main__")
        self.assertEqual(stop.exception.code, 2)
