"""测试期间不许真联网：最底层 HTTPS 建连的守卫，以及运行器对它的接线。

为什么要有这道守卫：会真建主窗的测试文件有 55 个，窗口首次变活跃时“切回补查更新”会起后台线程去连
线上清单，而夹具只替换了行情请求（探针实测：聚焦跑一个模块就有 1 次检查、2 次域名解析）。
守卫放在最底层真正要开 socket 的那一步，打了 `safe_urlopen` 替身的用例照常走替身，只有漏了替身的路径被拦。
"""
import http.client
import os
import pathlib
import socket
import sys
import types
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase
except ImportError:  # pragma: no cover - 包模式导入
    from tests._base import SFCase

import sf_feedback_github
import sf_feedback_transport
import sf_http
import sf_network
import sf_update
import test_runner as runner

GUARD = sf_http.TEST_REJECT_NETWORK_ENV
PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


def _env(**values):
    """干净环境：去掉代理变量（避免 ProxyHandler 改道），守卫变量按需设置。"""
    env = dict(os.environ)
    for name in PROXY_VARS + (GUARD,):
        env.pop(name, None)
    env.update(values)
    return mock.patch.dict(os.environ, env, clear=True)


class GuardAtTheLowestLayer(SFCase):
    def test_guard_refuses_before_any_name_lookup(self):
        """开着守卫时，真实建连在解析域名之前就失败：一个包都出不去。"""
        with _env(**{GUARD: "1"}), mock.patch.object(socket, "getaddrinfo") as lookup:
            with self.assertRaisesRegex(OSError, "network disabled"):
                sf_http.safe_urlopen("https://example.com/manifest", timeout=1)
        lookup.assert_not_called()

    def test_guard_off_keeps_the_real_handler_path(self):
        """没设守卫（装机版、发版流程）时不能改变任何行为：请求照常交给原处理器。"""
        fake = types.SimpleNamespace(code=200, msg="OK", info=lambda: {})
        with _env(), mock.patch.object(urllib.request.HTTPSHandler, "https_open",
                                       return_value=fake) as real:
            response = sf_http.safe_urlopen("https://example.com/manifest", timeout=1)
        self.assertIs(response, fake)
        real.assert_called_once()

    def test_a_value_other_than_one_does_not_switch_it_on(self):
        fake = types.SimpleNamespace(code=200, msg="OK", info=lambda: {})
        with _env(**{GUARD: "0"}), mock.patch.object(urllib.request.HTTPSHandler, "https_open",
                                                     return_value=fake) as real:
            sf_http.safe_urlopen("https://example.com/manifest", timeout=1)
        real.assert_called_once()

    def test_update_check_cannot_reach_the_network_under_the_guard(self):
        """真正漏过的那条路：更新检查走 safe_urlopen，开着守卫时只会得到“无法连接”，且不触发域名解析。"""
        with _env(**{GUARD: "1"}), mock.patch.object(socket, "getaddrinfo") as lookup, \
                mock.patch.object(sf_update, "_record_manifest"):
            with self.assertRaises(sf_update.UpdateError) as caught:
                sf_update.check_for_update("0.0.1", attempts=1)
        lookup.assert_not_called()
        # 失败原因必须是守卫，而不是别的什么原因恰好让它失败（顺着 __cause__ 找到守卫的报错）。
        causes, current = [], caught.exception
        while current is not None:
            causes.append(str(current))
            current = current.__cause__
        self.assertTrue(any("network disabled" in text for text in causes), causes)


class _OnePool:
    """只装一条连接的池替身；记录被丢弃与归还的次数。"""

    def __init__(self, conn):
        self.conn = conn
        self.discarded = 0
        self.released = 0

    def get_connection(self, host, timeout=None):
        return self.conn

    def discard(self, host, conn):
        self.discarded += 1

    def release_connection(self, host, conn):
        self.released += 1


class _FakeResponse:
    status = 200
    headers = {}

    def read1(self, size):
        return b""

    def read(self, size=-1):
        return b""


class _FakeConn:
    """不是 HTTPConnection：模拟用例里的替身连接。"""

    def __init__(self):
        self.requests = 0

    def request(self, method, path, headers=None):
        self.requests += 1

    def getresponse(self):
        return _FakeResponse()


class GuardOnThePooledPath(SFCase):
    """行情/日历走的连接池路径：真连接被拦，替身连接照常。"""

    def test_real_pooled_connection_is_refused_without_lookup_or_retry(self):
        real = http.client.HTTPSConnection("example.com", timeout=1)
        pool = _OnePool(real)
        with _env(**{GUARD: "1"}), mock.patch.object(sf_network, "_NET_POOL", pool), \
                mock.patch.object(socket, "getaddrinfo") as lookup, \
                mock.patch.object(sf_network, "_interruptible_sleep") as nap:
            with self.assertRaisesRegex(OSError, "network disabled"):
                sf_network._http_get_pooled("https://example.com/a", retries=3)  # discipline-ok(R5): 守卫就长在连接池这一步里，经公开 http_get 会先被别的分支挡住，量不到它
        lookup.assert_not_called()
        nap.assert_not_called()          # 策略类错误直接上抛，不为必败的请求睡着重试
        self.assertEqual(pool.discarded, 1)

    def test_fake_connection_still_reaches_its_own_double(self):
        fake = _FakeConn()
        pool = _OnePool(fake)
        with _env(**{GUARD: "1"}), mock.patch.object(sf_network, "_NET_POOL", pool):
            sf_network._http_get_pooled("https://example.com/a", retries=0)  # discipline-ok(R5): 要证明“替身连接在守卫开着时仍到得了自己的替身”，必须直达连接池这一步
        self.assertEqual(fake.requests, 1, "打了替身的用例不能被守卫挡在替身之外")
        self.assertEqual(pool.released, 1)

    def test_pool_rejection_still_falls_back_to_a_stubbed_urllib_path(self):
        """老用例只替换了 safe_urlopen：连接池被拒后必须仍能回退到那个替身，而不是被策略错误直接截断。"""
        real = http.client.HTTPSConnection("example.com", timeout=1)
        pool = _OnePool(real)
        opened = []

        class _Resp:
            status = 200
            headers = {}

            def read1(self, size):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=4):
            opened.append(request.full_url)
            return _Resp()

        with _env(**{GUARD: "1"}), mock.patch.object(sf_network, "_NET_POOL", pool), \
                mock.patch.object(sf_network, "safe_urlopen", fake_urlopen):
            body = sf_network.http_get("https://example.com/a", retries=0)
        self.assertEqual(body, "")
        self.assertEqual(opened, ["https://example.com/a"], "回退路径没有走到 urllib 的替身")
        # 必须真的先走过连接池再回退：连接池特性关掉时 http_get 直接走 urllib，上面两条照样绿，
        # 这条用例就在守“池被拒 → 回退”这一段而不是别的东西。
        self.assertEqual(pool.discarded, 1, "没有经过连接池路径，这条用例没在测它该测的东西")

    def test_real_pooled_connection_is_untouched_when_guard_is_off(self):
        real = http.client.HTTPSConnection("example.com", timeout=1)
        pool = _OnePool(real)
        with _env(), mock.patch.object(sf_network, "_NET_POOL", pool), \
                mock.patch.object(http.client.HTTPSConnection, "request",
                                  side_effect=OSError("reached the transport")) as sent:
            with self.assertRaisesRegex(OSError, "reached the transport"):
                sf_network._http_get_pooled("https://example.com/a", retries=0)  # discipline-ok(R5): 对照组，与上面同一入口，只差守卫开关，用来证明拦的是守卫而不是别的
        sent.assert_called_once()

    def test_calendar_style_http_get_cannot_leave_the_machine(self):
        """日历线程走的 http_get（含连接池与 urllib 回退）整条路径都被挡在 socket 之前。"""
        with _env(**{GUARD: "1"}), mock.patch.object(socket, "getaddrinfo") as lookup:
            with self.assertRaises(OSError):
                sf_network.http_get("https://www.szse.cn/api/x", retries=0)
        lookup.assert_not_called()


class GuardOnFeedbackOpeners(SFCase):
    """反馈上传与后台 GitHub 管理 API 自建 opener，必须同样经过测试守卫。"""

    def test_feedback_opener_rejects_before_delegating_when_guarded(self):
        request = urllib.request.Request("https://feedback.invalid/v1/feedback")
        opener = urllib.request.build_opener(sf_feedback_transport.NoRedirect(),
                                            sf_feedback_transport.RejectRealConnectionUnderTest())
        with _env(**{GUARD: "1"}), mock.patch.object(urllib.request.HTTPSHandler, "https_open") as real:
            with self.assertRaisesRegex(OSError, "network disabled"):
                opener.open(request, timeout=1)
        real.assert_not_called()

    def test_feedback_opener_delegates_when_guard_is_off(self):
        request = urllib.request.Request("https://feedback.invalid/v1/feedback")
        fake = types.SimpleNamespace(code=200, msg="OK", info=lambda: {})
        opener = urllib.request.build_opener(sf_feedback_transport.NoRedirect(),
                                            sf_feedback_transport.RejectRealConnectionUnderTest())
        with _env(), mock.patch.object(urllib.request.HTTPSHandler, "https_open",
                                       return_value=fake) as real:
            self.assertIs(opener.open(request, timeout=1), fake)
        real.assert_called_once()

    def test_real_feedback_upload_cannot_reach_the_network_under_the_guard(self):
        """用真实归档与信封走 submit_https：开着守卫时在域名解析之前失败。"""
        import tempfile
        from pathlib import Path
        from sf_feedback import build_archive, make_envelope
        from sf_feedback_capture import CONSENT_VERSION, package_files
        from tests.test_feedback_pipeline import snapshot

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid.zip"
            state = snapshot()
            build_archive(path, package_files(state, "synthetic only"),
                          app_version="1.4.96", bundle_id="com.skyrocket.app", run_id="abcdef",
                          omitted=state["omitted"], truncated=state["truncated"],
                          capture_started_at=state["capture_started_at"],
                          capture_completed_at=state["capture_completed_at"])
            envelope = make_envelope(archive_path=path, app_version="1.4.96",
                                     outbox_id="a" * 32, consent_policy_version=CONSENT_VERSION)
            with _env(**{GUARD: "1"}), mock.patch.object(socket, "getaddrinfo") as lookup:
                with self.assertRaisesRegex(OSError, "network disabled"):
                    sf_feedback_transport.submit_https("https://feedback.invalid/v1/feedback",
                                                       envelope, path)
        lookup.assert_not_called()

    def test_github_request_builds_an_opener_with_the_guard_handler(self):
        api = sf_feedback_github.GitHubAPI("synthetic-token")
        opener = mock.Mock()
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = b"{}"
        opener.open.return_value = response
        with _env(), mock.patch.object(sf_feedback_github, "build_opener", return_value=opener) as built:
            api.request("case")
        self.assertIsInstance(built.call_args.args[1], sf_feedback_transport.RejectRealConnectionUnderTest)


class RunnerWiring(SFCase):
    def test_runner_and_product_agree_on_the_variable_name(self):
        self.assertEqual(runner.NETWORK_GUARD_ENV, sf_http.TEST_REJECT_NETWORK_ENV)
        self.assertEqual(sf_feedback_transport.TEST_REJECT_NETWORK_ENV, sf_http.TEST_REJECT_NETWORK_ENV)
        # 三处只互相比较，同步改名不会红；钉住字面量，改名就得同时改 AGENTS.md 里那段说明。
        self.assertEqual(sf_http.TEST_REJECT_NETWORK_ENV, "SF_TEST_REJECT_NETWORK")

    def test_context_sets_and_restores_when_unset(self):
        with _env():
            with runner.network_guard_environment():
                self.assertEqual(os.environ.get(GUARD), "1")
            self.assertNotIn(GUARD, os.environ)

    def test_context_restores_a_previous_value(self):
        with _env(**{GUARD: "0"}):
            with runner.network_guard_environment():
                self.assertEqual(os.environ.get(GUARD), "1")
            self.assertEqual(os.environ.get(GUARD), "0")

    def test_context_restores_after_an_exception(self):
        with _env():
            with self.assertRaises(RuntimeError):
                with runner.network_guard_environment():
                    raise RuntimeError("boom")
            self.assertNotIn(GUARD, os.environ)

    def _seen_by_run_tests(self, argv):
        seen = []
        with _env(), mock.patch.object(runner, "run_tests",
                                       side_effect=lambda *a, **k: seen.append(os.environ.get(GUARD)) or 0):
            code = runner.main(argv)
            after = os.environ.get(GUARD)
        self.assertEqual(code, 0)
        self.assertIsNone(after, "运行器退出后必须还原环境")
        return seen

    def test_focused_module_run_is_guarded(self):
        self.assertEqual(self._seen_by_run_tests(["--module", "test_core"]), ["1"])

    def test_full_run_is_guarded(self):
        self.assertEqual(self._seen_by_run_tests([]), ["1"])
