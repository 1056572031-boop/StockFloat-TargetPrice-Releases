# -*- coding: utf-8 -*-
"""受限更新暂存区的身份、重放记录与单写入者协调。"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Iterator

import fcntl

from sf_constants import UPDATE_STAGING_FREE_MARGIN_BYTES

CACHE_SCHEMA = 1
_CACHE_NAME = "update-cache.json"
_LOCK_NAME = ".update-cache.lock"
_ARCHIVE_RE = re.compile(
    r"Skyrocket-\d+\.\d+\.\d+(?:-s\d+-[0-9a-f]{8})?\.zip"
    r"(?:\.part|\.resume\.json)?\Z", re.ASCII)
_LAYOUT_NAMES = (_CACHE_NAME, _LOCK_NAME, "retired")
_MAX_RESUME_PARTS = 16


class UpdateCacheError(RuntimeError):
    """更新缓存的本地文件边界错误。"""


class UpdateCacheBusyError(UpdateCacheError):
    """另一个下载者正在修改暂存目录。"""


class UpdateCacheSpaceError(UpdateCacheError):
    """暂存目录没有完成下载及原子交付所需的空间。"""


class UpdateCache:
    """唯一拥有更新缓存元数据与身份相关文件名的服务。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._ensure_root()

    def _ensure_root(self) -> None:
        try:
            if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
                raise UpdateCacheError("更新暂存目录不安全")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise UpdateCacheError("更新暂存目录不安全")
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise UpdateCacheError("更新暂存目录不可用") from exc

    def identity(self, info: Any) -> tuple[str, int | None, str]:
        sequence = info.release_sequence if type(info.release_sequence) is int else None
        digest = info.archive_sha256 if isinstance(info.archive_sha256, str) else ""
        if not digest:
            return (str(info.version), None, "")
        return (str(info.version), sequence, digest)

    def _stem(self, info: Any) -> str:
        version, sequence, digest = self.identity(info)
        if sequence is None:
            return "Skyrocket-%s" % version
        return "Skyrocket-%s-s%d-%s" % (version, sequence, digest[:8])

    def archive_path(self, info: Any) -> Path:
        return self.root / (self._stem(info) + ".zip")

    def part_path(self, info: Any) -> Path:
        return self.root / (self._stem(info) + ".zip.part")

    def resume_path(self, info: Any) -> Path:
        return self.root / (self._stem(info) + ".zip.resume.json")

    def _state_path(self) -> Path:
        return self.root / _CACHE_NAME

    def _ensure_layout_safe(self) -> None:
        for path in (self.root / name for name in _LAYOUT_NAMES):
            if path.is_symlink():
                raise UpdateCacheError("更新暂存布局不安全")
        try:
            for path in self.root.iterdir():
                if path.is_symlink():
                    raise UpdateCacheError("更新暂存目录含不安全符号链接")
            retired = self.root / "retired"
            if retired.is_dir():
                for path in retired.rglob("*"):
                    if path.is_symlink():
                        raise UpdateCacheError("更新回收目录含不安全符号链接")
        except OSError as exc:
            raise UpdateCacheError("更新暂存布局不可读") from exc

    def _read_state(self) -> dict:
        self._ensure_layout_safe()
        target = self._state_path()
        if not target.exists():
            return {"schema": CACHE_SCHEMA}
        if target.is_symlink():
            raise UpdateCacheError("更新缓存状态不安全")
        self._safe_entry(target)
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise UpdateCacheError("更新缓存状态损坏或不可读") from exc
        if not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA:
            raise UpdateCacheError("更新缓存状态版本无效")
        return value

    def _write_state(self, state: dict) -> None:
        self._ensure_layout_safe()
        state["schema"] = CACHE_SCHEMA
        target = self._state_path()
        temporary = target.with_name(target.name + ".tmp-%d-%d" % (os.getpid(), time.time_ns()))
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(state, sort_keys=True, separators=(",", ":")))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _record_identity(self, state: dict[str, Any], info: Any) -> None:
        version, sequence, digest = self.identity(info)
        state["identity"] = {"version": version, "release_sequence": sequence, "sha256": digest}

    def accept_manifest_timestamp(self, published_at: str | None) -> bool:
        if published_at is None:
            return True
        state = self._read_state()
        latest = state.get("latest_published_at")
        if isinstance(latest, str) and published_at < latest:
            return False
        state["latest_published_at"] = published_at
        self._write_state(state)
        return True

    def accept_manifest_identity(self, info: Any) -> bool:
        """调用方持锁；持久记录现代清单时间和身份，拒绝降级及同时间异身份。"""
        state = self._read_state()
        latest = state.get("latest_published_at")
        sequence = getattr(info, "release_sequence", None)
        if sequence is None:
            return not isinstance(latest, str)
        published_at = getattr(info, "published_at", None)
        if not isinstance(published_at, str):
            return False
        identity = list(self.identity(info))
        manifest_digest = getattr(info, "manifest_digest", None)
        if isinstance(latest, str):
            if published_at < latest:
                return False
            previous = state.get("latest_manifest_identity")
            previous_digest = state.get("latest_manifest_digest")
            # 历史 identity 是包缓存身份，与最新清单时间并非原子绑定，不能据此推断同源。
            if published_at == latest and previous is not None and previous != identity:
                return False
            if (published_at == latest and previous_digest is not None and
                    previous_digest != manifest_digest):
                return False
        state["latest_published_at"] = published_at
        state["latest_manifest_identity"] = identity
        if manifest_digest is not None:
            state["latest_manifest_digest"] = manifest_digest
        elif published_at != latest:
            state.pop("latest_manifest_digest", None)
        self._write_state(state)
        return True

    @staticmethod
    def _safe_entry(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            raise UpdateCacheError("更新暂存文件不可读") from exc
        if not stat.S_ISREG(mode):
            raise UpdateCacheError("更新暂存文件类型不安全")

    def ensure_identity_paths_safe(self, info: Any) -> None:
        self._ensure_layout_safe()
        for path in (self.archive_path(info), self.part_path(info), self.resume_path(info)):
            if not _inside_root(path, self.root):
                raise UpdateCacheError("更新暂存文件不安全")
            self._safe_entry(path)

    def ensure_resume_path_safe(self, info: Any) -> None:
        self.ensure_identity_paths_safe(info)

    def validate_cached_archive(self, info: Any) -> Path | None:
        with self.write_lock():
            return self.validate_cached_archive_locked(info)

    def validate_cached_archive_locked(self, info: Any) -> Path | None:
        """Validate while the caller holds the cache's exclusive write lock."""
        self.ensure_identity_paths_safe(info)
        archive = self.archive_path(info)
        valid = _file_matches(archive, info.archive_size, info.archive_sha256)
        state = self._read_state()
        self._record_identity(state, info)
        state["startup_verification"] = {"result": "valid" if valid else "invalid"}
        self._write_state(state)
        if valid:
            return archive
        archive.unlink(missing_ok=True)
        return None

    def invalidate_stale(self, info: Any) -> None:
        self._ensure_layout_safe()
        current = {self.archive_path(info).name, self.part_path(info).name, self.resume_path(info).name}
        retired = self.root / "retired"
        for path in self.root.iterdir():
            if path.name in current or not path.is_file() or path.is_symlink() or not _ARCHIVE_RE.fullmatch(path.name):
                continue
            retired.mkdir(mode=0o700, exist_ok=True)
            os.chmod(retired, 0o700)
            target = retired / path.name
            if target.exists():
                target = retired / (path.name + ".%d" % time.time_ns())
            try:
                os.replace(path, target)
            except OSError:
                shutil.move(str(path), str(target))
        state = self._read_state()
        self._record_identity(state, info)
        self._write_state(state)

    def mark_resume(self, info: Any, completed_parts: set[int]) -> None:
        self.ensure_resume_path_safe(info)
        completed = sorted({int(part) for part in completed_parts
                            if isinstance(part, int) and part >= 0})
        resume = {"version": info.version, "release_sequence": info.release_sequence,
                  "size": info.archive_size, "sha256": info.archive_sha256,
                  "completed": completed}
        state = self._read_state()
        self._record_identity(state, info)
        state["resume_completed"] = completed
        target = self.resume_path(info)
        temporary = target.with_name(target.name + ".tmp-%d" % os.getpid())
        try:
            temporary.write_text(json.dumps(resume, separators=(",", ":")), encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self._write_state(state)

    def load_resume(self, info: Any) -> set[int]:
        self.ensure_resume_path_safe(info)
        target = self.resume_path(info)
        if not target.exists():
            return set()
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        if not isinstance(value, dict):
            return set()
        expected = {"version": info.version, "release_sequence": info.release_sequence,
                    "size": info.archive_size, "sha256": info.archive_sha256}
        if any(value.get(key) != expected[key] for key in expected):
            return set()
        max_parts = min(_MAX_RESUME_PARTS, max(1, info.archive_size))
        completed = value.get("completed")
        if not isinstance(completed, list):
            return set()
        return {part for part in completed
                if type(part) is int and 0 <= part < max_parts}

    def preserve_interrupted_download(self, info: Any) -> None:
        self.mark_resume(info, self.load_resume(info))

    def discard_partial_download(self, info: Any) -> None:
        self.ensure_identity_paths_safe(info)
        self.part_path(info).unlink(missing_ok=True)
        self.resume_path(info).unlink(missing_ok=True)
        state = self._read_state()
        if state.get("identity") == {"version": info.version, "release_sequence": info.release_sequence,
                                     "sha256": info.archive_sha256}:
            state.pop("resume_completed", None)
            self._write_state(state)

    def require_space(self, info: Any) -> None:
        required = info.archive_size * 2 + UPDATE_STAGING_FREE_MARGIN_BYTES
        usage = shutil.disk_usage(self.root)
        free = usage.free if hasattr(usage, "free") else usage[2]
        if free < required:
            raise UpdateCacheSpaceError("更新暂存空间不足")

    @contextlib.contextmanager
    def write_lock(self) -> Iterator["UpdateCache"]:
        self._ensure_layout_safe()
        lock_path = self.root / _LOCK_NAME
        if lock_path.is_symlink():
            raise UpdateCacheError("更新锁文件不安全")
        self._safe_entry(lock_path)
        handle = lock_path.open("a+")
        os.chmod(lock_path, 0o600)
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateCacheBusyError("更新下载正在进行") from exc
            yield self
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def diagnostics(self) -> dict[str, Any]:
        state = self._read_state()
        return {"schema": CACHE_SCHEMA, "identity": state.get("identity"),
                "latest_published_at": state.get("latest_published_at"),
                "startup_verification": state.get("startup_verification")}


def _inside_root(path: Path, root: Path) -> bool:
    try:
        return path.parent.resolve(strict=False) == root.resolve(strict=True)
    except OSError:
        return False


def _file_matches(path, expected_size, expected_sha256):
    try:
        if path.is_symlink() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False
