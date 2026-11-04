# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_config —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import fcntl
import functools
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from sf_constants import (BG_MODE_DEFAULT, CONFIG_BACKUP_PATH, CONFIG_PATH,
                          CONFIG_SCHEMA_VERSION, DEFAULT_STOCK, LAST_QUOTE_PATH,
                          LEGACY_CONFIG_PATH, MAX_H, MIN_H)
from sf_logging import log_line, log_swallow
from sf_privacy import redact_url, redact_urls_in_text
from sf_utils import normalize_code

# 目标价版从拆分前正式版继承配置的只读来源；独立常量便于测试隔离。
SPLIT_SOURCE_CONFIG_PATH = os.path.expanduser(
    "~/Library/Application Support/StockFloat/config.json")

_config_thread_lock = threading.RLock()
_config_lock_state = threading.local()
_CONFIG_LOCK_TIMEOUT = 0.5


@contextmanager
def config_transaction(path=None, timeout=None):
    """Serialize cooperating configuration writers using a stable advisory lock."""
    path = os.path.abspath(path or CONFIG_PATH)
    timeout = _CONFIG_LOCK_TIMEOUT if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + timeout
    if not _config_thread_lock.acquire(timeout=timeout):
        raise TimeoutError("configuration lock timed out")
    descriptor = None
    try:
        active = getattr(_config_lock_state, "active", {})
        if path in active:
            yield
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path + ".lock", flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("configuration lock is not an owned regular file")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("configuration lock timed out")
                time.sleep(0.02)
        active[path] = True
        _config_lock_state.active = active
        try:
            yield
        finally:
            del active[path]
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _config_thread_lock.release()


def target_tick_publisher_directory():
    """Return the per-user directory holding in-flight publication markers."""
    return os.path.abspath(CONFIG_PATH) + ".target-tick-publishers"


class TickPublisherToken:
    """One live process' claim that it may still publish ticks for a stock.

    The claim must outlive the call that created it whenever the tick is still
    only in memory, so ownership is explicit: ``close()`` releases it and is
    idempotent, letting a caller hand the token to ``TargetTickBuffer`` instead.
    """

    __slots__ = ("_path", "_descriptor")

    def __init__(self, path, descriptor):
        self._path = path
        self._descriptor = descriptor

    def close(self):
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)
        path = self._path
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError as exc:
                log_swallow("sf_config.target_tick_publisher.cleanup", exc)
            # Forget the path only after the removal succeeded or the entry is
            # already gone, so a failed removal can be retried by the next close().
            self._path = None

def target_tick_publisher_open(code):
    """Register an in-flight publisher before any step that can lose the tick.

    Returns ``None`` when the registry itself is unavailable; that is logged and
    the caller must fail closed: without a marker no other process can learn that
    a tick is in flight, so the quote may still be displayed but must not become
    target-price input (reminders pause and the user is warned instead).
    """
    directory = target_tick_publisher_directory()
    name = "%s.%s-%s-%s" % (normalize_code(code), os.getpid(), threading.get_ident(), uuid.uuid4().hex)
    path = os.path.join(directory, name)
    try:
        os.makedirs(directory, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        log_swallow("sf_config.target_tick_publisher_open", exc)
        return None
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
    except OSError as exc:
        os.close(descriptor)
        try:
            os.unlink(path)
        except OSError as unlink_exc:
            log_swallow("sf_config.target_tick_publisher_open.cleanup", unlink_exc)
        log_swallow("sf_config.target_tick_publisher_open", exc)
        return None
    return TickPublisherToken(path, descriptor)


@contextmanager
def target_tick_publisher(code):
    """Expose an in-flight publisher across processes before lock acquisition."""
    token = target_tick_publisher_open(code)
    try:
        yield token
    finally:
        if token is not None:
            token.close()


def target_tick_publisher_status(code):
    """Return INACTIVE, ACTIVE, or UNKNOWN for a stock's publisher registry."""
    prefix = normalize_code(code) + "."
    directory = target_tick_publisher_directory()
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return "INACTIVE"
    except OSError as exc:
        log_swallow("sf_config.target_tick_publisher_status", exc)
        return "UNKNOWN"
    status = "INACTIVE"
    # Scan every entry even once one is live: stale neighbours are pruned on the
    # way, and a name we cannot read must not shield them from that cleanup.
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(directory, name)
        try:
            # 持有者身份取自文件名。目录项在 O_CREAT|O_EXCL 那一刻就带全名，正文却是
            # 第二次系统调用才写的——进程被杀在两次调用之间时只剩一个空文件。若按正文
            # 判活，这只股票会被永久算成"有人在发布"：面板保存与过期清理再也不会放行。
            pid = int(name[len(prefix):].split("-")[0])
            os.kill(pid, 0)
        except ProcessLookupError as exc:
            log_swallow("sf_config.target_tick_publisher.stale", exc)
            # The owner is confirmed gone, so the entry protects nothing even if
            # it cannot be removed now; the next scan retries the removal.
            try:
                os.unlink(path)
            except OSError as unlink_exc:
                log_swallow("sf_config.target_tick_publisher.stale_cleanup", unlink_exc)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            # OverflowError：登记名里的数字超出 C int 时 os.kill 会抛它。
            # 认不出持有者的登记按"可能有人持有"处理，不能让整个状态查询抛出。
            log_swallow("sf_config.target_tick_publisher_status", exc)
            status = "ACTIVE"
        else:
            status = "ACTIVE"
    return status


def target_tick_publisher_active(code):
    """Return whether a live process may still publish this stock."""
    return target_tick_publisher_status(code) != "INACTIVE"


@contextmanager
def target_tick_transaction(*, blocking=True):
    """Serialize tick publication with target cleanup across processes."""
    path = os.path.abspath(CONFIG_PATH) + ".target-ticks.lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("target tick lock is not an owned regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def _config_write_transaction(func):
    @functools.wraps(func)
    def guarded(*args, **kwargs):
        try:
            with config_transaction():
                return func(*args, **kwargs)
        except Exception as exc:
            log_line("configuration transaction failed: %r" % (exc,), "ERROR")
            return False
    return guarded


def _migrate_legacy_config():
    """将拆分前正式配置优先复制到目标价目录，其次兼容旧隐藏路径；绝不删除源。"""
    try:
        os.stat(CONFIG_PATH)
        return
    except FileNotFoundError:
        if os.path.islink(CONFIG_PATH):
            return
    except OSError as exc:
        log_line("config migration skipped: destination unavailable: %r" % (exc,), "WARNING")
        return
    source = (SPLIT_SOURCE_CONFIG_PATH
              if os.path.exists(SPLIT_SOURCE_CONFIG_PATH)
              else LEGACY_CONFIG_PATH)
    if not os.path.exists(source):
        return
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(source, "r", encoding="utf-8") as f:
            d = json.load(f)
        _atomic_write_json(_config_write_path(), d)
        log_line("config migrated (copied) %s -> %s" % (source, CONFIG_PATH))
    except Exception as exc:
        log_line("config migrate failed: %r" % (exc,), "WARNING")

def _read_json_or_none(path):
    """尽力读取 JSON 配置；文件缺失或解析失败一律返回 None（不抛异常）。

    v1.3.42 第4轮走查：编码由 utf-8 改为 **utf-8-sig**。
    用户用「文本编辑/记事本」另存配置时几乎必然带上 UTF-8 BOM（EF BB BF），
    而 utf-8 严格模式下 BOM 会被当成文件首字符 → json 直接抛
    JSONDecodeError → 本函数返回 None。后果是连锁的：

      - load_alerts() 读到空 → 界面上目标价**全部消失**；
      - save_config() 拿不到旧底稿 → 以空 dict 为底稿整体覆盖写回，
        alerts / api_urls 等字段**静默丢失**（保存还返回 True）；
      - _backup_config() 判定"当前不可解析"跳过备份，好数据只留在旧 .bak 里，
        而 load_alerts 又不会去读 .bak —— 用户看到的就是"目标价不见了"。

    utf-8-sig 对无 BOM 的普通 UTF-8 文件行为与 utf-8 完全一致，无副作用。

    本函数保持"一律 None"的老契约（sf_credentials / sf_diagnostics 等外部调用点只关心
    "拿到 dict 没有"）。需要区分"读不出来"与"读到了但内容坏"的配置读写链路，
    一律改用 `_read_json_checked`（2026-11-02 走查 F-C-01）。
    """
    value = _read_json_checked(path)
    return None if value is _UNREADABLE else value


# 2026-11-02 走查 F-C-01：读文件这一步本身失败（权限被改、IO 错误、句柄耗尽……）不是
# "内容损坏"。旧写法把两者都变成 None，于是主配置暂时读不出来时 load_config 会拿更旧的
# .bak 恢复并**写回主配置**（实测 main.x 500→10、alerts 2→0），save_* 也会在这种状态下
# 以备份为底稿覆盖主配置。专用哨兵让写路径能认出"我根本没读到"，宁可这次不保存，
# 也不拿旧快照覆盖一份只是暂时读不了的好数据。
_UNREADABLE = object()
_untrusted_config_path = None


def config_unavailable():
    return _untrusted_config_path == CONFIG_PATH


class ConfigUnreadableError(OSError):
    """主配置存在但读不出来（OSError，非 FileNotFoundError）：写路径必须拒绝覆盖。"""


def _read_json_checked(path):
    """读 JSON，三态返回：解析出的值 / None / `_UNREADABLE`。

    - 文件不存在（FileNotFoundError）→ None：首次启动的正常路径，不记警告；
    - 读到了但不是合法 JSON（含 UnicodeDecodeError、空文件）→ None：内容损坏，
      调用方可按既有口径回落备份；
    - 其它 OSError（PermissionError、EIO、EMFILE、IsADirectoryError……）→ `_UNREADABLE`：
      文件可能完好，只是这一刻读不到，调用方**不得**据此恢复/覆盖。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return _UNREADABLE if os.path.islink(path) else None
    except OSError:
        return _UNREADABLE
    except Exception:
        return None


def _base_dict_for_write(path, backup_path):
    """写入前的旧配置底稿（dict）。主文件不可解析时回落到备份快照。

    v1.3.42 第4轮走查：save_config / save_alerts 原先都在 `except: pass` 里
    静默用空 dict 当底稿。于是只要主配置一次读不出来（BOM / 半截 JSON /
    顶层非对象），下一次保存就会把它**整体覆盖成只有已知字段的残缺配置** ——
    目标价、自定义接口等全部蒸发，而保存仍然返回 True，界面没有任何提示。
    备份快照里明明有一份好数据，却只被 load_config 用于恢复、从不被写路径采用。

    现在：主文件不可用 → 改用备份作底稿（等于顺手把好数据救回来）；
    两者都不可用 → 保留原行为但留 ERROR，不再静默。
    """
    if _untrusted_config_path == path:
        raise ConfigUnreadableError("config must be reloaded before saving: %s" % path)
    try:
        os.stat(path)
    except FileNotFoundError:
        if os.path.islink(path):
            raise ConfigUnreadableError("config is a dangling symlink: %s" % path)
        return {}
    except OSError as exc:
        raise ConfigUnreadableError("config stat failed: %s" % path) from exc
    loaded = _read_json_checked(path)
    if loaded is _UNREADABLE:
        # 2026-11-02 走查 F-C-01：读不出来 ≠ 损坏。这里若回落备份当底稿，写回就等于
        # 用更旧的快照覆盖一份只是暂时读不了的主配置。抛出由 save_* 的外层 except
        # 记 ERROR 并返回 False（走既有的"保存失败"气泡），主配置原封不动。
        log_line("%s 存在但读取失败（权限/IO 错误，不是内容损坏）：本次保存放弃，"
                 "不用备份快照覆盖主配置" % path, "WARNING")
        raise ConfigUnreadableError("config unreadable: %s" % path)
    if isinstance(loaded, dict):
        return loaded
    if loaded is not None:
        log_line("%s 顶层结构非法（%s），旧字段不保留"
                 % (path, type(loaded).__name__), "WARNING")
    else:
        log_line("%s 无法解析，旧字段不保留" % path, "WARNING")
    bak = _read_json_or_none(backup_path)
    if isinstance(bak, dict):
        log_line("%s 不可用，改用备份快照 %s 作为本次保存底稿"
                 % (path, backup_path), "WARNING")
        bak = dict(bak)
        bak["alerts_epoch"] = uuid.uuid4().hex
        return bak
    log_line("%s 不可用且备份快照 %s 也不可用：本次保存只写入已知字段，"
             "其余配置（目标价/自定义接口等）会丢失" % (path, backup_path), "ERROR")
    return {}


def _atomic_write_json(path, payload):
    """原子写入并强制落盘：断电后只会保留旧文件或完整新文件。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # mkstemp atomically creates an exclusive 0600 file on the destination volume.
    descriptor, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                                       dir=directory or ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            descriptor = -1
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # 目录项也要 fsync：只 sync 文件的话，断电时可能丢掉"新文件名"这条
        # 目录记录，os.replace 等于没发生过。
        if directory:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise

def _config_write_path():
    """拒绝写入符号链接，避免覆盖链接或写穿到外部文件。"""
    if os.path.islink(CONFIG_PATH):
        raise ConfigUnreadableError("config is a symlink: %s" % CONFIG_PATH)
    return CONFIG_PATH


def _backup_config():
    """保存新配置前，把当前主配置备份为最近 1 份快照（.bak，覆盖旧快照）。
    仅在主配置已存在且可正常读取时备份；备份失败记录 WARNING 日志，但不阻断
    保存主流程（备份只是保险，不是写盘的前置条件）。
    用 copy2 而非 move：备份期间主配置保持完整，写新配置失败也不会丢旧数据。

    v1.2.71 第4轮走查：备份前先校验主配置**可解析**。原实现无条件 copy2，
    于是主配置被写坏（崩溃/断电留下半截 JSON）后的第一次保存，会把这份坏数据
    存成"备份"、覆盖掉此前唯一的好备份；等到 load_config 发现损坏想恢复时，
    备份也已经是坏的了 —— 用户的自选代码与窗口位置彻底丢失且无从恢复。
    校验不通过时**跳过备份**（保留既有好备份）并留 WARNING，绝不覆盖。
    """
    try:
        if _untrusted_config_path == CONFIG_PATH or os.path.islink(CONFIG_PATH):
            return
        if os.path.islink(CONFIG_BACKUP_PATH):
            log_line("config backup SKIPPED: backup path is a symlink", "WARNING")
            return
        if not os.path.exists(CONFIG_PATH):
            return
        current = _read_json_checked(CONFIG_PATH)
        if not isinstance(current, dict):
            # 2026-11-02 走查 F-C-01：读不出来（_UNREADABLE）同样不备份 —— 此刻没读到
            # 主配置内容，复制出去的未必是好数据；保留既有备份不被覆盖。
            log_line("config backup SKIPPED：%s 当前%s，保留既有备份 %s 不被覆盖"
                     % (CONFIG_PATH,
                        "读取失败" if current is _UNREADABLE else "不可解析",
                        CONFIG_BACKUP_PATH), "WARNING")
            return
        shutil.copy2(CONFIG_PATH, CONFIG_BACKUP_PATH)
    except Exception as exc:
        # v1.2.65 第1轮走查修复：备份失败此前被 `except: pass` 静默吞掉 ——
        # 磁盘满 / 权限不足 / 文件被占用时，用户和日志都不知情。危害在于备份是
        # load_config 主配置损坏时的唯一兜底：备份悄悄失败 + 主配置写坏 = 用户
        # 自选代码与窗口位置全丢且无从恢复。此处必须留 WARNING 便于排查；
        # 同时只记录、不向外抛 —— 备份失败绝不能中断 save_config 的写盘尝试。
        log_line("config backup FAILED %s -> %s: %r "
                 "(主配置损坏时将无法自动恢复)" % (CONFIG_PATH, CONFIG_BACKUP_PATH, exc),
                 "WARNING")

# v1.2.65 第1轮走查修复：配置读回清洗的默认落点与合理区间。
# x/y 只做「类型正确 + 落在合理数值区间」校验，**不**做当前屏幕尺寸钳制 ——
# 那份逻辑在 stock_float_window.py 的 _clamp_to_screen / _keep_on_screen 里已有，
# 此处重复实现会与多屏/空间切换时的实际可用区域冲突。
# ±32768 只是拦住 NaN/inf/1e300 这类脏值，足够容纳任意多屏与负坐标布局。
CONFIG_DEFAULT_X = 120
CONFIG_DEFAULT_Y = 120
CONFIG_XY_MIN = -32768
CONFIG_XY_MAX = 32768

def _clean_code(v):
    """读回的股票代码清洗：必须是非空字符串，否则回退默认代码并记 WARNING。
    不做 sh/sz/bj 前缀强校验 —— 纯数字代码由 sf_utils.normalize_code 正常补全前缀，
    在这里卡前缀会误伤可正常使用的旧配置；本函数只保证类型是 str 且非空，
    脏值（null / 数字 / 空白）不会一路拼进行情 URL。"""
    if isinstance(v, str) and v.strip():
        text = v.strip()
        # 旧版本曾把输入法全角状态下敲的 ６００５１９ 原样写进配置，读回后行情永远是"--"。
        # 只有含全角字符时才改写；纯 ASCII 的写法（含裸码 600519）一律原样保留。
        folded = unicodedata.normalize("NFKC", text)
        return text if folded == text else normalize_code(folded)
    log_line("config code 非法(%r)，回退默认代码 %s" % (v, DEFAULT_STOCK), "WARNING")
    return DEFAULT_STOCK


def _canon_code(value):
    """代码比较用的规范形态：600519 与 sh600519 是同一只股票，全角数字也算。

    只用于"是不是同一只"的判断，不改写存储里的字符串。目标价按 code 分家存储后，
    config 顶层是裸码 600519、编辑器切回来是 sh600519，字符串比较会把它们当成两只，
    切走再切回目标价就读不到了（2026-10-29 走查实测）。
    """
    return normalize_code(unicodedata.normalize("NFKC", str(value or "")))

def _clean_int(v, name, default, lo, hi):
    """读回的数值字段清洗：必须是有限数值且落在 [lo, hi]，否则回退默认值并记 WARNING。
    统一转 int：PyQt6 的 move()/resize() 不接受 float，脏配置里的 120.5 会让
    窗口构造直接 TypeError；bool 在 Python 里是 int 的子类，同样按脏值处理。"""
    n = None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            n = int(v)
        except (ValueError, OverflowError):
            n = None   # NaN / inf
    if n is not None and lo <= n <= hi:
        return n
    log_line("config %s 非法(%r)，回退默认值 %s" % (name, v, default), "WARNING")
    return default

def _as_config_dict(v, path):
    """v1.2.65 第1轮走查修复：读回内容的结构收口 —— JSON 顶层不一定是对象。
    配置文件被别的程序写坏时顶层可能是 list / str / int / bool，此时后续
    d.get(...) 直接抛 AttributeError 崩在 FloatWindow.__init__ 里，表现为
    启动即闪退、无日志、用户无法自救。非 dict 一律按「损坏」处理（回退默认值
    并记 WARNING，写明实际类型便于排查）；None（文件不存在 / 内容为空）是既有的
    正常首装路径，原样返回，不刷日志。"""
    if v is None or isinstance(v, dict):
        return v
    log_line("%s 顶层结构非法（实际类型 %s，应为对象），按配置损坏处理"
             % (path, type(v).__name__), "WARNING")
    return None

# ---------- v1.3.45：首屏数据快照（last_quote.json） ----------
# 目的：冷启动时网络请求再快也要几十毫秒，首帧必然没数据，界面先画占位符
# "--"、等行情到了再整屏换掉 —— 用户看到的就是"闪一下/变形"。
# 把最后一条行情落盘，下次启动第 1 帧直接有价格，真实行情到了只做一次小滚动。
#
# 为什么单独一个文件而不是塞进 config.json：
#   1. 行情每秒都在变，写盘频率远高于配置；混在一起会高频重写用户配置，
#      任何一次写坏都连带把目标价、自定义接口一起毁掉；
#   2. 快照是"可再生的缓存"，丢了最多首屏多显示一次 "--"，
#      配置是"不可再生的用户数据"，两者的可靠性要求完全不同。
def _last_quote_path():
    """首屏快照路径。支持 SF_LAST_QUOTE_PATH 覆盖 —— 测试必须重定向到临时目录，
    否则用例之间会互相"预填"上一次的行情，把「无数据显示 --」这类断言全部打穿，
    也会在跑测试时往用户真实配置目录里写东西。"""
    return os.environ.get("SF_LAST_QUOTE_PATH") or LAST_QUOTE_PATH


def save_last_quote(code, quote, minute_points, day):
    """写首屏快照。任何异常都只记 WARNING —— 缓存写失败不影响行情主流程。"""
    try:
        payload = {
            "code": str(code or ""),
            "day": str(day or ""),
            "ts": time.time(),
            "quote": quote if isinstance(quote, dict) else {},
            "minute": [list(p) for p in (minute_points or [])],
            "minute_day": str(getattr(minute_points, "day", day) or ""),
        }
        _atomic_write_json(_last_quote_path(), payload)
        return True
    except Exception as exc:
        log_line("save_last_quote FAILED: %r" % (exc,), "WARNING")
        return False


def load_last_quote():
    """读首屏快照；缺失/损坏/结构非法一律返回 None（调用方自然退化为占位符）。"""
    d = _read_json_or_none(_last_quote_path())
    if not isinstance(d, dict):
        return None
    if not isinstance(d.get("quote"), dict):
        return None
    return d


def load_config():
    try:
        with config_transaction():
            return _load_config_locked()
    except Exception as exc:
        global _untrusted_config_path
        _untrusted_config_path = CONFIG_PATH
        log_line("config transaction unavailable: %r" % (exc,), "WARNING")
        return DEFAULT_STOCK, 120, 120, MIN_H, BG_MODE_DEFAULT


def _load_config_locked():
    """读主配置，返回主窗启动需要的 5 元组 `(code, x, y, h, bg)`；本函数**不抛错**。

    四个值的口径：
        code  当前股票，带交易所前缀（经 _clean_code，非法形态回落 DEFAULT_STOCK）
        x, y  窗口左上角屏幕坐标（px），越出 CONFIG_XY_MIN..MAX 时回落默认值
        h     收起态窗口高度（px），缺失/非法降级为 MIN_H（v1.2.54：与首装默认一致）
        bg    背景模式，只有 solid/translucent/glass 三种，界面无切换入口 → 正常恒为 glass

    损坏处理是这条链路的重点：主文件解析失败时**先试备份快照**，恢复成功再写回主配置，
    而不是把用户数据当空的（v1.2.71 之前那一步写回失败被 `except: pass` 静默吞掉，
    后果是每次启动都走恢复分支、而日志看不出恢复成没成功）。
    目标价条目不在这里 —— 它由 load_alerts 独立读写，与本 5 元组契约互不干扰。
    """
    global _untrusted_config_path
    _migrate_legacy_config()
    raw = _read_json_checked(CONFIG_PATH)
    if raw is _UNREADABLE:
        _untrusted_config_path = CONFIG_PATH
        # 2026-11-02 走查 F-C-01：主配置在盘上、但这一刻读不出来（权限被改 / IO 错误 /
        # 句柄耗尽）。这不是损坏：**不**走备份恢复（备份通常更旧），更**不**把它写回主配置
        # （旧实现正是这样把更新的主配置覆盖成旧备份）。本次只用默认值启动；之后各 save_*
        # 遇到同样读不出来会拒绝落盘，主配置原封不动，等下次读得出来时数据还在。
        log_line("config.json 存在但读取失败（权限/IO 错误，不是内容损坏）：本次使用默认配置，"
                 "不从备份恢复、也不改写主配置", "WARNING")
        return DEFAULT_STOCK, 120, 120, MIN_H, BG_MODE_DEFAULT
    d = _as_config_dict(raw, CONFIG_PATH)
    restored = False
    if d is None and os.path.exists(CONFIG_PATH):
        # 主配置存在但解析失败（损坏）→ 自动从最近备份快照恢复，避免用户数据全丢
        d = _as_config_dict(_read_json_or_none(CONFIG_BACKUP_PATH), CONFIG_BACKUP_PATH)
        if d is not None:
            log_line("config.json 解析失败，已从备份快照恢复: %s" % CONFIG_BACKUP_PATH, "WARNING")
            restored = True
    if d is None:
        if os.path.exists(CONFIG_PATH):
            # 主配置损坏且备份快照也不可用：降级默认值，但必须留日志便于排查
            log_line("config.json 损坏且备份快照不可用，使用默认配置", "WARNING")
        # v1.2.54：首次安装（无配置文件）默认高度 = 最低高度 MIN_H ——
        # 旧默认 H(192) 低于窗口最小高度限制，靠 Qt 钳制“碰巧”垫到 MIN_H，
        # 默认值本身失真；现直接给出合规的最低高度，首启即紧凑态（如图）
        return DEFAULT_STOCK, 120, 120, MIN_H, BG_MODE_DEFAULT
    # 5.0 v1.2.0：结构版本号兼容——旧配置无 version 视为 v1；仅校验类型，不强制升级
    ver = d.get("version", CONFIG_SCHEMA_VERSION)
    if not isinstance(ver, int):
        ver = CONFIG_SCHEMA_VERSION
    # v1.2.54：高度字段缺失/非法时的降级值同步为最低高度（与首装默认一致）
    h = _clean_int(d.get("h"), "h", MIN_H, MIN_H, MAX_H)
    bg = d.get("bg", BG_MODE_DEFAULT)
    if bg not in ("solid", "translucent", "glass"):
        bg = BG_MODE_DEFAULT
    # v1.3.47：背景模式只有三种，且界面上没有任何切换入口，正常情况下恒为
    # 默认值 glass。读到非默认值说明配置文件被异常写入过（如并发保存踩踏），
    # 这里留一条痕 —— 本次线上 bg 被写成 translucent 导致毛玻璃消失，
    # 排查时全靠日志里有没有这条。
    if bg != BG_MODE_DEFAULT:
        log_line("config bg=%s (default=%s) restored_from_backup=%s"
                 % (bg, BG_MODE_DEFAULT, restored), "WARNING")
    if restored:
        # 恢复旧备份可能回退修订号；让恢复前打开的目标价面板失效。
        d["alerts_revision"] = uuid.uuid4().hex
        d["alerts_epoch"] = uuid.uuid4().hex
        # 从备份恢复成功后写回主配置，避免下次启动仍走恢复分支
        try:
            _atomic_write_json(_config_write_path(), d)
        except Exception as exc:
            # v1.2.71：写回失败原被 `except: pass` 静默吞掉。后果是主配置一直是
            # 坏文件、每次启动都走"从备份恢复"分支，日志里只有一条 WARNING
            # 却看不出恢复到底成没成功 —— 且若此后备份再被覆盖，数据就真丢了。
            log_line("config 从备份恢复成功但写回主配置失败：%r"
                     "（主配置仍为损坏状态，下次启动会再次走恢复分支）" % (exc,), "WARNING")
    return (_clean_code(d.get("code", DEFAULT_STOCK)),
            _clean_int(d.get("x", CONFIG_DEFAULT_X), "x", CONFIG_DEFAULT_X, CONFIG_XY_MIN, CONFIG_XY_MAX),
            _clean_int(d.get("y", CONFIG_DEFAULT_Y), "y", CONFIG_DEFAULT_Y, CONFIG_XY_MIN, CONFIG_XY_MAX),
            h, bg)

@_config_write_transaction
def save_config(code, x, y, h=None, bg=None):
    """把主窗的位置与偏好写回 config.json。返回 bool：True=已落盘，False=没写成。

    两条 False 的路径都要认真对待，调用方（主窗 `_after_save_config`）靠它弹一次性气泡，
    磁盘满/权限问题不能只留在日志里：
        · 存量配置带更高的 schema 版本 → 拒绝降版本覆盖（未来版本由更新版本自己写回）；
        · 备份或原子写抛异常 → ERROR 留痕 + 清掉残留的 .tmp。

    写入形状是"**以旧配置为底稿**整体更新"（v1.2.71 之前只搬已知字段，等于每次保存
    都在静默删掉目标价、自定义接口、用户手工加的任何键，且没有日志痕迹）。
    `h`/`bg` 传 None 表示"这一项不动"；`bg` 非默认值会额外留一条 WARNING（界面上没有
    切换入口，出现非默认值说明配置文件被异常写入过）。
    写之前强制 `_backup_config()`，落盘走 `_atomic_write_json`（含 fsync）。
    """
    try:
        _backup_config()
        # v1.3.42 第4轮走查：底稿读取统一走 _base_dict_for_write ——
        # 主文件不可解析（BOM / 半截 JSON / 顶层非对象）时回落备份快照，
        # 而不是静默以空 dict 为底稿把目标价、自定义接口等整体覆盖掉。
        old = _base_dict_for_write(CONFIG_PATH, CONFIG_BACKUP_PATH)
        # v1.2.71 第4轮走查：以旧配置为**底稿**整体更新，而不是"只挑几个已知
        # 字段搬走"。原实现除 code/x/y/h/bg/version 外只额外搬运 os_major 与
        # api_urls，其余字段（含后续版本新增的任何键、以及用户手工加的配置）
        # 一律被静默丢弃 —— 每次保存都在删数据，且没有任何日志痕迹。
        d = dict(old)
        d.pop("target_flash", None)  # 功能已移除，后续任意保存顺带清理历史残留键
        d["code"] = code
        d["x"] = x
        d["y"] = y
        # 未来版本由更新版本负责写回；旧版只允许保留其字段，禁止降版本覆盖。
        old_version = old.get("version", 1)
        if isinstance(old_version, int) and old_version > CONFIG_SCHEMA_VERSION:
            log_line("save_config skipped: future schema version %s" % old_version, "WARNING")
            return False
        d["version"] = CONFIG_SCHEMA_VERSION
        if h is not None:
            d["h"] = int(h)
        if bg is not None and bg != BG_MODE_DEFAULT:
            log_line("save_config: writing non-default bg=%r" % (bg,), "WARNING")
        d["bg"] = bg if bg is not None else old.get("bg", BG_MODE_DEFAULT)
        # 原子写并强制落盘：断电时不会留下半截配置或缺少目录更新。
        _atomic_write_json(_config_write_path(), d)
        return True
    except Exception as exc:
        # v1.2.39：保存失败不再静默吞掉 —— 记录 ERROR 日志并返回 False，
        # 调用方（主窗）据此弹一次性气泡提示用户（磁盘空间/权限问题）。
        log_line("save_config FAILED for %s: %r" % (code, exc), "ERROR")
        try:
            if os.path.exists(CONFIG_PATH + ".tmp"):
                os.remove(CONFIG_PATH + ".tmp")
        except Exception:
            pass
        return False


# ---- v1.4.40 人脸在场守卫的用户开关 ----
# 为什么单独写两个函数、不塞进 load_config/save_config 的五元组返回值：
# load_config 的返回形状被主窗与二十多个用例按位置解包，加一个布尔要把所有调用点
# 一起改；而 save_config 是"以旧配置为底稿整体写回"，未知键本来就会被保留，
# 所以单独一个键 + 专用读写口，既不动既有契约，也不会被下一次保存踩掉。
FACE_GUARD_CONFIG_KEY = "face_guard"


def load_face_guard_pref():
    """用户有没有开启人脸守卫。**默认 False**：要摄像头权限的功能不能默认打开
    （一开就亮绿灯且无法隐藏）。配置缺失/损坏或不是布尔值一律按未开启处理 —— 与"保护没在跑"
    这个事实一致，而不是把字符串 "false" 误当成已开启。

    主配置暂时读不出来时开关未知，保持关闭，不能凭旧备份启动摄像头。
    """
    raw = _read_json_checked(CONFIG_PATH)
    if raw is _UNREADABLE:
        log_line("config.json 读取失败（权限/IO 错误），人脸守卫开关未知，本次不启用", "WARNING")
        return False
    d = _as_config_dict(raw, CONFIG_PATH)
    if not isinstance(d, dict):
        return False
    value = d.get(FACE_GUARD_CONFIG_KEY, False)
    return value if isinstance(value, bool) else False


@_config_write_transaction
def save_face_guard_pref(enabled):
    """写入用户开关；沿用 save_config 的"先备份 + 以旧配置为底稿 + 原子写"三件套。"""
    try:
        _backup_config()
        old = _base_dict_for_write(CONFIG_PATH, CONFIG_BACKUP_PATH)
        version = old.get("version", 1)
        if isinstance(version, int) and version > CONFIG_SCHEMA_VERSION:
            log_line("save_face_guard_pref skipped: future schema version %s" % version,
                     "WARNING")
            return False
        d = dict(old)
        d[FACE_GUARD_CONFIG_KEY] = bool(enabled)
        _atomic_write_json(_config_write_path(), d)
        return True
    except Exception as exc:
        log_line("save_face_guard_pref FAILED (%s): %r"
                 % ("on" if enabled else "off", exc), "ERROR")
        return False

def load_alerts(code=None):
    """「目标价」独立读取 config 的 alerts 字段；缺失/损坏视为无目标价（返回 []），记 WARNING。

    命名口径（AGENTS §7 词表，2026-10-13 定案）：**target 是产品名**（用户看到的、界面上的、
    `targets.py`/`target_panel.py` 里的都叫 target），**alert 是存储与函数名的旧称** ——
    config 里那个键叫 `alerts`。这一对不再统一：键名与函数名是**对外契约**（改了等于让用户
    已录的目标价读不回来，v1.4.59/1.4.60 那两次丢数据的事故就是这么来的），所以只在这里
    把关系写明，不批量改名。读代码时按"alert == 目标价条目"翻译即可。

    v1.4.59（S4 彻底解）：传 `code` 时只返回属于这只股票的条目 —— 切股不再删数据，
    存储里会同时留着别的股票的条目。
    v1.4.60（复核 F1）：没带 code 的旧条目**归属 config 顶层那只股票**（`d["code"]`），
    不再"对任何股票都可见"；顶层也没有 code 时才退回对谁都可见（宁可多显示，
    不许看不见）。旧口径会让升级带来的条目在切股时显示到别的股票名下，并被下一次
    写入盖章成别的 code —— 原股目标价就此丢失。
    与 load_config 的 5 元组契约互不干扰（load_config 签名不变）。"""
    from sf_constants import TARGET_DIR_ABOVE, TARGET_DIR_BELOW
    global _untrusted_config_path
    if _untrusted_config_path == CONFIG_PATH:
        log_line("配置读取失败，暂停加载目标价；恢复后请重新启动", "WARNING")
        return []
    raw = _read_json_checked(CONFIG_PATH)
    if raw is _UNREADABLE:
        _untrusted_config_path = CONFIG_PATH
        log_line("config.json 读取失败，暂停加载目标价；恢复后请重新启动", "WARNING")
        return []
    d = _as_config_dict(raw, CONFIG_PATH)
    if d is None and os.path.exists(CONFIG_PATH):
        # v1.3.42 第4轮走查：与 load_config 同口径 —— 主配置解析失败时回落备份
        # 快照。原实现直接返回 []，于是配置一旦读不出来（BOM/半截 JSON），
        # 界面上目标价就**整片消失**，而备份里明明有；用户会以为是自己没设过。
        d = _as_config_dict(_read_json_or_none(CONFIG_BACKUP_PATH), CONFIG_BACKUP_PATH)
        if d is not None:
            log_line("config.json 解析失败，load_alerts 已从备份快照读取目标价: %s"
                     % CONFIG_BACKUP_PATH, "WARNING")
    if d is None:
        return []
    alerts = d.get("alerts")
    if not isinstance(alerts, list):
        return []
    out = []
    for a in alerts:
        if not isinstance(a, dict):
            log_line("load_alerts：跳过非法条目 %r" % (a,), "WARNING")
            continue
        if a.get("direction") not in (TARGET_DIR_ABOVE, TARGET_DIR_BELOW):
            log_line("load_alerts：跳过方向非法条目 %r" % (a,), "WARNING")
            continue
        # R46：与 targets._valid_alert 统一口径。旧实现只看 isinstance(t, float)，
        # 于是 NaN/inf 能混进来（_valid_alert 判定为非法，条目会"读得出来却永远
        # 不生效"）；而字符串数字（手工编辑配置/其它版本写入）又被整条丢弃。
        # 现在：字符串能转数字就归一成 float，不能转或非正/非有限则跳过。
        t = a.get("target")
        if isinstance(t, str):
            try:
                t = float(t.strip())
            except (TypeError, ValueError):
                log_line("load_alerts：跳过 target 非数字条目 %r" % (a,), "WARNING")
                continue
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            log_line("load_alerts：跳过 target 类型非法条目 %r" % (a,), "WARNING")
            continue
        t = float(t)
        if t != t or t in (float("inf"), float("-inf")) or t <= 0:
            log_line("load_alerts：跳过 target 非正/非有限条目 %r" % (a,), "WARNING")
            continue
        # session_date 是目标价的"归属交易日"，也是到期/非交易日清空的依据；
        # 缺了就无从判断这条何时失效，只能丢弃。
        if not str(a.get("session_date") or "").strip():
            log_line("load_alerts：跳过缺 session_date 条目 %r" % (a,), "WARNING")
            continue
        if t != a.get("target"):
            a = dict(a)
            a["target"] = t
        out.append(a)
    if code:
        want = str(code)
        # v1.4.60（复核 F1）：无 code 的条目属于 **config 顶层那只股票**，不属于"任何一只"。
        # v1.4.59 的口径是"无主条目对谁都可读"，本意是升级第一次别把用户已有的目标价藏起来，
        # 但它连带出一个丢数据：在 A 看到的旧条目，切到 B 之后仍在 B 的面板上显示，而任何一次
        # B 的写入都会把它们盖章成 code=B（下面 save_alerts 的补码逻辑）—— 切回 A 就一条不剩，
        # 期间还会拿 B 的股价去判 A 的价位、为 A 的目标价发 B 的提醒。
        # 顶层 code 缺失（手工编辑过的配置、首装）时退回"对谁都可见"，宁可多显示不许看不见。
        owner = str(d.get("code") or "").strip()
        out = [a for a in out
               if _canon_code(a.get("code")) == _canon_code(want)
               or (not str(a.get("code") or "").strip()
                   and (not owner or _canon_code(owner) == _canon_code(want)))]
    return out


ALERT_STORE_MAX_CODES = 12        # 休眠股票最多保留几只（S4 彻底解的上限）
ALERT_STORE_MAX_TOTAL = 120       # 存储总条目上限，防无限增长

# 最近一次"超上限剪枝"的现场（复核 F7）。只留最近一次：它要回答的是"我那条目标价
# 什么时候、为什么不见的"，而日志被 7 天轮转挤掉之后这件事就没人能说清。
_last_store_prune: dict = {}


def _record_store_prune(dropped, codes):
    """登记一次剪枝。绝不抛异常——诊断信息值再大也不许把保存流程带崩。"""
    global _last_store_prune
    try:
        _last_store_prune = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "dropped": int(dropped),
                             "pruned_codes": [str(c) for c in codes][:ALERT_STORE_MAX_CODES]}
    except Exception as exc:
        log_swallow("sf_config._record_store_prune", exc)


def store_prune_report():
    """诊断包读取口：{"at","dropped","pruned_codes"}；从没剪过就是 {}。"""
    return dict(_last_store_prune)


def _dormant_of(stored, code, owner=""):
    """取出不属于 `code` 的条目 —— 它们是"休眠条目"，这次写入不许把它们带走。

    带 code 的：code != 当前股票就是休眠，与 v1.4.59 同口径。
    不带 code 的（v1.4.59 及更早写下的旧条目）：v1.4.60 复核 F1 起按**归属**判断 ——
    它们属于 config 顶层那只股票 `owner`。owner 已知且不是当前股票时，它们同样是休眠条目，
    并且顺手把 owner 补上（补了下次就不用再猜；不补就会被这次写入按"当前股票的旧条目"清掉，
    换个地方丢数据）。owner 就是当前股票、或 owner 未知时，维持 v1.4.59 的行为：
    这批条目算当前这只的，跟着本次写入一起被替换。
    """
    want = str(code)
    out = []
    for a in stored:
        have = str(a.get("code") or "").strip()
        if have:
            if _canon_code(have) != _canon_code(want):
                out.append(a)
        elif owner and _canon_code(owner) != _canon_code(want):
            stamped = dict(a)
            stamped["code"] = owner
            out.append(stamped)
    return out


def _session_is_past(alert):
    """仅在顺延后的交易日已确认且超过清空时刻时视为过期。"""
    from targets import _beijing_now, _clear_dt, is_trading_day_confirmed, roll_to_trading_day
    if not isinstance(alert, dict):
        return False
    day = roll_to_trading_day(alert.get("session_date"))
    clear_at = _clear_dt(day)
    return (clear_at is not None
            and is_trading_day_confirmed(clear_at.date()) is True
            and _beijing_now() >= clear_at)


def _prune_store(active, dormant):
    """上限修剪只动**休眠**条目：当前股票的条目永远不因为"别人太多"被丢掉。

    2026-11-02 走查 F-B-01：存储顺序是「刚写入的排最前」（见 save_alerts 的 active + dormant），
    所以最早写入的在尾部。旧实现从头部剪，剪掉的恰是刚切走的那只，而别的股票那些早已过期、
    永远没人清理的条目反而占满名额。现在：先丢已过期的休眠条目（它们本来就要被清掉），
    还超限再从尾部（最早写入）按股票剪。股票按规范代码计数，600519 与 sh600519 是同一只。
    """
    keep = list(active) + list(dormant)
    if len(keep) <= ALERT_STORE_MAX_TOTAL:
        codes = {_canon_code(a.get("code")) for a in dormant if str(a.get("code") or "")}
        if len(codes) <= ALERT_STORE_MAX_CODES:
            return keep, 0
    dropped = 0
    pruned_codes = []
    # 第一步：已过期的休眠条目没有任何保留价值，先丢。
    live = []
    for a in dormant:
        if _session_is_past(a):
            c = _canon_code(a.get("code")) or str(a.get("code"))
            if c not in pruned_codes:
                pruned_codes.append(c)
        else:
            live.append(a)
    dropped += len(dormant) - len(live)
    dormant = live
    # 第二步：还超限，从尾部（最早写入）按股票剪。
    dormant_codes = []
    for a in dormant:
        c = _canon_code(a.get("code")) or str(a.get("code"))
        if c not in dormant_codes:
            dormant_codes.append(c)
    while dormant and (len(active) + len(dormant) > ALERT_STORE_MAX_TOTAL
                       or len(dormant_codes) > ALERT_STORE_MAX_CODES):
        victim = dormant_codes.pop()
        before = len(dormant)
        dormant = [a for a in dormant
                   if (_canon_code(a.get("code")) or str(a.get("code"))) != victim]
        dropped += before - len(dormant)
        pruned_codes.append(victim)
    if dropped:
        log_line("目标价存储超上限，已丢弃 %d 条休眠条目（保留最近 %d 只股票）"
                 % (dropped, ALERT_STORE_MAX_CODES), "WARNING")
        # 复核 F7：光写日志等于"用户问不出条目为什么少了"。界面不加弹窗（v1.4.52 的
        # 口径），但诊断包必须能回答：什么时候剪的、剪了几条、剪掉哪几只股票。
        _record_store_prune(dropped, pruned_codes)
    return list(active) + dormant, dropped


def alerts_revision(code=None):
    """Return structural revision globally or for one stock, including legacy data."""
    raw = _read_json_checked(CONFIG_PATH)
    from_backup = False
    if raw is _UNREADABLE or config_unavailable():
        raise ConfigUnreadableError("cannot read target revision")
    if not isinstance(raw, dict):
        if os.path.exists(CONFIG_PATH):
            raw = _read_json_checked(CONFIG_BACKUP_PATH)
            if not isinstance(raw, dict):
                raise ConfigUnreadableError("cannot read target revision from backup")
            from_backup = True
        else:
            raw = {}
    if code is not None:
        revisions = raw.get("alerts_revisions")
        revisions = revisions if isinstance(revisions, dict) else {}
        owner = _canon_code(raw.get("code"))
        wanted = _canon_code(code)
        items = raw.get("alerts")
        items = items if isinstance(items, list) else []
        active = [a for a in items if isinstance(a, dict) and
                  (_canon_code(a.get("code")) == wanted or
                   (not str(a.get("code") or "").strip() and (not owner or owner == wanted)))]
        structural = [{k: v for k, v in alert.items() if k != "code"} for alert in active]
        epoch = ("backup", raw.get("alerts_epoch")) if from_backup else raw.get("alerts_epoch")
        return (epoch, revisions.get(wanted), tuple(_alert_structure(structural)))
    return raw.get("alerts_revision")


def _alert_structure(alerts):
    values = [{key: value for key, value in alert.items()
               if key not in ("state", "last_side")}
              for alert in alerts if isinstance(alert, dict)]
    return sorted((json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                   for value in values))


@contextmanager
def expect_alerts_unchanged(code, alerts):
    previous = getattr(_config_lock_state, "expected_alerts", None)
    _config_lock_state.expected_alerts = (code, alerts)
    try:
        yield
    finally:
        _config_lock_state.expected_alerts = previous


TARGET_NOTIFICATION_OUTBOX_MAX = 1024
_TARGET_NOTIFICATION_OUTBOX_KEY = "target_notification_outbox"


def _outbox_base():
    if config_unavailable() or os.path.islink(CONFIG_PATH):
        raise ConfigUnreadableError("configuration unavailable")
    try:
        os.stat(CONFIG_PATH)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigUnreadableError("configuration stat failed") from exc
    raw = _read_json_checked(CONFIG_PATH)
    if not isinstance(raw, dict):
        raise ConfigUnreadableError("configuration unreadable or corrupt")
    return raw


def _validated_outbox(value):
    if not isinstance(value, list) or len(value) > TARGET_NOTIFICATION_OUTBOX_MAX:
        raise ConfigUnreadableError("invalid notification outbox")
    ids = set()
    for event in value:
        if not isinstance(event, dict):
            raise ConfigUnreadableError("invalid notification event")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip() or event_id in ids:
            raise ConfigUnreadableError("invalid or duplicate notification event id")
        ids.add(event_id)
    return value


def load_target_notification_outbox():
    """Return pending events; raise ConfigUnreadableError instead of using a backup."""
    try:
        with config_transaction():
            config = _outbox_base()
            return _validated_outbox(config.get(_TARGET_NOTIFICATION_OUTBOX_KEY, []))
    except ConfigUnreadableError:
        raise
    except Exception as exc:
        raise ConfigUnreadableError("cannot read notification outbox") from exc


@_config_write_transaction
def sync_target_notification_outbox(expected_events):
    """Confirm exact pending events and durably rewrite the current main config."""
    try:
        expected = _validated_outbox(expected_events)
        if not expected:
            return False
        current = _outbox_base()
        version = current.get("version", 1)
        if isinstance(version, int) and version > CONFIG_SCHEMA_VERSION:
            return False
        events = _validated_outbox(current.get(_TARGET_NOTIFICATION_OUTBOX_KEY, []))
        by_id = {event["id"]: event for event in events}
        if any(by_id.get(event["id"]) != event for event in expected):
            return False
        with open(_config_write_path(), "rb") as current_file:
            os.fsync(current_file.fileno())
        if os.path.islink(CONFIG_BACKUP_PATH):
            raise ConfigUnreadableError("notification backup is a symlink")
        _atomic_write_json(CONFIG_BACKUP_PATH, current)
        directory = os.path.dirname(CONFIG_PATH) or "."
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except Exception as exc:
        log_line("sync_target_notification_outbox FAILED: %r" % (exc,), "ERROR")
        return False


@_config_write_transaction
def ack_target_notification_outbox(event_ids):
    """Remove pending event IDs idempotently; return False on any write failure."""
    try:
        if not isinstance(event_ids, (list, tuple, set, frozenset)) or any(
                not isinstance(item, str) or not item.strip() for item in event_ids):
            return False
        old = _outbox_base()
        version = old.get("version", 1)
        if isinstance(version, int) and version > CONFIG_SCHEMA_VERSION:
            return False
        events = _validated_outbox(old.get(_TARGET_NOTIFICATION_OUTBOX_KEY, []))
        ids = set(event_ids)
        remaining = [event for event in events if event["id"] not in ids]
        if len(remaining) == len(events):
            if os.path.exists(CONFIG_PATH):
                with open(_config_write_path(), "rb") as current:
                    os.fsync(current.fileno())
                directory = os.path.dirname(CONFIG_PATH) or "."
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return True
        updated = dict(old)
        updated[_TARGET_NOTIFICATION_OUTBOX_KEY] = remaining
        _backup_config()
        _atomic_write_json(_config_write_path(), updated)
        return True
    except Exception as exc:
        log_line("ack_target_notification_outbox FAILED: %r" % (exc,), "ERROR")
        return False


@_config_write_transaction
def save_alerts(alerts, code=None, *, outbox_events=None):
    """「目标价」独立写入 config 的 alerts 字段（原子写 + 备份），保留其他字段。

    v1.4.59（S4 彻底解）：给了 `code` 时，这次写入**只替换属于这只股票的条目**，
    其它股票的条目原样留在存储里（切股不再等于删数据）。同时给没有 code 的条目
    补上当前 code —— 不补的话，下一次切走再切回，这些"无主"条目会被当成别人的
    而清掉，等于换了个地方丢数据。上限修剪只动休眠条目。
    v1.4.60（复核 F1）：什么叫"属于当前这只"有了唯一判据 —— 无 code 条目归属 config
    顶层那只股票。切到别的股票时写入，它们按 `_dormant_of` 进休眠（并补上 owner 的 code），
    不再被这次写入替换掉；在归属股票自己那里写入，仍是"跟着本次一起被替换"。
    返回 True/False；失败时由调用方走 _after_save_config 失败气泡。
    v6 §5.5：save_config 现已 `d = dict(old)` 全字段保留，故 alerts 不会被任意保存冲掉；
    本函数仅负责 alerts 字段自身的读写，不改动 save_config。"""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        expected = getattr(_config_lock_state, "expected_alerts", None)
        if expected is not None and (code != expected[0] or load_alerts(code) != expected[1]):
            log_line("save_alerts skipped: target snapshot changed", "WARNING")
            return False
        # Outbox transactions must never replace the main file with an older backup.
        if outbox_events is None:
            _backup_config()
            old = _base_dict_for_write(CONFIG_PATH, CONFIG_BACKUP_PATH)
        else:
            old = _outbox_base()
        d = dict(old)
        if outbox_events is not None:
            events = _validated_outbox(old.get(_TARGET_NOTIFICATION_OUTBOX_KEY, []))
            _validated_outbox(outbox_events)
            d[_TARGET_NOTIFICATION_OUTBOX_KEY] = _validated_outbox(events + outbox_events)
            _backup_config()
        old_version = old.get("version", 1)
        if isinstance(old_version, int) and old_version > CONFIG_SCHEMA_VERSION:
            log_line("save_alerts skipped: future schema version %s" % old_version, "WARNING")
            return False
        if outbox_events is None:
            d.pop("target_flash", None)  # Preserve legacy save behavior.
        active = [dict(a) for a in (alerts or []) if isinstance(a, dict)]
        if code:
            for a in active:
                if not str(a.get("code") or "").strip():
                    a["code"] = str(code)
            stored = d.get("alerts")
            stored = stored if isinstance(stored, list) else []
            stored = [a for a in stored if isinstance(a, dict)]
            # v1.4.60（复核 F1）：无 code 的旧条目属于顶层 code 那只股票，不属于"当前这只"。
            owner = str(d.get("code") or "").strip()
            dormant = _dormant_of(stored, code, owner)
            durable_enabled = (os.environ.get("SF_TARGET_TICKS_PATH") is not None or
                               os.environ.get("SF_TARGET_TICKS_DISABLE") != "1")
            dormant_codes = {_canon_code(a.get("code")) for a in dormant
                             if str(a.get("code") or "")}
            if durable_enabled:
                combined = active + dormant
                previous_codes = {_canon_code(a.get("code")) for a in stored
                                  if str(a.get("code") or "")}
                combined_codes = {_canon_code(a.get("code")) for a in combined
                                  if str(a.get("code") or "")}
                too_many = (len(combined) > ALERT_STORE_MAX_TOTAL and len(combined) > len(stored))
                too_many_codes = (len(dormant_codes) > ALERT_STORE_MAX_CODES and
                                  len(combined_codes) > len(previous_codes))
                if too_many or too_many_codes:
                    log_line("save_alerts refused: target storage limit while durable ticks enabled", "WARNING")
                    return False
                d["alerts"], dropped = combined, 0
            else:
                d["alerts"], dropped = _prune_store(active, dormant)
            if dropped:
                kept_ids = {id(item) for item in d["alerts"]}
                affected = {_canon_code(item.get("code")) for item in dormant
                            if id(item) not in kept_ids}
                revisions = d.get("alerts_revisions")
                revisions = dict(revisions) if isinstance(revisions, dict) else {}
                for affected_code in affected:
                    revisions[affected_code] = uuid.uuid4().hex
                d["alerts_revisions"] = revisions
        else:
            d["alerts"] = active
        previous_alerts = old.get("alerts")
        previous_alerts = previous_alerts if isinstance(previous_alerts, list) else []
        if _alert_structure(previous_alerts) != _alert_structure(d["alerts"]):
            d["alerts_revision"] = uuid.uuid4().hex
            if code:
                wanted = _canon_code(code)
                owner = _canon_code(old.get("code"))
                previous_current = [a for a in previous_alerts if isinstance(a, dict) and
                                    (_canon_code(a.get("code")) == wanted or
                                     (not str(a.get("code") or "").strip() and
                                      (not owner or owner == wanted)))]
                previous_current = [{k: v for k, v in a.items() if k != "code"}
                                    for a in previous_current]
                current = [{k: v for k, v in a.items() if k != "code"} for a in active]
                if _alert_structure(previous_current) != _alert_structure(current):
                    revisions = d.get("alerts_revisions")
                    revisions = dict(revisions) if isinstance(revisions, dict) else {}
                    revisions[wanted] = uuid.uuid4().hex
                    d["alerts_revisions"] = revisions
            else:
                d["alerts_epoch"] = uuid.uuid4().hex
        _atomic_write_json(_config_write_path(), d)
        if outbox_events is not None:
            if os.path.islink(CONFIG_BACKUP_PATH):
                raise ConfigUnreadableError("notification backup is a symlink")
            _atomic_write_json(CONFIG_BACKUP_PATH, d)
        return True
    except Exception as exc:
        log_line("save_alerts FAILED: %r" % (exc,), "ERROR")
        return False


# v1.2.71 第4轮走查：诊断导出脱敏（纯函数，便于单测）。
# 背景：「导出诊断包」会把 config.json 原文打进 zip 发到桌面，而这份 zip 的
# 用途正是**发给别人排障**。用户自定义的 api_urls 里可能带第三方行情的
# token/sign（形如 ?token=xxx），原文打包等于把凭据交给陌生人。
# 规则只抹掉查询串中敏感参数的**值**，保留 scheme/host/path 与其余参数 ——
# 既切断泄露路径，又不损失"接口模板写错了吗"这类排障信息。
# 与 sf_network._SENSITIVE_QUERY_KEYS 保持同一安全口径：诊断包的风险与
# 网络日志相同，不能出现"日志已遮住、诊断包反而漏出"的两套规则。
_REDACT_PARAM_RE = re.compile(
    r"((?:token|key|secret|sign|password|cookie|auth|ticket|session|jwt|code2session)[^=&]*)=([^&]*)",
    re.IGNORECASE)
_REDACT_KEY_TOKENS = ("token", "secret", "password", "passwd", "cookie",
                      "auth", "apikey", "api_key", "key", "sign", "ticket",
                      "session", "jwt", "access_token", "refresh_token",
                      "code2session")
_REDACTED = "***REDACTED***"


def _redact_url(v):
    """解码参数名后脱敏，同时保留行情 URL 的 %s 模板。"""
    return redact_url(v, _REDACTED)


# v1.3.42 第10轮走查：本机家目录路径脱敏。诊断日志/配置里大量出现
# /Users/<用户名>/... 与 C:\Users\<用户名>\... —— 用户名是稳定的设备标识，
# 诊断包一旦外发就等于把机器主人一起交出去。只遮用户名段，路径结构保留，
# 排障时仍看得出"读的是家目录下的哪个文件"。
_HOME_PATH_RES = (
    re.compile(r"((?:/Users/|/home/))([^/\s\"'\\]+)"),
    re.compile(r"([A-Za-z]:\\\\?Users\\\\?)([^\\\\\s\"']+)"),
)


def _redact_home_path(text):
    """把家目录里的本机用户名替换为 <USER>（非字符串原样返回）。"""
    if not isinstance(text, str):
        return text
    out = _HOME_PATH_RES[0].sub(lambda m: m.group(1) + "<USER>", text)
    out = _HOME_PATH_RES[1].sub(lambda m: m.group(1) + "<USER>", out)
    return out


def redact_log_text(text):
    """诊断日志脱敏：屏蔽 URL 凭据、请求头凭据与常见 secret 键值。"""
    if not isinstance(text, str):
        return ""
    # 先抹掉 URL 里的 user:pass@ —— 自定义接口带 basic-auth 时，用户名密码就在这一段。
    out = redact_urls_in_text(text, _REDACTED)
    out = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@",
                 r"\1", out)
    # 日志 URL 查询串按 ?/& 边界替换，避免把同一行后续的普通参数或日志字段吞掉。
    out = re.sub(r"([?&](?:token|key|secret|sign|password|cookie|auth|ticket|session|jwt|code2session)[^=&]*=)[^&\s]*",
                 lambda m: m.group(1) + _REDACTED, out, flags=re.IGNORECASE)
    # Authorization 常带 "Bearer <token>" 两段值，先整体遮住再处理普通键值。
    out = re.sub(r"\bauthorization\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
                 "Authorization=" + _REDACTED, out, flags=re.IGNORECASE)
    # v1.3.42 第10轮走查：键值两侧允许成对引号。原正则要求键名后紧跟空格/冒号，
    # 而网络调试日志里最常见的是 **JSON 响应体**（{"token": "xxx"}）—— 键名后面
    # 是引号，匹配不上，整段凭据原样进了诊断包。
    key_re = re.compile(r"[\"']?(cookie|token|api[_-]?key|secret|password|passwd|auth|session|jwt|ticket)[\"']?\s*[:=]\s*[\"']?([^\s,;&\"']+)", re.IGNORECASE)
    out = key_re.sub(lambda m: m.group(1) + "=" + _REDACTED, out)
    return _redact_home_path(out)


def redact_config(d):
    """返回脱敏后的配置副本（不修改入参），供诊断导出使用。

    键名含 token/secret/password/cookie/auth 的字段整体脱敏（不论类型）；
    字符串值按 _redact_url 处理；dict/list **递归**，其余类型原样保留。
    递归不能只做一层：用户自定义的 api_urls 之外也可能嵌套认证信息（例如
    {"provider": {"auth": {"token": "..."}}}），诊断包一旦外发就不可收回。
    """
    def _redact(v, key=""):
        lk = str(key).lower()
        if any(t in lk for t in _REDACT_KEY_TOKENS):
            return _REDACTED
        if isinstance(v, dict):
            return {k: _redact(item, k) for k, item in v.items()}
        if isinstance(v, list):
            return [_redact(item) for item in v]
        if isinstance(v, str):
            # v1.3.42：URL 凭据与家目录用户名都要遮（配置里可能存自定义路径）。
            return _redact_home_path(_redact_url(v))
        return v

    return _redact(d) if isinstance(d, dict) else {}
