"""Update interaction decisions, separate from native/Qt dialog rendering.

The host port supplies update state and UI commands. All methods execute on the
UI thread; blocking transport remains in UpdateController.
"""

import inspect
import os
import re
import threading
import time
from urllib.parse import urlparse

import sf_update_policy
from sf_update_cache import UpdateCacheBusyError
from sf_constants import RELEASE_SEQUENCE, UPDATE_CHECK_STALE_SEC
from sf_update_policy import PolicyInput, PolicyState


class UpdateFlowPort:
    """最小宿主边界：流程只通过这些公开操作读状态、发动作。"""

    def get_local_version(self) -> str:
        raise NotImplementedError

    def get_local_sequence(self) -> int:
        raise NotImplementedError

    def start_download(self, update, *, prompt_when_ready: bool, show_progress: bool,
                       token=None) -> None:
        raise NotImplementedError

    def cancel_download(self, update=None, *, token=None) -> None:
        raise NotImplementedError

    def prompt_update(self, update, *, kind: str, generation=None, identity=None) -> None:
        raise NotImplementedError

    def show_download_progress(self) -> None:
        raise NotImplementedError

    def show_update_message(self, title: str, body: str) -> None:
        raise NotImplementedError

    def install_update(self, update, archive: str) -> None:
        raise NotImplementedError

    def request_check(self, manual: bool, token=None) -> None:
        raise NotImplementedError


class _ForcedUpdateFlowController:
    """Owns forced-update session state and leaves transport/UI to public ports."""

    def __init__(self, backend=None, log=None, swallow=None, *, port=None, cache=None):
        self._backend = backend
        self._port = port
        self._cache = cache
        self.log = log or (lambda *args: None)
        self.swallow = swallow or (lambda *args: None)
        self._update = None
        self._archive = None
        self._decision = None
        self._manifest_verified = False
        self._prompted = set()
        self._download_active = False
        self._download_show_progress = False
        self._generation = 0
        self._download_token = None
        self._download_update = None
        self._cancelled_token = None
        self._pending_manifest = None
        self._install_started = False
        self._error_counts = {}
        self._last_error = None

    def _record_error(self, category, error):
        self._error_counts[category] = self._error_counts.get(category, 0) + 1
        self._last_error = {
            "category": str(category),
            "type": type(error).__name__,
            "reason": _safe_error(error),
        }

    @staticmethod
    def _identity(update):
        if update is None:
            return None
        if hasattr(update, "release_sequence") and update.release_sequence is not None:
            return (update.version, update.release_sequence, update.archive_sha256)
        return (update.version, None, update.archive_sha256)

    def _port_value(self, name, fallback):
        value = getattr(self._port, name, None)
        return value() if callable(value) else fallback

    def _evaluate(self, *, package_ready=False):
        if self._update is None or not self._manifest_verified:
            return None
        decision = sf_update_policy.evaluate(PolicyInput(
            local_version=str(self._port_value("get_local_version", "0.0.0")),
            local_sequence=int(self._port_value("get_local_sequence", RELEASE_SEQUENCE)),
            live_version=self._update.version,
            live_sequence=self._update.release_sequence,
            package_ready=bool(package_ready),
            manifest_verified_this_session=True,
        ))
        self._decision = decision
        return decision

    def clear_verified_session(self):
        if self._download_active and self._download_update is not None:
            self._port.cancel_download(self._download_update, token=self._download_token)
        self._generation += 1
        self._update = None
        self._archive = None
        self._decision = None
        self._manifest_verified = False
        self._prompted.clear()
        self._download_active = False
        self._download_token = None
        self._download_update = None
        self._pending_manifest = None

    def is_current(self, *, identity=None, generation=None):
        return (self._update is not None and
                (identity is None or identity == self._identity(self._update)) and
                (generation is None or generation == self._generation))

    def is_current_download(self, update, token=None):
        return (self._download_active and token is not None and
                self.is_current(identity=self._identity(update)) and
                token is self._download_token)

    def accepts_download_terminal(self, update, token=None):
        return (self.is_current_download(update, token) or
                (token is not None and token is self._cancelled_token))

    @property
    def has_pending_manifest(self):
        return self._pending_manifest is not None

    @property
    def generation(self):
        return self._generation

    @property
    def download_token(self):
        return self._download_token

    def on_check_result(self, update, error, *, automatic=False):
        if error is not None:
            return self.on_check_failed(error, automatic=automatic)
        if update is None:
            self.clear_verified_session()
            return None
        return self.on_manifest_verified(update, automatic=automatic)

    def _prompt_once(self, kind):
        identity = self._identity(self._update)
        key = (identity, kind)
        if identity is None or key in self._prompted:
            return False
        self._prompted.add(key)
        generation = self._generation
        try:
            self._port.prompt_update(self._update, kind=kind,
                                     generation=generation, identity=identity)
        except Exception:
            if self.is_current(identity=identity, generation=generation):
                self._prompted.discard(key)
            raise
        return True

    def on_manifest_verified(self, update, *, automatic=False):
        """Consume one already-verified manifest and apply the gap policy."""
        if (self._pending_manifest is not None and
                (not isinstance(update.published_at, str) or
                 update.published_at < self._pending_manifest[0].published_at)):
            self._record_error("manifest_replay", ValueError("更新清单已过期"))
            if not automatic:
                self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
            return None
        if (self._pending_manifest is not None and
                update.published_at == self._pending_manifest[0].published_at and
                (self._identity(update) != self._identity(self._pending_manifest[0]) or
                 getattr(update, "manifest_digest", None) !=
                 getattr(self._pending_manifest[0], "manifest_digest", None))):
            self._record_error("manifest_replay", ValueError("更新时间相同但身份不同"))
            if not automatic:
                self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
            return None
        if update.release_sequence is not None and self._cache is None:
            self.clear_verified_session()
            self._record_error("manifest_replay", RuntimeError("更新缓存不可用"))
            if not automatic:
                self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
            return None
        if update.release_sequence is not None:
            try:
                with self._cache.write_lock():
                    accepted = self._cache.accept_manifest_identity(update)
                if not accepted:
                    raise ValueError("更新清单已过期")
            except UpdateCacheBusyError as exc:
                if (self._download_active and self._download_update is not None and
                        self._identity(update) == self._identity(self._download_update) and
                        update.published_at == self._download_update.published_at and
                        getattr(update, "manifest_digest", None) != getattr(self._download_update, "manifest_digest", None)):
                    self.clear_verified_session()
                    self._record_error("manifest_replay", ValueError("同时间清单内容不同"))
                    if not automatic:
                        self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
                    return None
                if (self._download_active and self._download_update is not None and
                        self._identity(update) == self._identity(self._download_update) and
                        update.published_at > self._download_update.published_at and
                        self._pending_manifest is None):
                    self._pending_manifest = (update, automatic)
                    self._generation += 1
                    self._port.cancel_download(self._download_update, token=self._download_token)
                    return None
                if (self._download_active and self._download_update is not None and
                        self._identity(update) == self._identity(self._download_update) and
                        self._pending_manifest is None):
                    if not automatic:
                        self._download_show_progress = True
                        self._port.show_download_progress()
                    return None
                if self._pending_manifest is not None:
                    old_update, old_automatic = self._pending_manifest
                    if update.published_at > old_update.published_at:
                        self._pending_manifest = (update, automatic and old_automatic)
                        self._generation += 1
                    elif not automatic and old_automatic:
                        self._pending_manifest = (old_update, False)
                    self._record_error("manifest_busy", exc)
                    schedule = getattr(self._port, "schedule_manifest_retry", None)
                    if callable(schedule):
                        schedule()
                    return None
                if (self._download_active and self._download_update is not None and
                        self._identity(update) != self._identity(self._download_update)):
                    self._pending_manifest = (update, automatic)
                    self._generation += 1
                    self._port.cancel_download(self._download_update, token=self._download_token)
                    return None
                self._record_error("manifest_busy", exc)
                if not automatic:
                    self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
                return None
            except Exception as exc:
                # 严格更旧的回放不能撤销已验签的较新会话；同时间冲突或未知错误则失效。
                current_time = getattr(self._update, "published_at", None)
                same_download = (self._download_active and self._download_update is not None and
                                 self._identity(update) == self._identity(self._download_update) and
                                 update.published_at == self._download_update.published_at and
                                 getattr(update, "manifest_digest", None) ==
                                 getattr(self._download_update, "manifest_digest", None))
                if not (same_download or (isinstance(current_time, str) and
                        isinstance(update.published_at, str) and update.published_at < current_time)):
                    self.clear_verified_session()
                self._record_error("manifest_replay", exc)
                self.swallow("sf_update_flow.accept_manifest_timestamp", exc)
                if not automatic:
                    self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
                return None
        if self._pending_manifest is not None:
            pending = self._pending_manifest
            if (self._identity(update) == self._identity(pending[0]) or
                    update.published_at >= pending[0].published_at):
                automatic = automatic and pending[1]
                self._pending_manifest = None
        if not automatic:
            identity = self._identity(update)
            self._prompted = {key for key in self._prompted if key[0] != identity}
        identity = self._identity(update)
        if self._update is not None and identity != self._identity(self._update):
            self._generation += 1
            if self._download_active:
                self._port.cancel_download(self._download_update, token=self._download_token)
                self._download_active = False
                self._download_update = None
            self._archive = None
            self._download_token = None
        elif self._update is None:
            self._generation += 1
        self._update = update
        self._manifest_verified = True
        self._archive = self._validated_archive(update)
        decision = self._evaluate(package_ready=self._archive is not None)
        if decision is None:
            return None
        if decision.state is PolicyState.UP_TO_DATE:
            if not automatic:
                self._port.show_update_message("检查更新", "当前已是最新版本。")
        elif decision.state is PolicyState.OFFER:
            self._prompt_once("offer")
        elif decision.state is PolicyState.PREPARE:
            if automatic and decision.should_download and not self._download_active:
                self._download_active = True
                self._start_download(update, prompt_when_ready=True, show_progress=False)
            elif not automatic:
                self._prompt_once("offer")
        elif decision.state is PolicyState.FORCE_PENDING:
            if automatic and decision.should_download and not self._download_active:
                self._download_active = True
                identity_before = self._identity(self._update)
                generation_before = self._generation
                try:
                    self._start_download(update, prompt_when_ready=False, show_progress=False)
                except Exception:
                    if (self.is_current(identity=identity_before, generation=generation_before)
                            and self.state is PolicyState.FORCE_PENDING):
                        self._prompted.discard((identity_before, "force_pending"))
                        try:
                            self._prompt_once("force_pending")
                        except Exception as exc:
                            self.swallow("sf_update_flow.force_prompt_after_auto_start", exc)
                    raise
            self._prompt_once("force_pending")
        elif decision.state is PolicyState.FORCE_READY:
            self._prompt_once("force_ready")
        elif decision.state is PolicyState.ROLLBACK_OFFER:
            self._prompt_once("rollback")
        elif decision.state is PolicyState.REPLACE_OFFER:
            self._prompt_once("offer")
        return decision

    def _validated_archive(self, update):
        if self._cache is None:
            return None
        try:
            return self._cache.validate_cached_archive(update)
        except Exception as exc:
            self._record_error("cache_validation", exc)
            self.swallow("sf_update_flow.validate_cached_archive", exc)
            return None

    def on_first_frame_ready(self):
        """Recheck only the latest verified identity; offline startup remains neutral."""
        if self._update is None or not self._manifest_verified:
            return None
        self._archive = self._validated_archive(self._update)
        decision = self._evaluate(package_ready=self._archive is not None)
        if decision is not None and decision.state is PolicyState.FORCE_READY:
            self._prompt_once("force_ready")
        elif decision is not None and decision.state is PolicyState.OFFER:
            self._prompt_once("offer")
        return decision

    def on_download_ready(self, update, archive):
        if self._update is None or self._identity(update) != self._identity(self._update):
            return None
        verified = self._validated_archive(update)
        if verified is None or archive is None or os.fspath(verified) != os.fspath(archive):
            return None
        self._download_active = False
        self._archive = verified
        decision = self._evaluate(package_ready=True)
        if decision is not None:
            if decision.state is PolicyState.FORCE_READY:
                self._prompt_once("force_ready")
            elif decision.state is PolicyState.PREPARE:
                self._prompt_once("ready")
            elif decision.state in (PolicyState.OFFER, PolicyState.REPLACE_OFFER, PolicyState.ROLLBACK_OFFER):
                self._prompted.discard((self._identity(update), "offer"))
                self._prompt_once("ready")
        return decision

    def on_check_failed(self, error, *, automatic=False):
        """Record a failed recheck after invalidating the verified session."""
        self.clear_verified_session()
        self._record_error("check", error)
        self.log("update check failed: %r" % (error,), "WARNING")
        if not automatic:
            self._port.show_update_message("检查更新失败", "暂时无法检查更新。")
        return False

    def on_download_finished(self, update, archive, error, token=None):
        """Classify one terminal result without retrying or installing implicitly."""
        if token is not None and token is self._cancelled_token:
            self._cancelled_token = None
            if self._download_active:
                return False
            if self._pending_manifest is not None:
                pending_update, pending_automatic = self._pending_manifest
                self.on_manifest_verified(pending_update, automatic=pending_automatic)
            elif self.state is PolicyState.FORCE_PENDING:
                self._prompted.discard((self._identity(self._update), "force_pending"))
                self._prompt_once("force_pending")
            return False
        if not self.is_current_download(update, token):
            return False
        self._download_active = False
        self._download_token = None
        self._download_update = None
        if self._pending_manifest is not None:
            pending_update, pending_automatic = self._pending_manifest
            self.on_manifest_verified(pending_update, automatic=pending_automatic)
            return False
        if error is None:
            if self.on_download_ready(update, archive) is not None:
                return True
            error = RuntimeError("更新包复核失败")
        if self.state is PolicyState.FORCE_PENDING:
            self._prompted.discard((self._identity(self._update), "force_pending"))
            try:
                self._prompt_once("force_pending")
            except Exception as exc:
                self.swallow("sf_update_flow.force_prompt_after_download", exc)
        try:
            import sf_update
            cancelled = isinstance(error, sf_update.UpdateCancelled)
            fatal = isinstance(error, sf_update.UpdateFatal)
        except Exception as exc:
            self._record_error("download_classification", exc)
            self.swallow("sf_update_flow.classify_download", exc)
            cancelled = fatal = False
        if cancelled:
            return False
        if fatal:
            self._record_error("download_fatal", error)
            self.log("update download fatal: %r" % (error,), "ERROR")
            if self._download_show_progress:
                self._port.show_update_message("更新未完成", "下载或校验失败。")
            return False
        self._record_error("download", error)
        self.log("update download failed: %r" % (error,), "WARNING")
        self._port.show_update_message("更新未完成", "下载或校验失败。")
        return False


    def _start_download(self, update, *, prompt_when_ready, show_progress):
        self._download_show_progress = bool(show_progress)
        self._download_token = object()
        self._download_update = update
        token = self._download_token
        try:
            self._port.start_download(update, prompt_when_ready=prompt_when_ready,
                                      show_progress=show_progress, token=token)
        except Exception:
            if self._download_token is token:
                self._download_active = False
                self._download_token = None
                self._download_update = None
                self._download_show_progress = False
            raise

    def progress_revealed(self):
        if self._download_active:
            self._download_show_progress = True

    def start_download(self, *, prompt_when_ready: bool, show_progress: bool):
        if self._update is None or self._download_active:
            return False
        self._download_active = True
        self._start_download(self._update, prompt_when_ready=prompt_when_ready,
                             show_progress=show_progress)
        return True


    def handle_force_action(self, action, *, identity=None, generation=None):
        if (self._update is None or self._decision is None or
                (identity is not None and identity != self._identity(self._update)) or
                (generation is not None and generation != self._generation)):
            return False
        if action == "开始下载" and self._decision.state is PolicyState.FORCE_PENDING:
            if not self._download_active:
                self._download_active = True
                identity_before = self._identity(self._update)
                generation_before = self._generation
                try:
                    self._start_download(self._update, prompt_when_ready=False, show_progress=True)
                except Exception:
                    if (self.is_current(identity=identity_before, generation=generation_before)
                            and self.state is PolicyState.FORCE_PENDING):
                        self._prompted.discard((identity_before, "force_pending"))
                        try:
                            self._prompt_once("force_pending")
                        except Exception as exc:
                            self.swallow("sf_update_flow.force_prompt_after_start", exc)
                    raise
            else:
                self._download_show_progress = True
                self._port.show_download_progress()
            return True
        if action == "继续使用" and self._decision.state is PolicyState.FORCE_PENDING:
            return True
        return False

    def cancel_download(self):
        if self._download_active and self._download_update is not None:
            self._port.cancel_download(self._download_update, token=self._download_token)
            self._cancelled_token = self._download_token
            self._download_token = None
            self._download_active = False

    def retry_pending_manifest(self):
        if self._pending_manifest is None or self._download_active:
            return None
        update, automatic = self._pending_manifest
        return self.on_manifest_verified(update, automatic=automatic)

    def manual_check(self):
        self._port.request_check(True)

    def install_ready(self):
        if self._install_started or self._update is None or self._archive is None:
            return False
        archive = self._validated_archive(self._update)
        if not archive:
            self._archive = None
            self._record_error("ready_archive", RuntimeError("更新包不存在或校验失败"))
            self._port.show_update_message("更新未完成", "更新包不存在或校验失败，请重新下载。")
            decision = self._evaluate(package_ready=False)
            kind = "force_pending" if decision and decision.state is PolicyState.FORCE_PENDING else "offer"
            self._prompted.discard((self._identity(self._update), kind))
            self._prompt_once(kind)
            return False
        self._archive = archive
        self._install_started = True
        try:
            result = self._port.install_update(self._update, archive)
        except Exception:
            self._install_started = False
            raise
        if result is False:
            self._install_started = False
            if self.state is PolicyState.FORCE_READY:
                self._prompted.discard((self._identity(self._update), "force_ready"))
                self._prompt_once("force_ready")
            return False
        return True

    @property
    def state(self):
        return self._decision.state if self._decision is not None else None

    def diagnostics(self):
        decision = self._decision
        update = self._update
        archive_url = getattr(update, "archive_url", "") if update is not None else ""
        parsed = urlparse(archive_url)
        return {
            "schema": "update_flow.v2",
            "state": decision.state.value if decision is not None else None,
            "gap": decision.gap if decision is not None else None,
            "identity": self._identity(self._update),
            "manifest_verified_this_session": self._manifest_verified,
            "download_active": self._download_active,
            "archive_ready": self._archive is not None,
            "update": ({
                "version": update.version,
                "release_sequence": update.release_sequence,
                "stage": update.stage,
            } if update is not None else None),
            "package": ({
                "size": update.archive_size,
                "sha256": update.archive_sha256,
            } if update is not None else None),
            "manifest": {"host": parsed.hostname, "verified": self._manifest_verified},
            "policy": ({
                "state": decision.state.value,
                "gap": decision.gap,
                "force": bool(decision.force),
                "should_download": bool(decision.should_download),
                "should_prompt": bool(decision.should_prompt),
            } if decision is not None else None),
            "download": {
                "active": self._download_active,
                "ready": self._archive is not None,
                "progress": None,
            },
            "errors": {
                "counts": dict(self._error_counts),
                "last": dict(self._last_error) if self._last_error else None,
            },
            "install": {"status": None, "rollback_reason": None},
        }

def _safe_error(error):
    """Keep error evidence useful without exporting paths, URLs, or credentials."""
    text = str(error or "")[:200]
    text = re.sub(r"https?://[^\s]+", "<url>", text)
    text = re.sub(r"(?:/Users/|/private/|/var/|/tmp/)[^\s]+", "<path>", text)
    return text


class UpdateFlowController:
    """Compatibility facade for the new ported flow and the legacy host flow."""

    def __init__(self, *args, **kwargs):
        self._impl: _ForcedUpdateFlowController | _LegacyUpdateFlowController
        if "port" in kwargs or "cache" in kwargs:
            self._impl = _ForcedUpdateFlowController(*args, **kwargs)
        else:
            self._impl = _LegacyUpdateFlowController(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._impl, name)


def update_flow_diagnostics(host, field, install_result):
    """Read the existing update snapshot on the UI thread, one field at a time."""
    port = getattr(host, "_update_port", None)
    flow = getattr(port, "_flow", None)
    cache = getattr(port, "_cache", None)
    controller = getattr(host, "_update_controller", None)
    controller_diag = getattr(controller, "diagnostics", None)
    return {
        "downloading": field("update.downloading", lambda: bool(host._update_downloading), None),
        "transition_pending": field("update.transition",
                                    lambda: bool(host._update_transition_pending), None),
        "manual_wait": field("update.manual_wait", lambda: bool(host._update_manual_wait), None),
        "last_check_at": field("update.last_check_at", lambda: float(host._update_last_check_at), None),
        "stale_resets": field("update.stale_resets", lambda: int(host._update_stale_resets), None),
        "last_result": field("update.last_result", lambda: host._update_last_result, None),
        "last_download": field("update.last_download", lambda: host._update_last_download, None),
        "install_result": field("update.install_result", lambda: install_result(), None),
        "flow": field("update.flow", flow.diagnostics, None) if flow is not None else None,
        "cache": field("update.cache", cache.diagnostics, None) if cache is not None else None,
        "controller": field("update.controller", controller_diag, None)
        if callable(controller_diag) else None,
    }


class _LegacyUpdateFlowController:
    def __init__(self, backend, log, swallow):
        self._backend = backend
        self.log = log
        self.swallow = swallow

    def check(self, host, manual=False):
        """检查更新只占用独立线程，绝不与行情轮询共用连接池或阻塞界面。

        v1.4.43：手动点击【必须有可见反馈】，并把「能立刻答复的」排到守卫之前。
        为什么要改（离屏探针按真实分支实测）：右键悬浮窗的那一下就把应用切成了
        活跃，而「变为活跃」本身就会触发一次静默补查 —— 用户接着点「检查更新」时
        几乎必然撞上那次还在飞的检查。旧代码在这里直接 return，连「正在检查」的框
        都不给；5 秒后那次检查又按静默处理什么都不弹。用户看到的就是「点了没反应」，
        而日志里一行都不留。
        """
        # 已知旧提示不能代替重新检查：线上清单可能已经撤回或升级为新协议。
        # 下载正在进行时仍先展示进度，避免重复下载；否则手动检查重新联网。
        pending = getattr(host, "_update_pending", None)
        if manual and pending is not None and getattr(host, "_update_downloading", False):
            host.show_background_download_progress()
            return
        if getattr(host, "_threads_stopped", False):
            return
        in_flight = bool(host._update_checking
                         or getattr(host, "_update_transition_pending", False))
        if in_flight and host._update_check_stale():
            # v1.4.44：在飞太久就当它已经死了。回包是有界队列（32 格，满时丢最旧）里的
            # 一次性事件，一旦被丢掉，这两个标记会**永久锁真**：此后任何检查都起不来，
            # 那张「正在检查更新」也永远关不掉，只能重启程序。放开重来比从此不问好。
            host._update_checking = False
            host._update_transition_pending = False
            host._update_manual_wait = False
            # 只收这张「正在检查」框：整包收口会连带关掉下载进度框，那是另一件事。
            host._dispose_update_dialog(getattr(host, "_update_check_dialog", None))
            self.log("update check stale over %ds, state reset"
                     % UPDATE_CHECK_STALE_SEC, "WARNING")
            # 诊断留痕：复位过一次就说明有一轮检查的回包被丢掉过（那正是"点了没
            # 反应、框永远关不掉"的成因）。此前只有一行会被挤掉的 WARNING。
            host._update_stale_resets = int(getattr(host, "_update_stale_resets", 0)) + 1
            in_flight = False
        if in_flight:
            # ② 已有一次检查在飞：不再另起第二个请求，但这次点击要记在账上，由那次
            #    检查的回包按「用户主动要求」兑现（见 _handle_update_check）。
            if manual:
                host._update_manual_wait = True
                host._update_check_aborted = False
                # 与下面发起新检查的分支同理：等待框只是提示，它出错不能让这次点击没有回音。
                try:
                    host._show_checking_dialog()
                except Exception as exc:
                    self.swallow("sf_update_flow.show_checking_dialog", exc)
                self.log("update check click reused in-flight check")
            return
        if host._update_downloading:
            # ③ 正在下载：更新已经在跑，说清楚在跑什么，别让人以为这次点击无效。
            if manual:
                host.show_background_download_progress()
            return
        host._update_checking = True
        host._update_transition_pending = True
        # 上一次「取消」只作废它那一次，不许跟着下一次检查一路走下去。
        host._update_check_aborted = False
        # 墙钟：既用于激活补查的节流，也用于回包时算出这次检查实际花了多久。
        host._update_last_check_at = time.time()
        # 只有手动检查才弹「正在检查」：启动后 15 秒的自检、切回程序的补查、
        # 6 小时复查与失败退避都必须是静默的 —— 系统弹窗会把应用带到最前，
        # 看盘时抢一次焦点比慢一点更糟。（加强后的既有用例当场逮到过这个。）
        if manual:
            # 等待框只是提示，不是检查的前提：它内部会惰性 import 系统弹窗模块，
            # 安装包在程序运行中被换掉时归档读出坏数据（实测 zlib "Error -3"），
            # 异常若冒出去，检查既没发起、上面两个状态位又留在 True 直到被判过期。
            # 失败只留痕，照常往下发起检查（AGENTS §6 第 9 条，不许静默）。
            try:
                host._show_checking_dialog()
            except Exception as exc:
                self.swallow("sf_update_flow.show_checking_dialog", exc)

        port = getattr(host, "_update_port", None)
        if port is not None:
            port.request_check(manual)
        else:
            self._backend(host).check(manual)

    def check_stale(self, host):
        """这一次在飞检查是不是已经过期（起算时刻 + UPDATE_CHECK_STALE_SEC）。

        单独成方法是为了让调用点只问一句"它还活着吗"，也便于用例直接驱动时间。
        """
        started = getattr(host, "_update_last_check_at", 0.0) or 0.0
        return (time.time() - started) > UPDATE_CHECK_STALE_SEC

    def prepare_checked(self, host, manual):
        """收口一次检查的宿主状态，再把结果交给具体策略。"""
        return host.finish_update_check(manual)

    def record_checked(self, host, manual, update, error):
        """记录检查结果，并统一处理失败计数和成功后的退避收口。"""
        started = getattr(host, "_update_last_check_at", 0.0) or 0.0
        self.log("update check done: asked=%s result=%s elapsed=%.1fs"
                 % ("manual" if manual else "auto",
                    "error" if error is not None else
                    ("new" if update is not None else "latest"),
                    (time.time() - started) if started else -1.0))
        # 诊断留痕（runtime.json 的 update_last_result）：asked/result/elapsed 与
        # 远端版本、阶段此前只活在上面这行会被轮转挤掉的日志里 —— "点了没反应"
        # "还停在 Beta""已是最新但通道没推上去"三件事在包里全部同形。
        try:
            host._update_last_result = {
                "asked": "manual" if manual else "auto",
                "outcome": ("error" if error is not None
                            else ("new" if update is not None else "latest")),
                "elapsed_sec": round(time.time() - started, 1) if started else None,
                "remote_version": (getattr(update, "version", "") or ""
                                   if update is not None else ""),
                "remote_stage": (getattr(update, "stage", "") or ""
                                 if update is not None else ""),
                "error": (str(error)[:200] if error is not None else ""),
                "at": time.time(),
            }
        except Exception as _rec_exc:
            # 记账失败不影响更新回包，但必须留痕（AGENTS §6 第 9 条）
            self.swallow("stock_float_window._update_last_result", _rec_exc)
        if getattr(host, "_quitting", False):
            return
        if error is not None:
            host._update_failure_count = getattr(host, "_update_failure_count", 0) + 1
            self.log("update check failed: %r" % (error,), "WARNING")
            host._schedule_update_retry()
            if manual:
                host._show_update_message("检查更新失败",
                                          "暂时无法检查更新。", "当前版本不受影响。")
            return
        host._update_failure_count = 0
        timer = getattr(host, "_update_retry_timer", None)
        if timer is not None:
            timer.stop()

    def checked(self, host, manual, update, error):
        manual = self.prepare_checked(host, manual)
        self.record_checked(host, manual, update, error)
        if getattr(host, "_quitting", False) or error is not None:
            return
        if update is None:
            # 2026-11-02 走查 F-H-07：线上清单被撤回或回滚后，检查结果会变回「已是最新」，
            # 但常驻会话里还记着之前发现的版本：手动「检查更新」会跳过联网、直接弹出
            # 已经撤回的版本。这里把待更新版本和「已就绪包」的记账一起放下（只清记账，
            # 不删磁盘上的包；下次真有新版时 ready_archive 会按版本重新核对）。
            host._update_pending = None
            host._update_ready_archive = None
            host._update_ready_version = None
            if manual:
                host._show_update_message("检查更新", "当前已是最新版本。")
            return
        host._update_prompted_version = update.version
        host._update_pending = update
        if manual:
            host._prompt_update(update)
        elif host._ready_update_archive(update) is None:
            # 自动检查不抢焦点：下载在后台完成，成功后再弹一次包含更新内容的确认框。
            host._download_update_background(update, prompt_when_ready=True,
                                             show_progress=False)

    def ready_archive(self, host, update):
        """返回仍存在的已校验更新包；版本变化或文件消失时清掉旧状态。"""
        archive = getattr(host, "_update_ready_archive", None)
        if (not archive or getattr(host, "_update_ready_version", None) != update.version
                or not os.path.isfile(archive)):
            if getattr(host, "_update_ready_version", None) != update.version:
                host._update_ready_archive = None
                host._update_ready_version = None
            elif archive and not os.path.isfile(archive):
                host._update_ready_archive = None
                host._update_ready_version = None
            return None
        return archive

    def answered(self, host, button):
        """主线程处理系统确认框的按钮结果；只认“立即更新”，其余视为放弃。"""
        dialog = getattr(host, "_update_prompt_dialog", None)
        host._update_prompt_dialog = None
        # 弹窗已随用户点击自行结束；这里只兜底收口异常未退出的子进程。
        host._dispose_update_dialog(dialog)
        if button == "暂不更新" or getattr(host, "_quitting", False):
            return
        update = getattr(host, "_update_pending", None)
        if update is None:
            return
        ready_archive = host._ready_update_archive(update)
        if button == "更新并重启应用":
            if ready_archive is not None:
                host._launch_downloaded_update(update, ready_archive)
            else:
                # 包被外部清理后，回到自动下载流程；不把旧路径交给安装助手。
                host._download_update_background(update, prompt_when_ready=True,
                                                 show_progress=False)
        elif button == "立即更新":
            host._accept_update_prompt(None, update)

    def download(self, host, update, prompt_when_ready=False, show_progress=True):
        if host._update_downloading or getattr(host, "_quitting", False):
            return
        host._update_downloading = True
        cancel_event = threading.Event()
        download_token = object()
        try:
            host.begin_legacy_update_download(cancel_event, download_token)
            host.reset_update_progress()
            host.set_legacy_update_prompt_when_ready(bool(prompt_when_ready))
            if show_progress:
                host.open_update_download_progress()
            method = self._backend(host).download
            arguments: tuple = (update, cancel_event, download_token)
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError) as exc:
                self.swallow("sf_update_flow.inspect_signature", exc)
            else:
                try:
                    signature.bind(update, cancel_event, download_token)
                    arguments = (update, cancel_event, download_token)
                except TypeError:
                    signature.bind(update, cancel_event)
                    arguments = (update, cancel_event)
            if method(*arguments) is False:
                raise RuntimeError("更新下载未能启动")
        except Exception as exc:
            cancel_event.set()
            if getattr(host, "_update_download_token", None) is download_token:
                try:
                    self.downloaded(host, update, None, exc)
                except Exception as cleanup_exc:
                    self.swallow("sf_update_flow.legacy_start_cleanup", cleanup_exc)
            raise

    def cancel_download(self, host):
        cancel = getattr(host, "_update_cancel", None)
        if cancel is not None:
            cancel.set()

    def _is_cancel(self, error):
        """error 是不是「用户取消下载」。惰性 import 与本模块其余更新代码同口径：
        取不到 sf_update（安装包在运行中被换掉等）就按普通失败处理并留痕，不外抛。"""
        try:
            import sf_update
            return isinstance(error, sf_update.UpdateCancelled)
        except Exception as exc:
            self.swallow("sf_update_flow.is_cancel", exc)
            return False

    def _record_download(self, host, update, error, background):
        """把这次下载的收场方式记进宿主状态，随诊断包外发（runtime.json 的 update.last_download）。

        只记类型名与截断后的原因；地址类文本由诊断导出那一步统一脱敏（先截断再脱敏是理论
        缺口：残片可能匹配不上整串规则，通用 /Users/<x> 规则仍会兜住）。
        记账失败只留痕，绝不影响下载收尾和给用户的提示。
        """
        cancelled = fatal = False
        try:
            import sf_update
            cancelled = isinstance(error, sf_update.UpdateCancelled)
            fatal = isinstance(error, sf_update.UpdateFatal)
        except Exception as exc:
            # 安装包在运行中被换掉时拿不到 sf_update：分类退回"普通失败"，记录本身照写。
            self.swallow("sf_update_flow.last_download.classify", exc)
        try:
            host._update_last_download = {
                "outcome": ("ok" if error is None else ("cancelled" if cancelled else "failed")),
                "fatal": bool(fatal),
                "error_type": type(error).__name__ if error is not None else "",
                "error": str(error)[:200] if error is not None else "",
                "version": str(getattr(update, "version", "") or ""),
                "silent": bool(background),
                "at": time.time(),
            }
        except Exception as exc:
            self.swallow("sf_update_flow.last_download", exc)

    def downloaded(self, host, update, archive, error):
        prompt_when_ready = bool(getattr(host, "_update_prompt_when_ready", False))
        host._update_prompt_when_ready = False
        host._update_downloading = False
        host._update_cancel = None
        clear_token = getattr(host, "clear_legacy_update_token", None)
        if callable(clear_token):
            clear_token()
        self._record_download(host, update, error, prompt_when_ready)
        close_progress = getattr(host, "close_update_progress", None)
        if callable(close_progress):
            close_progress()
        else:
            host.close_update_flow()
        if getattr(host, "_quitting", False):
            return
        if error is not None and self._is_cancel(error):
            # 2026-11-02 走查 F-H-06：用户自己点了「取消」不是失败，只留一行日志，不再弹
            # 「更新未完成：下载或校验失败」。状态位上面已经放下，下一次下载照常能发起。
            self.log("update download cancelled by user")
            return
        if error is not None:
            self.log("update download failed: %r" % (error,), "WARNING")
            if not prompt_when_ready:
                host._show_update_message("更新未完成",
                                          "下载或校验失败。", "当前版本未受影响。")
            return
        if prompt_when_ready:
            host._update_ready_archive = archive
            host._update_ready_version = update.version
            host._prompt_update(update)
            return
        host._launch_downloaded_update(update, archive)
