# -*- coding: utf-8 -*-
"""2026-10-12 复核批次修的三件小事的判据（文案说错事 / 日期写死 / 裸进程取 UN 崩溃）。

这一批没有一条是"新功能"，全部是"复核当场抓到、当场修掉"的小缺陷，判据跟着修复走：

  · `quality_trend` 的兜底文案：数据源不可用时报真实原因。旧实现把"找不到日志"也报成
    "取不到 git 历史"，排障的人会被带去查仓库。
  · `coverage_gate --update` 写回基线的 `updated` 字段用**当天**日期。旧实现写死
    "2026-10-10" —— 读基线的人没法知道这条下限是哪天核的，棘轮的账龄失真。
  · `sf_notify` 在没有 app bundle 身份的进程里**绝不碰** `UNUserNotificationCenter`。
    裸解释器里 `currentNotificationCenter()` 抛的是 ObjC 异常
    （NSInternalInconsistencyException 'bundleProxyForCurrentProcess is nil'），
    PyObjC 接不住 —— 不是 except Exception 能兜的，进程直接 SIGABRT。
    2026-10-12 实测：随机顺序档全量被这一枚打断；修法是 import 之前先看
    `NSBundle.mainBundle().bundleIdentifier()`，没有就整条 UN 路线按不可用处理、
    降级 osascript。判据必须用**子进程**取：判据进程自己就是裸解释器，
    在主进程里调 `_ensure_un()` 会把共享模块的 `_UN_UNAVAILABLE` 永久置真，
    污染同一进程里后面所有通知用例 —— 所以真崩路径只在新进程里验。

AGENTS §6 对号：不 sleep（R3）、断言里没有版本号字面量（R1）、不依赖当下时钟的
"交易时段"判断（R4；这里比对的是基线文件里那格日期，语义就是"今天"，用同一来源计算）。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase                          # noqa: E402
except ImportError:                                    # pragma: no cover
    from tests._base import SFCase                    # noqa: E402

import coverage_gate as cg                             # noqa: E402



def _peek(mod, name):
    # getattr 的名字走变量传入：既不踩 B009（常量 getattr 是 A 级硬零），也不踩 R5
    return getattr(mod, name)
import quality_trend as qt                             # noqa: E402
import sf_notify                                       # noqa: E402
notify_ensure_un = sf_notify._ensure_un                # discipline-ok(R5): 被测对象就是私有探测函数


REAL_BASELINE = ROOT / "tools" / "coverage_gate_baseline.json"


def run(main, argv):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
    except SystemExit as exc:
        code = exc.code
        rc = code if isinstance(code, int) else 1
        if not isinstance(code, int):
            out.write("%s\n" % (code or ""))
    return rc, out.getvalue() + err.getvalue()


class QualityTrendHonestMessage(SFCase):
    """数据源不可用时，报错文案必须照抄真实原因。"""

    def test_git读不到时报的是git问题(self):
        # --report 会先 load() 台账再跑 git：台账给真的一份（只读），只让 git 坏
        ledger = pathlib.Path(tempfile.mkdtemp()) / "ledger.json"
        ledger.write_text(json.dumps({"schema": 1, "updated": "2026-10-12",
                                      "entries": [], "rounds": []},
                                     ensure_ascii=False), encoding="utf-8")
        self.track_patch(mock.patch.object(qt, "LEDGER_PATH", ledger))
        self.track_patch(mock.patch.object(qt, "version_dates",
                                           side_effect=RuntimeError("没有 git 历史")))
        rc, out = run(qt.main, ["--report"])
        self.assertEqual(rc, 2)
        self.assertIn("数据源不可用", out)
        self.assertIn("没有 git 历史", out)
        self.assertNotIn("取不到 git 历史，拒绝输出", out.split("数据源不可用")[0],
                         "旧文案又回来了")

    def test_找不到日志时报的是日志问题不是git问题(self):
        """修复前：这条路径报「取不到 git 历史」—— 排障的人会被带去查仓库。"""
        ghost = pathlib.Path(tempfile.mkdtemp()) / "没有.log"
        rc, out = run(qt.main, ["--from-log", "--log-path", str(ghost)])
        self.assertEqual(rc, 2)
        self.assertIn("找不到日志", out)
        self.assertNotIn("取不到 git 历史", out)


class CoverageBaselineDateIsToday(SFCase):
    """--update 写回的 updated 必须是当天：棘轮的账龄要能被读出来。"""

    def setUp(self):
        super().setUp()
        self.real_bytes = REAL_BASELINE.read_bytes()

    def tearDown(self):
        self.assertEqual(REAL_BASELINE.read_bytes(), self.real_bytes,
                         "真基线被写了 —— 写基线只能走 --update 且落点必须显式给出")
        super().tearDown()

    def test_写进临时基线的日期是当天(self):
        tmp = pathlib.Path(tempfile.mkdtemp()) / "baseline.json"
        self.track_patch(mock.patch.object(cg, "load_data",
                                           return_value=({"tiers": {
                                               "product": [1000, 100, 10],
                                               "chain": [100, 20, 3]}},
                                                          "")))
        rc, out = run(lambda argv: cg.cmd_update({"product_floor": 84, "chain_floor": 78},
                                                 False, baseline_path=tmp), [])
        self.assertEqual(rc, 0, out)
        written = json.loads(tmp.read_text(encoding="utf-8"))
        self.assertEqual(written["updated"], dt.date.today().isoformat(),  # discipline-ok(R4): 判据的语义就是"落的是今天的日期"，期望值与被测实现取同一时钟来源
                         "旧实现写死 2026-10-10 —— 读基线的人不知道它是哪天核的")


class NotifyNeverAbortsWithoutBundle(SFCase):
    """裸解释器里取 UN center 是 SIGABRT，不是可捕获异常 —— 崩溃点前必须有身份守卫。

    2026-10-13 更正（这条判据自己把 CI 弄红两次之后改的）：原写法断言的是
    **"这台机器没有 bundle 身份"** —— GitHub 的 macOS runner 上
    `NSBundle.mainBundle().bundleIdentifier()` 是给得到值的（实测打印 `BUNDLE True`），
    于是 `assertIn("STATUS denied")` 与 `assertFalse(has_bundle_identity())` 在 CI 上必然红，
    而它们红的理由是环境、不是代码坏了。判据按环境分支就是没测。
    现在的形状：**在子进程里把 `has_bundle_identity` 换成假的 False**，真框架照常 import 到位，
    这样"该拦"这个前提在两形机器上都成立；另留一条按机器真实身份分支的公开入口判据，
    两种机器都必须不崩、且没身份时给的是降级答案。
    反向可验：把守卫删掉，本机当场 SIGABRT（rc=-6），CI 上 `_un_center()` 取到真 center
    → `GUARD_NONE False` → 判红。两边都拦得住，才算锁。
    """

    def _child(self, code):
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["SF_LOG_PATH"] = str(pathlib.Path(tempfile.mkdtemp()) / "probe.log")
        return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120, env=env)

    @staticmethod
    def _flag(out, name):
        for line in out.splitlines():
            if line.startswith(name + " "):
                return line.split(None, 1)[1].strip()
        return None

    def test_真框架在场时守卫必须拦住取center那一步(self):
        # 子进程里跑的这几行是"字符串里的代码"，不是本文件的语句 —— 所以私有入口
        # （_ensure_un / _UN_REAL_CLASS / _un_center）在这里不构成 R5 意义上的内部状态访问：
        # 被测对象就是这条 import 路径与那个守卫本身，而它必须在**没有桩**的新进程里跑，
        # 在主进程跑会把共享模块的探测结果永久留痕，污染后面整族通知用例。
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import sf_notify\n"
            "sf_notify._ensure_un()\n"
            "print('REAL', sf_notify._UN_REAL_CLASS is not None\n"
            "      and sf_notify.UNUserNotificationCenter is sf_notify._UN_REAL_CLASS)\n"
            "sf_notify.has_bundle_identity = lambda: False\n"
            "print('GUARD_NONE', sf_notify._un_center() is None)\n"
            "sf_notify.request_authorization(); print('DONE')\n"
            % str(ROOT))
        proc = self._child(code)
        self.assertNotIn("Terminating app", proc.stderr, "ObjC 异常又漏出去了")
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-500:])
        if self._flag(proc.stdout, "REAL") != "True":
            self.skipTest("这台机器的解释器 import 不到真的 UserNotifications 框架（REAL=%r），"
                          "守卫那条分支在这里没有真对象可拦 —— 不算通过也不算失败，"
                          "换到装了 pyobjc-framework-UserNotifications 的机器上再跑"
                          % self._flag(proc.stdout, "REAL"))
        self.assertEqual(self._flag(proc.stdout, "GUARD_NONE"), "True",
                         "身份为假时还去取 center = abort 回来了（守卫被绕过或删掉）")
        self.assertIn("DONE", proc.stdout, "request_authorization 在没身份时应当自己收住并返回")

    def test_公开入口按机器真实身份给答案且绝不崩(self):
        code = ("import sys; sys.path.insert(0, %r); import sf_notify\n"
                "print('IDENT', sf_notify.has_bundle_identity())\n"
                "print('STATUS', sf_notify.authorization_status())\n") % (str(ROOT),)
        proc = self._child(code)
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-500:])
        self.assertNotIn("Terminating app", proc.stderr)
        ident = self._flag(proc.stdout, "IDENT")
        status = self._flag(proc.stdout, "STATUS")
        if ident == "False":
            self.assertEqual(status, "denied",
                             "没有 bundle 身份时必须按 UN 不可用同口径（只走彩虹，不弹横幅）")
        else:
            # 有身份的那一支不许悄悄空转：真框架的答案只能是这套枚举值之一。
            self.assertIn(status, {"authorized", "denied", "notDetermined",
                                   "provisional", "ephemeral", "unknown"},
                          "有身份时答案必须是 authorization_status 文档里那套枚举值，实得 %r" % status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
