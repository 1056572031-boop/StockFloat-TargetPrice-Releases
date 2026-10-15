# -*- coding: utf-8 -*-
"""系统弹窗（NSAlert）"真弹出→点按钮→回调→收口"全链路回归。

背景（v1.4.17）：v1.4.8 起的 NSAlert 路径此前只有"桩 AppKit"用例 —— 断言的是
**我们传给 AppKit 的参数**，不是"用户真点下去会发生什么"。弹窗链路任何一段
（按钮 target 绑定、回调转交、点完收口）坏了，表现都是用户点"立即更新"没反应，
而全量测试照样绿。这类"改了界面行为、测试只会证明调用顺序没变"的缺口就是本轮补的。

做法与代价：真弹窗必须建窗口，放在**隔离子进程**里跑（同进程建 AppKit 窗口历史上
会把整个测试会话冲掉）；窗口建好后立刻挪到屏幕外再点，所以你不会看到它闪一下，
但可能会有一瞬焦点被抢。没有窗口服务的机器（SSH/CI）由 probe 预检整体跳过。

断言全部走可观测输出：子进程打印的标记（窗口可见性、按钮标题与顺序、回调收到的
按钮名、收口后窗口是否还在）与退出码。
"""

import pathlib
import subprocess
import sys
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "_alert_child.py"
TIMEOUT_SECONDS = 90


def _spawn(mode):
    proc = subprocess.run([sys.executable, str(CHILD), mode],
                          capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                          cwd=str(ROOT))
    markers = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            markers[key.strip()] = value.strip()
    return proc, markers


class AlertChainTest(unittest.TestCase):
    """真 AppKit 弹窗：不建 Qt、不弹在屏幕上，只验证链路本身通。"""

    @classmethod
    def setUpClass(cls):
        try:
            import sf_system_alert
        except Exception as exc:                       # noqa: BLE001
            raise unittest.SkipTest("无法导入 sf_system_alert: %r" % (exc,))
        if sf_system_alert.AppKit is None:
            raise unittest.SkipTest("本机无 AppKit，NSAlert 路径不存在")
        try:
            proc, markers = _spawn("probe")
        except subprocess.TimeoutExpired:
            raise unittest.SkipTest("弹窗预检超时：本机可能没有窗口服务")
        if markers.get("probe") != "ok":
            raise unittest.SkipTest(
                "弹窗预检未通过（无窗口服务？）probe=%s rc=%s err=%s"
                % (markers.get("probe"), proc.returncode, (proc.stderr or "")[-200:]))

    def _run(self, mode):
        proc, markers = _spawn(mode)
        self.assertNotIn("error", markers,
                         "子进程抛异常: %s" % markers.get("error"))
        return proc, markers

    def test_click_on_real_alert_reaches_callback_and_closes_window(self):
        proc, m = self._run("click")
        self.assertEqual(m.get("numbered_notes"), "True", "原生弹窗不得改写编号正文")
        self.assertEqual(m.get("note_lines"), "4", "原生弹窗必须保留逐项换行")
        self.assertEqual(m.get("visible"), "True", "弹窗必须真的上了屏")
        self.assertEqual(m.get("titles"), "立即更新,暂不更新",
                         "默认按钮必须排第一（蓝色、位置最右/最上），与系统弹窗一致")
        self.assertEqual(m.get("esc_codepoint"), "27", "Esc 绑到取消按钮")
        self.assertEqual(m.get("result"), "立即更新",
                         "点按钮后回调必须收到按钮标题 —— 这是「立即更新」能不能生效的那一环")
        self.assertEqual(m.get("visible_after_click"), "False",
                         "点完必须自行收口，不能留一个幽灵窗口在屏上")
        self.assertEqual(m.get("live_after"), "0",
                         "防 GC 注册表收口后必须清空，否则每弹一次永久漏一个窗口")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_progress_alert_mounts_real_bar_and_cancel_closes(self):
        """v1.4.17 下载进度框：系统进度条挂得上去、刷得动、点取消能收口。

        桩 AppKit 只能证明"按顺序调了方法"，证明不了 NSAlert 真的接受
        accessoryView、进度条真的存下数值 —— 这两件事用户都看得见。
        """
        proc, m = self._run("progress")
        self.assertEqual(m.get("bar_class"), "NSProgressIndicator",
                         "进度条必须是系统控件，不是我们自画的")
        self.assertEqual(m.get("indeterminate"), "False",
                         "确定型：用户要看得还剩多少，而不是转圈")
        self.assertEqual(m.get("first_update"), "True")
        self.assertEqual(m.get("bar_value"), "37.0", "37% 必须真的进了进度条")
        self.assertEqual(m.get("subtext"), "37%（9.9 / 26.8 MB）")
        self.assertEqual(m.get("repeat_update"), "False",
                         "同一条进度不得再让 AppKit 重排一次弹窗")
        self.assertEqual(m.get("result"), "取消", "点取消要回调出按钮名，下载才知道要停")
        self.assertEqual(m.get("visible_after_cancel"), "False")
        self.assertEqual(m.get("late_update_accepted"), "False",
                         "收口后的迟到进度必须被丢弃")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_programmatic_close_discards_result_and_hides_window(self):
        proc, m = self._run("close")
        self.assertEqual(m.get("visible"), "True")
        self.assertEqual(m.get("result"), "",
                         "程序主动收口不是用户选择，不得回调出任何按钮")
        self.assertEqual(m.get("visible_after_close"), "False",
                         "重复 close() 必须安全且窗口已消失")
        self.assertEqual(m.get("live_after"), "0", "主动收口同样要退出注册表")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_broken_button_bridge_does_not_escape(self):
        _proc, m = self._run("broken")
        self.assertEqual(m.get("result"), "", "取不到标题时不得回调出脏值")
        self.assertEqual(m.get("visible_after_broken"), "True",
                         "异常被吞掉后弹窗仍在，等用户再点一次，而不是无声消失")

    def test_content_is_centered_and_buttons_have_no_focus_ring(self):
        """v1.4.22：图标在上、整体水平居中；按钮不画蓝色焦点描边（真 AppKit 几何）。"""
        proc, m = self._run("centered")
        self.assertNotIn("error", m, "子进程抛异常: %s" % m.get("error"))
        self.assertEqual(m.get("centered"), "ok",
                         "居中/描边契约被破坏：%s" % m)


class NativeAvailabilityTest(unittest.TestCase):
    """降级闸门：只有主线程才允许走 NSAlert，否则窗口创建会直接崩。"""

    @classmethod
    def setUpClass(cls):
        import sf_system_alert
        cls.mod = sf_system_alert

    def test_main_thread_may_use_native_alert(self):
        if self.mod.AppKit is None:
            self.skipTest("本机无 AppKit")
        self.assertTrue(self.mod.native_available())

    def test_worker_thread_must_not_use_native_alert(self):
        answers = []

        def worker():
            answers.append(self.mod.native_available())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(answers, [False],
                         "后台线程弹 NSAlert 会让 AppKit 直接崩，必须判为不可用")


if __name__ == "__main__":
    unittest.main()
