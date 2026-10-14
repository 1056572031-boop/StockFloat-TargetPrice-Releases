# -*- coding: utf-8 -*-
"""sf_logging 基础设施补测：功能开关解析、日志轮转代际、系统日志格式串、崩溃扫描。

为什么单独补这一层（本轮为扩大自动化覆盖而加）
----------------------------------------------
sf_logging 是**全仓唯一的排障出口**：200+ 处兜异常最终都要落到 `log_line`，
功能开关决定 v1.4.32 的 system_drag 这类行为是否生效。它自己坏掉时表现是
"日志莫名少一截 / 某个开关静默失效"，正好是本项目最怕的那类缺陷。

本文件不重复 test_v1295_logging.py 与 test_arch_constraints.py 已守的内容
（节流计数、SWALLOWED 前缀），只补它们没走到的分支：
  · 功能开关的两条配置来源（环境变量 / features.ini）与"二选一"语义
  · 多代轮转的**搬迁方向**（正序会把整条历史压成同一份）
  · NSLog 的格式串参数化（'%' 当格式串会读栈垃圾，最坏崩溃）
  · 陈旧 socket 判定必须用 lstat（符号链接不得被当成 socket 删掉）
  · 启动崩溃扫描的三条分支

说明：这里大量直接调用下划线开头的函数，是因为它们**没有公开入口** ——
`_load_feature_flags` 在 import 时执行一次、`_shift_log_generations` 只被
轮转内部调用。按 AGENTS.md 第 6 节第 5 条，逐处写明"为什么必须看内部"。
"""
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_constants          # noqa: E402
import sf_logging            # noqa: E402

try:
    from _base import SFCase
except ImportError:         # pragma: no cover - 包模式导入
    from tests._base import SFCase


# --------------------------------------------------------------------------
# 功能开关
# --------------------------------------------------------------------------

class TestFeatureFlagParsing(SFCase):
    """`STOCKFLOAT_FEATURES` 的四种写法必须各自解析正确。

    私有函数直调原因：开关在 import 时就固化进 `_FEATURES`，除了直接调
    `_load_feature_flags()` 没有第二条路能观察到解析过程。
    """

    def _flags(self, env=None):
        old = os.environ.get("STOCKFLOAT_FEATURES")
        if env is None:
            os.environ.pop("STOCKFLOAT_FEATURES", None)
        else:
            os.environ["STOCKFLOAT_FEATURES"] = env
        try:
            # 仓库里不放 features.ini（已核实不存在）；临时挪开 __file__ 所在目录，
            # 保证 ini 分支不会被将来某天出现的本地 ini 干扰。
            with mock.patch.object(sf_logging, "__file__",
                                   os.path.join(tempfile.mkdtemp(), "sf_logging.py")):
                return sf_logging._load_feature_flags()
        finally:
            if old is None:
                os.environ.pop("STOCKFLOAT_FEATURES", None)
            else:
                os.environ["STOCKFLOAT_FEATURES"] = old

    def test_bare_name_enables_and_defaults_are_kept_for_absent_keys(self):
        flags = self._flags("!conn_pool")
        self.assertFalse(flags["conn_pool"], "叹号前缀=关闭")
        # 未提及的开关保持常量默认，不能被解析过程清成 False
        self.assertEqual(flags["net_monitor"],
                         sf_constants._FEATURE_DEFAULTS["net_monitor"])

    def test_equality_form_accepts_all_four_negative_literals(self):
        for token, expect in (("conn_pool=0", False), ("conn_pool=off", False),
                              ("conn_pool=false", False), ("conn_pool=no", False),
                              ("conn_pool=1", True), ("conn_pool=whatever", True)):
            with self.subTest(token=token):
                self.assertEqual(self._flags(token)["conn_pool"], expect)

    def test_names_and_values_are_case_insensitive(self):
        self.assertFalse(self._flags("CONN_POOL=OFF")["conn_pool"])
        self.assertFalse(self._flags("System_Drag=0")["system_drag"])
        self.assertTrue(self._flags("  !Conn_Pool  ")["conn_pool"] is False,
                        "首尾空白要容忍，且叹号大小写无关")

    def test_spaces_around_the_equals_sign_are_not_supported(self):
        """记录既有边界（非缺陷复现）：整串先按空白切词，所以 `k = v` 会被切成三截。

        写成用例是为了让这个口径显式可见 —— 将来若给解析加"按逗号切、保留空格"，
        这条会红，届时按新语义改断言，而不是让"配置写了不生效"重新变成谜案。
        """
        flags = self._flags("conn_pool = off")
        self.assertEqual(flags["conn_pool"],
                         sf_constants._FEATURE_DEFAULTS["conn_pool"],
                         "带空格的写法整条被忽略（既不关也不开）")

    def test_unknown_names_are_ignored_without_breaking_known_ones(self):
        flags = self._flags("definitely_not_a_flag, !targets")
        self.assertNotIn("definitely_not_a_flag", flags,
                         "未知开关不得凭空造键（会让 feature_enabled 误判成已配置）")
        self.assertFalse(flags["targets"])

    def test_comma_and_space_separated_forms_are_equivalent(self):
        self.assertEqual(self._flags("conn_pool=0 targets=0"),
                         self._flags("conn_pool=0,targets=0"))

    def test_ini_branch_is_used_only_when_env_is_absent(self):
        """环境变量与 ini 是**二选一**：这是打包后"不动安装目录也能临时开关"的通道。"""
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "features.ini"), "w", encoding="utf-8") as f:
            f.write("# 注释行\n")
            f.write("targets = off\n")
            f.write("system_drag=0\n")
            f.write("no_equals_sign_here\n")
            f.write("unknown_key = 1\n")
        old = os.environ.pop("STOCKFLOAT_FEATURES", None)
        try:
            with mock.patch.object(sf_logging, "__file__",
                                   os.path.join(d, "sf_logging.py")):
                flags = sf_logging._load_feature_flags()
        finally:
            if old is not None:
                os.environ["STOCKFLOAT_FEATURES"] = old
        self.assertFalse(flags["targets"], "ini 的 off 必须生效")
        self.assertFalse(flags["system_drag"])
        self.assertEqual(flags["conn_pool"], sf_constants._FEATURE_DEFAULTS["conn_pool"])
        self.assertNotIn("unknown_key", flags)

    def test_env_takes_precedence_over_ini(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "features.ini"), "w", encoding="utf-8") as f:
            f.write("targets = off\n")
        old = os.environ.get("STOCKFLOAT_FEATURES")
        os.environ["STOCKFLOAT_FEATURES"] = "targets"
        try:
            with mock.patch.object(sf_logging, "__file__",
                                   os.path.join(d, "sf_logging.py")):
                self.assertTrue(sf_logging._load_feature_flags()["targets"],
                                "环境变量在时 ini 必须完全不被读取")
        finally:
            if old is None:
                os.environ.pop("STOCKFLOAT_FEATURES", None)
            else:
                os.environ["STOCKFLOAT_FEATURES"] = old

    def test_unreadable_ini_falls_back_to_defaults_without_raising(self):
        """配置文件损坏不能让进程起不来。私有直调原因同上。"""
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "features.ini"), "w", encoding="utf-8") as f:
            f.write("targets = off\n")
        os.chmod(os.path.join(d, "features.ini"), 0)
        old = os.environ.pop("STOCKFLOAT_FEATURES", None)
        try:
            with mock.patch.object(sf_logging, "__file__",
                                   os.path.join(d, "sf_logging.py")):
                flags = sf_logging._load_feature_flags()
        finally:
            os.chmod(os.path.join(d, "features.ini"), 0o600)
            if old is not None:
                os.environ["STOCKFLOAT_FEATURES"] = old
        self.assertEqual(flags, dict(sf_constants._FEATURE_DEFAULTS),
                         "读取失败必须整体回退默认，而不是给出半份开关")

    def test_feature_enabled_on_unknown_name_is_false_but_never_raises(self):
        self.assertFalse(sf_logging.feature_enabled("no_such_switch_xyz"))
        self.assertTrue(sf_logging.feature_enabled("targets"))
        # 开关表本身损坏（非 dict）时按"功能开启"放行 —— 见该函数注释：
        # 返回 False 会让用户看到功能凭空消失，比留一条错误更难排查。
        old = sf_logging._FEATURES
        try:
            sf_logging._FEATURES = None
            self.assertTrue(sf_logging.feature_enabled("conn_pool"))
        finally:
            sf_logging._FEATURES = old


# --------------------------------------------------------------------------
# 多代轮转
# --------------------------------------------------------------------------

class TestLogRotationGenerations(SFCase):
    """轮转必须**从最老一代往新代倒序**搬迁。

    私有函数直调原因：`_shift_log_generations` 只在日志超过 5MB 时被
    `_maybe_rotate_log` 调用，构造 5MB 日志来触发它既慢又测不到方向性。
    """

    def _dir(self):
        d = tempfile.mkdtemp()
        return d, os.path.join(d, "debug.log")

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    @classmethod
    def _read_or_none(cls, path):
        return cls._read(path) if os.path.exists(path) else None

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_three_generations_shift_without_collapsing_into_one_copy(self):
        d, p = self._dir()
        self._write(p, "gen0")
        self._write(p + ".1", "gen1")
        self._write(p + ".2", "gen2")
        sf_logging._shift_log_generations(p, keep=3)
        got = {suffix: self._read_or_none(p + suffix)
               for suffix in ("", ".1", ".2", ".3")}
        self.assertEqual(got[""], None, "主日志已让位")
        self.assertEqual(got[".1"], "gen0")
        self.assertEqual(got[".2"], "gen1", "正序搬迁会把 gen2 覆盖成 gen1 再压给 .3")
        self.assertEqual(got[".3"], "gen2")

    def test_oldest_generation_beyond_keep_is_dropped(self):
        d, p = self._dir()
        self._write(p + ".3", "oldest")
        self._write(p, "current")
        sf_logging._shift_log_generations(p, keep=3)
        self.assertFalse(os.path.exists(p + ".3"),
                         "超出 keep 的最老一代必须删除，否则轮转档无限增长")
        self.assertEqual(self._read(p + ".1"), "current")

    def test_missing_generations_do_not_abort_the_shift(self):
        d, p = self._dir()
        self._write(p, "only")            # 只有主日志，没有任何历史代
        sf_logging._shift_log_generations(p, keep=3)
        self.assertFalse(os.path.exists(p))
        self.assertEqual(self._read(p + ".1"), "only")

    def test_rotated_files_are_owner_only(self):
        """轮转档同样含用户查询过的股票代码，权限必须收到 0600。"""
        d, p = self._dir()
        self._write(p, "x")
        self._write(p + ".1", "y")
        sf_logging._shift_log_generations(p, keep=3)
        for suffix in (".1", ".2"):
            mode = stat.S_IMODE(os.stat(p + suffix).st_mode)
            self.assertEqual(mode, 0o600, "%s 实际 %o" % (suffix, mode))


class TestMaybeRotateLog(SFCase):
    """`_maybe_rotate_log` 的三条分支：节流窗口内直接返回、超限才轮转、按天清旧档。"""

    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "debug.log")
        self._g = (sf_logging.LOG_PATH, sf_logging.LOG_MAX_BYTES,
                   sf_logging.LOG_ROTATE_CHECK_INTERVAL,
                   sf_logging.LOG_MAX_AGE_DAYS, sf_logging._LAST_ROTATE_CHECK)
        sf_logging.LOG_PATH = self.p
        sf_logging.LOG_ROTATE_CHECK_INTERVAL = 0.0
        sf_logging._LAST_ROTATE_CHECK = 0.0

    def tearDown(self):
        (sf_logging.LOG_PATH, sf_logging.LOG_MAX_BYTES,
         sf_logging.LOG_ROTATE_CHECK_INTERVAL, sf_logging.LOG_MAX_AGE_DAYS,
         sf_logging._LAST_ROTATE_CHECK) = self._g
        super().tearDown()

    def test_small_log_is_not_rotated(self):
        sf_logging.LOG_MAX_BYTES = 10_000
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("small")
        sf_logging._maybe_rotate_log()
        self.assertTrue(os.path.exists(self.p))
        self.assertFalse(os.path.exists(self.p + ".1"))

    def test_oversized_log_is_rotated(self):
        sf_logging.LOG_MAX_BYTES = 10
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("0123456789abcdef")
        sf_logging._maybe_rotate_log()
        self.assertFalse(os.path.exists(self.p))
        self.assertTrue(os.path.exists(self.p + ".1"))

    def test_check_is_throttled_within_the_interval(self):
        """每条日志都 stat+listdir 是纯浪费：窗口内第二次调用必须直接返回。"""
        sf_logging.LOG_ROTATE_CHECK_INTERVAL = 600.0
        sf_logging.LOG_MAX_BYTES = 1
        sf_logging._LAST_ROTATE_CHECK = time.monotonic()
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("0123456789")
        sf_logging._maybe_rotate_log()
        self.assertTrue(os.path.exists(self.p), "节流窗口内不该动日志")

    def test_stale_rotations_are_removed_but_sibling_files_survive(self):
        """按天清理只认 "<日志名>." 前缀 —— 同目录还放着 config.json 等用户数据。"""
        sf_logging.LOG_MAX_BYTES = 10 ** 9
        sf_logging.LOG_MAX_AGE_DAYS = 7
        old = time.time() - 30 * 86400
        stale = self.p + ".1"
        with open(stale, "w", encoding="utf-8") as f:
            f.write("ancient")
        os.utime(stale, (old, old))
        keep_cfg = os.path.join(self.d, "config.json")
        keep_log = self.p                        # 主日志本身超期也不能被"清理"掉
        with open(keep_cfg, "w", encoding="utf-8") as f:
            f.write("{}")
        with open(keep_log, "w", encoding="utf-8") as f:
            f.write("current")
        for p in (keep_cfg, keep_log):
            os.utime(p, (old, old))
        sf_logging._maybe_rotate_log()
        self.assertFalse(os.path.exists(stale), "超期轮转档必须清掉")
        self.assertTrue(os.path.exists(keep_cfg), "无前缀的同目录文件不得被删")
        self.assertTrue(os.path.exists(keep_log),
                        "清理只针对轮转代，主日志即便很旧也留着继续追加")

    def test_prefix_rule_is_by_name_not_by_number(self):
        """记录既有口径：判定是 startswith("<日志名>.")，不校验后缀是数字。

        所以 debug.log.old 也在按天清理范围内 —— 这是"宁缺毋滥删轮转档"的
        同一套规则，写清楚比留给下一个人猜要好。
        """
        sf_logging.LOG_MAX_BYTES = 10 ** 9
        sf_logging.LOG_MAX_AGE_DAYS = 7
        old = time.time() - 30 * 86400
        weird = self.p + ".old"
        with open(weird, "w", encoding="utf-8") as f:
            f.write("x")
        os.utime(weird, (old, old))
        sf_logging._maybe_rotate_log()
        self.assertFalse(os.path.exists(weird))

    def test_unreadable_log_directory_never_raises(self):
        sf_logging.LOG_PATH = os.path.join(self.d, "no", "such", "dir", "debug.log")
        sf_logging.LOG_MAX_BYTES = 1
        sf_logging._maybe_rotate_log()          # 只要求"不抛"


# --------------------------------------------------------------------------
# log_line 的落盘契约
# --------------------------------------------------------------------------

class TestLogLineContract(SFCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "sub", "debug.log")
        self._old = sf_logging.LOG_PATH
        sf_logging.LOG_PATH = self.p
        self._muted = sf_logging._LOG_MUTED

    def tearDown(self):
        sf_logging.LOG_PATH = self._old
        sf_logging._LOG_MUTED = self._muted
        super().tearDown()

    def _tail(self):
        with open(self.p, encoding="utf-8") as f:
            return f.read()

    def test_line_format_level_and_directory_creation(self):
        sf_logging.log_line("股价刷新完成", level="INFO")
        text = self._tail()
                # v1.4.33 起前缀带完整日期与 6 位 run id（见 sf_logging.log_ts）
        self.assertRegex(text.strip(),
                         r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [0-9a-f]{6}\] INFO: 股价刷新完成$")
        self.assertEqual(stat.S_IMODE(os.stat(self.p).st_mode), 0o600)

    def test_muted_log_line_writes_nothing_and_creates_no_directory(self):
        """卸载清理期间静音：否则任何一次写日志都会把刚移进废纸篓的目录建回来。"""
        sf_logging.set_log_muted(True)
        self.assertTrue(sf_logging.is_log_muted())
        sf_logging.log_line("不该出现", level="ERROR")
        self.assertFalse(os.path.exists(self.p))
        self.assertFalse(os.path.exists(os.path.dirname(self.p)))
        sf_logging.set_log_muted(False)
        sf_logging.log_line("恢复后写入")
        self.assertIn("恢复后写入", self._tail())

    def test_only_warning_and_error_reach_system_log(self):
        """统一日志是全局共享的，INFO 级刷屏会淹没其它进程。"""
        with mock.patch.object(sf_logging, "_nslog_line") as ns:
            sf_logging.log_line("普通", "INFO")
            self.assertEqual(ns.call_count, 0)
            sf_logging.log_line("警告", "WARNING")
            sf_logging.log_line("错误", "ERROR")
            self.assertEqual(ns.call_count, 2)
            self.assertIn("警告", ns.call_args_list[0].args[0])
            self.assertIn("WARNING", ns.call_args_list[0].args[0])

    def test_unwritable_log_path_is_swallowed(self):
        """日志盘点下去不了也不能影响主流程（该函数契约就是"不抛异常"）。"""
        sf_logging.LOG_PATH = os.path.join(self.d, "a", "..", "x", "debug.log")
        sf_logging.log_line("仍然要活着")

    def test_surrogate_text_does_not_lose_the_whole_line(self):
        """行情源偶发返回含代理项的脏串：errors="replace" 保证只坏一个字符。"""
        sf_logging.log_line("脏串 \ud800 后面还在")
        text = self._tail()
        self.assertIn("后面还在", text, "整条日志被丢掉是本项目最难查的故障形态")

    def test_concurrent_writers_lose_nothing(self):
        """轮转与写入统一持锁：并发写不得互相截断（v1.2.71 第 4 轮走查）。"""
        sf_logging.LOG_MAX_BYTES = 10 ** 9
        per_thread = 30

        def writer(i):
            for n in range(per_thread):
                sf_logging.log_line("t%d-%d" % (i, n))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = [l for l in self._tail().splitlines() if l]
        self.assertEqual(len(lines), 6 * per_thread, "并发丢行 = 排障时看不到现场")
        # 每行必须是完整的一条，而不是两次写入交错拼出来的半截
        import re as _re
        broken = [l for l in lines
                  if not _re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [0-9a-f]{6}\] INFO: t\d+-\d+", l)]
        self.assertEqual(broken, [], "行内容被交错截断：%s" % broken[:3])


# --------------------------------------------------------------------------
# NSLog：格式串参数化
# --------------------------------------------------------------------------

class TestNslogLine(SFCase):
    """守住 v1.2.65 那个真事故：日志原文当格式串 → NSLog 按 % 读栈垃圾。"""

    def test_pyobjc_path_passes_text_as_argument_not_format(self):
        sent = []

        class _NSString:
            @staticmethod
            def alloc():
                return _NSString()

            def initWithUTF8String_(self, data):
                self.raw = data
                return self

        fake = mock.MagicMock()
        fake.NSString = _NSString
        fake.NSLog = lambda *a: sent.append(a)
        with mock.patch.dict(sys.modules, {"Foundation": fake}):
            sf_logging._nslog_line("monitor rate=100.0% 完整")
        self.assertEqual(len(sent), 1)
        fmt, value = sent[0]
        self.assertEqual(fmt, "%@", "第一个参数必须是格式串 %@")
        self.assertEqual(value.raw, "monitor rate=100.0% 完整".encode("utf-8"))

    def test_ctypes_fallback_also_formats(self):
        """pyobjc 缺失时走 ctypes；同样不得把原文当格式串传给 NSLog。"""
        objc_lib = mock.MagicMock(name="libobjc")
        foundation = mock.MagicMock(name="libFoundation")

        def fake_cdll(path):
            return objc_lib if "libobjc" in path else foundation

        text = "paintEvent error: 100.0%"
        # 每次消息发送给出**不同**的对象并按参数记账，才能验证 NSLog 收到的
        # 两个实参分别是谁 —— MagicMock 默认复用同一个 return_value，
        # 传参错位也看不出来。
        built = {}

        def msg_send(*a):
            obj = object()
            if len(a) >= 3:
                built[a[2]] = obj
            return obj

        objc_lib.objc_msgSend.side_effect = msg_send
        with mock.patch.dict(sys.modules, {"Foundation": None}), \
                mock.patch("ctypes.CDLL", side_effect=fake_cdll):
            sf_logging._nslog_line(text)

        self.assertEqual(foundation.NSLog.call_count, 1)
        self.assertIn(b"%@", built, "格式串必须单独构造，否则原文里的 % 会被当格式说明符")
        self.assertIn(text.encode("utf-8"), built)
        self.assertEqual(foundation.NSLog.call_args.args,
                         (built[b"%@"], built[text.encode("utf-8")]),
                         "NSLog 必须是 (格式串, 日志原文) 的顺序，两个都得是构造出的 NSString")
        self.assertIn(b"stringWithUTF8String:",
                      [c.args[0] for c in objc_lib.sel_registerName.call_args_list])

    def test_total_failure_is_silent(self):
        """连 ctypes 都拿不到时不能把异常抛回日志调用方。"""
        with mock.patch.dict(sys.modules, {"Foundation": None}), \
                mock.patch("ctypes.CDLL", side_effect=OSError("no libobjc")):
            sf_logging._nslog_line("仍然不能抛")


# --------------------------------------------------------------------------
# 单实例 socket
# --------------------------------------------------------------------------

class TestStaleSocketHandling(SFCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()

    def _p(self, name):
        return os.path.join(self.d, name)

    def test_socket_file_is_unlinked(self):
        p = self._p("live.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(p)
        self.assertTrue(sf_logging._unlink_stale_single_socket(p))
        self.assertFalse(os.path.exists(p))

    def test_regular_file_is_never_touched(self):
        """非 socket 路径必须原样保留：误删会把用户文件当陈旧 endpoint 清掉。"""
        p = self._p("important.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("do not delete")
        with mock.patch.object(sf_logging, "log_line") as lg:
            self.assertFalse(sf_logging._unlink_stale_single_socket(p))
        self.assertTrue(os.path.exists(p))
        self.assertIn("not a socket", lg.call_args.args[0])
        self.assertEqual(lg.call_args.kwargs.get("level") or lg.call_args.args[1],
                         "WARNING")

    def test_symlink_to_socket_is_not_followed(self):
        """必须用 lstat：跟随符号链接会把判断打到目标上，进而删掉无关文件。"""
        real = self._p("real.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(real)
        link = self._p("link.sock")
        os.symlink(real, link)
        self.assertFalse(sf_logging._unlink_stale_single_socket(link),
                         "符号链接本身不是 socket，不得删除")
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(real))

    def test_absent_path_reports_not_unlinked(self):
        self.assertFalse(sf_logging._unlink_stale_single_socket(self._p("nope.sock")))

    def test_permission_error_is_reported_as_skipped(self):
        p = self._p("locked.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(p)
        srv.close()
        with mock.patch("os.unlink", side_effect=PermissionError("read-only dir")):
            with mock.patch.object(sf_logging, "log_line") as lg:
                self.assertFalse(sf_logging._unlink_stale_single_socket(p))
        self.assertIn("skipped", lg.call_args.args[0])


class TestBecomeSingleInstance(SFCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.sock = os.path.join(self.d, "stock_float.sock")
        self._old = sf_logging.SINGLE_SOCK
        sf_logging.SINGLE_SOCK = self.sock

    def tearDown(self):
        sf_logging.SINGLE_SOCK = self._old
        super().tearDown()

    def test_existing_listener_means_not_the_single_instance(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(self.sock)
        srv.listen(1)
        self.assertFalse(sf_logging._become_single_instance(),
                         "已有实例在监听时必须放行 False，让调用方退出")
        self.assertTrue(os.path.exists(self.sock), "在听的 socket 绝不能被清掉")

    def test_absent_socket_lets_this_instance_win(self):
        """私有函数直调原因：多开保护没有对外查询口，返回 True/False 就是它的契约。

        该分支会起一个阻塞 accept 的守护线程，随进程退出即回收；
        用独立 socket 路径，不会牵连真实实例。
        """
        self.assertTrue(sf_logging._become_single_instance())
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(stat.S_IMODE(os.stat(self.sock).st_mode), 0o600,
                         "socket 默认 umask 可能放开读权限")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.connect(self.sock)          # 监听成功才连得上

    def test_bind_conflict_on_stale_socket_is_retried_once(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock)
        srv.close()                        # 监听者已消失 → 典型陈旧 endpoint
        self.assertTrue(sf_logging._become_single_instance())

    def test_bind_conflict_on_live_socket_does_not_steal_it(self):
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(live.close)
        live.bind(self.sock)
        live.listen(1)
        with mock.patch("os.unlink", side_effect=OSError("busy")) as unl:
            # 第二实例走 connect 成功分支，直接 False，不会尝试 bind
            self.assertFalse(sf_logging._become_single_instance())
        self.assertEqual(unl.call_count, 0)

    def test_probe_errors_other_than_refusal_do_not_trigger_cleanup(self):
        """权限/非 socket 等探测错误不得被当成"陈旧 socket"去清别人的 endpoint。

        实测口径：路径是个目录时 connect 报 ENOTSOCK（errno 38），既不是
        FileNotFoundError 也不是 ConnectionRefusedError → stale 判定不成立。
        """
        os.makedirs(self.sock)
        with mock.patch.object(sf_logging, "log_line") as lg:
            self.assertTrue(sf_logging._become_single_instance(),
                            "探测失败不得误拦正常启动")
        self.assertTrue(os.path.isdir(self.sock),
                        "非 socket 路径绝不能被当成陈旧 endpoint 删除")
        msgs = [c.args[0] for c in lg.call_args_list]
        self.assertTrue(any("without cleanup" in m for m in msgs),
                        "探测异常必须留痕：%s" % msgs)
        self.assertFalse(any("stale socket cleanup" in m for m in msgs))


# --------------------------------------------------------------------------
# 崩溃报告扫描
# --------------------------------------------------------------------------

class TestRecentCrashScan(SFCase):
    """启动时扫 DiagnosticReports，把 7 天内的本应用崩溃记进日志。"""

    def _run(self, files, now=None, logs=None):
        d = tempfile.mkdtemp()
        base = os.path.join(d, "Library", "Logs", "DiagnosticReports")
        os.makedirs(base)
        for name, age_days in files.items():
            p = os.path.join(base, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("{}")
            t = time.time() - age_days * 86400
            os.utime(p, (t, t))
        real_expanduser = os.path.expanduser

        def fake(path):
            if path.startswith("~/Library/Logs/DiagnosticReports"):
                return real_expanduser(path).replace(
                    real_expanduser("~/Library/Logs/DiagnosticReports"), base)
            return real_expanduser(path)

        recorder = logs if logs is not None else []
        with mock.patch("os.path.expanduser", side_effect=fake), \
                mock.patch.object(sf_logging, "log_line",
                                  side_effect=lambda m, level="INFO": recorder.append(
                                      (level, m))):
            sf_logging._check_recent_crashes()
        return recorder

    def test_recent_crash_is_logged_as_warning(self):
        out = self._run({"StockFloatTargetPrice-2026-09-20.ips": 1})
        self.assertEqual([lv for lv, _ in out], ["WARNING"])
        self.assertIn("StockFloatTargetPrice-2026-09-20.ips", out[0][1])

    def test_other_processes_and_old_reports_are_ignored(self):
        out = self._run({"Safari-2026-09-22.ips": 1,
                         "StockFloatTargetPrice-2026-01-01.ips": 60,
                         "StockFloatTargetPrice-notes.txt": 1})
        self.assertEqual(out, [], "别人的崩溃、超期报告、非崩溃文件都不该记")

    def test_newest_report_wins(self):
        out = self._run({"StockFloatTargetPrice-2026-09-10.ips": 14,
                         "StockFloatTargetPrice-2026-09-21.ips": 3,
                         "StockFloatTargetPrice-2026-09-15.ips": 9})
        self.assertEqual(len(out), 1)
        self.assertIn("09-21", out[0][1])

    def test_missing_diagnostic_dir_is_not_an_error(self):
        d = tempfile.mkdtemp()
        with mock.patch("os.path.expanduser",
                        return_value=os.path.join(d, "nope")):
            with mock.patch.object(sf_logging, "log_line") as lg:
                sf_logging._check_recent_crashes()
        self.assertEqual(lg.call_count, 0)


# --------------------------------------------------------------------------
# reopen 代理类（Dock 图标点击恢复窗口）
# --------------------------------------------------------------------------

class TestEnsureReopenClass(SFCase):
    """`_ensure_reopen_class` 的三条路径：已缓存、造不出来、造出来且真能恢复窗口。

    全程只构建一次 ObjC 子类 —— pyobjc 的类注册是进程级的，
    反复用同名类重建会踩 "implemented in both" 一类的注册冲突。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klass = sf_logging._ensure_reopen_class()

    def test_cached_class_is_returned_without_reimport(self):
        old = sf_logging._REOPEN_CLASS
        try:
            sf_logging._REOPEN_CLASS = "sentinel"
            self.assertEqual(sf_logging._ensure_reopen_class(), "sentinel",
                             "已缓存时不得重新 import Foundation")
        finally:
            sf_logging._REOPEN_CLASS = old

    def test_second_call_reuses_the_same_class(self):
        self.assertIs(sf_logging._ensure_reopen_class(), self.klass)

    def test_missing_foundation_yields_none_and_never_raises(self):
        old = sf_logging._REOPEN_CLASS
        try:
            sf_logging._REOPEN_CLASS = None
            with mock.patch.dict(sys.modules, {"Foundation": None}):
                self.assertIsNone(sf_logging._ensure_reopen_class(),
                                  "没有 pyobjc 时返回 None，由调用方走无代理降级")
            self.assertIsNone(sf_logging._REOPEN_CLASS,
                              "失败结果也要写回缓存，否则每条日志都会重试一次 import")
        finally:
            sf_logging._REOPEN_CLASS = old

    def _require_class(self):
        if self.klass is None:                # 无 pyobjc 的环境（非 macOS CI）
            self.skipTest("本机无 Foundation/pyobjc")
        return self.klass

    def test_reopen_restores_a_hidden_window(self):
        """真跑一次 pyobjc 路径：点 Dock 图标时隐藏窗口必须被恢复。

        私有类直调原因：这个 delegate 交给 AppKit 回调，Python 侧没有调用点。
        """
        restored = []

        class _Win:
            def __init__(self, visible):
                self._v = visible

            def isVisible(self):
                return self._v

            def _restore_window(self):
                restored.append(True)

        inst = self._require_class().alloc().init()
        inst._window = _Win(False)
        with mock.patch.object(sf_logging, "QTimer") as qt:
            self.assertTrue(
                inst.applicationShouldHandleReopen_hasVisibleWindows_(None, False))
            self.assertEqual(qt.singleShot.call_count, 1,
                             "恢复动作必须排进 Qt 事件循环，否则窗口不会真正映射")
            qt.singleShot.call_args.args[1]()     # 执行被排队的回调
        self.assertEqual(restored, [True])

        # 已经可见的窗口不该被再恢复一次
        restored.clear()
        inst._window = _Win(True)
        with mock.patch.object(sf_logging, "QTimer") as qt:
            inst.applicationShouldHandleReopen_hasVisibleWindows_(None, True)
        self.assertEqual(qt.singleShot.call_count, 0)
        self.assertEqual(restored, [])

    def test_reopen_logs_visibility_before_deciding(self):
        inst = self._require_class().alloc().init()
        inst._window = None
        with mock.patch.object(sf_logging, "log_line") as lg:
            self.assertTrue(
                inst.applicationShouldHandleReopen_hasVisibleWindows_(None, False),
                "没有窗口时必须照常返回 True，让 AppKit 走默认行为")
        self.assertEqual(lg.call_count, 0, "无窗口时不该留下 reopen 日志噪声")

    def test_reopen_with_broken_window_still_returns_true(self):
        """窗口已析构（isVisible 抛）时不能让 AppKit 收到异常。"""
        class _Dead:
            def isVisible(self):
                raise RuntimeError("wrapped C+C++ object has been deleted")

        inst = self._require_class().alloc().init()
        inst._window = _Dead()
        with mock.patch.object(sf_logging, "log_line"):
            self.assertTrue(
                inst.applicationShouldHandleReopen_hasVisibleWindows_(None, False))

    def test_unknown_selectors_are_forwarded_to_the_original_delegate(self):
        """本 delegate 只实现 reopen 一个 selector，其余必须转给 Qt 原 delegate，
        否则 Dock 菜单等原生行为整体失效（见方法注释）。"""
        calls = []

        class _Orig:
            def methodSignatureForSelector_(self, sel):
                calls.append(("sig", sel))
                return "SIG"

        inst = self._require_class().alloc().init()
        inst._orig = _Orig()
        self.assertEqual(inst.methodSignatureForSelector_("anythingElse"), "SIG")
        self.assertEqual(calls, [("sig", "anythingElse")])

    def test_forwarding_without_original_does_not_raise(self):
        class _Inv:
            def __init__(self):
                self.target = None

            def invokeWithTarget_(self, t):
                raise RuntimeError("no original")

        inst = self._require_class().alloc().init()
        inst._orig = None
        self.assertIsNone(inst.forwardInvocation_(_Inv()))


if __name__ == "__main__":
    unittest.main()
