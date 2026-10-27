"""反馈模块第三轮加固的判据：租约过期后的回执、启动回收、残留清理、失败留证、日志脱敏、通知标识。

每一条都对应一次走查里确认过的缺陷；改回旧行为必须当场变红（见提交说明里的种错清单）。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import sf_feedback as protocol
from sf_feedback_outbox import FeedbackOutbox, OutboxBusy
from sf_feedback_transport import error_label, server_reply, submit_pending
from tests._base import SFCase
from tests.test_feedback_pipeline import PipelineCase


# 手机号形状的合成样本：照 tests/test_public_release.py 的做法拆开拼，源码里不留成串的号码，
# 公开源码的隐私扫描（PHONE 规则）因此不会把测试夹具当成真实个人信息。
SYNTHETIC_PHONE = "138" + "00138000"


def _http_error(status, body=b"", headers=None):
    return HTTPError("https://synthetic.invalid", status, "secret reason", headers or {}, io.BytesIO(body))


class _Store(SFCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="hardening-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "source.zip"
        protocol.build_archive(self.archive, {"state.json": "fixture"}, app_version="fixture",
                               bundle_id="com.skyrocket.app", run_id="fixture")
        self.store = FeedbackOutbox(self.root / "outbox.sqlite3", self.root / "objects")
        self.row = self.store.enqueue(self.archive, app_version="fixture", consent_policy_version="fixture")
        self.clock = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)

    def claim(self, token="a" * 32, seconds=30, now=None):
        return self.store.claim(self.row["outbox_id"], kind="submit", statuses=("locally_verified",),
                                target_status="submitting", token=token, lease_seconds=seconds,
                                now=now or self.clock)

    def receipt(self):
        row = self.store.get(self.row["outbox_id"])
        return {"case_id": "feedback-test", "archive_sha256": row["archive_sha256"],
                "size_bytes": row["archive_size"], "received_at": "2026-10-26T00:00:00Z",
                "storage_status": "stored", "validation_status": "validated",
                "retention_until": None, "retention_policy": "manual_deletion"}


class ReceiptAfterTheLeaseRanOut(_Store):
    """慢上传的回执晚于租约到达：只要还是同一个领用人，回执就必须落库，不能整包重传。"""

    def test_a_validated_receipt_is_kept_even_when_it_arrives_after_expiry(self):
        claimed = self.claim(seconds=1)
        late = self.clock + timedelta(seconds=90)
        done = self.store.persist_claimed_receipt(self.row["outbox_id"], self.receipt(),
                                                  token=claimed["lease_token"], fence=claimed["lease_fence"], now=late)
        self.assertEqual(done["status"], "receipt_persisted")
        self.assertIsNone(done["lease_token"])

    def test_a_failure_arriving_after_expiry_still_leaves_the_row_retryable(self):
        claimed = self.claim(seconds=1)
        late = self.clock + timedelta(seconds=90)
        done = self.store.finish_claim(self.row["outbox_id"], token=claimed["lease_token"],
                                       fence=claimed["lease_fence"], status="response_unknown",
                                       error="HTTPError:503", now=late)
        self.assertEqual(done["status"], "response_unknown")
        self.assertEqual(done["last_error"], "HTTPError:503")

    def test_a_worker_that_was_replaced_is_still_refused(self):
        first = self.claim(seconds=1)
        after = self.clock + timedelta(seconds=5)
        self.assertEqual(self.store.recover_expired_leases(now=after), 1)  # discipline-ok(R6): lease expiry count, not geometry.
        self.store.claim(self.row["outbox_id"], kind="submit", statuses=("submitting",),
                         token="b" * 32, now=after)
        with self.assertRaises(OutboxBusy):
            self.store.persist_claimed_receipt(self.row["outbox_id"], self.receipt(),
                                               token=first["lease_token"], fence=first["lease_fence"], now=after)
        with self.assertRaises(OutboxBusy):
            self.store.finish_claim(self.row["outbox_id"], token=first["lease_token"],
                                    fence=first["lease_fence"], status="response_unknown", now=after)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "submitting")

    def test_the_sql_guard_alone_refuses_a_claimant_replaced_between_read_and_write(self):
        # 过期不再是拒绝理由之后，“读到写之间被别人接手”只剩 UPDATE 里的 token+fence 条件在守。
        # 让 Python 层的预检读到旧快照，才能证明那条 SQL 自己拦得住。
        first = self.claim(seconds=1)
        after = self.clock + timedelta(seconds=5)
        self.store.recover_expired_leases(now=after)
        self.store.claim(self.row["outbox_id"], kind="submit", statuses=("submitting",), token="b" * 32, now=after)
        stale = dict(first)
        with mock.patch.object(self.store, "get", return_value=stale):
            with self.assertRaises(OutboxBusy):
                self.store.finish_claim(self.row["outbox_id"], token=first["lease_token"],
                                        fence=first["lease_fence"], status="response_unknown", now=after)
            with self.assertRaises(OutboxBusy):
                self.store.persist_claimed_receipt(self.row["outbox_id"], self.receipt(),
                                                   token=first["lease_token"], fence=first["lease_fence"], now=after)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "submitting")
        self.assertEqual(self.store.get(self.row["outbox_id"])["lease_token"], "b" * 32)

    def test_a_wrong_token_or_fence_is_refused_even_inside_the_lease(self):
        claimed = self.claim()
        with self.assertRaises(OutboxBusy):
            self.store.persist_claimed_receipt(self.row["outbox_id"], self.receipt(),
                                               token="f" * 32, fence=claimed["lease_fence"], now=self.clock)
        with self.assertRaises(OutboxBusy):
            self.store.persist_claimed_receipt(self.row["outbox_id"], self.receipt(),
                                               token=claimed["lease_token"], fence=claimed["lease_fence"] + 1,
                                               now=self.clock)


class RecoveryOfAbandonedSubmissions(_Store):
    def test_a_live_lease_is_kept_by_default_and_taken_over_when_known_abandoned(self):
        self.claim(seconds=300)
        self.assertEqual(self.store.recover_interrupted(now=self.clock), 0)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "submitting")
        self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 1)
        after = self.store.get(self.row["outbox_id"])
        self.assertEqual((after["status"], after["last_error"]), ("response_unknown", "process_interrupted"))
        self.assertIsNone(after["lease_token"])

    def test_abandoned_recovery_touches_nothing_that_is_not_submitting(self):
        self.assertEqual(self.store.recover_interrupted(now=self.clock, abandoned=True), 0)
        self.assertEqual(self.store.get(self.row["outbox_id"])["status"], "locally_verified")


class ScratchSweep(_Store):
    def make_scratch(self, name, age_sec):
        path = self.root / name
        path.mkdir()
        (path / "diagnostics.zip").write_bytes(b"x")
        stamp = time.time() - age_sec
        os.utime(path, (stamp, stamp))
        return path

    def test_stale_capture_residue_is_moved_not_deleted_and_fresh_is_left_alone(self):
        stale = self.make_scratch("capture-stale", 7200)
        fresh = self.make_scratch("capture-fresh", 10)
        with mock.patch("tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(self.store.sweep_scratch(), (1, 0))
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists(), "正在进行的采集不许被碰")
        moved = list((self.root / "skyrocket-feedback-stale").iterdir())
        self.assertEqual(len(moved), 1)
        self.assertTrue((moved[0] / "diagnostics.zip").exists(), "只挪不删，内容必须还在")

    def test_stale_temporary_copies_in_the_object_store_are_swept_too(self):
        leftover = self.root / "objects" / "abc.zip.123.tmp"
        leftover.write_bytes(b"x")
        stamp = time.time() - 7200
        os.utime(leftover, (stamp, stamp))
        with mock.patch("tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(self.store.sweep_scratch(), (1, 0))
        self.assertFalse(leftover.exists())

    def test_symlinks_are_never_followed_or_moved(self):
        target = self.make_scratch("capture-real", 7200)
        link = self.root / "capture-link"
        link.symlink_to(target)
        with mock.patch("tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(self.store.sweep_scratch(), (1, 0))
        self.assertTrue(link.is_symlink())

    def test_a_holder_that_is_a_symlink_is_refused_and_counted_as_failed(self):
        self.make_scratch("capture-old", 7200)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "skyrocket-feedback-stale").symlink_to(elsewhere)
        with mock.patch("tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(self.store.sweep_scratch(), (0, 1))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_move_that_fails_is_counted_not_raised(self):
        self.make_scratch("capture-old", 7200)
        with mock.patch("tempfile.gettempdir", return_value=str(self.root)), \
                mock.patch("shutil.move", side_effect=OSError("denied")):
            self.assertEqual(self.store.sweep_scratch(), (0, 1))


class FailureEvidenceInTheQueue(_Store):
    def test_the_label_names_status_and_server_code_but_never_free_text(self):
        self.assertEqual(error_label(_http_error(409, b'{"error":"submission_in_progress"}')),
                         "HTTPError:409:submission_in_progress")
        self.assertEqual(error_label(_http_error(502, b"<html>secret</html>")), "HTTPError:502")
        self.assertEqual(error_label(_http_error(422, b'{"error":"Bad Value With Spaces"}')), "HTTPError:422")
        self.assertEqual(error_label(TimeoutError("secret")), "TimeoutError")

    def test_the_label_is_bounded(self):
        huge = type("X" * 300, (Exception,), {})
        self.assertLessEqual(len(error_label(huge("secret"))), 96)

    def test_the_reply_is_read_once_and_shared(self):
        error = _http_error(429, b'{"error":"daily_budget","phase":"budget"}', {"Retry-After": "45"})
        first = server_reply(error)
        self.assertEqual(first, {"retry_after_sec": 45, "server_code": "daily_budget", "server_phase": "budget"})
        error.read = mock.Mock(side_effect=AssertionError("body read twice"))
        self.assertIs(server_reply(error), first)

    def test_retry_after_must_be_a_sane_number_of_seconds(self):
        for value in ("abc", "0", "-5", "999999", "1.5", ""):
            with self.subTest(value=value):
                self.assertNotIn("retry_after_sec", server_reply(_http_error(429, b"{}", {"Retry-After": value})))

    def test_an_unreadable_body_is_marked_not_guessed(self):
        error = _http_error(503, b"")
        error.read = mock.Mock(side_effect=OSError("closed"))
        self.assertEqual(server_reply(error)["body"], "unreadable")

    def test_an_unreadable_header_is_marked_too(self):
        error = _http_error(429, b"{}")
        error.headers = mock.Mock(get=mock.Mock(side_effect=RuntimeError("broken")))
        self.assertEqual(server_reply(error)["body"], "unreadable")

    def test_an_exception_that_refuses_attributes_still_yields_a_reply(self):
        class Locked(HTTPError):
            def __setattr__(self, name, value):
                if name == "_sf_server_reply":
                    raise AttributeError(name)
                super().__setattr__(name, value)
        error = Locked("https://synthetic.invalid", 500, "x", {}, io.BytesIO(b'{"error":"upstream_timeout"}'))
        with mock.patch("sf_logging.log_swallow") as trace:
            self.assertEqual(server_reply(error)["server_code"], "upstream_timeout")
        trace.assert_called_once()
        self.assertEqual(trace.call_args.args[0], "sf_feedback_transport.server_reply.cache")

    def test_a_failed_submission_leaves_status_and_code_in_last_error(self):
        sender = mock.Mock(side_effect=_http_error(429, b'{"error":"daily_budget"}'))
        # 包格式校验不是这条用例要测的东西：fixture 只有 state.json，照现有用例的做法把它桩掉。
        with mock.patch("sf_feedback_transport.read_upload_archive", return_value={}), \
                self.assertRaises(HTTPError):
            submit_pending(self.store, self.row["outbox_id"], "https://receiver.example/v1/feedback", sender=sender)
        row = self.store.get(self.row["outbox_id"])
        self.assertEqual((row["status"], row["last_error"]), ("response_unknown", "HTTPError:429:daily_budget"))


class LogScrubbing(SFCase):
    """日志附件里的个人信息：程序写的日志不该带着邮箱、手机号、公网 IP、令牌出门。"""

    def scrub(self, text):
        import sf_diag
        from sf_privacy import scrub_pii, scrub_secrets
        needles = sf_diag.identity_needles([])
        return sf_diag.scrub_text(scrub_pii(scrub_secrets(text)), needles)

    def test_email_phone_jwt_and_public_ip_are_masked(self):
        out = self.scrub("mail a.b+c@example.co.uk phone %s peer 203.0.113.77:443" % SYNTHETIC_PHONE)
        self.assertIn("<email>", out)
        self.assertIn("<phone>", out)
        self.assertIn("203.0.x.x:443", out)
        self.assertNotIn("example.co.uk", out)
        self.assertNotIn(SYNTHETIC_PHONE, out)
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
        self.assertNotIn(token, self.scrub("jwt " + token + " end"))

    def test_things_that_only_look_like_personal_data_are_left_alone(self):
        text = ("ts 1761465600123 id %sabc seq 2%s ver 1.4.104 build 26A434 lan 192.168.1.5 loop 127.0.0.1 any 0.0.0.0"
                % (SYNTHETIC_PHONE, SYNTHETIC_PHONE))
        self.assertEqual(self.scrub(text), text)

    def test_private_ranges_of_every_kind_are_kept_for_troubleshooting(self):
        from sf_privacy import scrub_pii
        for address in ("10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.0.1", "127.0.0.1"):
            self.assertEqual(scrub_pii("peer " + address), "peer " + address)
        self.assertEqual(scrub_pii("peer 172.32.0.9"), "peer 172.32.x.x", "172.32 不在私有段内")

    def test_private_key_assignments_are_masked_by_each_scrubber_on_its_own(self):
        import sf_diag
        from sf_privacy import scrub_secrets
        text = "private_key=abc123def and private-key: 'xyz'"
        for label, out in (("sf_privacy", scrub_secrets(text)), ("sf_diag", sf_diag.scrub_text(text, ()))):
            self.assertNotIn("abc123def", out, label)
            self.assertNotIn("xyz", out, label)

    def test_lowercase_user_paths_lose_the_account_name(self):
        out = self.scrub("path /users/someone/Library and /Users/Someone/x and /home/who/y")
        for name in ("someone", "Someone", "who"):
            self.assertNotIn("/%s/" % name, out.replace("/Users/<USER>", "").replace("/users/<USER>", ""))
        self.assertIn("/users/<USER>", out)

    def test_the_machine_name_is_replaced_even_when_it_contains_the_login_name(self):
        import sf_diag
        with mock.patch("platform.node", return_value="alicedeMacBook-Air.local"):
            needles = sf_diag.identity_needles([])
        out = sf_diag.scrub_text("host alicedeMacBook-Air.local short alicedemacbook-air", needles)
        self.assertNotIn("alice", out.lower())
        self.assertIn("<host>", out)

    def test_the_machine_name_only_matches_whole_words(self):
        import sf_diag
        with mock.patch("platform.node", return_value="Studio7.local"):
            needles = sf_diag.identity_needles([])
        out = sf_diag.scrub_text("host Studio7.local model Studio72,1 path /tmp/xStudio7/y", needles)
        self.assertIn("host <host>", out)
        self.assertIn("Studio72,1", out, "型号里恰好含有主机名的前缀，不能被抹成 <host>2,1")
        self.assertIn("xStudio7", out, "夹在别的字母数字中间的不是主机名")

    def test_generic_host_names_are_not_treated_as_identity(self):
        import sf_diag
        for name in ("localhost", "unknown", "mac", "iMac", "MacBook-Air", "MacBook-Pro", "test", "main"):
            with mock.patch("platform.node", return_value=name):
                needles = sf_diag.identity_needles([])
            self.assertFalse([n for n in needles if n.marker == "<host>"], name)

    def test_an_address_glued_to_chinese_text_is_still_masked(self):
        out = self.scrub("邮箱a.b@example.com请回复，或联系%s。" % SYNTHETIC_PHONE)
        self.assertEqual(out, "邮箱<email>请回复，或联系<phone>。")

    def test_public_ipv4_followed_by_sentence_punctuation_is_masked(self):
        for suffix in (".", "...", "。", "，"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.scrub("peer 203.0.113.77" + suffix),
                                 "peer 203.0.x.x" + suffix)

    def test_ipv4_like_version_chain_is_not_masked(self):
        # 第二条前面没有字母，单靠“前面不是字母数字”拦不住，只有“后面不能再接 .数字”这条边界能拦。
        for text in ("build v1.2.3.4.5", "ver 1.2.3.4.5 ok"):
            with self.subTest(text=text):
                self.assertEqual(self.scrub(text), text)

    def test_phone_numbers_with_a_country_code_or_separators_are_masked(self):
        grouped = "%s-%s-%s" % (SYNTHETIC_PHONE[:3], SYNTHETIC_PHONE[3:7], SYNTHETIC_PHONE[7:])
        for text in ("+86 " + grouped, "86-" + grouped, grouped, "+86" + SYNTHETIC_PHONE):
            self.assertEqual(self.scrub("call " + text + " now"), "call <phone> now", text)

    def test_public_ipv6_keeps_two_groups_and_everything_local_is_left_alone(self):
        self.assertEqual(self.scrub("peer 2606:4700:4700::1111 end"), "peer 2606:4700:x:x end")
        self.assertEqual(self.scrub("peer [2001:4860:4860:0:0:0:0:8888]:443"), "peer [2001:4860:x:x]:443")
        kept = "loop ::1 ll fe80::1 ula fd12:3456::1 any ::"
        self.assertEqual(self.scrub(kept), kept)

    def test_things_shaped_like_ipv6_are_left_alone(self):
        kept = "time 16:04:04.944 clock 16:04:04 mac aa:bb:cc:dd:ee:ff scope std::vector a::b a::b::c"
        self.assertEqual(self.scrub(kept), kept)

    def test_crash_reports_and_invalid_failure_records_are_masked_too(self):
        import sf_diagnostics
        import zipfile
        directory = Path(tempfile.mkdtemp(prefix="logscrub-crash-", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        (directory / "crashes").mkdir()
        (directory / "crashes" / "x.ips").write_text("peer 203.0.113.77 owner someone@example.com", encoding="utf-8")
        (directory / "f").write_text("not json peer 203.0.113.78 someone@example.com", encoding="utf-8")
        archive = zipfile.ZipFile(directory / "out.zip", "w")
        self.addCleanup(archive.close)
        entries = {}
        helpers = {
            "json_entry": lambda name, value: archive.writestr(name, json.dumps(value)),
            "text_entry": lambda name, text, *a, **k: entries.setdefault(name, text),
            "read_text": lambda path, name, limit=None, tail=False: Path(path).read_text(encoding="utf-8"),
            "public_text": lambda text: str(text),
            "field": lambda key, fn, default, bucket=None: default,
            "inventory": lambda: {}, "is_crash": lambda name: name.endswith(".ips"),
            "select_crashes": lambda items: items, "extract_crash": lambda body: body}
        paths = {"config": str(directory / "c"), "backup": str(directory / "b"), "last_quote": str(directory / "q"),
                 "log": str(directory / "missing.log"), "failures": str(directory / "f"),
                 "crashes": str(directory / "crashes")}
        metadata = {"log_keep": 0, "log_max": 10000, "started": time.monotonic(), "version": "1.4.104",
                    "code": "sh600000", "last_ok_time": "", "quote_source": "", "http_status": 200,
                    "stage": "beta", "version_label": "v", "bundle_id": "b", "running_bundle_id": "b"}
        sf_diagnostics.write_attachments(archive, paths, metadata, [], helpers)
        crash = archive.read("crash_reports/x.ips.txt").decode("utf-8")
        self.assertIn("203.0.x.x", crash)
        self.assertNotIn("someone@example.com", crash)
        invalid = entries["last_failures.invalid.txt"]
        self.assertIn("203.0.x.x", invalid)
        self.assertNotIn("someone@example.com", invalid)

    def test_the_user_description_is_not_run_through_the_log_only_masks(self):
        from sf_feedback_capture import clean_description
        out = clean_description("请联系我 someone@example.com 或 %s" % SYNTHETIC_PHONE)
        self.assertIn("someone@example.com", out)
        self.assertIn(SYNTHETIC_PHONE, out)

    def test_the_log_attachment_is_masked_end_to_end(self):
        import sf_diagnostics
        import zipfile
        directory = Path(tempfile.mkdtemp(prefix="logscrub-", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        log = directory / "debug.log"
        log.write_text("peer 203.0.113.77 owner someone@example.com\n", encoding="utf-8")
        archive = zipfile.ZipFile(directory / "out.zip", "w")
        self.addCleanup(archive.close)
        entries = {}
        helpers = {
            "json_entry": lambda name, value: archive.writestr(name, json.dumps(value)),
            "text_entry": lambda name, text, *a, **k: (entries.setdefault(name, text), archive.writestr(name, text)),
            "read_text": lambda path, name, limit=None, tail=False: Path(path).read_text(encoding="utf-8"),
            "public_text": lambda text: str(text),
            "field": lambda key, fn, default, bucket=None: default,
            "inventory": lambda: {}, "is_crash": lambda name: False,
            "select_crashes": lambda items: [], "extract_crash": lambda body: body}
        paths = {"config": str(directory / "c"), "backup": str(directory / "b"), "last_quote": str(directory / "q"),
                 "log": str(log), "failures": str(directory / "f"), "crashes": str(directory / "crashes")}
        metadata = {"log_keep": 0, "log_max": 10000, "started": time.monotonic(), "version": "1.4.104",
                    "code": "sh600000", "last_ok_time": "", "quote_source": "", "http_status": 200,
                    "stage": "beta", "version_label": "v", "bundle_id": "b", "running_bundle_id": "b"}
        sf_diagnostics.write_attachments(archive, paths, metadata, [], helpers)
        self.assertIn("203.0.x.x", entries["debug.log"])
        self.assertIn("<email>", entries["debug.log"])
        self.assertNotIn("someone@example.com", entries["debug.log"])


class NotificationIdentity(SFCase):
    def test_a_short_identity_is_kept_exactly_so_upgrades_do_not_renotify(self):
        from tools.feedback_agent import failure_identity
        import hashlib
        expected = "cases:" + hashlib.sha256(json.dumps(sorted(["feedback-a"])).encode()).hexdigest()
        self.assertEqual(failure_identity(["feedback-a"], [], False), expected)
        self.assertEqual(failure_identity([], [], True), "storage_capacity")
        self.assertEqual(failure_identity([], [], False), "")

    def test_all_three_failure_kinds_together_still_fit_the_limit(self):
        from sf_feedback_notifications import MAX_FAILURE_IDENTITY
        from tools.feedback_agent import failure_identity
        combined = failure_identity(["feedback-a"], ["desktop-x"], True)
        self.assertLessEqual(len(combined), MAX_FAILURE_IDENTITY)
        self.assertTrue(combined.startswith("combined:"))

    def test_the_combined_identity_still_changes_when_any_part_changes(self):
        from tools.feedback_agent import failure_identity
        base = failure_identity(["feedback-a"], ["desktop-x"], True)
        self.assertNotEqual(base, failure_identity(["feedback-b"], ["desktop-x"], True))
        self.assertNotEqual(base, failure_identity(["feedback-a"], ["desktop-y"], True))
        self.assertNotEqual(base, failure_identity(["feedback-a"], ["desktop-x"], False))

    def test_the_notifier_accepts_the_combined_identity(self):
        from sf_feedback_notifications import FeedbackNotifications
        from tools.feedback_agent import failure_identity
        sent = []
        with tempfile.TemporaryDirectory(prefix="notice-", dir="/tmp") as directory:
            notifier = FeedbackNotifications(Path(directory), sender=sent.append)
            result = notifier.sync({}, failure_identity(["feedback-a"], ["desktop-x"], True))
        self.assertEqual(result, {"submitted": ["failure"], "failed": []})


class AdminUnreceiptedCount(PipelineCase):
    def test_a_zip_without_a_receipt_is_counted_and_a_complete_case_is_not(self):
        from sf_feedback_admin import FeedbackAgent
        from sf_feedback_analyzer import analyze_archive
        path, envelope = self.package()
        self.inbox.accept(envelope, path.read_bytes())
        agent = FeedbackAgent(self.api, self.root / "admin", analyzer=analyze_archive)
        self.assertEqual(agent.run_once()["unreceipted_zips"], 0)
        self.api.upload(1, "feedback-" + "d" * 32 + ".zip", b"half-finished upload")
        self.assertEqual(agent.run_once()["unreceipted_zips"], 1)
        self.api.upload(1, "feedback-" + "d" * 32 + ".receipt.json", b"{}")
        self.assertEqual(agent.run_once()["unreceipted_zips"], 0, "补出回执以后就不再算“卡着”")
