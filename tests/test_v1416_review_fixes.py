# -*- coding: utf-8 -*-
"""v1.4.16 走查修复的行为回归（补"改了却没被测到"的四行）。

背景：v1.4.10~1.4.15 那轮功耗/帧率改动的代码走查里，有四处"最好修"级别的
加固改动（异常兜底、失败留痕、文档与实现对齐、测试注入时钟）。改动本身没有
新增功能，因此**极易出现"代码合了、那几行却永远是未覆盖"** —— 未覆盖的兜底
分支等于没写：谁也不能保证它在真实异常下真的兜得住。

本文件守的是行为，不是书写形态（遵守 AGENTS.md §6）：
  1. `sf_system_alert._AlertTarget.buttonClicked_`：回调抛异常 / sender 缺方法
     时，异常**不得冲穿 AppKit 事件循环**；正常点击仍要原样转交按钮标题。
  2. `stock_float_window.FloatWindow._dispose_update_dialog`：弹窗 close() 失败
     时**不得抛出**，且必须留一条 WARNING 日志（否则退出期问题无迹可查）。
     这里必须直接调该私有方法：它的全部职责就是"收口 + 兜底 + 留痕"，
     没有任何公开路径能在不真弹系统窗口的情况下把 close() 弄失败。
  3. `tools/verify_bundle_consts.py`、`tools/publish_update_release.py`：
     发版闸门与对外发布脚本的**拒绝路径**必须真的拒绝（SystemExit），
     不能只在真跑一次发布时才第一次发现。
"""

import contextlib
import base64
import io
import json
import marshal
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:                                                   # 与存量用例同口径：两种发现方式都要能用
    from _base import SFCase
except ImportError:                                    # pragma: no cover - 取决于启动方式
    try:
        from tests._base import SFCase
    except ImportError:                                # pragma: no cover
        SFCase = unittest.TestCase


def _http_error(code, reason, body=b"{}"):
    """带 fp 的 HTTPError：不留 ResourceWarning（3.14 会对未关闭的响应报）。"""
    import urllib.error
    return urllib.error.HTTPError("https://api.github.com/x", code, reason,
                                  {}, io.BytesIO(body))


def _load_tool(name):
    """按文件加载 tools/ 下的脚本（它们不是包内模块，只能这样取）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. NSAlert 按钮回调的异常兜底
# --------------------------------------------------------------------------

class _Owner:
    """最小宿主：记录转交进来的按钮标题，boom=True 时模拟回调内部炸掉。"""

    def __init__(self, boom=False):
        self.got = []
        self._boom = boom

    def _handle_click(self, title):
        if self._boom:
            raise RuntimeError("handler blew up")
        self.got.append(title)


class _Sender:
    def __init__(self, title="立即更新"):
        self._title = title

    def title(self):
        return self._title


class TestAlertTargetClickGuard(SFCase):
    """buttonClicked_ 由 AppKit 事件循环直接调用：异常冲出去会打断事件循环。"""

    @classmethod
    def setUpClass(cls):
        import sf_system_alert
        cls.mod = sf_system_alert

    def setUp(self):
        if self.mod._AlertTarget is None:
            raise unittest.SkipTest("本机无 AppKit 桥接，NSAlert 路径不存在")
        super().setUp()

    def _target(self, owner):
        t = self.mod._AlertTarget.alloc().init()
        if owner is not None:
            t.setOwner_(owner)
        return t

    def test_normal_click_still_forwards_button_title(self):
        owner = _Owner()
        self._target(owner).buttonClicked_(_Sender("立即更新"))
        self.assertEqual(owner.got, ["立即更新"],
                         "加了兜底后正常点击仍须原样转交标题")

    def test_handler_exception_does_not_escape(self):
        owner = _Owner(boom=True)
        with self.assertRaises(RuntimeError):
            owner._handle_click("立即更新")           # 先证明异常是真的
        try:
            self._target(owner).buttonClicked_(_Sender())
        except BaseException as exc:                   # noqa: BLE001
            self.fail(f"buttonClicked_ 把异常冲穿到事件循环: {exc!r}")

    def test_sender_without_title_does_not_escape(self):
        class _NoTitle:
            pass
        try:
            self._target(_Owner()).buttonClicked_(_NoTitle())
        except BaseException as exc:                   # noqa: BLE001
            self.fail(f"sender 缺 title() 时不得抛出: {exc!r}")

    def test_missing_owner_is_silent(self):
        try:
            self._target(None).buttonClicked_(_Sender())
        except BaseException as exc:                   # noqa: BLE001
            self.fail(f"未绑定 owner 时不得抛出: {exc!r}")


# --------------------------------------------------------------------------
# 2. 更新弹窗收口失败要留痕、不抛出
# --------------------------------------------------------------------------

class _Dialog:
    def __init__(self, exc=None):
        self.closed = 0
        self._exc = exc

    def close(self):
        self.closed += 1
        if self._exc is not None:
            raise self._exc


class _Host:
    """只带 _dispose_update_dialog 需要的三个槽位，避免构造真实浮窗。"""

    def __init__(self):
        self._update_message_dialog = None
        self._update_prompt_dialog = None
        self._update_check_dialog = None


class TestDisposeUpdateDialog(SFCase):
    """私有方法是最小可测单元：它的职责就是收口 + 兜底 + 留痕（见模块文档）。"""

    @classmethod
    def setUpClass(cls):
        import stock_float_window
        cls.mod = stock_float_window

    def _dispose(self, dialog):
        host = _Host()
        host._update_message_dialog = dialog
        logs = []
        with patch.object(self.mod, "log_line",
                          side_effect=lambda msg, level="INFO": logs.append((level, msg))):
            self.mod.FloatWindow._dispose_update_dialog(host, dialog)
        return host, logs

    def test_successful_close_logs_nothing(self):
        dlg = _Dialog()
        host, logs = self._dispose(dlg)
        self.assertEqual(dlg.closed, 1, "必须真的调用一次收口")
        self.assertEqual(logs, [], "正常收口不该产生告警噪声")
        self.assertIsNone(host._update_message_dialog, "槽位必须清掉，避免重复析构")

    def test_close_failure_is_swallowed_and_logged(self):
        boom = RuntimeError("dialog already gone")
        dlg = _Dialog(exc=boom)
        host, logs = self._dispose(dlg)
        self.assertEqual(dlg.closed, 1, "只试一次，不重试")
        self.assertEqual([lv for lv, _ in logs], ["WARNING"],
                         "close() 失败必须留一条 WARNING，否则退出期问题查不到")
        self.assertIn(repr(boom), logs[0][1], "日志里要能看到原始异常")
        self.assertIsNone(host._update_message_dialog,
                          "即便 close 失败也要先把槽位清掉")

    def test_none_dialog_returns_early(self):
        _, logs = self._dispose(None)
        self.assertEqual(logs, [])


# --------------------------------------------------------------------------
# 3. 产物常量核验工具
# --------------------------------------------------------------------------

class TestVerifyBundleConsts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool("verify_bundle_consts")

    def test_missing_bundle_executable_is_a_hard_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = pathlib.Path(tmp) / "X.app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            with (app / "Contents" / "Info.plist").open("wb") as f:
                plistlib.dump({"CFBundleName": "X"}, f)     # 没有 CFBundleExecutable
            with self.assertRaises(SystemExit):
                self.tool.bundle_executable(app)

    def test_unreadable_plist_is_a_hard_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = pathlib.Path(tmp) / "X.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_bytes(b"not-a-plist")
            with self.assertRaises(SystemExit):
                self.tool.bundle_executable(app)

    def test_distinguishes_literal_derived_and_absent_consts(self):
        # A=7 是这条用例的重点：Python 3.14 把小整数编成 LOAD_SMALL_INT，
        # 老版核验器只认 LOAD_CONST，会把 `GAP = 24` 这类**硬编码**误报成
        # "推导值（未被硬编码）"（QC_CHECKLIST 里挂着的历史遗留项）。
        code = compile("A = 7\nB = 'txt'\nC = A + 1\nD = None\n", "<consts>", "exec")
        pairs = self.tool.top_level_consts(code)
        self.assertEqual(pairs["A"]["value"], 7, "小整数字面量必须读出真值")
        self.assertEqual(pairs["B"]["value"], "txt")
        self.assertIs(pairs["C"]["value"], self.tool.NOT_LITERAL,
                      "表达式赋值不得读出值，要报「不是字面量」")
        self.assertIn("A", pairs["C"]["expr_names"], "要列出参与推导的名字")
        self.assertIsNone(pairs["D"]["value"], "字面量 None 不能被当成「非字面量」")
        self.assertNotIn("__doc__", pairs, "未 STORE_NAME 的不该出现在表里")

    def test_load_module_accepts_both_pyinstaller_shapes(self):
        code = compile("X = 1", "<m>", "exec")
        as_bytes = marshal.dumps(code)

        class _Z:
            def __init__(self, payload):
                self._payload = payload

            def extract(self, _name):
                return self._payload

        for payload in (code, as_bytes, (code, 1)):
            got = self.tool.load_module(_Z(payload), "m")
            self.assertIsInstance(got, type(code), f"解包形态 {type(payload)} 未归一")

    def test_usage_text_when_arguments_are_short(self):
        with patch.object(sys, "argv", ["verify_bundle_consts.py"]):
            with self.assertRaises(SystemExit):
                self.tool.main()

    def test_report_lines_for_all_three_outcomes(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "exe"
            exe.write_bytes(b"x")
            with patch.object(sys, "argv",
                              ["verify_bundle_consts.py", "X.app", "m",
                               "LIT", "DER", "GONE"]):
                with patch.object(self.tool, "bundle_executable", return_value=exe), \
                     patch.object(self.tool, "extract_pyz",
                                  return_value=pathlib.Path(tmp) / "PYZ.pyz"), \
                     patch.object(self.tool, "ZlibArchiveReader"), \
                     patch.object(self.tool, "load_module", return_value=compile(
                         "LIT = 42\nDER = LIT + 1\n", "<m>", "exec")), \
                     contextlib.redirect_stdout(out):
                    self.tool.main()
        text = out.getvalue()
        self.assertIn("42", text)
        self.assertIn("推导值", text)
        self.assertIn("<未找到该常量>", text)
        self.assertIn("GONE", text)


# --------------------------------------------------------------------------
# 4. 对外发布脚本的拒绝路径
# --------------------------------------------------------------------------

class TestPublishToolGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool("publish_update_release")

    # ---- 凭据 ----
    def test_missing_credential_is_a_hard_error(self):
        for stdout, why in (("", "returncode 非 0"), ("password=\n", "无 password")):
            done = subprocess.CompletedProcess([], returncode=0 if stdout else 1,
                                               stdout=stdout, stderr="")
            with patch.object(self.tool.subprocess, "run", return_value=done):
                with self.assertRaises(SystemExit, msg=why):
                    self.tool.github_token()

    def test_credential_is_read_from_git_helper(self):
        done = subprocess.CompletedProcess([], returncode=0,
                                           stdout="protocol=https\npassword=t0ken\n\n",
                                           stderr="")
        with patch.object(self.tool.subprocess, "run", return_value=done):
            self.assertEqual(self.tool.github_token(), "t0ken")

    # ---- ZIP 验证 ----
    def _release(self, assets):
        return {"id": 1, "assets": assets}

    def test_verified_asset_needs_both_name_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            arc = pathlib.Path(tmp) / "a.zip"
            arc.write_bytes(b"12345")
            hit = {"name": "a.zip", "size": 5}
            self.assertIs(self.tool.verified_asset(self._release([hit]), arc), hit)
            for wrong in ({"name": "a.zip", "size": 4}, {"name": "b.zip", "size": 5}):
                with self.assertRaises(SystemExit, msg=str(wrong)):
                    self.tool.verified_asset(self._release([wrong]), arc)

    def test_duplicate_asset_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            arc = pathlib.Path(tmp) / "a.zip"
            arc.write_bytes(b"x")
            calls = []
            with patch.object(self.tool, "request", side_effect=calls.append):
                with self.assertRaises(SystemExit):
                    self.tool.upload_archive("t", self._release([{"name": "a.zip"}]), arc, b"x")
            self.assertEqual(calls, [], "同名版本 ZIP 存在时不得发出任何上传请求")

    def test_upload_then_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            arc = pathlib.Path(tmp) / "a.zip"
            arc.write_bytes(b"zip")
            urls = []
            with patch.object(self.tool, "request",
                              side_effect=lambda url, *a, **k: urls.append((url, k.get("method")))), \
                 patch.object(self.tool, "load_release",
                              return_value=self._release([{"name": "a.zip", "size": 3}])), \
                 patch.object(self.tool, "download_asset", return_value=b"zip"):
                got = self.tool.upload_archive("t", self._release([]), arc, b"zip")
            self.assertEqual(got["name"], "a.zip")
            self.assertEqual([m for _, m in urls], ["POST"], "只应发一次上传请求")

    # ---- 清单指针 ----
    def test_payload_carries_sha_only_when_file_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            mf = pathlib.Path(tmp) / "m.json"
            mf.write_bytes(b"{}")
            base = json.loads(self.tool.manifest_commit_payload(mf.read_bytes(), None))
            with_sha = json.loads(self.tool.manifest_commit_payload(mf.read_bytes(), "abc123"))
            self.assertNotIn("sha", base)
            self.assertEqual(with_sha["sha"], "abc123")
            self.assertEqual(with_sha["branch"], "main")

    def test_first_publish_tolerates_missing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            mf = pathlib.Path(tmp) / "m.json"
            mf.write_bytes(b"{}")
            verbs = []
            gone = _http_error(404, "Not Found")
            self.addCleanup(gone.close)      # 3.14 会对未关闭的响应报 ResourceWarning

            def fake_request(url, token, method="GET", data=None, **k):
                verbs.append(method)
                if method == "GET":
                    if len(verbs) == 1:
                        raise gone
                    return json.dumps({"content": base64.b64encode(b"{}").decode()}).encode()
                return b"{}"

            with patch.object(self.tool, "request", side_effect=fake_request):
                self.tool.publish_manifest_pointer("t", mf.read_bytes())
            self.assertEqual(verbs, ["GET", "PUT", "GET"], "404 之后仍要提交并读回清单")

    def test_server_error_is_not_mistaken_for_missing_manifest(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            mf = pathlib.Path(tmp) / "m.json"
            mf.write_bytes(b"{}")
            boom = _http_error(500, "boom")
            self.addCleanup(boom.close)
            with patch.object(self.tool, "request", side_effect=boom):
                with self.assertRaises(urllib.error.HTTPError):
                    self.tool.publish_manifest_pointer("t", mf.read_bytes())

    def test_legacy_manifest_reupload_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            mf = pathlib.Path(tmp) / "m.json"
            mf.write_bytes(b"123")
            verbs = []
            with patch.object(self.tool, "request",
                              side_effect=lambda url, t, method="GET", **k: verbs.append(method)), \
                 patch.object(self.tool, "load_release",
                              return_value=self._release([{"name": "m.json", "size": 99}])):
                with self.assertRaises(SystemExit):
                    self.tool.publish_legacy_manifest(
                        "t", self._release([{"id": 9, "name": self.tool.MANIFEST_PATH}]), mf.read_bytes())
            self.assertEqual(verbs[:2], ["DELETE", "POST"], "旧清单先删后传")

    def test_legacy_manifest_publishes_and_verifies_when_size_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            mf = pathlib.Path(tmp) / "m.json"
            mf.write_bytes(b"123")
            with patch.object(self.tool, "request", return_value=b"{}"), \
                 patch.object(self.tool, "load_release",
                              return_value=self._release(
                                  [{"name": self.tool.MANIFEST_PATH, "size": 3}])), \
                 patch.object(self.tool, "download_asset", return_value=b"123"):
                self.tool.publish_legacy_manifest("t", self._release([]), mf.read_bytes())

    # ---- 入口闸门 ----
    def _run_main(self, notes_text, zips):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            notes = root / "notes.txt"
            notes.write_text(notes_text, encoding="utf-8")
            assets = root / "assets"
            assets.mkdir()
            for name in zips:
                (assets / name).write_bytes(b"zip")
            with patch.object(sys, "argv",
                              ["publish_update_release.py", "--assets", str(assets),
                               "--notes", str(notes)]):
                self.tool.main()

    def test_empty_notes_are_refused(self):
        with self.assertRaises(SystemExit):
            self._run_main("   ", ["StockFloatTargetPrice-9.9.9-macos.zip"])

    def test_overlong_notes_are_refused(self):
        with self.assertRaises(SystemExit):
            self._run_main("说" * 4001, ["StockFloatTargetPrice-9.9.9-macos.zip"])

    def test_ambiguous_or_missing_archive_is_refused(self):
        for zips in ([], ["StockFloatTargetPrice-1-macos.zip",
                          "StockFloatTargetPrice-2-macos.zip"]):
            with self.assertRaises(SystemExit, msg=str(zips)):
                self._run_main("说明", zips)


if __name__ == "__main__":
    unittest.main()
