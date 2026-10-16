# -*- coding: utf-8 -*-
"""自动更新协议回归：签名清单、版本边界与下载校验失败均不得触碰安装目录。"""

import base64
import concurrent.futures
import dataclasses
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nacl.signing import SigningKey
import sf_constants
import sf_update


def _load_publish_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "publish_update_release.py"
    spec = importlib.util.spec_from_file_location("publish_update_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_QAPP = None


def _ensure_qapp():
    """建 Qt 控件前必须先有 QApplication，且**必须被模块级引用持有**。

    写成 `QApplication.instance() or QApplication([])` 而不赋值，CPython 会当场
    把刚建的 app 释放掉，下一句就报 "QWidget: Must construct a QApplication
    before a QWidget"。
    """
    global _QAPP
    from PyQt6.QtWidgets import QApplication
    _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


class _Response:
    def __init__(self, body, url="https://github.com/a/b/releases/download/v/a.zip"):
        self._body = body
        self._pos = 0
        self._url = url
        self.headers = {"Content-Length": str(len(body))}

    def geturl(self):
        return self._url

    def read(self, count=-1):
        if count < 0:
            count = len(self._body) - self._pos
        chunk = self._body[self._pos:self._pos + count]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestUpdateManifest(unittest.TestCase):
    def _manifest(self, key, **changes):
        data = {
            "schema": 1,
            "channel": "stable",
            "version": "1.3.71",
            # 身份字段从产品常量现推：v1.4.55 全局改名（含 bundle id）时，写死字面量
            # 的夹具会让"清单校验"这条判据悄悄验的是另一个产品。
            "bundle_id": sf_constants.BUNDLE_ID,
            "app_name": sf_constants.APP_DISPLAY_NAME + ".app",
            "notes": "测试更新说明",
            "archive": {
                "url": "https://github.com/a/b/releases/download/v/a.zip",
                "size": 3,
                "sha256": hashlib.sha256(b"zip").hexdigest(),
            },
        }
        data.update(changes)
        payload = sf_update._canonical_payload(data)
        data["signature"] = {
            "algorithm": "ed25519",
            "value": base64.b64encode(key.sign(payload).signature).decode("ascii"),
        }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def test_valid_signed_manifest_is_accepted(self):
        key = SigningKey.generate()
        raw = self._manifest(key)
        with patch.object(sf_update, "UPDATE_PUBLIC_KEY_B64",
                          base64.b64encode(bytes(key.verify_key)).decode("ascii")):
            info = sf_update.verify_manifest(raw)
        self.assertEqual(info.version, "1.3.71")
        self.assertTrue(sf_update.is_newer(info.version, "1.3.70"))

    def test_tampered_manifest_is_rejected(self):
        key = SigningKey.generate()
        raw = self._manifest(key).replace("测试更新说明".encode(), "被篡改".encode())
        with patch.object(sf_update, "UPDATE_PUBLIC_KEY_B64",
                          base64.b64encode(bytes(key.verify_key)).decode("ascii")):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.verify_manifest(raw)

    def test_non_github_download_address_is_rejected(self):
        key = SigningKey.generate()
        raw = self._manifest(key, archive={
            "url": "https://example.com/a.zip", "size": 3,
            "sha256": hashlib.sha256(b"zip").hexdigest(),
        })
        with patch.object(sf_update, "UPDATE_PUBLIC_KEY_B64",
                          base64.b64encode(bytes(key.verify_key)).decode("ascii")):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.verify_manifest(raw)

    def test_same_or_lower_version_never_updates(self):
        self.assertFalse(sf_update.is_newer("1.3.70", "1.3.70"))
        self.assertFalse(sf_update.is_newer("1.3.69", "1.3.70"))
        with self.assertRaises(sf_update.UpdateError):
            sf_update.parse_version("1.3.70-beta")

    def test_unicode_release_tag_is_percent_encoded_only_for_request(self):
        source = "https://github.com/a/b/releases/download/版本更新/a.zip"
        self.assertEqual(
            sf_update._request_url(source),
            "https://github.com/a/b/releases/download/%E7%89%88%E6%9C%AC%E6%9B%B4%E6%96%B0/a.zip",
        )

    def test_release_notes_are_the_only_manifest_notes_source(self):
        root = Path(__file__).resolve().parents[1]
        tool = (root / "tools" / "make_update_assets.py").read_text(encoding="utf-8")
        release = (root / "tools" / "publish_update_release.py").read_text(encoding="utf-8")
        build = (root / "build_release.sh").read_text(encoding="utf-8")
        self.assertIn('notes_file = root / "release_notes.txt"', tool)
        self.assertIn('"notes": notes', tool)
        self.assertNotIn('add_argument("--notes"', tool)
        self.assertIn('parser.add_argument("--notes", type=Path, required=True)', release)
        self.assertIn('git", "credential", "fill"', release)
        self.assertIn('publish_manifest_pointer(token, manifest_bytes)', release)
        self.assertIn('def publish_legacy_manifest', release)
        self.assertIn('publish_legacy_manifest(token, load_release(token), manifest_bytes)', release)
        self.assertIn('NOTES_FILE="$PROJ/release_notes.txt"', build)
        self.assertIn('release_notes.txt', build)

    def test_raw_manifest_url_is_trusted_and_requestable(self):
        source = "https://raw.githubusercontent.com/1056572031-boop/StockFloat-TargetPrice-Releases/main/StockFloatTargetPrice-update.json"
        sf_update._check_release_url(source)
        self.assertEqual(sf_update._request_url(source), source)

    def test_install_result_is_consumed_once_and_invalid_result_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sf_update, "UPDATE_STAGING_DIR", folder):
            result = Path(folder) / "update-result.json"
            result.write_text(json.dumps({"status": "rolled_back", "message": "restart_new_app_failed"}),
                              encoding="utf-8")
            self.assertEqual(sf_update.consume_install_result(), {
                "status": "rolled_back", "message": "restart_new_app_failed"})
            self.assertFalse(result.exists())
            self.assertIsNone(sf_update.consume_install_result())
            result.write_text('{"status":"unknown","message":"x"}', encoding="utf-8")
            self.assertIsNone(sf_update.consume_install_result())
            self.assertFalse(result.exists())


class TestUpdatePublisherContract(unittest.TestCase):
    def test_manifest_commit_payload_keeps_unicode_bytes_and_existing_sha(self):
        publisher = _load_publish_tool()
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / publisher.MANIFEST_PATH
            body = '{"notes":"中文说明"}\n'.encode("utf-8")
            manifest.write_bytes(body)
            payload = json.loads(publisher.manifest_commit_payload(body, "old-sha"))
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["sha"], "old-sha")
        self.assertEqual(base64.b64decode(payload["content"]), body)

    def test_upload_url_encodes_chinese_archive_name(self):
        publisher = _load_publish_tool()
        url = publisher.upload_url(42, "实时股价-1.3.96.zip")
        self.assertIn("releases/42/assets?name=%E5%AE%9E%E6%97%B6", url)

    def test_verified_asset_requires_exact_name_and_size(self):
        publisher = _load_publish_tool()
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "Skyrocket-1.3.96-macos.zip"
            archive.write_bytes(b"zip")
            release = {"assets": [{"name": archive.name, "size": 3}]}
            self.assertEqual(publisher.verified_asset(release, archive)["name"], archive.name)
            with self.assertRaises(SystemExit):
                publisher.verified_asset({"assets": [{"name": archive.name, "size": 2}]}, archive)


class TestUpdateHelperContract(unittest.TestCase):
    def test_launch_rejects_partial_signing_anchor_configuration(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sf_update, "UPDATE_STAGING_DIR", folder), \
             patch.object(sf_update, "installed_bundle_path", return_value=folder), \
             patch.object(sf_update, "bundled_helper_path", return_value=__file__), \
             patch.object(sf_update, "UPDATE_SIGNING_TEAM_ID", "TEAM"), \
             patch.object(sf_update, "UPDATE_SIGNING_CERT_SHA1", ""):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.launch_installer(__file__, "a" * 64, "1.3.90", target_app=folder)

    def test_helper_contains_required_failure_protections(self):
        """安装助手由 macOS 系统工具执行；这里守住所有极端故障出口与防护顺序。"""
        helper = Path(__file__).resolve().parents[1] / "updater" / "update_helper.sh"
        body = helper.read_text(encoding="utf-8")
        for marker in (
                "main_app_exit_timeout", "insufficient_disk_space", "archive_preflight_failed",
                "archive_symlink_invalid", "archive_path_invalid", "archive_unpacked_size_invalid",
                "archive_file_count_invalid", "EXTRACT_TIMEOUT=90", "rollback_or_fail",
                "restart_new_app_failed", "rollback_failed", "new_exec=", "pgrep -f", "首次", "EXPECTED_TEAM_ID",
                "EXPECTED_CERT_SHA1", "TeamIdentifier=$EXPECTED_TEAM_ID"):
            self.assertIn(marker, body, marker)



class TestUpdateRequest(unittest.TestCase):
    def test_manual_check_shows_feedback_before_worker_starts(self):
        # 手动点击后应先用原生弹窗给出“正在检查更新”反馈，再启动后台检查线程，
        # 不再等待网络返回后才有界面反应；非手动检查（启动静默自检）不弹窗，
        # 但仍在后台启动检查线程。
        window_events = []
        thread_events = []

        class _Thread:
            def __init__(self, *args, **kwargs):
                del args, kwargs
            def start(self):
                thread_events.append("thread")

        import stock_float_window
        from stock_float_window import FloatWindow

        # 手动与静默检查都不再预先打开自绘面板；网络完成后才显示 macOS 默认提示框。
        for manual in (True, False):
            thread_events.clear()
            feedback = []
            fake = type("FakeWindow", (), {
                "_threads_stopped": False,
                "_update_checking": False,
                "_update_downloading": False,
                "_update_transition_pending": False,
                "_update_pending": None,
                "_update_manual_wait": False,
                "_update_last_check_at": 0.0,
                "_show_checking_dialog": lambda self: feedback.append("checking"),
            })()
            with patch("stock_float_window.threading.Thread", _Thread):
                FloatWindow._check_updates_background(fake, manual=manual)
            self.assertEqual(thread_events, ["thread"])
            # v1.4.43：手动点击当下就要有可见反馈；静默自检仍然不抢焦点。
            self.assertEqual(feedback, ["checking"] if manual else [])


class TestUpdateWindowLifecycle(unittest.TestCase):
    def test_progress_updates_visible_download_state(self):
        updates = []
        dialog = type("Dialog", (), {
            "setValue": lambda self, value: updates.append(("value", value)),
            "setLabelText": lambda self, text: updates.append(("label", text)),
            "setText": lambda self, text: updates.append(("label", text)),
        })()
        fake = type("FakeWindow", (), {"_update_flow_box": dialog, "_update_flow_progress": dialog})()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._handle_update_progress(fake, 47, 5 * 1048576, 10 * 1048576)
        self.assertEqual(updates[0], ("value", 47))
        self.assertIn("47%", updates[1][1])
        self.assertIn("5.0 / 10.0 MB", updates[1][1])

    def test_check_transition_blocks_second_update_flow(self):
        """在飞检查期间再点一次：不起第二个请求，但这次点击必须被看见、被记账。

        v1.4.43 把这条用例的判据从"什么都不做"改成"不重复请求 + 必须给反馈"：
        旧行为会在守卫处静默 return，用户从点击到检查结束看不到任何东西（实测
        0 个弹窗），正是"检查更新点了没反应"的成因。
        """
        feedback = []
        fake = type("FakeWindow", (), {
            "_threads_stopped": False,
            "_update_downloading": False,
            "_update_transition_pending": True,
            "_update_checking": True,
            "_update_check_stale": lambda self: False,   # 测"在飞期间被接住"，这次检查当作还活着
            "_update_pending": None,
            "_update_manual_wait": False,
            "_update_last_check_at": 0.0,
            "_show_checking_dialog": lambda self: feedback.append("checking"),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        with patch("stock_float_window.threading.Thread") as thread:
            FloatWindow._check_updates_background(fake, manual=True)
        thread.assert_not_called()
        self.assertEqual(feedback, ["checking"],
                         "点击必须立刻有可见反馈；点了却什么都不出现就是本轮修的 bug。"
                         "「点击有没有被回包兑现」由 tests/test_v1443_update_feedback.py "
                         "用公开行为来证明，这里不去读私有标记（书写纪律 R5）。")

    def test_cancel_download_sets_cancel_event(self):
        event = threading.Event()
        fake = type("FakeWindow", (), {"_update_cancel": event})()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._cancel_update_download(fake)
        self.assertTrue(event.is_set())

    def test_exit_closes_update_dialog_before_main_window_teardown(self):
        """退出时不得把仍显示的更新框交给 QApplication.quit() 的析构阶段。

        v1.4.20 起判据从"close 之后有没有 deleteLater"改成"C++ 对象是否已经没了"：
        macOS 上 QApplication.quit() 等于 -[NSApplication terminate:] → exit()，
        排队的 deleteLater 会被推到 C++ 静态析构阶段才执行，那时销毁带 QWindow 的
        框就是空指针 SIGSEGV（iMac 线上崩溃）。所以必须**就地销毁**。
        """
        import PyQt6.sip as sip
        from PyQt6.QtWidgets import QMessageBox
        _ensure_qapp()
        box = QMessageBox()
        box.show()
        fake = type("FakeWindow", (), {
            "_update_flow_box": box,
            "_update_flow_progress": object(),
            "_update_flow_cancel": object(),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._close_update_flow_box(fake)
        self.assertTrue(sip.isdeleted(box),
                        "兜底框必须收口时就地销毁，不能留延迟删除事件到退出之后")
        self.assertIsNone(fake._update_flow_box)
        self.assertIsNone(fake._update_flow_progress)
        self.assertIsNone(fake._update_flow_cancel)

    def test_accept_prompt_defers_download_until_prompt_is_closed(self):
        events = []
        fake = type("FakeWindow", (), {
            "_dispose_update_dialog": lambda self, box: events.append("close"),
            "_download_update_background": lambda self, info: events.append(("download", info)),
        })()
        update = object()
        import stock_float_window
        from stock_float_window import FloatWindow
        with patch("stock_float_window.QTimer.singleShot",
                   side_effect=lambda _delay, callback: (events.append("queued"), callback())[1]):
            FloatWindow._accept_update_prompt(fake, object(), update)
        self.assertEqual(events, ["close", "queued", ("download", update)])

    def test_exit_close_is_idempotent_when_update_callback_arrives_late(self):
        """退出后网络线程迟到的回包不得再次触碰已关闭的对话框。"""
        import PyQt6.sip as sip
        from PyQt6.QtWidgets import QMessageBox
        _ensure_qapp()
        box = QMessageBox()
        box.show()
        fake = type("FakeWindow", (), {
            "_update_flow_box": box,
            "_update_flow_progress": object(),
            "_update_flow_cancel": object(),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._close_update_flow_box(fake)
        self.assertTrue(sip.isdeleted(box))
        try:
            FloatWindow._close_update_flow_box(fake)
        except Exception as exc:                       # noqa: BLE001
            self.fail("第二次收口必须安全（引用已摘），实际抛了 %r" % (exc,))

    def test_manual_cached_update_opens_without_starting_network_worker(self):
        events = []
        update = sf_update.UpdateInfo("9.9.9", "更新说明", "https://github.com/a/b/releases/download/v/a.zip",
                                      8, "a" * 64)
        fake = type("FakeWindow", (), {
            "_threads_stopped": False,
            "_update_checking": False,
            "_update_downloading": False,
            "_update_pending": update,
            "_prompt_update": lambda self, value: events.append(value),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._check_updates_background(fake, manual=True)
        self.assertEqual(events, [update])
        self.assertFalse(fake._update_checking)

    def test_silent_check_starts_background_download_without_prompting(self):
        events = []
        downloads = []
        update = sf_update.UpdateInfo("9.9.9", "更新说明", "https://github.com/a/b/releases/download/v/a.zip",
                                      8, "a" * 64)
        fake = type("FakeWindow", (), {
            "_update_checking": True,
            "_quitting": False,
            "_update_failure_count": 2,
            "_update_retry_timer": None,
            "_update_pending": None,
            "_update_prompted_version": None,
            "_ready_update_archive": lambda self, value: None,
            "_prompt_update": lambda self, value: events.append(value),
            "_download_update_background": lambda self, value, **kwargs: (
                downloads.append((value, kwargs))),
            "_close_update_flow_box": lambda self: FloatWindow._close_update_flow_box(self),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._handle_update_check(fake, False, update, None)
        self.assertEqual(fake._update_pending, update)
        self.assertEqual(events, [])
        self.assertEqual(downloads, [(update, {
            "prompt_when_ready": True, "show_progress": False})])
        self.assertEqual(fake._update_failure_count, 0)

    def test_retry_schedule_uses_five_then_thirty_minutes(self):
        started = []
        timer = type("Timer", (), {"start": lambda self, delay: started.append(delay)})()
        fake = type("FakeWindow", (), {"_update_retry_timer": timer, "_quitting": False})()
        import stock_float_window
        from stock_float_window import FloatWindow
        fake._update_failure_count = 1
        FloatWindow._schedule_update_retry(fake)
        fake._update_failure_count = 2
        FloatWindow._schedule_update_retry(fake)
        self.assertEqual(started, [5 * 60 * 1000, 30 * 60 * 1000])

    def test_resume_only_triggers_silent_check_when_active(self):
        events = []
        fake = type("FakeWindow", (), {
            "_check_updates_background": lambda self: events.append("check"),
        })()
        from PyQt6.QtCore import Qt
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._check_updates_after_resume(fake, Qt.ApplicationState.ApplicationInactive)
        FloatWindow._check_updates_after_resume(fake, Qt.ApplicationState.ApplicationActive)
        self.assertEqual(events, ["check"])

    def test_prompt_update_uses_system_dialog_with_clean_copy(self):
        """新版确认走 macOS 系统弹窗：文案干净、按钮"暂不更新/立即更新"、默认立即更新。

        v1.4.6 起本用例从"读源码断言字符串"改为行为断言（观察 show_alert 的
        实际入参），源码怎么改都不误报，但文案与按钮契约仍然被守住。
        """
        import queue as _queue

        import sf_system_alert
        captured = {}

        def fake_show_alert(**kwargs):
            captured.update(kwargs)
            return object()

        update = sf_update.UpdateInfo("9.9.9", "修复若干问题",
                                      "https://github.com/a/b/releases/download/v/a.zip",
                                      8, "a" * 64)
        fake = type("FakeWindow", (), {
            "_quitting": False,
            "_update_prompt_dialog": None,
            "_update_pending": update,
            "_ready_update_archive": lambda self, value: None,
            "_dispose_update_dialog": lambda self, dialog: None,
            # v1.4.38：弹窗前先登记「弹在哪块屏」。本用例只量文案与按钮契约，
            # 选屏接线由 tests/test_v1438_alert_center.py 用真浮窗守，这里给个空实现。
            "_anchor_update_dialog_screen": lambda self: None,
            "q": _queue.Queue(),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        with patch.object(sf_system_alert, "show_alert", fake_show_alert), \
             patch.object(sf_system_alert, "bundled_icon_path", return_value=None):
            FloatWindow._prompt_update(fake, update)
        self.assertEqual(captured["title"], "软件更新")
        self.assertIn("发现新版本 v9.9.9", captured["text"])
        self.assertIn("修复若干问题", captured["text"])
        self.assertEqual(captured["message"], "发现新版本 v9.9.9。",
                         "原生 NSAlert 的粗体主文案单独传（v1.4.8）")
        self.assertEqual(captured["info"], "1. 修复若干问题",
                         "更新说明作为副文案，不混进主文案")
        self.assertNotIn("现在下载并安装吗", captured["text"])
        self.assertNotIn("测试版", captured["text"])
        self.assertEqual(captured["buttons"], ("暂不更新", "立即更新"))
        self.assertEqual(captured["default"], "立即更新")
        self.assertEqual(captured["cancel"], "暂不更新")
        self.assertTrue(callable(captured["on_result"]),
                        "点击结果必须经回调转投主线程队列")

    def test_prompt_answer_starts_download_only_for_install(self):
        """系统弹窗回包：仅"立即更新"触发下载；"暂不更新"与迟到回包静默收口。"""
        import queue as _queue

        import stock_float_window
        from stock_float_window import FloatWindow
        events = []
        fake = type("FakeWindow", (), {
            "_quitting": False,
            "_update_prompt_dialog": object(),
            "_update_pending": "U",
            "_ready_update_archive": lambda self, value: None,
            "_dispose_update_dialog": lambda self, dialog: events.append(("close", dialog)),
            "_accept_update_prompt": lambda self, box, update: FloatWindow._accept_update_prompt(
                self, box, update),
            "q": _queue.Queue(),
        })()
        with patch("stock_float_window.QTimer.singleShot",
                   side_effect=lambda _delay, callback: events.append(("queued",))):
            FloatWindow._handle_update_prompt_answer(fake, "立即更新")
            self.assertIsNone(fake._update_prompt_dialog)
            FloatWindow._handle_update_prompt_answer(fake, "暂不更新")
        kinds = [e[0] for e in events]
        self.assertIsNotNone(events[0][1], "首次回包须收口当时持有的弹窗")
        self.assertEqual(kinds.count("queued"), 1, "只有立即更新会启动下载")
        self.assertLess(kinds.index("close"), kinds.index("queued"),
                        "必须先收口弹窗，再启动唯一下载框")

    def test_quitting_suppresses_prompt_answer(self):
        fake = type("FakeWindow", (), {
            "_quitting": True,
            "_update_prompt_dialog": None,
            "_update_pending": "U",
            "_ready_update_archive": lambda self, value: None,
            "_dispose_update_dialog": lambda self, dialog: None,
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        with patch("stock_float_window.QTimer.singleShot") as single:
            FloatWindow._handle_update_prompt_answer(fake, "立即更新")
        single.assert_not_called()

    def test_manual_recheck_redisplays_same_available_version(self):
        events = []
        update = sf_update.UpdateInfo("9.9.9", "更新说明", "https://github.com/a/b/releases/download/v/a.zip",
                                      8, "a" * 64)
        fake = type("FakeWindow", (), {
            "_update_checking": True,
            "_quitting": False,
            "_update_prompted_version": "9.9.9",
            "_update_pending": None,
            "_prompt_update": lambda self, value: events.append((value.version, value.notes)),
            "_close_update_flow_box": lambda self: FloatWindow._close_update_flow_box(self),
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        FloatWindow._handle_update_check(fake, True, update, None)
        self.assertEqual(events, [("9.9.9", "更新说明")])
        self.assertEqual(fake._update_pending, update)

    def test_shutdown_signals_update_cancel(self):
        # 退出时不等待下载线程，但必须先通知下载停止后续 Range 请求。
        event = threading.Event()
        fake = type("FakeWindow", (), {
            "_threads_stopped": False,
            "_update_cancel": event,
            "running": True,
            "_wake": None,
            "_editor": None,
            "_quote_thread": None,
            "_minute_thread": None,
        })()
        import stock_float_window
        from stock_float_window import FloatWindow
        with patch("stock_float_window._NET_POOL.close_all"):
            FloatWindow._shutdown_threads(fake)
        self.assertTrue(event.is_set())
        self.assertFalse(fake.running)


class TestUpdateDownload(unittest.TestCase):
    @staticmethod
    def _fake_adaptive(body, progress=None):
        def download(info, part_path, state_path, cancel_event, timeout,
                     progress_callback=None):
            del state_path, cancel_event, timeout
            Path(part_path).write_bytes(body)
            if progress_callback is not None:
                progress_callback(len(body), info.archive_size)
            if progress is not None:
                progress.append((len(body), info.archive_size))
        return download

    def test_hash_mismatch_removes_invalid_resume_files(self):
        info = sf_update.UpdateInfo("1.3.71", "", "https://github.com/a/b/releases/download/v/a.zip",
                                    3, hashlib.sha256(b"bad").hexdigest())
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sf_update, "UPDATE_STAGING_DIR", folder), \
             patch.object(sf_update, "_download_adaptive", side_effect=self._fake_adaptive(b"zip")):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.download_update(info)
            self.assertFalse((Path(folder) / "Skyrocket-1.3.71.zip").exists())
            self.assertFalse((Path(folder) / "Skyrocket-1.3.71.zip.part").exists())

    def test_verified_download_reports_progress_and_completes(self):
        body = b"zip"
        seen = []
        info = sf_update.UpdateInfo("1.3.71", "", "https://github.com/a/b/releases/download/v/a.zip",
                                    len(body), hashlib.sha256(body).hexdigest())
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sf_update, "UPDATE_STAGING_DIR", folder), \
             patch.object(sf_update, "_download_adaptive", side_effect=self._fake_adaptive(body)):
            archive = sf_update.download_update(info, progress_callback=lambda done, total: seen.append((done, total)))
            self.assertEqual(Path(archive).read_bytes(), body)
            self.assertEqual(seen[-1], (len(body), len(body)))

    def test_valid_completed_archive_is_reused_without_network(self):
        body = b"zip"
        info = sf_update.UpdateInfo("1.3.71", "", "https://github.com/a/b/releases/download/v/a.zip",
                                    len(body), hashlib.sha256(body).hexdigest())
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sf_update, "UPDATE_STAGING_DIR", folder), \
             patch.object(sf_update, "_download_adaptive") as download:
            final = Path(folder) / "Skyrocket-1.3.71.zip"
            final.write_bytes(body)
            self.assertEqual(sf_update.download_update(info), str(final))
            download.assert_not_called()

    def test_resume_state_only_accepts_matching_package(self):
        info = sf_update.UpdateInfo("1.3.71", "", "https://github.com/a/b/releases/download/v/a.zip",
                                    80, "a" * 64)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "resume.json"
            state.write_text(json.dumps({"version": "1.3.71", "size": 80,
                                         "sha256": "a" * 64, "completed": [0, 3]}),
                             encoding="utf-8")
            self.assertEqual(sf_update._load_resume_state(state, info), {0, 3})
            wrong = dataclasses.replace(info, archive_sha256="b" * 64)
            self.assertEqual(sf_update._load_resume_state(state, wrong), set())

    def test_tiny_archives_never_generate_invalid_ranges(self):
        # 合法签名清单允许很小的测试包；不能生成 Range: bytes=0--1。
        for size in range(1, 8):
            ranges = sf_update._download_ranges(size)
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[-1][1], size - 1)
            self.assertEqual(sum(end - start + 1 for start, end in ranges), size)
            self.assertTrue(all(0 <= start <= end < size for start, end in ranges))

    def test_adaptive_download_uses_eight_four_one_fallback(self):
        body = b"abcdefgh"
        info = sf_update.UpdateInfo("1.3.71", "", "https://github.com/a/b/releases/download/v/a.zip",
                                    len(body), hashlib.sha256(body).hexdigest())
        worker_counts = []

        class _Pool:
            def __init__(self, max_workers, **kwargs):
                del kwargs
                worker_counts.append(max_workers)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                del args
            def submit(self, fn, *args, **kwargs):
                future = concurrent.futures.Future()
                if worker_counts[-1] in (8, 4):
                    future.set_exception(sf_update.UpdateError("down"))
                else:
                    try:
                        fn(*args, **kwargs)
                        future.set_result(None)
                    except Exception as exc:
                        future.set_exception(exc)
                return future

        def fake_range(url, start, end, part_path, cancel_event, timeout, progress, source=None):
            del url, cancel_event, timeout
            with open(part_path, "r+b") as out:
                out.seek(start)
                out.write(body[start:end + 1])
            progress(end - start + 1)

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sf_update.concurrent.futures, "ThreadPoolExecutor", _Pool), \
             patch.object(sf_update, "resolve_download_url", return_value=info.archive_url), \
             patch.object(sf_update, "_download_range", side_effect=fake_range):
            part = Path(folder) / "a.part"
            state = Path(folder) / "a.resume.json"
            sf_update._download_adaptive(info, part, state, threading.Event(), 60)
            self.assertEqual(part.read_bytes(), body)
        self.assertEqual(worker_counts, [8, 4, 1])


if __name__ == "__main__":
    unittest.main()
