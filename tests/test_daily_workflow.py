"""Behavioral checks for read-only daily test navigation."""
import contextlib
import io
import runpy
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._base import SFCase
from tools import daily_workflow as workflow


class TestDailyWorkflow(SFCase):
    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = workflow.main(list(args))
        return result, output.getvalue()

    def test_repository_targets_exist_and_contain_tests(self):
        self.assertEqual(workflow.check_navigation(workflow.ROOT), [])
        result, output = self.invoke("--check")
        self.assertEqual(result, 0)
        self.assertIn("no tests executed", output)

    def test_list_displays_every_topic_without_checking_targets(self):
        with mock.patch.object(workflow, "check_navigation") as check:
            result, output = self.invoke("--list")
        self.assertEqual(result, 0)
        for topic in workflow.TOPICS:
            self.assertIn(topic + ":", output)
        check.assert_not_called()

    def test_topic_union_keeps_order_and_removes_duplicates(self):
        topics = ["data", "storage", "data"]
        expected = list(dict.fromkeys((*workflow.TOPICS["data"][1],
                                       *workflow.TOPICS["storage"][1])))
        self.assertEqual(workflow.modules_for(topics), expected)
        result, output = self.invoke("--topic", "data", "--topic", "storage")
        self.assertEqual(result, 0)
        tokens = shlex.split(output.splitlines()[-1])
        self.assertEqual(tokens[:3], ["env", "QT_QPA_PLATFORM=offscreen", "SF_RERUN_FAILED=0"])
        self.assertEqual(tokens[3:5], [str(workflow.ROOT / ".venv/bin/python"),
                                      str(workflow.ROOT / "tools/test_runner.py")])
        self.assertEqual(tokens[5::2], ["--module"] * len(expected))
        self.assertEqual(tokens[6::2], expected)
        self.assertIn("not the full gate", output.lower())

    def test_missing_syntax_empty_and_non_utf8_targets_are_rejected(self):
        cases = {"missing": None, "syntax": "def broken(:\n",
                 "empty": "value = 1\n", "encoding": b"\xff"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in cases.items():
                with self.subTest(name=name):
                    path = root / "tests" / ("test_" + name + ".py")
                    if source is not None:
                        path.parent.mkdir(exist_ok=True)
                        path.write_bytes(source if isinstance(source, bytes)
                                         else source.encode("utf-8"))
                    topics = {"sample": ("sample", ("tests.test_" + name,))}
                    with mock.patch.object(workflow, "TOPICS", topics):
                        self.assertTrue(workflow.check_navigation(root))
                        with mock.patch.object(workflow, "ROOT", root):
                            result, output = self.invoke("--topic", "sample")
                    self.assertEqual(result, 1)
                    self.assertIn("Navigation error", output)
                    self.assertNotIn("--module", output)

    def test_check_parses_but_never_imports_or_executes_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tests" / "test_sample.py"
            path.parent.mkdir()
            path.write_text("raise RuntimeError('must not execute')\n"
                            "class Sample(unittest.TestCase):\n"
                            "    def test_sample(self): pass\n", encoding="utf-8")
            topics = {"sample": ("sample", ("tests.test_sample",))}
            with mock.patch.object(workflow, "TOPICS", topics):
                self.assertEqual(workflow.check_navigation(root), [])
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_empty_malformed_duplicate_and_unsafe_navigation_are_rejected(self):
        cases = [{}, {"sample": ("description", ())}, {"sample": ("", ("tests.test_x",))},
                 {"sample": ("description", "tests.test_x")}, {"sample": ("description",)},
                 {"sample": ("description", ("tests.test_x", "tests.test_x"))},
                 {"sample": ("description", ("tests../outside",))},
                 {"sample": ("description", ("tests.test_x;exit",))}]
        for topics in cases:
            with self.subTest(topics=topics), mock.patch.object(workflow, "TOPICS", topics):
                self.assertTrue(workflow.check_navigation(workflow.ROOT))
                for action in ("--list", "--check"):
                    result, output = self.invoke(action)
                    self.assertEqual(result, 1)
                    self.assertIn("Navigation error", output)

    def test_plain_nested_and_async_functions_are_not_unittest_entries(self):
        sources = ["def test_sample(): pass\n",
                   "def helper():\n    def test_nested(): pass\n",
                   "class Sample(unittest.TestCase):\n    async def test_async(self): pass\n",
                   "class Sample:\n    def test_sample(self): pass\n"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tests/test_sample.py"
            path.parent.mkdir()
            topics = {"sample": ("description", ("tests.test_sample",))}
            with mock.patch.object(workflow, "TOPICS", topics):
                for source in sources:
                    with self.subTest(source=source):
                        path.write_text(source, encoding="utf-8")
                        self.assertTrue(workflow.check_navigation(root))

    def test_unrelated_broken_topic_does_not_block_selected_topic(self):
        topics = {"valid": workflow.TOPICS["storage"],
                  "broken": ("description", ("tests.test_missing_navigation_fixture",))}
        with mock.patch.object(workflow, "TOPICS", topics):
            result, output = self.invoke("--topic", "valid")
            self.assertEqual(result, 0, output)
            result, output = self.invoke("--check")
            self.assertEqual(result, 1)
            self.assertIn("test_missing_navigation_fixture", output)

    def test_symlink_outside_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            (root / "tests").mkdir(parents=True)
            outside = base / "outside.py"
            outside.write_text("class Sample(unittest.TestCase):\n"
                               "    def test_sample(self): pass\n", encoding="utf-8")
            (root / "tests/test_sample.py").symlink_to(outside)
            topics = {"sample": ("description", ("tests.test_sample",))}
            with mock.patch.object(workflow, "TOPICS", topics):
                errors = workflow.check_navigation(root)
            self.assertIn("outside repository", errors[0])

    def test_absolute_command_preserves_repository_paths_with_spaces(self):
        root = Path(tempfile.gettempdir()) / "repo with spaces"
        tokens = shlex.split(workflow.test_command(["tests.test_daily_workflow"], root))
        self.assertEqual(tokens[3], str(root.resolve() / ".venv/bin/python"))
        self.assertEqual(tokens[4], str(root.resolve() / "tools/test_runner.py"))

    def test_no_action_unknown_topic_and_conflicting_actions_fail(self):
        for args in ([], ["--topic", "unknown"], ["--check", "--list"],
                     ["--topic", "data", "--check"], ["--execute"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    workflow.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_command_quotes_shell_metacharacters(self):
        module = "tests.example; touch must_not_exist"
        tokens = shlex.split(workflow.test_command([module]))
        self.assertEqual(tokens[-1], module)

    def test_empty_command_cannot_expand_to_full_discovery(self):
        with self.assertRaises(ValueError):
            workflow.test_command([])

    def test_invalid_and_unknown_topic_identifiers_are_rejected(self):
        for topics in ([None], [17], ["data;exit"], ["unknown_topic"]):
            with self.subTest(topics=topics), self.assertRaises(ValueError):
                workflow.modules_for(topics)

    def test_direct_cli_entry_checks_navigation_without_running_tests(self):
        with mock.patch.object(sys, "argv", [str(workflow.__file__), "--check"]), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                self.assertRaises(SystemExit) as result:
            runpy.run_path(str(workflow.__file__), run_name="__main__")
        self.assertEqual(result.exception.code, 0)
        self.assertIn("Navigation valid", output.getvalue())


if __name__ == "__main__":
    unittest.main()
