# -*- coding: utf-8 -*-
"""对外发布脚本的成功主流程端到端回归（本地假 GitHub，绝不碰真实 Release）。

背景（v1.4.17）：`tools/publish_update_release.py` 原先只测了"该拒绝时有没有拒绝"，
成功主流程（上传 ZIP → 复验 → 切 raw 清单指针 → 换旧客户端清单）从未被跑过。
而这条链是**每次 `./build_release.sh` 第 6 步自动执行**的：顺序一错就是"清单指向
一个还不存在的包"，线上用户点更新即失败 —— 最需要回归、又最容易被"反正每次都成功"
糊过去的一段。

做法：起一个本地 HTTP 服务器，按 GitHub Releases/Contents/Uploads API 的最小形状
应答，只把 `API_ROOT` / `UPLOAD_ROOT` 两个常量指过去；其余（`request()` 的真实
构造与解析、PUT/POST/DELETE 顺序、base64 载荷、大小复验、凭据头）全走真实代码路径。
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import hashlib
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

# 首次发布时清单 GET 必然 404，urllib 造的 HTTPError 内部挂着临时文件，回收时
# 会报 ResourceWarning。只屏蔽这一条消息：其余 ResourceWarning 照旧要看得见。
@contextlib.contextmanager
def _quiet_http_error_warnings():
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=ResourceWarning,
                                message="Implicitly cleaning up <HTTPError")
        yield

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ARCHIVE_NAME = "StockFloatTargetPrice-1.4.17-macos.zip"
ARCHIVE_BYTES = b"zip-bytes"


def _load_publish_tool():
    path = ROOT / "tools" / "publish_update_release.py"
    spec = importlib.util.spec_from_file_location("publish_update_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _State:
    """假服务器看到的"线上"状态。"""

    def __init__(self):
        self.release = {"id": 77, "assets": []}
        self.blobs = {}            # 资产名 -> 收到的字节
        self.contents = {}         # 清单路径 -> {"sha", "content"}
        self.log = []              # [(method, 路由)]，按到达顺序
        self.last_headers = {}     # 最近一次请求的请求头
        self.content_refs = []
        self.next_asset_id = 900
        self.drop_asset_after_upload = False    # 模拟"传上去但服务端查不到"
        self.corrupt_archive_after_upload = False
        self.corrupt_contents_readback = False
        self.corrupt_release_body_readback = False
        self.fail_asset_download = False
        self.manifest_name = "Skyrocket-update.json"
        self.base = ""

    def add(self, method, route):
        self.log.append((method, route))

    def routes(self):
        return [route for _, route in self.log]

    def asset_names(self):
        return [a["name"] for a in self.release["assets"]]


class _Handler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):            # 静音：测试输出不该夹 HTTP 访问日志
        pass

    @property
    def st(self):
        return type(self).state

    @property
    def tail(self):
        return urllib.parse.unquote(self.path)

    def _reply(self, code, payload=b""):
        self.send_response(code)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        if "/raw/" in self.tail:
            self.st.raw_headers = headers
            # 模拟公开 raw URL；它只读当前 Contents 内容，不走 authenticated API。
            entry = self.st.contents.get(self.st.manifest_name)
            if entry is None:
                return self._reply(404, b'{"message":"Not Found"}')
            return self._reply(200, entry["content"])
        self.st.last_headers = headers
        if "releases/tags/" in self.tail:
            self.st.add("GET", "release")
            release = dict(self.st.release)
            if self.st.corrupt_release_body_readback:
                release["body"] = "server-drift"
            return self._reply(200, json.dumps(release).encode())
        if "/contents/" in self.tail:
            self.st.add("GET", "contents")
            parsed = urllib.parse.urlsplit(self.path)
            ref = urllib.parse.parse_qs(parsed.query).get("ref")
            self.st.content_refs.append(ref)
            if ref != ["main"]:
                return self._reply(404, b'{"message":"manifest is not on the default branch"}')
            name = urllib.parse.unquote(parsed.path).rsplit("/contents/", 1)[1]
            entry = self.st.contents.get(name)
            if entry is None:
                return self._reply(404, b'{"message":"Not Found"}')
            content = entry["content"]
            if self.st.corrupt_contents_readback:
                content = b"X" + content[1:]
            return self._reply(200, json.dumps(
                {"sha": entry["sha"],
                 "content": base64.b64encode(content).decode()}).encode())
        match = re.search(r"/releases/assets/(\d+)$", self.tail)
        if match:
            asset_id = int(match.group(1))
            asset = next((item for item in self.st.release["assets"]
                          if item.get("id") == asset_id), None)
            if asset is None:
                return self._reply(404, b'{"message":"Not Found"}')
            if self.st.fail_asset_download:
                return self._reply(503, b'{"message":"temporary failure"}')
            return self._reply(200, self.st.blobs[asset["name"]])
        return self._reply(404, b'{"message":"Not Found"}')

    def do_POST(self):
        self.st.last_headers = {k.lower(): v for k, v in self.headers.items()}
        if "/assets" not in self.tail:
            return self._reply(405, b'{"message":"nope"}')
        self.st.add("POST", "upload")
        name = urllib.parse.parse_qs(self.tail.split("?", 1)[1]).get("name", [""])[0]
        body = self._body()
        self.st.blobs[name] = body
        if self.st.corrupt_archive_after_upload and name.endswith("-macos.zip"):
            self.st.blobs[name] = b"X" * len(body)
        asset_id = self.st.next_asset_id
        asset = {"id": asset_id, "name": name, "size": len(body),
                 "url": self.st.base + "/repos/test/releases/assets/%d" % asset_id}
        self.st.next_asset_id += 1
        if not self.st.drop_asset_after_upload:
            self.st.release["assets"].append(asset)
        return self._reply(201, json.dumps(asset).encode())

    def do_PATCH(self):
        self.st.last_headers = {k.lower(): v for k, v in self.headers.items()}
        if not re.search(r"/releases/\d+$", self.tail):
            return self._reply(405, b'{"message":"nope"}')
        self.st.add("PATCH", "release_body")
        self.st.release["body"] = json.loads(self._body()).get("body")
        return self._reply(200, json.dumps(self.st.release).encode())

    def do_PUT(self):
        self.st.last_headers = {k.lower(): v for k, v in self.headers.items()}
        if "/contents/" not in self.tail:
            return self._reply(405, b'{"message":"nope"}')
        self.st.add("PUT", "contents")
        payload = json.loads(self._body())
        name = self.tail.rsplit("/contents/", 1)[1]
        content = base64.b64decode(payload.get("content", ""))
        existing = self.st.contents.get(name)
        if existing is not None and payload.get("sha") != existing["sha"]:
            # 真实 GitHub 在缺 sha / sha 过期时拒绝覆盖，这里保持同等严格度
            return self._reply(409, b'{"message":"sha required"}')
        self.st.contents[name] = {"sha": "sha-%d" % len(content), "content": content}
        return self._reply(200, json.dumps(
            {"content": {"sha": self.st.contents[name]["sha"]}}).encode())

    def do_DELETE(self):
        self.st.last_headers = {k.lower(): v for k, v in self.headers.items()}
        match = re.search(r"/releases/assets/(\d+)$", self.tail)
        if not match:
            return self._reply(405, b'{"message":"nope"}')
        self.st.add("DELETE", "asset")
        asset_id = int(match.group(1))
        self.st.release["assets"] = [a for a in self.st.release["assets"]
                                     if a["id"] != asset_id]
        return self._reply(204)


class PublishEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_publish_tool()

    def setUp(self):
        self.state = _State()
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.state.base = base
        self.raw_root = base + "/raw"
        self.state.manifest_name = self.tool.MANIFEST_PATH
        roots = (self.tool.API_ROOT, self.tool.UPLOAD_ROOT)
        self.addCleanup(setattr_chain, self.tool, roots)
        self.tool.API_ROOT = base + "/repos/" + self.tool.REPO
        self.tool.UPLOAD_ROOT = base + "/repos/" + self.tool.REPO

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _workspace(self, notes="修了几个问题", with_archive=True, manifest_notes=None):
        """造一份 build_release.sh 第 6 步交给脚本的资产目录。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        notes_file = root / "release_notes.txt"
        notes_file.write_text(notes, encoding="utf-8")
        assets = root / "update_assets"
        assets.mkdir()
        if with_archive:
            (assets / ARCHIVE_NAME).write_bytes(ARCHIVE_BYTES)
            app = root / "Skyrocket.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_bytes(b"app-fixture")
            dmg = root / "Skyrocket-1.4.17.dmg"
            dmg.write_bytes(b"dmg-fixture")
            (assets / self.tool.MANIFEST_PATH).write_text(
                json.dumps({"version": "1.4.17",
                            "notes": notes if manifest_notes is None else manifest_notes,
                            "archive": {
                                "url": "https://github.com/%s/releases/download/%s/%s" % (
                                    self.tool.REPO, self.tool.TAG, ARCHIVE_NAME),
                                "size": len(ARCHIVE_BYTES),
                                "sha256": hashlib.sha256(ARCHIVE_BYTES).hexdigest(),
                            }}), encoding="utf-8")
        # 真执行器、真核验器，只在临时 git 源树中运行；观测是协议夹具而非真机验收。
        quality = self.tool.quality_module()
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "--quiet", str(source)], check=True,
                       capture_output=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        (source / "probe.py").write_text("print('protocol fixture')\n", encoding="utf-8")
        receipt_path = quality.run_command(source, root / "sessions", ["/usr/bin/true"], 3)
        receipt = quality.load(receipt_path)
        reference = receipt_path.relative_to(root).as_posix()
        record = quality.template()
        record.update({
            "intent": "离线发布协议测试，不证明产品真机验收", "scope": ["probe.py"],
            "acceptance": ["发布前证据可核验"],
            "acceptance_map": [{"acceptance": "发布前证据可核验", "evidence_index": 0}],
            "verification_evidence": [{"command": shlex.join(receipt["argv"]),
                                       "exit_code": 0, "receipt": reference}],
        })
        if with_archive:
            record.update({
                "status": "release_candidate",
                "release_qualification": {**record["release_qualification"],
                                           "stage": "pre_release"},
                "release_artifacts": {
                    "version": "1.4.17",
                    "roles": {
                        "app": {"path": "Skyrocket.app"},
                        "dmg": {"path": "Skyrocket-1.4.17.dmg"},
                        "zip": {"path": "update_assets/" + ARCHIVE_NAME},
                        "ota_manifest": {"path": "update_assets/" + self.tool.MANIFEST_PATH},
                    },
                    "ota": {"archive_role": "zip", "manifest_role": "ota_manifest"},
                },
                "artifact_fingerprint": {"artifacts": [
                    {"path": "Skyrocket.app"},
                    {"path": "Skyrocket-1.4.17.dmg"},
                    {"path": "update_assets/" + ARCHIVE_NAME},
                    {"path": "update_assets/" + self.tool.MANIFEST_PATH},
                ]},
                "unverified_limitations": ["仅合成协议夹具；产品真实场景全部未执行"],
            })
            receipt_hash = quality.sha256_file(receipt_path)
            # 保留政策模板：四专项永久排除与 Intel 能力边界均不代表通过；
            # OTA rollback 仍 required=true，仅发布前阶段允许待验证。
            artifact_hashes = {
                role: quality.artifact_token(source.parent / path)["sha256"]
                for role, path in {
                    "app": "Skyrocket.app", "dmg": "Skyrocket-1.4.17.dmg",
                    "zip": "update_assets/" + ARCHIVE_NAME,
                    "ota_manifest": "update_assets/" + self.tool.MANIFEST_PATH,
                }.items()
            }
            for name in quality.RELEASE_PRE_SCENARIOS:
                signals = quality.SCENARIO_SIGNALS[name]
                record["scenarios"][quality.SCENARIOS.index(name)].update(
                    status="passed", required=True,
                    observation={
                        "source": receipt["source_start"],
                        "device_id": receipt["machine"]["device_id"],
                        "observer": "offline-protocol-fixture",
                        "procedure": "synthetic release candidate protocol",
                        "observed_at": receipt["ended_at"],
                        "artifact_sha256": artifact_hashes["zip"],
                        "signals": {signal: True for signal in signals},
                        "attachments": [{"path": reference, "sha256": receipt_hash}],
                    },
                    receipts=[reference])
        record_path = root / "change.json"
        quality.atomic_json(record_path, record)
        if with_archive:
            quality.atomic_json(record_path.with_suffix(".manifest.json"),
                                quality.manifest(source, record, root))
        self.enterContext(patch.object(self.tool, "SOURCE_ROOT", source))
        self.enterContext(patch.dict(os.environ, {"SF_QUALITY_RECORD": str(record_path)}))
        return assets, notes_file

    def _publish(self, assets, notes_file, token="local-test-token"):
        out = io.StringIO()
        with patch.object(self.tool, "github_token", return_value=token), \
             patch.object(sys, "argv", ["publish_update_release.py",
                                        "--assets", str(assets),
                                        "--notes", str(notes_file)]), \
             patch.object(self.tool, "RAW_MANIFEST_URL",
                          self.raw_root + "/manifest"), \
             contextlib.redirect_stdout(out), _quiet_http_error_warnings():
            self.tool.main()
        return out.getvalue()

    # ---- 成功主流程 ----
    def test_publish_sequence_is_upload_verify_pointer_then_legacy(self):
        assets, notes = self._workspace()
        text = self._publish(assets, notes)
        self.assertEqual(self.state.routes(),
                         ["release", "upload", "release",          # 传 ZIP 并复验
                          "release_body", "release",                 # 写入并读回 GitHub Release 正文
                          "contents", "contents", "contents",       # GET/PUT/读回清单指针
                          "release", "upload", "release",          # 换旧客户端清单
                          "contents"],                                # 发布后读回仓库侧清单
                         "顺序错了就等于清单指向还不存在的包，或公开说明与清单不同步")
        self.assertTrue(self.state.content_refs)
        self.assertTrue(all(ref == ["main"] for ref in self.state.content_refs),
                        "清单读回必须固定 main，不能读默认分支后碰运气降级")
        self.assertIn("published_archive=%s" % ARCHIVE_NAME, text)
        self.assertIn("published_manifest_pointer", text)
        # 「打印 published」不等于「线上生效」：读回必须真的发生并给出结论
        self.assertIn("读回通过", text, "宣布成功之前没有权威读回，成功与失败看起来一模一样")

    def test_same_size_wrong_archive_bytes_stop_before_manifest_pointer(self):
        assets, notes = self._workspace()
        self.state.corrupt_archive_after_upload = True
        with self.assertRaises(SystemExit) as got:
            self._publish(assets, notes)
        self.assertIn("原始字节与本地固定快照不一致", str(got.exception))
        self.assertNotIn(("PUT", "contents"), self.state.log)

    def test_release_body_readback_mismatch_stops_before_manifest_pointer(self):
        """Release 正文读回不等于签名 notes 时，客户端仍必须停留在上一版清单。"""
        assets, notes = self._workspace()
        self.state.corrupt_release_body_readback = True
        with self.assertRaises(SystemExit) as got:
            self._publish(assets, notes)
        self.assertIn("Release 发布说明读回不一致", str(got.exception))
        self.assertNotIn(("PUT", "contents"), self.state.log)

    def test_contents_readback_bytes_stop_before_legacy_manifest(self):
        assets, notes = self._workspace()
        self.state.corrupt_contents_readback = True
        with self.assertRaises(SystemExit) as got:
            self._publish(assets, notes)
        self.assertIn("Contents 清单写入后原始字节不一致", str(got.exception))
        self.assertNotIn(("DELETE", "asset"), self.state.log)

    def test_archive_download_failure_stops_before_manifest_pointer(self):
        assets, notes = self._workspace()
        self.state.fail_asset_download = True
        with self.assertRaises(SystemExit) as got:
            self._publish(assets, notes)
        self.assertIn("资产原始字节读回失败", str(got.exception))
        self.assertNotIn(("PUT", "contents"), self.state.log)

    # ---- 超时不许写死（2026-10-12 用户指定："工具里超时写死 60 秒，不要写死"）----
    def _timeouts_seen(self, env=None):
        """跑一次真发布，记下每个请求实际用的超时（None = 走元数据档）。"""
        assets, notes = self._workspace()
        seen = []
        real = self.tool.request

        def spy(url, token, *a, **kw):
            # 按 content_type 认"那次 ZIP 上传"，不按载荷大小 —— 清单资产也是 POST，
            # 载荷还可能比这份小 ZIP 更大，用大小认会认错（第一版就是这么红的）。
            seen.append((kw.get("method", a[0] if a else "GET"),
                         kw.get("content_type", ""), kw.get("timeout")))
            return real(url, token, *a, **kw)

        keys = (self.tool.UPLOAD_TIMEOUT_ENV, self.tool.API_TIMEOUT_ENV)
        saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        for k, v in (env or {}).items():
            os.environ[k] = v
        try:
            with patch.object(self.tool, "request", side_effect=spy):
                text = self._publish(assets, notes)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return seen, text

    def test_archive_upload_uses_the_long_timeout_not_the_metadata_one(self):
        """版本 ZIP 那条 POST 必须走可配的长超时；元数据请求仍走短档。

        为什么盯这一条：v1.4.38 发版时它被写死成 60 秒，两次都在 sendall 处超时，
        而打包/核验/DMG/装机全部已经真完成 —— 一个写死的数把整轮发版卡在最后一步。
        """
        seen, _text = self._timeouts_seen()
        up = [t for m, ct, t in seen if m == "POST" and ct == "application/zip"]
        self.assertEqual(len(up), 1, "没找到那次 ZIP 上传（用例的辨认条件失效了）")
        self.assertGreater(up[0], 60.0,
                           "上传超时又回到 60 秒这一档 —— 本机传 27MB 就是会超时")
        self.assertEqual(up[0], self.tool.UPLOAD_TIMEOUT_DEFAULT)
        small = [t for m, ct, t in seen if ct != "application/zip"]
        self.assertTrue(small, "没有元数据类请求可对照")
        for t in small:
            self.assertTrue(t is None or t <= self.tool.API_TIMEOUT_DEFAULT,
                            "元数据请求跟着放宽到 %r：GitHub 故障要等十几分钟才报" % t)

    def test_timeout_env_override_reaches_the_real_request(self):
        """设了环境变量就必须真的落到那次上传上 —— 静默退回默认值是最坏的说谎。"""
        seen, _text = self._timeouts_seen({self.tool.UPLOAD_TIMEOUT_ENV: "137"})
        up = [t for m, ct, t in seen if m == "POST" and ct == "application/zip"]
        self.assertEqual(up, [137.0], "环境变量没生效，放宽超时的话成了空话")

    def test_printed_timeouts_match_what_was_used(self):
        """日志里必须写清本轮真正用的超时，否则事后无法判断是不是被它卡住。"""
        _seen, text = self._timeouts_seen({self.tool.UPLOAD_TIMEOUT_ENV: "456"})
        self.assertIn("456", text, "日志没回显生效的上传超时")
        self.assertIn(self.tool.UPLOAD_TIMEOUT_ENV, text, "日志没说这个数来自哪个开关")

    def test_stale_repo_manifest_aborts_the_release(self):
        """读回发现仓库里还是上一版 → 非零中止，不许打印"发版可以宣布成功"。

        由来（2026-10-09 v1.4.47）：发布器原先只 print 一行 published_* 就结束，而用户侧
        那条 URL 走 raw CDN 有缓存 —— 发完直读 raw 仍是上一版，一度被判成"报了成功其实没生效"。
        没有权威读回时，成功和失败**长得一模一样**；这条锁住读回真的有牙齿。
        """
        assets, notes = self._workspace()
        real = self.tool.readback_verdict
        self.addCleanup(setattr, self.tool, "readback_verdict", real)
        patcher = patch.object(self.tool, "readback_verdict", lambda *a, **k: (False, ["假阴性"]))
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(SystemExit):
            self._publish(assets, notes)

    def test_archive_bytes_and_manifest_reach_the_server_intact(self):
        assets, notes = self._workspace()
        self._publish(assets, notes)
        self.assertEqual(self.state.blobs[ARCHIVE_NAME], ARCHIVE_BYTES,
                         "上传的字节必须与本地产物逐字节相同")
        served = self.state.contents[self.tool.MANIFEST_PATH]["content"]
        self.assertEqual(json.loads(served),
                         json.loads((assets / self.tool.MANIFEST_PATH).read_bytes()),
                         "切上去的清单必须与本地产物逐字段相同")
        self.assertIn(self.tool.MANIFEST_PATH, self.state.asset_names())
        self.assertIn(ARCHIVE_NAME, self.state.asset_names(),
                      "历史版本 ZIP 绝不被删，否则老客户端点更新直接 404")

    def test_existing_manifest_is_replaced_with_its_current_sha(self):
        """清单已存在时必须带 sha 提交（GitHub 用它做乐观并发控制）。"""
        self.state.contents[self.tool.MANIFEST_PATH] = {
            "sha": "preexisting-sha", "content": b'{"version":"1.4.16"}'}
        assets, notes = self._workspace()
        self._publish(assets, notes)
        self.assertEqual(json.loads(
            self.state.contents[self.tool.MANIFEST_PATH]["content"]),
            json.loads((assets / self.tool.MANIFEST_PATH).read_bytes()))

    def test_legacy_manifest_asset_is_deleted_before_the_new_one_is_posted(self):
        self.state.release["assets"].append(
            {"id": 500, "name": self.tool.MANIFEST_PATH, "size": 7})
        assets, notes = self._workspace()
        self._publish(assets, notes)
        routes = self.state.routes()
        self.assertEqual(routes.count("asset"), 1, "只替换清单这一枚小资产")
        self.assertLess(routes.index("asset"), routes.index("upload", 3),
                        "先删旧清单再传新清单，且绝不触碰版本 ZIP")

    # ---- 失败必须停在切指针之前 ----
    def test_unverified_upload_never_moves_the_manifest_pointer(self):
        self.state.drop_asset_after_upload = True
        assets, notes = self._workspace()
        with self.assertRaises(SystemExit):
            self._publish(assets, notes)
        self.assertNotIn("contents", self.state.routes(),
                         "ZIP 没验证过就不该发出任何清单请求")
        self.assertNotIn(self.tool.MANIFEST_PATH, self.state.contents)

    def test_incomplete_workspace_stops_before_any_request(self):
        assets, notes = self._workspace(with_archive=False)
        with patch.object(self.tool, "github_token", side_effect=AssertionError("不得读取凭据")):
            with self.assertRaises(SystemExit):
                with patch.object(sys, "argv", ["p", "--assets", str(assets),
                                                "--notes", str(notes)]):
                    self.tool.main()
        self.assertEqual(self.state.log, [], "闸门没过就不该发出任何 HTTP 请求")

    def test_notes_mismatch_stops_before_any_request(self):
        """清单里已签名的 notes 与 release_notes.txt 本版一节不一致 → 拒绝且零请求。

        守的是 build_release.sh 第 6 步注释里那句"清单和 GitHub 发布页强制读取同一份
        release_notes.txt"：改了文件却没重签清单（或反过来）时，用户弹窗看到的与仓库里
        写的就不是同一件事，而线上清单已经切过去了、改不回来（发布工具不删资产）。
        """
        assets, notes = self._workspace(manifest_notes="上一版没改干净的说明")
        with self.assertRaises(SystemExit) as got:
            self._publish(assets, notes)
        self.assertIn("不一致", str(got.exception))
        self.assertEqual(self.state.log, [], "闸门没过就不该发出任何 HTTP 请求")

    def test_manifest_without_notes_stops_before_any_request(self):
        """清单没有 notes 字段（旧形状/被裁过）→ 无从对拍，宁可不发。"""
        assets, notes = self._workspace(manifest_notes="")
        with self.assertRaises(SystemExit) as got:
            self._publish(assets, notes)
        self.assertIn("清单缺少 version 或 notes", str(got.exception))
        self.assertEqual(self.state.log, [])

    def test_preflight_failures_never_read_credentials_or_send_requests(self):
        assets, notes = self._workspace()
        record_path = pathlib.Path(os.environ["SF_QUALITY_RECORD"])
        evidence_path = record_path.with_suffix(".manifest.json")
        manifest_path = assets / self.tool.MANIFEST_PATH
        archive_path = assets / ARCHIVE_NAME
        paths = (record_path, evidence_path, manifest_path, archive_path)
        baseline = {p: p.read_bytes() for p in paths}
        cases = (
            ("missing_record", "缺少 SF_QUALITY_RECORD"),
            ("record_json", "发布前核验失败"),
            ("evidence_json", "manifest 无法读取"),
            ("unbound_zip", "正式产物未列入 artifact_fingerprint"),
            ("tampered_zip", "manifest 不一致: artifacts"),
            ("archive_missing", "缺少 archive 对象"),
            ("archive_type", "缺少 archive 对象"),
            ("size_missing", "manifest 不一致: artifacts"),
            ("size_wrong", "manifest 不一致: artifacts"),
            ("size_bool", "manifest 不一致: artifacts"),
            ("hash_missing", "manifest 不一致: artifacts"),
            ("hash_wrong", "manifest 不一致: artifacts"),
            ("url_wrong", "manifest 不一致: artifacts"),
        )
        for mutation, expected in cases:
            with self.subTest(mutation=mutation):
                for path, body in baseline.items():
                    path.write_bytes(body)
                metadata = json.loads(baseline[manifest_path])
                env = {"SF_QUALITY_RECORD": str(record_path)}
                if mutation == "missing_record":
                    env["SF_QUALITY_RECORD"] = ""
                elif mutation == "record_json":
                    record_path.write_bytes(b"{")
                elif mutation == "evidence_json":
                    evidence_path.write_bytes(b"{")
                elif mutation == "unbound_zip":
                    other = assets.parent / "other.bin"
                    other.write_bytes(ARCHIVE_BYTES)
                    quality = self.tool.quality_module()
                    record = quality.load(record_path)
                    record["artifact_fingerprint"]["artifacts"] = [{"path": "other.bin"}]
                    quality.atomic_json(record_path, record)
                    quality.atomic_json(evidence_path, quality.manifest(
                        self.tool.SOURCE_ROOT, record, record_path.parent))
                elif mutation == "tampered_zip":
                    archive_path.write_bytes(b"X" * len(ARCHIVE_BYTES))
                else:
                    info = metadata["archive"]
                    if mutation == "archive_missing":
                        del metadata["archive"]
                    elif mutation == "archive_type":
                        metadata["archive"] = []
                    elif mutation == "size_missing":
                        del info["size"]
                    elif mutation == "size_wrong":
                        info["size"] += 1
                    elif mutation == "size_bool":
                        info["size"] = True
                    elif mutation == "hash_missing":
                        del info["sha256"]
                    elif mutation == "hash_wrong":
                        info["sha256"] = "0" * 64
                    elif mutation == "url_wrong":
                        info["url"] = "https://example.invalid/wrong.zip"
                    manifest_path.write_text(json.dumps(metadata), encoding="utf-8")
                with patch.dict(os.environ, env), \
                     patch.object(self.tool, "github_token") as credentials, \
                     patch.object(self.tool, "request") as request, \
                     patch.object(sys, "argv", ["p", "--assets", str(assets),
                                                "--notes", str(notes)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as got:
                        self.tool.main()
                    self.assertIn(expected, str(got.exception))
                    credentials.assert_not_called()
                    request.assert_not_called()
                self.assertEqual(self.state.log, [])

    def test_preflight_snapshot_survives_local_file_replacement(self):
        assets, notes = self._workspace()
        manifest = assets / self.tool.MANIFEST_PATH
        original_manifest = manifest.read_bytes()
        snapshot = self.tool.publication_snapshot

        def replace_after_check(*args):
            result = snapshot(*args)
            (assets / ARCHIVE_NAME).write_bytes(b"replaced")
            manifest.write_bytes(b'{"version":"wrong-version"}')
            return result

        with patch.object(self.tool, "publication_snapshot", side_effect=replace_after_check):
            text = self._publish(assets, notes)
        self.assertEqual(self.state.blobs[ARCHIVE_NAME], ARCHIVE_BYTES)
        self.assertEqual(self.state.blobs[self.tool.MANIFEST_PATH], original_manifest)
        self.assertEqual(self.state.contents[self.tool.MANIFEST_PATH]["content"], original_manifest)
        self.assertIn("读回通过", text)
        self.assertNotIn("authorization", self.state.raw_headers)

    def test_credential_never_appears_in_request_line_but_in_header(self):
        assets, notes = self._workspace()
        self._publish(assets, notes, token="sekret-token")
        # urllib 会把请求头名规范化成 "X-github-api-version" 这种形态，
        # 所以服务器侧统一按小写记录，两边同口径才测得出"有没有发出去"。
        auth = self.state.last_headers.get("authorization", "")
        self.assertTrue(auth.startswith("Basic "), auth)
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        self.assertEqual(decoded, "x-access-token:sekret-token")
        self.assertEqual(self.state.last_headers.get("x-github-api-version"),
                         "2022-11-28")
        self.assertEqual(self.state.last_headers.get("accept"),
                         "application/vnd.github+json")
        logged = " ".join(path for _, path in self.state.log)
        self.assertNotIn("sekret-token", logged, "凭据不得出现在 URL / 路由里")


def setattr_chain(module, roots):
    """addCleanup 用：把两个 API 根还原，避免污染同进程其它用例。"""
    module.API_ROOT, module.UPLOAD_ROOT = roots


if __name__ == "__main__":
    unittest.main()
