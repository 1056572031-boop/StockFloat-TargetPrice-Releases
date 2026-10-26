# -*- coding: utf-8 -*-
"""装机包真点击走查工具的判据用例（2026-10-23，"发布流程补真实点击" B 档的可失败性核验）。

这份用例不碰屏幕、不发一个真事件：把 HID 层换成记账替身，喂进**假的屏上窗口列表**，
然后逐条问同一个问题——

    应用真的按预期响应了 → 动作判通过；
    应用压根没响应    → 动作必须判红，并把"为什么"写进证据。

这一层是整个走查工具自己的可失败性。没有它，工具就成了"永远说通过"的那道关
（AGENTS §3.1 里 `verify_dmg_pyz` 写死旧特征、十几个版本每次无脑 PASS 就是同一族事故）。

按 AGENTS §6：不写版本号字面量（R1）、不读源码做字符串断言（R2）、不 sleep 后断言（R3）、
不依赖当下时钟（R4）、只调公开函数与替身装配（R5）、继承 SFCase（R7）。
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from _base import SFCase                                   # noqa: E402
except ImportError:                                            # pragma: no cover
    from tests._base import SFCase                             # noqa: E402

import post_release_click_walkthrough as W                    # noqa: E402

MENU_WIDTH = 152              # §5.9 实测的菜单块尺寸 152x173，用来造"新出现的窗口"
PANEL_HEIGHT = 220


class _VirtualClock:
    """仅给纯替身走查阶段用：sleep 保留节拍并推进虚拟单调时间。"""

    def __init__(self):
        self.now = 0.0  # discipline-ok(R4): 这是夹具自建虚拟单调时间，不读取系统当前时刻
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now  # discipline-ok(R4): 返回夹具控制的虚拟时间以保留轮询节拍

    def sleep(self, seconds):
        seconds = float(seconds)
        self.sleeps.append(seconds)
        self.now += seconds  # discipline-ok(R4): sleep 必须推进夹具的虚拟时间而非读取系统时钟


class WalkthroughStage(SFCase):
    """假现场：窗口列表由测试脚本给，HID 只记账，等待时限全部压到百毫秒级。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sf_click_wt_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config: dict = {}
        self.log_text = ""
        self.rows: list[dict] = []
        self.clicks: list[tuple[int, int, bool]] = []
        self.click_timing: list[tuple[float, float]] = []
        self.drags: list[tuple[int, int, int, int]] = []
        self.keys: list[tuple[int, int, bool]] = []     # (keycode, pid, 是否带 Cmd)
        self.typed: list[str] = []
        self.typed_pids: list[int] = []
        self.pixel = W.MENU_BG_RGB
        # 菜单第一行文字的垂直中心（pt，相对 bounds 顶边）；真出图那一组另算
        self.first_row = 21.0
        # 记下还没被 mock 的那两把尺子：需要"真出图"的用例把它换回来
        self._pristine_first_row = W.first_text_band_center
        self._pristine_capture_region = W.capture_region
        self._pristine_hid_type = W.hid_type
        self.clock = _VirtualClock()
        self.on_click = None
        self.on_drag = None
        patches = [
            mock.patch.object(W, "_CLOCK", self.clock),
            mock.patch.object(W, "list_windows", lambda pid: list(self.rows)),
            mock.patch.object(W, "hid_click", self._click),
            mock.patch.object(W, "hid_drag", self._drag),
            mock.patch.object(W, "hid_key", self._key),
            mock.patch.object(W, "hid_select_all", lambda pid: self.keys.append((0, pid, True))),
            mock.patch.object(W, "hid_type", self._type),
            mock.patch.object(W, "capture_pixel", lambda point, out: self.pixel),
            mock.patch.object(W, "capture_region", lambda rect, out: out),
            mock.patch.object(W, "first_text_band_center", lambda path, rect, **kw: self.first_row),
            mock.patch.object(W, "LAUNCH_TIMEOUT_SEC", 0.3),
            mock.patch.object(W, "ACTION_WAIT_SEC", 0.3),
            mock.patch.object(W, "TOGGLE_WAIT_SEC", 0.3),
            mock.patch.object(W, "CLOSE_WAIT_SEC", 0.3),
            mock.patch.object(W, "POLL_SEC", 0.01),
        ]
        for p in patches:
            self.track_patch(p)

    def _click(self, x, y, right=False, settle_sec=W.CLICK_SETTLE_SEC,
               hold_sec=W.CLICK_DOWN_UP_SEC):
        self.clicks.append((int(x), int(y), right))
        self.click_timing.append((settle_sec, hold_sec))
        if self.on_click:
            self.on_click(int(x), int(y), right)

    def _key(self, code, pid, command=False):
        self.keys.append((int(code), int(pid), bool(command)))

    def _type(self, text, pid):
        self.typed.append(text)
        self.typed_pids.append(int(pid))

    def key_codes(self) -> list[int]:
        return [code for code, _pid, _cmd in self.keys]

    def _drag(self, x, y, dx, dy):
        self.drags.append((int(x), int(y), int(dx), int(dy)))
        if self.on_drag:
            self.on_drag(int(x), int(y), int(dx), int(dy))

    def write_state(self):
        """把当前 config / 日志落到临时目录，供动作内的读取路径真读。"""
        (self.home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.home / "debug.log").write_text(self.log_text, encoding="utf-8")

    def make_ctx(self, height: int, x: int = W.SEED_X, y: int = W.SEED_Y) -> object:
        main = {"X": x, "Y": y, "Width": W.LAYOUT.W, "Height": height}
        self.rows = [main]
        self.write_state()
        return W.Ctx(pid=4242, home=self.home, shot=self.tmp / "pixel.png", main=dict(main))


class ClockIsolation(SFCase):
    """生产默认仍用真实时钟，纯替身阶段才替换模块私有 `_CLOCK`。"""

    def test_default_clock_waits_in_real_time(self):
        self.assertIs(W._CLOCK, time)  # discipline-ok(R5): 必须确认生产默认没被虚拟时钟泄漏
        started = time.monotonic()
        with mock.patch.object(W, "POLL_SEC", 0.005), \
                mock.patch.object(W, "list_windows", return_value=[]):
            self.assertIsNone(W.wait_rows(4242, lambda rows: rows or None, timeout=0.01))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.005,
                                "生产默认时钟不许被纯替身用例的虚拟 sleep 污染")
        self.assertLess(elapsed, 0.2, "短轮询不应意外卡住")


class ChartAndStretch(WalkthroughStage):
    def test_点分时图_高度真的变小又回到原值_判通过(self):
        h0 = W.LAYOUT.MIN_H + 40
        ctx = self.make_ctx(height=h0)
        state = {"collapsed": False}

        def respond(x, y, right):
            # 第一次点击收起、第二次点回原高度：模拟主窗自己那条收展链路
            row = dict(self.rows[0])
            row["Height"] = 150 if not state["collapsed"] else h0
            state["collapsed"] = not state["collapsed"]
            self.rows = [row]
            self.write_state()

        self.on_click = respond
        result = W.action_chart(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(len(self.clicks), 2, "两次点击都该落在热区上")

    def test_点分时图_高度一动不动_必须判红(self):
        """负向对照：应用不理这一下时，工具不许替它说"通过"。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        result = W.action_chart(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["evidence"]["collapse"]["ok"])

    def test_收起态下点分时图_本轮跳过并写明理由(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H - 60)
        result = W.action_chart(ctx)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["evidence"]["reason"], "already_collapsed")

    def test_展开态拉下沿_变高判通过(self):
        h0 = W.LAYOUT.MIN_H + 20
        ctx = self.make_ctx(height=h0)

        def respond(x, y, dx, dy):
            row = dict(self.rows[0])
            row["Height"] = h0 + dy
            self.rows = [row]
            self.write_state()

        self.on_drag = respond
        result = W.action_stretch(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(self.drags[0][3], 40, "下沿手势必须真的向下拖")

    def test_下沿拖了但高度没变_必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 20)
        result = W.action_stretch(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["evidence"]["verdict"]["ok"])

    def test_收起态不许拉高_直接跳过而不是假装通过(self):
        """负向对照的另一半：收起态底部不响应拉伸（v1.2.28），工具必须承认"这里测不了"。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H - 60)
        result = W.action_stretch(ctx)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["evidence"]["reason"], "needs_expanded")
        self.assertEqual(self.drags, [], "跳过就不该真的发手势")


class MenuAndDrag(WalkthroughStage):
    def test_右键之后菜单出现且取色对上_判通过(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)

        def respond(x, y, right):
            if right:
                self.rows = self.rows + [{"X": x - 40, "Y": y, "Width": MENU_WIDTH,
                                          "Height": 173}]
            else:
                # 点中某一行：产品会写一条 picked=<名字> 的留痕
                self.rows = [r for r in self.rows if int(r["Width"]) != MENU_WIDTH]
                self.log_text = ("[10:00:00] INFO: ctx menu opened, app_state=active\n"
                                 "[10:00:01] INFO: ctx menu closed after 270ms, "
                                 "picked=%s, reason=picked\n" % W.EXPECTED_MENU_PICK)
            self.write_state()

        self.on_click = respond
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertTrue(result["evidence"]["trace"]["picked"], "证据里要留下点中的行名")
        self.assertTrue(self.clicks[0][2], "第一下必须是右键")

    def test_菜单收起了但点中的不是期望那一行_必须判红(self):
        """落点是估算，所以"菜单关了"不等于"点对了"：点错行必须当场判红并留下行名。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)

        def respond(x, y, right):
            if right:
                self.rows = self.rows + [{"X": x - 40, "Y": y, "Width": MENU_WIDTH,
                                          "Height": 173}]
            else:
                self.rows = [r for r in self.rows if int(r["Width"]) != MENU_WIDTH]
                self.log_text = ("[10:00:00] INFO: ctx menu opened\n"
                                 "[10:00:01] INFO: ctx menu closed, "
                                 "picked=feedback, reason=picked\n")
            self.write_state()

        self.on_click = respond
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["trace"]["reason"], "missed_or_wrong_row")
        self.assertEqual(result["evidence"]["trace"]["picked"], "feedback",
                         "证据要写清楚实际点中了哪一行，好让人改落点")

    def test_菜单第一行文字量不出来时必须判红而不是盲点(self):
        """CGWindowList 的 bounds 含阴影；量不到文字中心就别点，盲点会点到别的窗口。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.first_row = None

        def respond(x, y, right):
            if right:
                self.rows = self.rows + [{"X": x - 40, "Y": y, "Width": MENU_WIDTH,
                                          "Height": 173}]
            self.write_state()

        self.on_click = respond
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "menu_first_row_not_found")
        self.assertEqual(len(self.clicks), 1, "量不出来就不许再点第二下")

    def test_右键之后没有任何新窗口_必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "menu_window_not_on_screen")

    def test_菜单建出来但截到的是白底_必须判红(self):
        """§5.9 第三问：建出来 ≠ 合成到屏上。取色拿不到内容就是没看见。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)

        def respond(x, y, right):
            if right:
                self.rows = self.rows + [{"X": x - 40, "Y": y, "Width": MENU_WIDTH,
                                          "Height": 173}]
            self.write_state()

        self.on_click = respond
        self.pixel = (255, 255, 255)
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["evidence"]["pixel"]["ok"])

    def test_拖动跟手_判通过(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        before = dict(self.rows[0])

        def respond(x, y, dx, dy):
            moved = dict(before)
            moved["X"] = int(before["X"]) + dx
            moved["Y"] = int(before["Y"]) + dy
            self.rows = [moved]
            self.write_state()

        self.on_drag = respond
        result = W.action_drag(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(ctx.main["X"], before["X"] + 60, "现场要记住窗口已经搬走")

    def test_拖了但窗口没动_必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        result = W.action_drag(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["evidence"]["actual"], "判红时不许编一个位移出来")


class SecondaryWindows(WalkthroughStage):
    def test_换股票_代码落进临时_config_判通过(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "x": W.SEED_X, "y": W.SEED_Y, "alerts": []}
        self.write_state()
        editor = {"X": W.SEED_X + W.LAYOUT.W + 12, "Y": W.SEED_Y,
                  "Width": W.LAYOUT.W, "Height": 300}
        entry = W.editor_entry_point(editor)
        confirm = W.editor_confirm_point(editor)

        pending = {"code": None}

        def respond(x, y, right):
            if int(x) == int(confirm["x"]) and int(y) == int(confirm["y"]):
                # 只有真点「确定」才允许应用并关窗；输入框那一下不能偷着完成换股。
                self.rows = [self.rows[0]]
                pending["code"] = W.STOCK_PREFIXED_CODE
            else:
                self.rows = [self.rows[0], dict(editor)]
            self.write_state()

        def save_on_drag(x, y, dx, dy):
            # 产品在拉伸松手时才写 config：模拟这一步
            row = dict(self.rows[0])
            row["Height"] = int(row["Height"]) + dy
            self.rows = [row]
            if pending["code"]:
                self.config["code"] = pending["code"]
            self.write_state()

        self.on_click = respond
        self.on_drag = save_on_drag
        result = W.action_stock(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(self.typed, [W.STOCK_CODE_DIGITS], "只能真敲数字样本")
        self.assertEqual(self.config["code"], W.STOCK_PREFIXED_CODE)
        self.assertEqual(result["evidence"]["confirm_point"], confirm)
        self.assertNotIn(W.KVK["return"], self.key_codes(),
                         "本轮需求是点确定，不许再拿 Return 替代")
        self.assertTrue(self.drags, "换股后必须用一次真实拉伸触发落盘")
        self.assertTrue(result["evidence"]["config"]["save_triggered"]["ok"])

    def test_换股票_不点确定即使输入已变也必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.write_state()
        editor = {"X": W.SEED_X + W.LAYOUT.W + 12, "Y": W.SEED_Y,
                  "Width": W.LAYOUT.W, "Height": 300}

        def respond(x, y, right):
            # 编辑器和输入框都正常，但「确定」点击没有闭窗也没有落盘。
            self.rows = [self.rows[0], dict(editor)]
            self.write_state()

        self.on_click = respond
        result = W.action_stock(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["evidence"]["config"]["editor_closed_seen"])

    def test_换股票_编辑窗压根没出现_必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.write_state()
        result = W.action_stock(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "editor_window_not_on_screen")

    def test_编辑窗出现但代码没换_必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.write_state()
        editor = {"X": W.SEED_X + W.LAYOUT.W + 12, "Y": W.SEED_Y,
                  "Width": W.LAYOUT.W, "Height": 300}

        def respond(x, y, right):
            self.rows = [self.rows[0], dict(editor)]      # 窗开了，config 永远不动
            self.write_state()

        self.on_click = respond
        result = W.action_stock(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["evidence"]["config"]["ok"])

    def test_目标价_加一条生效再清空_判通过(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "alerts": []}
        self.write_state()
        main_row = self.rows[0]
        panel = {"X": int(main_row["X"]) + 60, "Y": int(main_row["Y"]) + 240,
                 "Width": W.TP.PANEL_W, "Height": PANEL_HEIGHT}
        # 产品 _refresh_list() 成功插一条完整列表行后，真机量到 193→244pt（+51pt）。
        panel_after = {**panel, "Height": int(panel["Height"]) + 51}
        pts = W.panel_points(panel)
        pts_after = W.panel_points(panel_after)
        clears = {"count": 0}

        def respond(x, y, right):
            panel_open = next((r for r in self.rows if int(r["Width"]) == W.TP.PANEL_W), None)
            if panel_open is None:
                self.rows = [self.rows[0], dict(panel)]        # 点铃铛：面板出现
            elif int(x) == int(pts["plus"]["x"]) and int(y) == int(pts["plus"]["y"]):
                self.rows = [self.rows[0], dict(panel_after)]  # 加号成功后重排，底排下移
            elif int(x) == int(pts_after["apply"]["x"]) and int(y) == int(pts_after["apply"]["y"]):
                self.config["alerts"] = [{"target": 101.0, "direction": W.TP.TARGET_DIR_ABOVE,
                                          "code": "sh600519"}]
                self.rows = [self.rows[0]]                     # 生效成功后面板自己关闭
            elif int(x) == int(pts["clear"]["x"]):
                clears["count"] += 1
                if clears["count"] == 2:                        # 第二次「确认清空」才落盘
                    self.config["alerts"] = []
            self.write_state()

        self.on_click = respond
        result = W.action_target(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(self.config["alerts"], [], "走查结束时不许留下目标价")

    def test_目标价_点了生效但_config_里没有_必须判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "alerts": []}
        self.write_state()
        panel = {"X": W.SEED_X + 60, "Y": W.SEED_Y + 240,
                 "Width": W.TP.PANEL_W, "Height": PANEL_HEIGHT}

        def respond(x, y, right):
            if not any(int(r["Width"]) == W.TP.PANEL_W for r in self.rows):
                self.rows = [self.rows[0], dict(panel)]        # 面板开了，但落盘永远不发生
            self.write_state()

        self.on_click = respond
        result = W.action_target(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["evidence"]["saved"]["ok"])

    def test_面板没出现_必须判红而不是空跑(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.write_state()
        result = W.action_target(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "target_panel_not_on_screen")

    def test_换股票必须先全选再敲六位(self):
        """实测：编辑框预填当前代码，光标停在末尾 —— 直接敲会变成 sh600519000001，
        搜索失败、窗也不关。真人换股票是"全选→输入"，走查必须走同一条路。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.write_state()
        editor = {"X": W.SEED_X + W.LAYOUT.W + 12, "Y": W.SEED_Y,
                  "Width": W.LAYOUT.W, "Height": 151}

        def respond(x, y, right):
            self.rows = [self.rows[0], dict(editor)]
            self.write_state()

        self.on_click = respond
        W.action_stock(ctx)
        self.assertEqual(self.keys[0][2], True, "第一手键盘动作必须是带 Cmd 的全选")
        self.assertEqual(self.keys[0][0], W.KVK["a"])
        self.assertEqual(self.typed, [W.STOCK_CODE_DIGITS])

    def test_所有键盘事件都只能投递给被走查的那个pid(self):
        """这个程序永远不是前台应用：键一旦发到 HID 通道，就会敲进用户正在用的文档。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.write_state()
        editor = {"X": W.SEED_X + W.LAYOUT.W + 12, "Y": W.SEED_Y,
                  "Width": W.LAYOUT.W, "Height": 151}

        def respond(x, y, right):
            self.rows = [self.rows[0], dict(editor)]
            self.write_state()

        self.on_click = respond
        W.action_stock(ctx)
        self.assertTrue(self.keys, "这一条本来就该发键盘事件")
        self.assertEqual({pid for _code, pid, _cmd in self.keys}, {ctx.pid})
        self.assertEqual(set(self.typed_pids), {ctx.pid})

    def test_加号落点必须落在真截图量到的按钮带内(self):
        """2026-10-24 试运行实测：面板宽 206pt 时加号墨迹中心在 171~177pt，
        而"输入框右边 + 半个按钮"算出来是 159pt —— 落在按钮左边的空白 stretch 上，
        面板开着、点也发了，一条目标价都没加进去。判据按真截图的带来，不按公式。"""
        pts = W.panel_points({"X": 0, "Y": 0, "Width": W.TP.PANEL_W, "Height": 193})
        self.assertTrue(W.PANEL_PLUS_BAND[0] <= pts["plus"]["x"] <= W.PANEL_PLUS_BAND[1],
                        "加号中心 %s 不在实测带 %s 内" % (pts["plus"]["x"], W.PANEL_PLUS_BAND))
        self.assertGreater(pts["plus"]["x"], pts["input"]["x"])

    def test_窗口被人搬走后_必须按新位置取点(self):
        """实测过一次：等用户点那一下时他顺手把窗口拖到了别处，
        后面每个动作都按旧坐标点空。取点前必须重读窗口位置。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "alerts": []}
        self.write_state()
        moved = {"X": W.SEED_X + 900, "Y": W.SEED_Y + 300,
                 "Width": W.LAYOUT.W, "Height": W.LAYOUT.MIN_H + 40}
        self.rows = [moved]
        bell = W.top_button_point(moved, "bell")
        panel = {"X": bell["x"] - 40, "Y": bell["y"] + 260,
                 "Width": W.TP.PANEL_W, "Height": PANEL_HEIGHT}

        def respond(x, y, right):
            # 只有落在**新位置**的铃铛上才开面板：按旧坐标点就什么也不会发生
            if (x, y) == (int(bell["x"]), int(bell["y"])):
                self.rows = [moved, dict(panel)]
            self.write_state()

        self.on_click = respond
        result = W.action_target(ctx)
        self.assertEqual(ctx.main["X"], W.SEED_X + 900, "动作开头必须把主窗位置追回来")
        self.assertNotEqual(result["evidence"].get("reason"), "target_panel_not_on_screen",
                            "取点还在用搬走之前的旧坐标")


class SummarizeAccounting(SFCase):
    """结论口径：skip 不算通过、一条没跑成不许退 0、判红必须退 1。"""

    def test_全过才退零(self):
        summary = W.summarize([{"status": "pass"}, {"status": "pass"}])
        self.assertTrue(summary["ok"])
        self.assertEqual(W.overall_exit(summary, []), W.EXIT_OK)

    def test_有判红就退一(self):
        summary = W.summarize([{"status": "pass"}, {"status": "fail"}])
        self.assertFalse(summary["ok"])
        self.assertEqual(W.overall_exit(summary, []), W.EXIT_RED)

    def test_跳过不算通过但也不判红(self):
        summary = W.summarize([{"status": "pass"}, {"status": "skip"}])
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["ran"], 1)
        self.assertEqual(W.overall_exit(summary, []), W.EXIT_OK)

    def test_空或错误目标价不许冒充涨到已写入(self):
        bad = {"alerts": [{"target": "不是数字", "direction": W.TP.TARGET_DIR_ABOVE,
                           "code": "sz000001"},
                          {"target": None, "direction": W.TP.TARGET_DIR_ABOVE,
                           "code": "sz000001"}]}
        self.assertFalse(W.judge_alert_written(bad, "sz000001")["ok"])

    def test_一条都没跑成时不许退零(self):
        summary = W.summarize([{"status": "skip"}, {"status": "skip"}])
        self.assertTrue(summary["not_evaluated"])
        self.assertEqual(W.overall_exit(summary, []), W.EXIT_RED)

    def test_预检拦住永远压过执行结论(self):
        self.assertEqual(W.overall_exit(W.summarize([{"status": "pass"}]),
                                        ["screen_locked"]), W.EXIT_PRECONDITION)


class RealScreenshotJudges(WalkthroughStage):
    """真出图这一组不许走桩：菜单落点算错的后果是"点到别的窗口"，只能量真像素。"""

    def _real_first_row(self):
        return self.track_patch(mock.patch.object(W, "first_text_band_center",
                                                  self._pristine_first_row))

    def _strip(self, name: str, kinds: list[str]) -> tuple[pathlib.Path, dict]:
        """按"每行是什么"造一张 2x 截图：shadow=阴影、speckle=桌面零星亮点、
        text=一行文字、bg=菜单底。返回 (路径, 与之对应的窗口 bounds)。"""
        from PyQt6.QtGui import QColor, QImage
        palette = {"shadow": (20, 20, 20), "bg": W.MENU_BG_RGB,
                   "speckle": W.MENU_BG_RGB, "text": W.MENU_BG_RGB}
        image = QImage(60, len(kinds), QImage.Format.Format_RGB32)
        for y, kind in enumerate(kinds):
            for x in range(image.width()):
                image.setPixelColor(x, y, QColor(*palette[kind]))
            if kind == "text":                       # 24 个亮像素，与真菜单同一量级
                for x in range(16, 40):
                    image.setPixelColor(x, y, QColor(240, 240, 240))
            if kind == "speckle":                    # 3 个亮点：不够一行文字的量
                for x in (6, 30, 52):
                    image.setPixelColor(x, y, QColor(240, 240, 240))
        path = self.tmp / name
        self.assertTrue(image.save(str(path)), "夹具图都没写出来，判据等于空转")
        return path, {"X": 0, "Y": 0, "Width": image.width() // 2,
                      "Height": image.height() // 2}

    def test_第一行中心必须跳过阴影与桌面亮点(self):
        self._real_first_row()
        path, rect = self._strip("menu.png", ["shadow"] * 2 + ["speckle"] * 2
                                 + ["text"] * 6 + ["bg"] * 6)
        got = W.first_text_band_center(path, rect)
        # 文字带占第 4..9 行（0 基），中心 6.5 图像像素 = 3.25pt（截图是 2x）
        self.assertEqual(got, 3.25, "量到的必须是文字带中心，且已按 DPR 折成 pt")

    def test_整块都没有文字时必须给None而不是猜一行(self):
        self._real_first_row()
        path, rect = self._strip("blank.png", ["shadow"] * 3 + ["bg"] * 10)
        self.assertIsNone(W.first_text_band_center(path, rect))
        speckle, _ = self._strip("speckle.png", ["speckle"] * 2 + ["bg"] * 10)
        self.assertIsNone(W.first_text_band_center(speckle, rect),
                          "两行零星亮点不够「连续三行」，不能当成第一行文字")

    def test_没有截图或尺寸对不上时按答不了处理(self):
        self._real_first_row()
        path, rect = self._strip("menu2.png", ["text"] * 4 + ["bg"] * 4)
        self.assertIsNone(W.first_text_band_center(None, rect))
        self.assertIsNone(W.first_text_band_center(self.tmp / "没有这个文件.png", rect))
        self.assertIsNone(W.first_text_band_center(path, {"X": 0, "Y": 0, "Width": 0,
                                                         "Height": 0}),
                          "宽度取不到就算不出缩放，硬给一个中心等于编")
        self.assertIsNone(W.first_text_band_center(path, {"X": 0, "Y": 0, "Width": "宽度坏了",
                                                         "Height": 0}),
                          "缩放字段类型坏掉也只能答不了，不能把整个走查带崩")

    def test_取屏必须按整块窗口区域且截不到时返回None(self):
        self._real_capture_region()
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["check"] = kwargs.get("check")

        with mock.patch.object(W.subprocess, "run", fake_run):
            out = W.capture_region({"X": 10, "Y": 20, "Width": MENU_WIDTH, "Height": 173},
                                   self.tmp / "menu.png")
        self.assertEqual(out, self.tmp / "menu.png")
        self.assertEqual(seen["cmd"][-2], "-R10,20,%d,173" % MENU_WIDTH)
        self.assertTrue(seen["check"], "截屏失败必须能被 except 抓到，不能静默出白图")
        with mock.patch.object(W.subprocess, "run",
                               side_effect=OSError("screencapture 拒绝")):
            self.assertIsNone(W.capture_region({"X": 0, "Y": 0, "Width": 4, "Height": 4},
                                               self.tmp / "nope.png"))

    def _real_capture_region(self):
        return self.track_patch(mock.patch.object(W, "capture_region",
                                                  self._pristine_capture_region))


class SettleAndPainting(WalkthroughStage):
    """两条实测换来的判据：动画中间帧不能当结果、弹层没画上就不许敲字。"""

    def test_wait_stable_必须跳过过渡值取到稳定值(self):
        ticks = iter([218, 206, 146, 146])
        got = W.wait_stable(lambda: next(ticks, 146), timeout=2.0, tick=0.01)
        self.assertEqual(got, 146, "206 是收展动画的中间帧，量它等于量一个不存在的高度")
        self.assertEqual(self.clock.sleeps, [0.01, 0.01, 0.01, 0.01],
                         "稳定值仍需按原采样节拍连续三拍，不能跳过过渡帧")

    def test_wait_stable_一直取不到值时如实返回None(self):
        self.assertIsNone(W.wait_stable(lambda: None, timeout=0.1, tick=0.01))
        self.assertEqual(self.clock.sleeps, [0.01] * 11,
                         "取不到值时必须走完整个超时窗口")

    def test_主窗从窗口列表消失时不许编一个稳定高度(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.rows = []
        self.assertIsNone(W._sync_main(ctx))  # discipline-ok(R5): 必须种主窗消失这一条内部采样分支

    def test_点分时图_过渡帧不许被当成收起后的结果(self):
        """实测红过：218→206(过渡)→217，两拍各差 12/11 被判成"没变化"。"""
        ctx = self.make_ctx(height=218)
        state = {"clicks": 0, "reads": 0}

        def rows_now(_pid):
            if state["clicks"] == 0:
                height = 218                            # 还没点：稳定在展开态
            else:
                start, final = (218, 146) if state["clicks"] == 1 else (146, 218)
                state["reads"] += 1
                height = (start + final) // 2 if state["reads"] <= 2 else final
            return [{"X": W.SEED_X, "Y": W.SEED_Y, "Width": W.LAYOUT.W, "Height": height}]

        def respond(x, y, right):
            state["clicks"] += 1
            state["reads"] = 0

        self.track_patch(mock.patch.object(W, "list_windows", rows_now))
        self.on_click = respond
        result = W.action_chart(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(result["evidence"]["collapse"]["after"], 146)
        self.assertEqual(result["evidence"]["expand"]["before"], 146,
                         "第二拍的「变更前」必须是收起之后的高度，不是结果自己")

    def test_面板没画上时必须判红而不是往空气里敲字(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "alerts": []}
        self.first_row = None          # 截图里量不到一行字 = 还没合成到屏上
        self.write_state()
        panel = {"X": W.SEED_X + 200, "Y": W.SEED_Y, "Width": W.TP.PANEL_W,
                 "Height": PANEL_HEIGHT}

        def respond(x, y, right):
            self.rows = [self.rows[0], dict(panel)]
            self.write_state()

        self.on_click = respond
        result = W.action_target(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "target_panel_not_painted")
        self.assertEqual(self.typed, [], "没画上就不该把价格敲出去")

    def test_加号后面板没重画时不许拿旧坐标点立即生效(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "alerts": []}
        self.write_state()
        panel = {"X": W.SEED_X + 200, "Y": W.SEED_Y, "Width": W.TP.PANEL_W,
                 "Height": PANEL_HEIGHT}
        pts = W.panel_points(panel)

        def respond(x, y, right):
            if not any(int(r["Width"]) == W.TP.PANEL_W for r in self.rows):
                self.rows = [self.rows[0], dict(panel)]
            elif int(x) == int(pts["plus"]["x"]):
                # 加号后列表层没合成：工具必须停在这里，不能按旧底排坐标继续点。
                self.first_row = None
            self.write_state()

        self.on_click = respond
        result = W.action_target(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "target_panel_not_repainted_after_add")
        self.assertEqual(self.key_codes(), [W.KVK["escape"]])
        self.assertEqual(self.config["alerts"], [])

    def test_生效后第二次面板没画上时不许沿用旧坐标清空(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        panel = {"X": W.SEED_X + 200, "Y": W.SEED_Y, "Width": W.TP.PANEL_W,
                 "Height": PANEL_HEIGHT}
        after = dict(panel, Height=PANEL_HEIGHT + 40)
        self.config = {"code": "sh600519", "alerts": [{"direction": W.TP.TARGET_DIR_ABOVE, "target": 1515.1}]}
        self.write_state()
        responses = iter(((panel, True), (after, True), (None, False)))
        with mock.patch.object(W, "settle_popup", side_effect=lambda *_args: next(responses)), \
                mock.patch.object(W, "wait_popup_gone", return_value=True):
            result = W.action_target(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "target_panel_not_repainted_for_clear")
        self.assertEqual(len(self.clicks), 5, "第二次打开失败后不许发送两次清空点击")
        self.assertEqual(self.key_codes()[-1], W.KVK["escape"])

    def test_编辑窗没画上时同样判红(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519"}
        self.first_row = None
        self.write_state()
        editor = {"X": W.SEED_X + 200, "Y": W.SEED_Y, "Width": W.LAYOUT.W, "Height": 151}

        def respond(x, y, right):
            self.rows = [self.rows[0], dict(editor)]
            self.write_state()

        self.on_click = respond
        result = W.action_stock(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["reason"], "editor_not_painted")
        self.assertEqual(self.typed, [])

    def test_敲完字必须留下一张面板截图(self):
        """这一条失败时唯一能回答"字到底进框了没有"的就是它，不能只留一个布尔。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.config = {"code": "sh600519", "alerts": []}
        self.write_state()
        panel = {"X": W.SEED_X + 200, "Y": W.SEED_Y, "Width": W.TP.PANEL_W,
                 "Height": PANEL_HEIGHT}

        def respond(x, y, right):
            self.rows = [self.rows[0], dict(panel)]
            self.write_state()

        self.on_click = respond
        result = W.action_target(ctx)
        self.assertTrue(str(result["evidence"]["typed_shot"]).endswith("panel-typed.png"),
                        result["evidence"])


class EvidenceHandling(WalkthroughStage):
    def test_收起态下拉伸落盘必须如实回报而不是硬拖(self):
        """换股靠"拉伸松手"落盘；窗口处于收起态时这个动作做不到，必须回报而不是假装。"""
        ctx = self.make_ctx(height=int(W.LAYOUT.collapsed_height()))
        verdict = W.force_save_by_resize(ctx)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "needs_expanded")
        self.assertEqual(self.drags, [], "收起态不该被拖一下")

    def test_日志与config抄进证据目录后临时家目录必须消失(self):
        home = self.tmp / "runtime_home"
        home.mkdir()
        (home / "debug.log").write_text("STARTUP v\n", encoding="utf-8")
        (home / "config.json").write_text('{"code": "sh600519"}', encoding="utf-8")
        (home / "不该带走.txt").write_text("无关文件", encoding="utf-8")
        kept = self.tmp / "evidence"
        W.copy_runtime_evidence(home, kept)
        self.assertEqual(sorted(p.name for p in (kept / "runtime").iterdir()),
                         ["config.json", "debug.log"])
        self.assertEqual((kept / "runtime" / "debug.log").read_text(encoding="utf-8"),
                         "STARTUP v\n", "证据必须与运行时逐字相同，不是又生成一份")
        self.assertFalse(home.exists(), "临时家目录留在盘上就是走查自己的垃圾")

    def test_抄证据失败也不许把临时家目录留在盘上(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("这是文件，不是目录", encoding="utf-8")
        home = self.tmp / "runtime_home2"
        home.mkdir()
        (home / "debug.log").write_text("x", encoding="utf-8")
        W.copy_runtime_evidence(home, blocker / "sub")
        self.assertFalse(home.exists(), "收尾那一半必须无条件执行")


MENU_OPEN_LOG = "[10:00:00] INFO: ctx menu opened, app_state=ApplicationInactive\n"
PICK_LOG = ("[10:00:01] INFO: ctx menu closed after 270ms, picked=%s, reason=picked\n"
            % W.EXPECTED_MENU_PICK)
NONE_LOG = ("[10:00:01] INFO: ctx menu closed after 400ms, picked=none, reason=unknown\n")


class MenuClickDelivery(WalkthroughStage):
    """菜单这一条的三条实测事实：要慢点、留痕写得晚、点偏了不许拿键盘救场。"""

    def _open_menu_on_right_click(self, ctx):
        def respond(x, y, right):
            if right:
                self.rows = self.rows + [{"X": x - 40, "Y": y, "Width": MENU_WIDTH,
                                          "Height": 173}]
            self.write_state()

        self.on_click = respond
        return respond

    def test_菜单行必须用慢档点击(self):
        """实测：快档（0.03/0.06）那一下只把菜单收掉，picked=none；
        0.25/0.20 才真的选中第一行。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.log_text = MENU_OPEN_LOG + PICK_LOG
        self._open_menu_on_right_click(ctx)
        W.action_menu(ctx)
        _right_open, click_row = self.click_timing[0], self.click_timing[1]
        self.assertEqual(click_row, (W.MENU_CLICK_SETTLE_SEC, W.MENU_CLICK_HOLD_SEC),
                         "菜单那一行的按下时长与前置停顿必须走慢档")

    def test_留痕写得晚也必须判通过而不是当成没点中(self):
        """产品是在菜单收起那一刻才写这行的；只读一次会把"还没写"当成"没点到"。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.log_text = MENU_OPEN_LOG + PICK_LOG
        self._open_menu_on_right_click(ctx)
        reads = {"n": 0}

        def late_log():
            reads["n"] += 1
            return self.log_text if reads["n"] >= 3 else MENU_OPEN_LOG

        ctx.log = late_log
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertGreaterEqual(reads["n"], 3, "必须是轮询等到留痕，而不是读一次就判")

    def test_菜单一直挂着没留痕时才允许键盘兜底(self):
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.log_text = MENU_OPEN_LOG          # 永远没有 close 行 = 鼠标那一下没被接住

        def respond(x, y, right):
            if right:
                self.rows = self.rows + [{"X": x - 40, "Y": y, "Width": MENU_WIDTH,
                                          "Height": 173}]
            self.write_state()

        self.on_click = respond

        def key(code, pid, command=False):
            self.keys.append((code, pid, command))
            if code == W.KVK["return"]:
                self.log_text = MENU_OPEN_LOG + PICK_LOG
                self.rows = [r for r in self.rows if int(r["Width"]) != MENU_WIDTH]
                self.write_state()

        self.track_patch(mock.patch.object(W, "hid_key", key))
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(result["evidence"]["method"], "keyboard")
        self.assertIn(W.KVK["down"], self.key_codes(), "键盘兜底得先按方向键选中首行")

    def test_菜单已picked_none收起时不许偷偷改用键盘救场(self):
        """picked=none 说明坐标偏了：必须判红并把落点留在证据里，
        拿键盘把它救成"通过"等于把缺陷藏起来。"""
        ctx = self.make_ctx(height=W.LAYOUT.MIN_H + 40)
        self.log_text = MENU_OPEN_LOG + NONE_LOG
        self._open_menu_on_right_click(ctx)
        result = W.action_menu(ctx)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"]["method"], "click")
        self.assertEqual(self.key_codes(), [W.KVK["escape"]],
                         "菜单还挂着时可以补一发 Esc 收尾，但不许按方向键+Return 救场")
        self.assertEqual(result["evidence"]["clicked_row"]["y"],
                         int(result["evidence"]["menu_bounds"]["Y"]) + 21,
                         "证据要留下实际落点，好让人改取点")


class ReadingAndCli(WalkthroughStage):
    def test_read_json_坏文件返回空字典而不是抛错(self):
        (self.home / "config.json").write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(W.read_json(self.home / "config.json"), {})
        self.assertEqual(W.read_json(self.home / "没有这个文件.json"), {})
        (self.home / "list.json").write_text("[1,2]", encoding="utf-8")
        self.assertEqual(W.read_json(self.home / "list.json"), {})

    def test_Ctx_读日志与刷新主窗位置(self):
        ctx = self.make_ctx(height=250)
        self.log_text = "hello 走查\n"
        self.write_state()
        self.assertEqual(ctx.log().strip(), "hello 走查")
        moved = {"X": W.SEED_X + 50, "Y": W.SEED_Y + 10,
                 "Width": W.LAYOUT.W, "Height": 250}
        self.rows = [moved]
        self.assertEqual(ctx.refresh_main()["X"], W.SEED_X + 50)

    def test_走查样本全部有明确键盘投递规则(self):
        """六码只用数字；目标价用产品原生 `+1%`，其 +/% 两个 Shift 组合键必须由
        hid_type 显式处理，不能落到前台应用或静默跳过。"""
        self.assertTrue(all(ch in W.KVK for ch in W.STOCK_CODE_DIGITS))
        self.assertEqual(W.TARGET_PRICE, "+1%")
        calls = []
        with mock.patch.object(W, "hid_key",
                               lambda code, pid, command=False, shift=False:
                               calls.append((code, pid, command, shift))):
            self._pristine_hid_type(W.TARGET_PRICE, 4242)
        self.assertEqual(calls, [(0x18, 4242, False, True),
                                 (W.KVK["1"], 4242, False, False),
                                 (W.KVK["5"], 4242, False, True)])

    def test_seed_config_起点固定且目标价从空开始(self):
        seed = W.seed_config()
        self.assertEqual((seed["x"], seed["y"]), (W.SEED_X, W.SEED_Y))
        self.assertEqual(seed["alerts"], [])
        self.assertEqual(seed["code"], "sh600519")

    def test_bundle_version_读不到plist时给空串不抛错(self):
        self.assertEqual(W.bundle_version(ROOT / "sf_constants.py"), "")

    def test_self_test_分支确实分发到自检并返回成功(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = W.main(["--self-test"])
        self.assertEqual(rc, 0, out.getvalue())

    def test_自检种错后必须走判红分支(self):
        """不是只看自检绿：把三把尺子各坏一处，必须收集问题、打印问题并退 1。"""
        real_height = W.judge_height
        real_menu = W.menu_row_point

        def broken_height(before, after, direction, min_delta=W.STRETCH_MIN_DELTA_PX):
            if direction == "sideways":
                return {"ok": False}
            return real_height(before, after, direction, min_delta)

        def broken_menu(rect, index, rows):
            if rows == 0:
                return {"x": 0, "y": 0}
            return real_menu(rect, index, rows)

        out = io.StringIO()
        with mock.patch.object(W, "pick_main", lambda *args, **kwargs: None), \
             mock.patch.object(W, "judge_height", broken_height), \
             mock.patch.object(W, "menu_row_point", broken_menu), \
             redirect_stdout(out):
            rc = W.self_test()
        self.assertEqual(rc, 1)
        self.assertIn("自检", out.getvalue())
        self.assertIn("判红", out.getvalue())

    def test_高度辅助函数_找不到相符窗口时返回None(self):
        rows = [{"X": 0, "Y": 0, "Width": W.LAYOUT.W, "Height": 200}]
        missed = W._height_above(rows, 260)  # discipline-ok(R5): 纯辅助函数没有公开替代入口
        hit = W._height_above(rows, 150)     # discipline-ok(R5): 同上
        self.assertIsNone(missed)
        self.assertEqual(hit, 200)

    def test_递归快照必须抓到图标写入(self):
        with tempfile.TemporaryDirectory(prefix="sf_click_tree_") as temp:
            root = pathlib.Path(temp)
            icon = root / "icon.png"
            icon.write_bytes(b"old")
            before = W.snapshot_tree(root)
            icon.write_bytes(b"newer")
            after = W.snapshot_tree(root)
        verdict = W.judge_user_data_untouched({"support": before}, {"support": after})
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["changed"], ["support/icon.png"])

    def test_递归快照不存在目录返回空字典(self):
        self.assertEqual(W.snapshot_tree(pathlib.Path("/definitely/not/a/support-dir")), {})

    def test_用户目录快照涵盖支持目录和旧配置(self):
        with tempfile.TemporaryDirectory(prefix="sf_click_user_") as temp:
            home = pathlib.Path(temp)
            support = home / "support"
            support.mkdir()
            (support / "icon.png").write_bytes(b"icon")
            legacy = home / W.LEGACY_CONFIG_NAME
            legacy.write_text("{}", encoding="utf-8")
            with mock.patch.object(W.sf_constants, "APP_SUPPORT_DIR", str(support)), \
                 mock.patch.object(W.pathlib.Path, "home", return_value=home):
                snapshot = W.user_data_stat()
        self.assertIn("icon.png", snapshot["support"])
        self.assertIn(W.LEGACY_CONFIG_NAME, snapshot["legacy_config"])

    def test_wait_startup_首帧到达才通过(self):
        class _Running:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory(prefix="sf_click_start_") as temp:
            home = pathlib.Path(temp)
            (home / "debug.log").write_text("STARTUP vX\nfirst-frame painted\n", encoding="utf-8")
            verdict = W.wait_startup(home, _Running(), timeout=0.1)
        self.assertTrue(verdict["ok"])
        self.assertIsNone(verdict["process_returncode"])

    def test_wait_startup_进程提前退出必须判红(self):
        class _Exited:
            @staticmethod
            def poll():
                return 9

        with tempfile.TemporaryDirectory(prefix="sf_click_start_") as temp:
            verdict = W.wait_startup(pathlib.Path(temp), _Exited(), timeout=0.1)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["process_returncode"], 9)

    def test_wait_startup_活着但首帧超时必须判红(self):
        class _Running:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory(prefix="sf_click_start_") as temp:
            verdict = W.wait_startup(pathlib.Path(temp), _Running(), timeout=0.1)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "first_frame_timeout")
        self.assertEqual(self.clock.sleeps, [W.POLL_SEC] * 11,
                         "虚拟时钟仍要逐拍推进超时轮询，不能把 sleep 置空")

    def test_wait_startup_首帧稍后出现则在超时前通过(self):
        class _Running:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory(prefix="sf_click_start_") as temp:
            home = pathlib.Path(temp)
            reads = iter(("", "STARTUP vX\nfirst-frame painted\n"))
            with mock.patch.object(W, "read_log", side_effect=lambda _home: next(reads)):
                verdict = W.wait_startup(home, _Running(), timeout=0.1)
        self.assertTrue(verdict["ok"])
        self.assertEqual(self.clock.sleeps, [W.POLL_SEC],
                         "首帧第二拍出现时必须提前结束，不能硬等到超时")

    def test_快照遇到消失文件和子目录时不崩(self):
        with tempfile.TemporaryDirectory(prefix="sf_click_tree_") as temp:
            root = pathlib.Path(temp)
            child = root / "child"
            child.mkdir()
            gone = root / "gone.log"
            gone.write_text("gone", encoding="utf-8")
            original_lstat = pathlib.Path.lstat

            def flaky_lstat(self):
                if self == gone:
                    raise OSError("gone during snapshot")
                return original_lstat(self)

            with mock.patch.object(W.pathlib.Path, "lstat", new=flaky_lstat):
                snapshot = W.snapshot_tree(root)
        self.assertEqual(snapshot, {})

    def test_launch_frozen_必须传临时HOME和断网环境(self):
        class _Proc:
            pid = 99

        with tempfile.TemporaryDirectory(prefix="sf_click_launch_") as temp:
            home = pathlib.Path(temp) / "home"
            seen = {}

            def fake_popen(argv, **kwargs):
                seen.update(argv=argv, **kwargs)
                return _Proc()

            with mock.patch.object(W.VD, "bundle_executable", return_value="/tmp/Skyrocket"), \
                 mock.patch.object(W.SMOKE, "isolation_env", return_value={"HOME": "/real/home"}), \
                 mock.patch.object(W.subprocess, "Popen", side_effect=fake_popen):
                proc = W.launch_frozen(pathlib.Path("/tmp/Skyrocket.app"), home, W.seed_config())
            self.assertEqual(proc.pid, 99)
            env = seen["env"]
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["SF_UNIVERSE_DISABLE"], "1")
            self.assertEqual(env["SF_REJECT_NETWORK"], "1")
            self.assertEqual(env["https_proxy"], W.NETWORK_BLOCK_PROXY)
            self.assertEqual(env["HTTPS_PROXY"], W.NETWORK_BLOCK_PROXY)
            self.assertEqual(env["QT_QPA_PLATFORM"], "cocoa")
            self.assertTrue((home / "config.json").exists())

    def test_启动没画首帧就必须判红且不执行动作(self):
        class _Exited:
            pid = 77

            @staticmethod
            def poll():
                return 1

        with tempfile.TemporaryDirectory(prefix="sf_click_start_") as temp:
            root = pathlib.Path(temp)
            evidence = None
            with mock.patch.object(W, "launch_frozen", return_value=_Exited()), \
                 mock.patch.object(W, "_terminate", lambda proc: None), \
                 mock.patch.object(W, "user_data_stat", return_value={}), \
                 mock.patch.object(W, "action_menu") as action:
                evidence = W.run_walkthrough(pathlib.Path("/tmp/Skyrocket.app"), root, ["menu"])
            self.assertEqual(evidence["summary"]["failed"], 1)
            self.assertEqual(evidence["results"][0]["name"], "launch")
            action.assert_not_called()

    def test_启动首帧有了但动作后出现未捕获异常必须判红(self):
        class _Running:
            pid = 78

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory(prefix="sf_click_runtime_") as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            home.mkdir()
            (home / "debug.log").write_text("STARTUP vX\nfirst-frame painted\n", encoding="utf-8")
            main = {"X": W.SEED_X, "Y": W.SEED_Y, "Width": W.LAYOUT.W,
                    "Height": W.LAYOUT.MIN_H}

            def fake_action(ctx):
                (home / "debug.log").write_text("STARTUP vX\nfirst-frame painted\nUNCAUGHT boom\n",
                                                  encoding="utf-8")
                return {"status": "pass", "evidence": {}}

            with mock.patch.object(W, "launch_frozen", return_value=_Running()), \
                 mock.patch.object(W, "tempfile") as tempfile_mod, \
                 mock.patch.object(W, "_terminate", lambda proc: None), \
                 mock.patch.object(W, "copy_runtime_evidence", lambda h, o: None), \
                 mock.patch.object(W, "user_data_stat", return_value={}), \
                 mock.patch.object(W, "wait_startup", return_value={"ok": True}), \
                 mock.patch.object(W, "wait_rows", return_value=main), \
                 mock.patch.object(W, "action_menu", side_effect=fake_action):
                tempfile_mod.mkdtemp.return_value = str(home)
                evidence = W.run_walkthrough(pathlib.Path("/tmp/Skyrocket.app"), root, ["menu"])
            self.assertEqual(evidence["summary"]["failed"], 1)
            self.assertEqual(evidence["results"][-1]["name"], "runtime")

    def test_正式版正在运行必须阻断前置(self):
        reasons = W.preflight_reasons(pathlib.Path("/tmp/app"),
                                      {"locked": False, "display_asleep": False},
                                      True, True, ["menu"], installed_running=True)
        self.assertIn("installed_app_running", reasons)

    def test_screen_state_读接口异常时明确返回未知(self):
        with mock.patch.dict(sys.modules, {"commit_preflight": None}):
            self.assertEqual(W._screen_state(), {"locked": None, "display_asleep": None})  # discipline-ok(R5): 必须直接种工具的异常回退，公开 main 会先碰真实屏幕状态

    def test_cli_真跑结论里用户数据被改动时退1(self):
        """预检即使全绿，临时隔离若碰了真实配置也必须覆盖原本的通过结论。"""
        passed = {"ran": 1, "passed": 1, "failed": 0, "skipped": 0,
                  "not_evaluated": False, "ok": True}
        with tempfile.TemporaryDirectory(prefix="sf_click_cli_") as temp:
            out_dir = pathlib.Path(temp) / "evidence"
            evidence = {"summary": dict(passed), "user_data": {"ok": False,
                        "changed": ["config.json"]}}
            out = io.StringIO()
            with mock.patch.object(W, "_screen_state", return_value={"locked": False,
                                                                         "display_asleep": False}), \
                 mock.patch.object(W, "accessibility_trusted", return_value=True), \
                 mock.patch.object(W, "preflight_reasons", return_value=[]), \
                 mock.patch.object(W, "bundle_version", return_value="test"), \
                 mock.patch.object(W, "run_walkthrough", return_value=evidence), \
                 redirect_stdout(out):
                rc = W.main(["--i-am-at-the-machine", "--out", str(out_dir)])
            self.assertEqual(rc, 1, out.getvalue())
            self.assertIn("隔离缺陷", out.getvalue())
            self.assertTrue(out_dir.exists(), "CLI 仍应建好证据目录，方便保留失败现场")

    def test_cli_真跑全过时返回0(self):
        passed = {"ran": 1, "passed": 1, "failed": 0, "skipped": 0,
                  "not_evaluated": False, "ok": True}
        with tempfile.TemporaryDirectory(prefix="sf_click_cli_") as temp:
            evidence = {"summary": dict(passed), "user_data": {"ok": True, "changed": []}}
            with mock.patch.object(W, "_screen_state", return_value={"locked": False,
                                                                         "display_asleep": False}), \
                 mock.patch.object(W, "accessibility_trusted", return_value=True), \
                 mock.patch.object(W, "preflight_reasons", return_value=[]), \
                 mock.patch.object(W, "bundle_version", return_value="test"), \
                 mock.patch.object(W, "run_walkthrough", return_value=evidence):
                self.assertEqual(W.main(["--i-am-at-the-machine", "--out", temp]), 0)

    def test_report_只打字不发事件且点名缺产物(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = W.main(["--report", "--app", str(pathlib.Path("/definitely/not/here.app"))])
        text = out.getvalue()
        self.assertEqual(rc, 0, "报告档本身不判成败")
        self.assertIn("app_missing", text)
        self.assertNotIn("接管鼠标", text, "报告档不许宣布要接管鼠标")

    def test_没点头允许占屏_拒跑退2(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = W.main([])                       # 不带 --i-am-at-the-machine
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("不判通过也不判失败", out.getvalue())

    def test_未知参数退2(self):
        with self.assertRaises(SystemExit) as caught:
            W.main(["--deffinitely-not-a-flag"])
        self.assertEqual(caught.exception.code, 2)

    def test_走查清单里每条动作都能被调度(self):
        """名字表存的是**函数名**而不是函数对象（AGENTS §3.1 那条实测教训）：
        谁把某个动作改名或删掉，这里必须当场红，而不是等真跑时才 AttributeError。"""
        missing = [fn for _, fn in W.ACTIONS if not callable(getattr(W, fn, None))]
        self.assertEqual(missing, [])


class KeycodeGuard(SFCase):
    """`hid_type` 的拒绝路径。单独成类：上面的夹具把 hid_type 换成了替身，测不到真实现；
    而这里也**绝不能**放真键盘事件出去 —— 只 patch 到 hid_key 这一层。"""

    def test_表外字符当场抛错而不是静默跳过(self):
        sent: list[int] = []
        with mock.patch.object(W, "hid_key",
                               lambda code, pid, command=False: sent.append(code)):
            with self.assertRaises(ValueError):
                W.hid_type("600A19", 4242)
        self.assertEqual(len(sent), 3, "抛错前只发出已知的三位数字，不能整串发出去")


class NetworkGuardDisclosure(SFCase):
    """被测包带不带 `SF_REJECT_NETWORK` 要如实披露，不能让文档替旧包说一句它做不到的话。

    起因：已装 v1.4.103 的 `sf_network` 里没有这个开关，走查那轮日志里行情监控 `ok=1`，
    产品是真联网取过行情的；而文档曾写成“所有产品网络请求被硬拒绝”。"""

    def test_带开关的包判有_没有开关的旧包判无(self):
        self.assertIs(W.judge_network_guard({"a", W.NETWORK_GUARD_ENV})["present"], True)
        self.assertIs(W.judge_network_guard({"a", "b"})["present"], False)

    def test_读不出包内网络入口时不许猜成有或无(self):
        verdict = W.judge_network_guard(None)
        self.assertIsNone(verdict["present"])
        self.assertIn("不断言", verdict["note"])

    def test_旧包的说明必须点名会真实出网且不改变用户目录结论(self):
        note = W.judge_network_guard(set())["note"]
        self.assertIn("真实发出", note)
        self.assertIn("用户目录隔离结论不受影响", note)

    def test_字符串常量收集钻进嵌套函数且只收字符串(self):
        source = ("def outer():\n    def inner():\n        return 'deep-literal', 42\n"
                  "    return inner\n")
        found = W.string_literals(compile(source, "<fixture>", "exec"))
        self.assertIn("deep-literal", found)
        self.assertNotIn(42, found)

    def test_真实读取一个没有守卫的包_走完整读取链路(self):
        """用一个只含 sf_network 空壳字节码的假 Bundle，走 network_guard_state 的真实流程。"""
        class _FakeBundle:
            disposed = False

            def __init__(self, app):
                pass

            def code(self, name):
                return compile("x = 'plain'", "<sf_network>", "exec") if name == "sf_network" else None

            def dispose(self):
                _FakeBundle.disposed = True

        with mock.patch.object(W.VD, "Bundle", _FakeBundle):
            verdict = W.network_guard_state(pathlib.Path("/tmp/Fake.app"))
        self.assertIs(verdict["present"], False)
        self.assertTrue(_FakeBundle.disposed, "读完必须清理解包用的临时目录")

    def test_读取链路任何一环失败都落到读不出而不是抛错或判无(self):
        class _Boom:
            def __init__(self, app):
                raise SystemExit("执行文件不存在")

        with mock.patch.object(W.VD, "Bundle", _Boom):
            self.assertIsNone(W.network_guard_state(pathlib.Path("/tmp/none.app"))["present"])

        class _NoModule:
            def __init__(self, app):
                pass

            def code(self, name):
                return None

            def dispose(self):
                pass

        with mock.patch.object(W.VD, "Bundle", _NoModule):
            self.assertIsNone(W.network_guard_state(pathlib.Path("/tmp/none.app"))["present"])

        class _CodeBoom:
            disposed = False

            def __init__(self, app):
                pass

            def code(self, name):
                raise ValueError("归档里这条字节码读不出来")

            def dispose(self):
                _CodeBoom.disposed = True

        with mock.patch.object(W.VD, "Bundle", _CodeBoom):
            verdict = W.network_guard_state(pathlib.Path("/tmp/none.app"))
        self.assertIsNone(verdict["present"], "取字节码时抛错也只能是读不出，不能抛出去或判无")
        self.assertTrue(_CodeBoom.disposed, "抛错路径同样要清理解包用的临时目录")

    def test_结论行读的是证据里记下的说明而不是开头那一行(self):
        """开头（预检前）与结论（跑完后）各打一次：两处来源不同，改坏任一处必须能分辨。
        所以两个说明故意写成不同的哨兵文字。"""
        evidence = {"summary": {"ran": 1, "passed": 1, "failed": 0, "skipped": 0,
                                "not_evaluated": False, "ok": True},
                    "user_data": {"ok": True, "changed": []},
                    "network_guard": {"present": False, "note": "结论哨兵-来自证据"}}
        with tempfile.TemporaryDirectory(prefix="sf_click_net_") as temp:
            out = io.StringIO()
            with mock.patch.object(W, "_screen_state", return_value={"locked": False,
                                                                         "display_asleep": False}), \
                 mock.patch.object(W, "accessibility_trusted", return_value=True), \
                 mock.patch.object(W, "preflight_reasons", return_value=[]), \
                 mock.patch.object(W, "bundle_version", return_value="test"), \
                 mock.patch.object(W, "network_guard_state",
                                   return_value={"present": False, "note": "开头哨兵-来自探测"}), \
                 mock.patch.object(W, "run_walkthrough", return_value=evidence), \
                 redirect_stdout(out):
                rc = W.main(["--i-am-at-the-machine", "--out", temp])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("网络隔离: 开头哨兵-来自探测", text)
            self.assertIn("网络隔离: 结论哨兵-来自证据", text)

    def test_真实走查流程把网络守卫状态写进证据(self):
        """走 `run_walkthrough` 的真实实现（只替身起进程与取证），读它写出的 result.json。"""
        class _Exited:
            pid = 79

            @staticmethod
            def poll():
                return 1

        sentinel = {"present": False, "note": "证据哨兵"}
        with tempfile.TemporaryDirectory(prefix="sf_click_netev_") as temp:
            root = pathlib.Path(temp)
            with mock.patch.object(W, "launch_frozen", return_value=_Exited()), \
                 mock.patch.object(W, "_terminate", lambda proc: None), \
                 mock.patch.object(W, "user_data_stat", return_value={}), \
                 mock.patch.object(W, "network_guard_state", return_value=sentinel):
                evidence = W.run_walkthrough(pathlib.Path("/tmp/Skyrocket.app"), root, ["menu"])
            self.assertEqual(evidence["network_guard"], sentinel)
            written = json.loads((root / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(written["network_guard"], sentinel, "落盘的证据也必须带它")

    def test_报告档对存在的包也打印网络隔离说明(self):
        with tempfile.TemporaryDirectory(prefix="sf_click_netrep_") as temp:
            app = pathlib.Path(temp) / "Fake.app"
            app.mkdir()
            out = io.StringIO()
            with mock.patch.object(W, "network_guard_state",
                                   return_value=W.judge_network_guard({W.NETWORK_GUARD_ENV})), \
                 redirect_stdout(out):
                W.main(["--report", "--app", str(app)])
            self.assertIn("网络隔离: 包内带 SF_REJECT_NETWORK", out.getvalue())


if __name__ == "__main__":
    unittest.main()
