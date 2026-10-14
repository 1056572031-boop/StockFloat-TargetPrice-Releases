# -*- coding: utf-8 -*-
"""更新供给链与通知投递补测：签名清单校验、受限下载、安装交接、UN 降级。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
这两条链路都是"只在少数时刻发生、一旦坏了用户只能吃后果"的路径：
更新链错了会把来路不明的包装进 /Applications；通知链错了用户就等不到
"到达目标价"那一条横幅，而它在设计上是**静默降级**的（不打扰 = 看不出坏了）。

签名校验用**本轮生成的密钥对**（把公钥常量 patch 掉），因此正向与负向分支
都能覆盖到；不依赖任何线上清单，也不碰真实通知授权。
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_config             # noqa: E402
import sf_constants          # noqa: E402
import sf_notify             # noqa: E402
import sf_update             # noqa: E402
from tests._base import SFCase, wait_for   # noqa: E402

try:
    from nacl.signing import SigningKey
except Exception:            # pragma: no cover - 缺依赖时整类跳过
    SigningKey = None


# ==========================================================================
# 一、签名清单
# ==========================================================================

@unittest.skipIf(SigningKey is None, "本机无 PyNaCl")
class TestManifestVerification(SFCase):
    """`verify_manifest` 是"任何下载地址在验签前不得暴露给用户"的唯一执行者。"""

    def setUp(self):
        super().setUp()
        self.key = SigningKey.generate()
        self.pub = base64.b64encode(self.key.verify_key.encode()).decode()

    def _manifest(self, drop=(), **override):
        m = {
            "schema": 1,
            "channel": "stable",
            "bundle_id": sf_constants.BUNDLE_ID,
            "app_name": sf_update._APP_NAME,
            "version": "9.9.9",
            "notes": "本次修了什么",
            "stage": "stable",
            "archive": {
                "url": "https://github.com/owner/repo/releases/download/v9.9.9/app.zip",
                "size": 12345,
                "sha256": "a" * 64,
            },
            "signature": {"algorithm": "ed25519", "value": ""},
        }
        m.update(override)
        for key in drop:
            m.pop(key, None)
        m["signature"]["value"] = base64.b64encode(
            self.key.sign(sf_update._canonical_payload(m)).signature).decode()
        return m

    def _verify(self, manifest):
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        with mock.patch.object(sf_update, "UPDATE_PUBLIC_KEY_B64", self.pub):
            return sf_update.verify_manifest(raw)

    def test_a_correctly_signed_manifest_yields_full_info(self):
        info = self._verify(self._manifest(stage="beta"))
        self.assertEqual(info.version, "9.9.9")
        self.assertEqual(info.notes, "本次修了什么")
        self.assertEqual(info.archive_size, 12345)
        self.assertEqual(info.archive_sha256, "a" * 64)
        self.assertEqual(info.stage, "beta")
        self.assertIn("github.com", info.archive_url)

    def test_signature_covers_the_payload_not_just_the_url(self):
        """改任何一个字段（大小/摘要/说明）都必须让验签失败。"""
        for field, value in (("version", "9.9.8"),
                             ("notes", "被改过的说明"),):
            m = self._manifest()
            m[field] = value
            with self.assertRaises(sf_update.UpdateError, msg=field):
                self._verify(m)
        m = self._manifest()
        m["archive"]["size"] = 99999
        with self.assertRaises(sf_update.UpdateError):
            self._verify(m)

    def test_size_and_shape_guards_run_before_any_download_is_trusted(self):
        for bad in (b"", b"x" * (sf_constants.UPDATE_MANIFEST_MAX_BYTES + 1),
                    "不是 bytes", b"\xff\xfe"):
            with self.assertRaises(sf_update.UpdateError, msg=repr(bad)[:30]):
                sf_update.verify_manifest(bad)

    def test_non_json_and_non_object_bodies_are_refused(self):
        with mock.patch.object(sf_update, "UPDATE_PUBLIC_KEY_B64", self.pub):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.verify_manifest(b"{not json")
            with self.assertRaises(sf_update.UpdateError):
                sf_update.verify_manifest(b"[1,2,3]")

    def test_channel_schema_and_identity_mismatches_are_refused(self):
        for kw in ({"schema": 2}, {"channel": "beta"},
                   {"bundle_id": "com.other"}, {"app_name": "别的.app"}):
            with self.assertRaises(sf_update.UpdateError, msg=str(kw)):
                self._verify(self._manifest(**kw))

    def test_a_missing_or_foreign_signature_algorithm_is_refused(self):
        m = self._manifest()
        m["signature"] = {"algorithm": "rsa", "value": "x"}
        with self.assertRaises(sf_update.UpdateError):
            self._verify(m)
        m2 = self._manifest()
        m2.pop("signature")
        with self.assertRaises(sf_update.UpdateError):
            self._verify(m2)

    def test_garbage_base64_in_the_signature_cannot_crash_the_verifier(self):
        m = self._manifest()
        m["signature"]["value"] = "这不是 base64 !!!"
        with self.assertRaises(sf_update.UpdateError):
            self._verify(m)

    def test_archive_fields_are_validated_after_the_signature(self):
        for archive in ({"url": "https://github.com/a.zip", "size": 0,
                         "sha256": "a" * 64},
                        {"url": "https://github.com/a.zip", "size": 10,
                         "sha256": "zz" * 32},
                        {"url": "https://evil.example/a.zip", "size": 10,
                         "sha256": "a" * 64},
                        {"url": "http://github.com/a.zip", "size": 10,
                         "sha256": "a" * 64},
                        {"url": "https://github.com/a.zip", "size": 10 ** 30,
                         "sha256": "a" * 64}):
            m = self._manifest(archive=archive)
            with self.assertRaises(sf_update.UpdateError, msg=str(archive)):
                self._verify(m)

    def test_untrusted_download_hosts_are_refused_even_when_signed(self):
        for url in ("https://attacker.example/a.zip",
                    "https://github.com.evil.example/a.zip",
                    "https://user:pw@github.com/a.zip",
                    "https://github.com/a.zip#frag"):
            m = self._manifest(archive={"url": url, "size": 10,
                                        "sha256": "a" * 64})
            with self.assertRaises(sf_update.UpdateError, msg=url):
                self._verify(m)

    def test_every_official_github_asset_domain_is_accepted(self):
        for host in sf_update._ALLOWED_HOSTS:
            m = self._manifest(archive={"url": "https://%s/a.zip" % host,
                                        "size": 10, "sha256": "a" * 64})
            self.assertEqual(self._verify(m).archive_url,
                             "https://%s/a.zip" % host, host)

    def test_version_and_notes_shape_is_validated(self):
        for bad_version in ("9.9", "9.9.9.1", "v9.9.9", "9.9.9-beta", 9.9, None):
            m = self._manifest(version=bad_version)
            with self.assertRaises(sf_update.UpdateError, msg=repr(bad_version)):
                self._verify(m)
        for bad_notes in ("x" * 4001, 42, None):
            m = self._manifest(notes=bad_notes)
            with self.assertRaises(sf_update.UpdateError, msg=repr(bad_notes)[:20]):
                self._verify(m)
        # 缺失 notes 是合法的（旧清单就没这个字段）
        self.assertEqual(self._verify(self._manifest(drop=("notes",))).notes, "")

    def test_a_forged_stage_falls_back_to_stable_without_changing_the_flow(self):
        """stage 只影响文案；伪造/缺失都不得改变"照常更新"这件事。"""
        self.assertEqual(self._verify(self._manifest(stage="nightly")).stage,
                         "stable")
        self.assertEqual(self._verify(self._manifest(drop=("stage",))).stage,
                         "stable", "不含该字段的旧清单要照常更新")
        self.assertEqual(self._verify(self._manifest(stage="beta")).stage, "beta")


class TestVersionHelpers(SFCase):
    def test_only_three_numeric_segments_are_accepted(self):
        self.assertEqual(sf_update.parse_version("1.4.32"), (1, 4, 32))
        for bad in ("1.4", "1.4.32.1", "v1.4.32", "1.4.x", "", None, "01.4.32"):
            with self.assertRaises(sf_update.UpdateError, msg=repr(bad)):
                sf_update.parse_version(bad)

    def test_is_newer_compares_numerically_not_lexically(self):
        self.assertTrue(sf_update.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(sf_update.is_newer("1.9.9", "1.10.0"))
        self.assertFalse(sf_update.is_newer("1.9.9", "1.9.9"))

    def test_headline_marks_beta_but_keeps_the_stable_wording(self):
        self.assertEqual(sf_update.offer_headline("1.5.0", "beta"),
                         "发现新 Beta 版本 v1.5.0。")
        self.assertEqual(sf_update.offer_headline("1.5.0", "stable"),
                         "发现新版本 v1.5.0。")


# ==========================================================================
# 二、受限下载与分片
# ==========================================================================

class TestRangePlanning(SFCase):
    def test_ranges_cover_the_whole_file_without_gaps_or_overlap(self):
        ranges = sf_update._download_ranges(1000, parts=16)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 999)
        for (s1, e1), (s2, _) in zip(ranges, ranges[1:]):
            self.assertEqual(s2, e1 + 1, "分片之间必须严丝合缝")

    def test_more_parts_than_bytes_does_not_produce_illegal_ranges(self):
        """极小测试包不能生成 0--1 这类非法 Range。"""
        ranges = sf_update._download_ranges(3, parts=16)
        self.assertEqual(len(ranges), 3)
        self.assertTrue(all(e >= s for s, e in ranges), ranges)
        self.assertEqual(sum(e - s + 1 for s, e in ranges), 3)

    def test_size_must_be_a_positive_int(self):
        for bad in (0, -1, None, "1000", 1.5):
            with self.assertRaises(sf_update.UpdateError, msg=repr(bad)):
                sf_update._download_ranges(bad)

    def test_division_remainder_is_spread_over_the_first_parts(self):
        ranges = sf_update._download_ranges(10, parts=3)
        self.assertEqual([e - s + 1 for s, e in ranges], [4, 3, 3])


class TestLimitedOpen(SFCase):
    """`_open_limited`：初始地址与重定向终点都必须在白名单内，且长度有上限。"""

    def _resp(self, url, length=None, status=200):
        r = mock.Mock()
        r.geturl.return_value = url
        headers = {}
        if length is not None:
            headers["Content-Length"] = str(length)
        r.headers.get.side_effect = lambda k, d=None: headers.get(k, d)
        return r

    def test_untrusted_start_url_is_refused_before_any_request(self):
        with mock.patch("sf_update.safe_urlopen") as op:
            with self.assertRaises(sf_update.UpdateError):
                sf_update._open_limited("https://evil.example/a.zip", 100, 5)
        self.assertEqual(op.call_count, 0, "不受信任的地址连请求都不该发出去")

    def test_redirect_to_another_host_is_closed_and_refused(self):
        resp = self._resp("https://evil.example/payload.zip")
        with mock.patch("sf_update.safe_urlopen", return_value=resp):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._open_limited(
                    "https://github.com/o/r/a.zip", 100, 5)
        self.assertIn("重定向不安全", str(cm.exception))
        resp.close.assert_called_once()

    def test_credentials_on_the_final_url_are_refused(self):
        resp = self._resp("https://user:pw@github.com/a.zip")
        with mock.patch("sf_update.safe_urlopen", return_value=resp):
            with self.assertRaises(sf_update.UpdateError):
                sf_update._open_limited("https://github.com/o/r/a.zip", 100, 5)

    def test_declared_size_over_the_cap_is_refused(self):
        resp = self._resp("https://github.com/a.zip", length=10 ** 12)
        with mock.patch("sf_update.safe_urlopen", return_value=resp):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._open_limited("https://github.com/o/r/a.zip", 100, 5)
        self.assertIn("安全上限", str(cm.exception))
        resp.close.assert_called_once()

    def test_a_non_numeric_content_length_is_refused(self):
        resp = self._resp("https://github.com/a.zip", length="lots")
        with mock.patch("sf_update.safe_urlopen", return_value=resp):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._open_limited("https://github.com/o/r/a.zip", 100, 5)
        self.assertIn("长度无效", str(cm.exception))

    def test_connection_failure_becomes_a_user_readable_error(self):
        import urllib.error
        with mock.patch("sf_update.safe_urlopen",
                        side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._open_limited("https://github.com/o/r/a.zip", 100, 5)
        self.assertIn("无法连接更新服务", str(cm.exception))

    def test_unicode_path_is_percent_encoded_only_for_the_request(self):
        """GitHub 标签可用中文；验签用原文，发请求才百分号编码。"""
        url = "https://github.com/o/r/releases/download/v1.4.31-测试/app.zip"
        req = sf_update._request_url(url)
        self.assertNotIn("测试", req)
        self.assertIn("%E6%B5%8B%E8%AF%95", req)
        captured = {}

        def fake(request, timeout=None):
            captured["path"] = request.full_url
            return self._resp(url)

        with mock.patch("sf_update.safe_urlopen", side_effect=fake):
            sf_update._open_limited(url, 100, 5)
        self.assertIn("%E6%B5%8B%E8%AF%95", captured["path"])

    def test_an_already_escaped_url_is_not_double_escaped(self):
        url = "https://github.com/o/r/a%20b.zip?x=1%2F2"
        self.assertEqual(sf_update._request_url(url), url)


class TestDownloadRangeResume(SFCase):
    """分片下载：**读取中断**也要从断点续传，否则一次坏连接报废整轮并行。"""

    def _resp(self, chunks, start, end, status=206):
        class _R:
            def __init__(self):
                self.status = status
                self.headers = {"Content-Range": "bytes %d-%d/%d" % (start, end, end + 1)}
                self._i = 0
                self.closed = False

            def geturl(self):
                return "https://release-assets.githubusercontent.com/a.zip"

            def read(self, n):
                if self._i >= len(chunks):
                    raise OSError("connection reset mid-body")
                self._i += 1
                return chunks[self._i - 1]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.closed = True
                return False

        return _R()

    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.part = os.path.join(self.d, "p0")

    def _make_part(self, size):
        with open(self.part, "wb") as f:
            f.write(b"\x00" * size)

    def test_mid_body_interruption_resumes_from_the_offset(self):
        self._make_part(9)
        sent = []
        seq = [self._resp([b"abc"], 0, 8), self._resp([b"def", b"ghi"], 3, 8)]

        def fake(request, timeout=None):
            sent.append(request.headers.get("Range"))
            return seq.pop(0)

        with mock.patch("sf_update.safe_urlopen", side_effect=fake):
            sf_update._download_range("https://github.com/a.zip", 0, 8,
                                      self.part, None, 5, attempts=2)
        self.assertEqual(sent, ["bytes=0-8", "bytes=3-8"],
                         "第二次必须从断点 3 续传，而不是从头再来")
        with open(self.part, "rb") as f:
            self.assertEqual(f.read(), b"abcdefghi")

    def test_connection_refusal_is_retried_then_reported(self):
        self._make_part(3)
        attempts = []

        def fake(request, timeout=None):
            attempts.append(1)
            raise OSError("refused")

        with mock.patch("sf_update.safe_urlopen", side_effect=fake):
            with self.assertRaises(sf_update.UpdateError):
                sf_update._download_range("https://github.com/a.zip", 0, 2,
                                          self.part, None, 5, attempts=3)
        self.assertEqual(len(attempts), 3)

    def test_a_server_that_ignores_range_requests_is_refused(self):
        self._make_part(3)
        resp = self._resp([b"abc"], 0, 2, status=200)
        with mock.patch("sf_update.safe_urlopen", return_value=resp):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._download_range("https://github.com/a.zip", 0, 2,
                                          self.part, None, 5)
        self.assertIn("不支持分段下载", str(cm.exception))

    def test_a_mismatched_content_range_header_is_refused(self):
        self._make_part(3)
        resp = self._resp([b"abc"], 0, 2)
        resp.headers = {"Content-Range": "bytes 500-502/999"}
        with mock.patch("sf_update.safe_urlopen", return_value=resp):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._download_range("https://github.com/a.zip", 0, 2,
                                          self.part, None, 5)
        self.assertIn("响应无效", str(cm.exception))

    def test_cancel_is_not_a_failure_and_stops_at_once(self):
        self._make_part(3)
        ev = threading.Event()
        ev.set()
        with mock.patch("sf_update.safe_urlopen") as op:
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update._download_range("https://github.com/a.zip", 0, 2,
                                          self.part, ev, 5)
        self.assertIn("下载已取消", str(cm.exception))
        self.assertEqual(op.call_count, 0, "取消后不该再发一次请求")

    def test_progress_callback_sees_the_resumed_offset(self):
        self._make_part(9)
        seen = []
        seq = [self._resp([b"abc"], 0, 8), self._resp([b"def", b"ghi"], 3, 8)]
        with mock.patch("sf_update.safe_urlopen", side_effect=lambda r, timeout=None: seq.pop(0)):
            sf_update._download_range("https://github.com/a.zip", 0, 8, self.part,
                                      None, 5,
                                      progress_callback=lambda n: seen.append(n),
                                      attempts=2)
        self.assertTrue(seen, "进度回调必须被喂过")
        self.assertEqual(sum(seen), 9, "两次尝试合计把 9 字节写完")


class TestFileVerification(SFCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()

    def _file(self, data):
        p = __import__("pathlib").Path(self.d, "f.bin")
        p.write_bytes(data)
        return p

    def test_size_and_digest_both_have_to_match(self):
        data = b"x" * 100
        p = self._file(data)
        good = hashlib.sha256(data).hexdigest()
        self.assertTrue(sf_update._file_matches(p, 100, good))
        self.assertFalse(sf_update._file_matches(p, 99, good), "大小不符即失败")
        self.assertFalse(sf_update._file_matches(p, 100, "0" * 64), "摘要不符即失败")

    def test_a_missing_file_is_not_a_crash(self):
        self.assertFalse(sf_update._file_matches(
            __import__("pathlib").Path(self.d, "nope"), 1, "0" * 64))


# ==========================================================================
# 三、安装交接
# ==========================================================================

class TestInstallerHandoff(SFCase):
    def test_install_result_file_is_consumed_once_and_deleted(self):
        d = __import__("pathlib").Path(tempfile.mkdtemp())
        result = d / "update-result.json"
        body = {"status": "installed", "message": "已装好"}
        with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(d)):
            result.write_text(json.dumps(body), encoding="utf-8")
            got = sf_update.consume_install_result()
        self.assertEqual(got, body)
        self.assertFalse(result.exists(), "结果文件读完即删，否则下次启动会重放旧结果")

    def test_a_half_written_result_file_is_reported_as_nothing(self):
        d = __import__("pathlib").Path(tempfile.mkdtemp())
        with mock.patch.object(sf_update, "UPDATE_STAGING_DIR", str(d)):
            (d / "update-result.json").write_text("{半截", encoding="utf-8")
            self.assertIsNone(sf_update.consume_install_result())
            (d / "update-result.json").write_text(
                json.dumps({"status": "weird", "message": 1}), encoding="utf-8")
            self.assertIsNone(sf_update.consume_install_result(),
                             "状态与消息都必须是合法形状")

    def setUp(self):
        super().setUp()
        # launch_installer 会真去 os.path.isdir(target_app)，所以"存在的应用目录"
        # 只能用临时目录冒充，写死的 /Applications/X.app 在本机根本不存在。
        self.app_dir = tempfile.mkdtemp(prefix="Fake.app")

    def test_installer_refuses_to_run_outside_the_applications_folder(self):
        """从 /tmp 跑核验产物时不该把包装进 /Applications。"""
        with mock.patch.object(sf_update, "installed_bundle_path",
                               return_value=None):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update.launch_installer("/tmp/a.zip", "0" * 64, "9.9.9")
        self.assertIn("正式应用", str(cm.exception))

    def test_installer_refuses_a_missing_helper(self):
        with mock.patch.object(sf_update, "installed_bundle_path",
                               return_value=self.app_dir), \
                mock.patch.object(sf_update, "bundled_helper_path",
                                  return_value="/nope/helper.sh"):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update.launch_installer("/tmp/a.zip", "0" * 64, "9.9.9")
        self.assertIn("更新助手不可用", str(cm.exception))

    def test_incomplete_signing_identity_config_is_refused(self):
        """team-id 与 cert-sha1 必须同时有或同时没有，半套配置等于没校验。"""
        with mock.patch.object(sf_update, "installed_bundle_path",
                               return_value=self.app_dir), \
                mock.patch.object(sf_update, "bundled_helper_path",
                                  return_value=__file__), \
                mock.patch.object(sf_update, "UPDATE_SIGNING_TEAM_ID", "TEAM"), \
                mock.patch.object(sf_update, "UPDATE_SIGNING_CERT_SHA1", ""), \
                mock.patch.object(sf_update, "UPDATE_STAGING_DIR",
                                  tempfile.mkdtemp()):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update.launch_installer("/tmp/a.zip", "0" * 64, "9.9.9")
        self.assertIn("签名身份配置不完整", str(cm.exception))

    def test_a_version_that_cannot_be_parsed_never_reaches_the_helper(self):
        with mock.patch.object(sf_update, "installed_bundle_path",
                               return_value=self.app_dir), \
                mock.patch.object(sf_update, "bundled_helper_path",
                                  return_value=__file__), \
                mock.patch.object(sf_update, "UPDATE_STAGING_DIR",
                                  tempfile.mkdtemp()):
            with self.assertRaises(sf_update.UpdateError):
                sf_update.launch_installer("/tmp/a.zip", "0" * 64, "9.9")

    def test_helper_is_copied_outside_the_app_and_marked_executable(self):
        """助手必须在 App 外：主程序随后 quit()，包内文件会被替换掉。"""
        import stat
        staging = tempfile.mkdtemp()
        helper = os.path.join(tempfile.mkdtemp(), "update_helper.sh")
        with open(helper, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho ok\n")
        cmd = []
        with mock.patch.object(sf_update, "installed_bundle_path",
                               return_value=self.app_dir), \
                mock.patch.object(sf_update, "bundled_helper_path",
                                  return_value=helper), \
                mock.patch.object(sf_update, "UPDATE_STAGING_DIR", staging), \
                mock.patch.object(sf_update.subprocess, "Popen",
                                  side_effect=lambda c, **k: cmd.append(c)):
            sf_update.launch_installer("/tmp/a.zip", "0" * 64, "9.9.9")
        self.assertEqual(len(cmd), 1)
        argv = cmd[0]
        self.assertEqual(argv[0], "/bin/sh")
        copied = argv[1]
        self.assertTrue(copied.startswith(staging), "必须在包外")
        self.assertIn("--target", argv)
        self.assertIn(self.app_dir, argv)
        self.assertEqual(stat.S_IMODE(os.stat(copied).st_mode), 0o700,
                         "只有当前用户可执行")
        self.assertNotIn(helper, copied, "不得就地执行包内那份")

    def test_a_helper_that_cannot_be_spawned_cleans_up_its_copy(self):
        staging = tempfile.mkdtemp()
        helper = os.path.join(tempfile.mkdtemp(), "update_helper.sh")
        with open(helper, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        with mock.patch.object(sf_update, "installed_bundle_path",
                               return_value=self.app_dir), \
                mock.patch.object(sf_update, "bundled_helper_path",
                                  return_value=helper), \
                mock.patch.object(sf_update, "UPDATE_STAGING_DIR", staging), \
                mock.patch.object(sf_update.subprocess, "Popen",
                                  side_effect=OSError("fork failed")):
            with self.assertRaises(sf_update.UpdateError) as cm:
                sf_update.launch_installer("/tmp/a.zip", "0" * 64, "9.9.9")
        self.assertIn("无法启动更新助手", str(cm.exception))
        self.assertEqual(os.listdir(staging), [], "启动失败要把副本删掉")

    def test_bundle_paths_are_derived_from_the_real_executable(self):
        with mock.patch.object(sys, "executable",
                               "/Applications/实时股价-目标价版.app/Contents/MacOS/X"):
            self.assertEqual(sf_update.installed_bundle_path(),
                             "/Applications/实时股价-目标价版.app")
        with mock.patch.object(sys, "executable", "/private/tmp/foo/app"):
            self.assertIsNone(sf_update.installed_bundle_path(),
                              "源码运行/临时目录运行都不算装在应用程序里")

    def test_helper_path_follows_the_frozen_or_source_layout(self):
        """冻结产物读 _MEIPASS，源码运行读工程目录，两条都要指到 updater/ 下。"""
        with mock.patch("sys._MEIPASS", "/tmp/_MEI123", create=True):
            self.assertEqual(sf_update.bundled_helper_path(),
                             "/tmp/_MEI123/updater/update_helper.sh")
        with mock.patch.object(sys, "_MEIPASS", None, create=True):
            self.assertTrue(sf_update.bundled_helper_path().endswith(
                os.path.join("updater", "update_helper.sh")))
            self.assertTrue(sf_update.bundled_helper_path().startswith(
                os.path.dirname(os.path.abspath(sf_update.__file__))))


# ==========================================================================
# 四、通知投递
# ==========================================================================

class NotifyCase(SFCase):
    """每个用例都从"未探测"的干净状态出发，并且绝不碰真实授权/真实弹窗。"""

    _NAMES = ("UNUserNotificationCenter", "UNMutableNotificationContent",
              "UNNotificationRequest", "NSObject", "NSString",
              "dispatch_get_main_queue", "dispatch_async",
              "_UN_UNAVAILABLE", "_UN_PROBED", "_DELEGATE")

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(sf_notify, n) for n in self._NAMES}
        self._saved_probe = sf_notify._USE_OSASCRIPT
        sf_notify.reset_delivery_probe()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(sf_notify, n, v)
        sf_notify._USE_OSASCRIPT = self._saved_probe
        sf_notify.reset_delivery_probe()
        super().tearDown()

    def _fake_un(self, auth_status=2, request_raises=None):
        """auth_status=2 是 authorized。不显式设的话 MagicMock 的 int() 是 1 = denied，
        deliver() 会在授权分支直接返回 "denied"，后面全是假绿。"""
        sf_notify.UNUserNotificationCenter = mock.MagicMock()
        sf_notify.UNUserNotificationCenter.currentNotificationCenter() \
            .authorizationStatus.return_value = auth_status
        sf_notify.UNMutableNotificationContent = mock.MagicMock()
        sf_notify.UNNotificationRequest = mock.MagicMock()
        sf_notify.NSString = mock.MagicMock()
        sf_notify.NSObject = object
        sf_notify.dispatch_get_main_queue = lambda: "main-queue"
        queued = []
        sf_notify.dispatch_async = lambda q, fn: queued.append(fn)
        sf_notify._UN_UNAVAILABLE = False
        sf_notify._UN_PROBED = True
        return queued


class TestUnAvailabilityProbing(NotifyCase):
    def test_import_failure_degrades_the_whole_un_route(self):
        """UN 装不上是"降级"，不是"用户拒绝"—— 两条路的后果完全不同。"""
        sf_notify._UN_UNAVAILABLE = False
        sf_notify._UN_PROBED = False
        sf_notify.UNUserNotificationCenter = None
        with mock.patch.dict(sys.modules, {"UserNotifications": None}):
            sf_notify._ensure_un()
            self.assertTrue(sf_notify._UN_UNAVAILABLE)
            self.assertEqual(sf_notify.authorization_status(), "denied")

    def test_probe_result_is_remembered(self):
        """探测只做一次：反复 import 框架会把首次通知的耗时放大。"""
        sf_notify._UN_PROBED = False
        sf_notify._UN_UNAVAILABLE = False
        sf_notify.UNUserNotificationCenter = None
        with mock.patch.dict(sys.modules, {"UserNotifications": None}):
            sf_notify._ensure_un()
            self.assertTrue(sf_notify._UN_PROBED)
            self.assertTrue(sf_notify._UN_UNAVAILABLE)
            sf_notify.UNUserNotificationCenter = None   # 模拟"名字被清空"
            sf_notify._ensure_un()
            self.assertIsNone(sf_notify.UNUserNotificationCenter,
                             "已判定不可用后不得再去 import 真框架")

    def test_a_stub_already_in_place_is_never_overwritten(self):
        """测试桩/调用方先把名字填好时必须视为就绪，否则桩会被真框架顶掉。"""
        sentinel = object()
        sf_notify.UNUserNotificationCenter = sentinel
        sf_notify._UN_PROBED = False
        sf_notify._UN_UNAVAILABLE = False
        sf_notify._ensure_un()
        self.assertIs(sf_notify.UNUserNotificationCenter, sentinel)
        self.assertFalse(sf_notify._UN_UNAVAILABLE)

    def test_forced_unavailable_is_not_reset_by_a_successful_probe(self):
        sf_notify._UN_UNAVAILABLE = True
        sf_notify._UN_PROBED = False
        sf_notify.UNUserNotificationCenter = object()
        sf_notify._ensure_un()
        self.assertTrue(sf_notify._UN_UNAVAILABLE,
                        "用例要强制走降级路线时不能被探测结果覆盖回去")


class TestUnRouteWithRealFramework(NotifyCase):
    """v1.4.57 第二枚事故：**不用替身**跑一遍 UN 路线。

    pyobjc 12 起 Foundation 不再导出 dispatch_get_main_queue / dispatch_async
    （本机实测 ImportError），而 sf_notify 里那句 import 是一体的 —— 一个名字取不到
    就 `_UN_UNAVAILABLE = True`：横幅改走 osascript（身份不再是本 app）、点击横幅
    恢复窗口与 on_click 回调一起失效，日志里只留一行 install error。
    原有替身用例把这两个名字做成桩，所以这条路线在真机器上整体坏死时它们照样绿 ——
    "替身把故障整体测不到"这个形状必须有一条真框架用例顶着（同 v1.4.49 会话形状事故）。
    """

    def setUp(self):
        super().setUp()
        try:
            import UserNotifications        # noqa: F401
        except Exception:                   # pragma: no cover
            self.skipTest("本机没有 UserNotifications 框架（非 macOS 或依赖缺失）")

    def _use_real_framework(self):
        """把模块状态清回"没探测过"，让 _ensure_un 真的去 import 框架。

        下面几处私有访问是必须的：`_UN_UNAVAILABLE` 本身就是"整条 UN 路线是否可用"
        的降级判据，模块没有对外读取口；不碰它就测不出"路线静默坏死"这件事。
        """
        for name in ("UNUserNotificationCenter", "UNMutableNotificationContent",
                     "UNNotificationRequest", "NSObject", "NSString",
                     "dispatch_get_main_queue", "dispatch_async"):
            setattr(sf_notify, name, None)
        sf_notify._UN_UNAVAILABLE = False      # discipline-ok(R5): 降级判据本身，无公开读取口
        sf_notify._UN_PROBED = False           # discipline-ok(R5): 同上，不重置就永远不会真 import
        sf_notify._DELEGATE = None             # discipline-ok(R5): 单例本体，装没装上只能读它
        sf_notify._ensure_un()                 # discipline-ok(R5): 延迟导入入口只有内部名

    def test_real_framework_probe_keeps_the_un_route_alive(self):
        self._use_real_framework()
        self.assertFalse(sf_notify._UN_UNAVAILABLE,             # discipline-ok(R5): 见 _use_real_framework
                         "真框架下 UN 路线被判不可用 —— 点击横幅恢复窗口整条消失")
        self.assertTrue(callable(sf_notify.dispatch_async),
                        "回主线程的投递函数没填上：_ensure_un 的 import 又漏名字了")
        queue = sf_notify.dispatch_get_main_queue()
        self.assertTrue(hasattr(queue, "addOperationWithBlock_"),
                        "dispatch_get_main_queue 必须给出主队列载体（NSOperationQueue）")
        sf_notify.dispatch_async(queue, lambda: None)   # 投递本身不许抛

    def test_delegate_installs_against_the_real_objc_runtime(self):
        """delegate 真装得上才算数：装不上时点击响应根本收不到，而旧代码只留一行 WARNING。"""
        self._use_real_framework()
        installed = []

        class _Center:
            def setDelegate_(self, delegate):
                installed.append(delegate)

        sf_notify._install_delegate(_Center())    # discipline-ok(R5): 装配点没有公开入口，只能直调
        self.assertEqual(len(installed), 1,
                         "delegate 没装上（旧事故：TypeError('NoneType takes no arguments')）")
        self.assertIsNotNone(sf_notify._DELEGATE,  # discipline-ok(R5): 模块级单例本体
                             "delegate 必须模块级持有，否则点击回调会被 GC 掉")


class TestAuthorizationStatus(NotifyCase):
    def test_each_status_code_maps_to_a_name(self):
        for code, expect in ((0, "notDetermined"), (1, "denied"),
                             (2, "authorized"), (3, "provisional"),
                             (4, "ephemeral"), (99, "unknown")):
            queued = self._fake_un()
            sf_notify.UNUserNotificationCenter.currentNotificationCenter() \
                .authorizationStatus.return_value = code
            self.assertEqual(sf_notify.authorization_status(), expect, code)

    def test_a_broken_center_reports_unknown_rather_than_raising(self):
        """通知侧任何异常都不能把行情 tick 带崩。"""
        queued = self._fake_un()
        sf_notify.UNUserNotificationCenter.currentNotificationCenter \
            .side_effect = RuntimeError("no bundle")
        self.assertEqual(sf_notify.authorization_status(), "unknown")

    def test_request_authorization_is_a_no_op_when_un_is_missing(self):
        sf_notify._UN_UNAVAILABLE = True
        sf_notify._UN_PROBED = True
        sf_notify.request_authorization()   # 只要求不抛

    def test_request_authorization_asks_for_alert_sound_badge(self):
        queued = self._fake_un()
        center = sf_notify.UNUserNotificationCenter.currentNotificationCenter()
        with mock.patch.object(sf_notify, "_install_delegate"):
            sf_notify.request_authorization()
        args = center.requestAuthorizationWithOptions_completionHandler_.call_args.args
        self.assertEqual(args[0], 7, "alert(1)|sound(2)|badge(4)")
        args[1](True, None)          # 回调本身也不能抛


class TestSemanticIdentity(SFCase):
    def test_title_prefers_the_name_then_the_code_then_a_placeholder(self):
        t = sf_notify.target_notification_title
        self.assertEqual(t("贵州茅台", "sh600519"), "贵州茅台 目标价")
        self.assertEqual(t("", "sh600519"), "sh600519 目标价")
        self.assertEqual(t(None, None), "股票 目标价")

    def test_identifier_survives_method_and_attribute_shapes(self):
        class _Req:
            def identifier(self):
                return "target-above-1500"

        class _Note:
            def request(self):
                return _Req()

        class _Resp:
            def notification(self):
                return _Note()

        self.assertEqual(sf_notify._notification_identifier(_Resp()),
                         "target-above-1500")

    def test_identifier_also_reads_plain_test_doubles(self):
        ns = types_namespace()
        self.assertEqual(sf_notify._notification_identifier(ns),
                         "target-above-1500")

    def test_identifier_tolerates_only_the_shape_difference(self):
        """口径：本函数只兜"方法 vs 属性"这一种差异，真异常由 delegate 收口。

        写成用例是为了让这个边界显式：给一个连 notification 都没有的对象会抛
        AttributeError，而那条调用路径整体包在 try/except 里（见 _install_delegate）。
        """
        with self.assertRaises(AttributeError):
            sf_notify._notification_identifier(object())


def types_namespace():
    class _Req:
        identifier = "target-above-1500"

    class _Note:
        request = _Req()

    class _Resp:
        notification = _Note()

    return _Resp()


class TestDeliverRouting(NotifyCase):
    def test_un_route_returns_un_and_registers_the_click_handler(self):
        self._fake_un()
        center = sf_notify.UNUserNotificationCenter.currentNotificationCenter()
        sf_notify._DELEGATE = object()          # 假装 delegate 已装好
        cb = lambda: None
        out = sf_notify.deliver("贵州茅台 目标价", "现价 1600", "已到 1500",
                                semantic_key="k1", on_click=cb)
        self.assertEqual(out, "un")
        self.assertIs(sf_notify._CLICK_HANDLERS["k1"], cb,
                      "点击回调要按语义键存进模块级表，否则多条横幅会串回调")
        center.addNotificationRequest_withCompletionHandler_.assert_called_once()

    def test_denied_authorization_wins_over_the_osascript_fallback(self):
        """R48：用户关掉通知后，即便 UN 投递失败过也不能绕过他的选择。"""
        self._fake_un()
        sf_notify._USE_OSASCRIPT = True
        center = sf_notify.UNUserNotificationCenter.currentNotificationCenter()
        center.authorizationStatus.return_value = 1        # denied
        with mock.patch.object(sf_notify, "_deliver_osascript") as osa:
            self.assertEqual(sf_notify.deliver("t", "s", "b"), "denied")
        self.assertEqual(osa.call_count, 0)

    def test_missing_framework_uses_osascript_even_when_status_says_denied(self):
        """UN 装不上时"denied"只是探测的副产物，不该让用户收不到任何横幅。"""
        sf_notify._UN_UNAVAILABLE = True
        sf_notify._UN_PROBED = True
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as osa:
            self.assertEqual(sf_notify.deliver("t", "s", "b"), "osascript")
        self.assertEqual(osa.call_count, 1)

    def test_a_delivery_error_permanently_switches_to_the_fallback(self):
        self._fake_un()
        center = sf_notify.UNUserNotificationCenter.currentNotificationCenter()

        def add(req, done):
            done("delivery failed")

        center.addNotificationRequest_withCompletionHandler_.side_effect = add
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as osa:
            self.assertEqual(sf_notify.deliver("t", "s", "b"), "un")
        self.assertTrue(sf_notify._USE_OSASCRIPT,
                        "投递失败要一次性永久降级，不做运行时反复横跳")
        self.assertEqual(osa.call_count, 1, "这一次也必须补发，否则用户的提醒永久漏掉")

    def test_the_same_key_is_never_replayed_by_two_delivery_errors(self):
        """同一标识只补发一次：两条一模一样的横幅比漏发更糟。"""
        self._fake_un()
        center = sf_notify.UNUserNotificationCenter.currentNotificationCenter()
        center.addNotificationRequest_withCompletionHandler_.side_effect = \
            lambda req, done: done("boom")
        with mock.patch.object(sf_notify, "_deliver_osascript",
                               return_value="osascript") as osa:
            sf_notify.deliver("t", "s", "b", semantic_key="dup")
            self.assertEqual(osa.call_count, 1)
            self.assertIn("dup", sf_notify._FALLBACK_KEYS)
            # 模拟"另一条在途请求也报错"：降级标志被复位后仍不该重复补发
            sf_notify._USE_OSASCRIPT = False
            sf_notify.deliver("t", "s", "b", semantic_key="dup")
            self.assertEqual(osa.call_count, 1,
                             "_FALLBACK_KEYS 必须记住已经补发过的键")
            self.assertEqual(len(sf_notify._FALLBACK_KEYS), 1)

    def test_click_handlers_are_bounded(self):
        self._fake_un()
        sf_notify._DELEGATE = object()
        for i in range(sf_notify._CLICK_HANDLERS_MAX + 40):
            sf_notify.deliver("t", "s", "b", semantic_key="k%d" % i,
                              on_click=lambda: None)
        self.assertLessEqual(len(sf_notify._CLICK_HANDLERS),
                             sf_notify._CLICK_HANDLERS_MAX,
                             "identifier 带日期与价位，不封顶就是长挂机内存泄漏")

    def test_no_click_handler_is_registered_when_the_delegate_is_missing(self):
        """delegate 装不上 = 回调永远收不到，注册只会堆死条目。"""
        self._fake_un()
        sf_notify._DELEGATE = None
        sf_notify.deliver("t", "s", "b", semantic_key="orphan",
                          on_click=lambda: None)
        self.assertNotIn("orphan", sf_notify._CLICK_HANDLERS)

    def test_subtitle_is_only_set_when_present(self):
        self._fake_un()
        sf_notify._DELEGATE = None
        content = sf_notify.UNMutableNotificationContent.alloc.return_value.init.return_value
        sf_notify.deliver("标题", "", "正文")
        content.setSubtitle_.assert_not_called()
        sf_notify.deliver("标题", "副标题", "正文")
        content.setSubtitle_.assert_called_once()
        content.setSound_.assert_called_with(None)


class TestOsascriptFallback(SFCase):
    def test_quotes_are_neutralised_so_the_script_cannot_break(self):
        """正文里的双引号会改写 AppleScript，最坏变成脚本注入。"""
        with mock.patch.object(sf_notify.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stderr=b"")
            out = sf_notify._deliver_osascript('a"b', "s", 'c"d')
        self.assertEqual(out, "osascript")
        self.assertTrue(wait_for(lambda: run.called, timeout=3.0,
                                 msg="降级投递必须在后台线程跑，不能卡 UI"))
        script = run.call_args.args[0][-1]
        self.assertNotIn('"a\'b"', script)
        self.assertIn("display notification", script)
        self.assertIn("c'd", script)

    def test_a_failing_helper_is_logged_not_raised(self):
        with mock.patch.object(sf_notify.subprocess, "run",
                               return_value=mock.Mock(returncode=1,
                                                      stderr=b"no display")):
            self.assertEqual(sf_notify._deliver_osascript("t", "", "b"),
                             "osascript")
        with mock.patch.object(sf_notify.subprocess, "run",
                               side_effect=OSError("no osascript")):
            self.assertEqual(sf_notify._deliver_osascript("t", "", "b"),
                             "osascript")

    def test_thread_start_failure_reports_error(self):
        with mock.patch.object(sf_notify.threading, "Thread",
                               side_effect=RuntimeError("no threads")):
            self.assertEqual(sf_notify._deliver_osascript("t", "", "b"), "error")

    def test_the_helper_call_never_blocks_the_caller(self):
        """deliver() 由行情 tick 在 UI 线程调用：同步 subprocess 会明显卡界面。"""
        gate = threading.Event()
        with mock.patch.object(sf_notify.subprocess, "run",
                               side_effect=lambda *a, **k: gate.wait(3.0)):
            t0 = __import__("time").monotonic()
            sf_notify._deliver_osascript("t", "s", "b")
            self.assertLess(__import__("time").monotonic() - t0, 0.5)
            gate.set()


if __name__ == "__main__":
    unittest.main()
