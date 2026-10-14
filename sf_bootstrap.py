# -*- coding: utf-8 -*-
"""启动前置守卫：拦截"从系统临时目录误启动打包产物"。

背景（2026-09-22 崩溃报告，v1.3.97）：旧版发版脚本把上一轮 dist 产物整体
mv 到 /tmp/sf_prev_targetprice_*，里面留有完整可启动的「实时股价-目标价版.app」。
临时目录里的旧副本一旦被双击/系统重新拉起，会在导入 PyQt6 阶段 SIGSEGV，
用户只看到"程序意外退出"弹窗。第一道防线是发版脚本侧（build_release.sh 移走
旧产物时把内层 .app 改名中性化）；本守卫是第二道防线：即使日后又有完整副本
被从临时目录拉起，也给出可读提示后正常退出，而不是留下崩溃弹窗。

必须在 **import PyQt6 之前** 调用（见 stock_float_window.py 顶部），
因此本模块只允许依赖标准库。
"""

import os
import subprocess
import sys
import tempfile

# realpath 后仍留在这些前缀下的，都不是正式安装位置。
# /var → /private/var、TMPDIR 变体统一按解析后的绝对路径判定。
_TEMP_ROOTS = ("/private/tmp/", "/private/var/tmp/", "/private/var/folders/")


def is_temporary_location(path):
    """判断打包后的可执行文件路径是否位于系统临时目录（含 TMPDIR）。"""
    real = os.path.realpath(path) + os.sep
    if any(real.startswith(prefix) for prefix in _TEMP_ROOTS):
        return True
    tmpdir = os.path.realpath(tempfile.gettempdir()) + os.sep
    return real.startswith(tmpdir)


def guard_temporary_launch():
    """冻结产物若从临时目录启动：提示后正常退出，绝不进入 Qt 初始化。

    仅对 PyInstaller 产物（sys.frozen）生效；源码运行与测试不受影响。
    核验脚本确需从临时目录拉起产物时，设 SF_ALLOW_TEMP_LAUNCH=1 放行。
    该变量同时关闭 v1.4.23 的"临时目录旧副本自愈清扫"（见
    stock_float_window._sweep_temp_copies）：核验者常先把副本拷到 /tmp 再启动，
    那份尚未启动的副本不在"跳过运行中"的保护范围内，必须靠这个开关免被改名。
    """
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("SF_ALLOW_TEMP_LAUNCH", "").strip().lower() in ("1", "true", "yes"):
        return
    exe = os.path.realpath(getattr(sys, "executable", ""))
    if not exe or not is_temporary_location(exe):
        return
    message = ("检测到从临时目录启动的旧版副本：\n%s\n\n"
               "这可能是升级过程中残留的副本，无法正常运行。\n"
               "请在访达「应用程序」文件夹中打开正式版。" % exe)
    try:
        print("[sf_bootstrap] %s" % message.replace("\n", " "), file=sys.stderr)
    except Exception:
        pass
    # 用系统自带的 osascript 弹窗提示（此时不能依赖 PyQt —— 正是它崩溃）。
    # 30 秒无人确认也直接退出，避免留下无窗口的僵尸进程。
    # 标题里的产品名是**字面量**：本模块恰恰要在 PyQt 起不来时运行，而
    # sf_constants 会 import PyQt6.QtGui，所以这里不能去读唯一来源 —— 漂移由
    # tests/test_identity_literals.py 锁住（改名时漏掉这一处，那条用例会红）。
    script = ('display alert "Skyrocket：启动位置不正确" message "%s" as warning'
              % message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=30)
    except Exception:
        pass
    sys.exit(0)
