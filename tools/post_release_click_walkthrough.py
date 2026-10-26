#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布后验收：用**真实鼠标与键盘**点装机版 Skyrocket（2026-10-23 新增）。

为什么需要它
------------
装机版在发版链上只被"冷启动"过一次（第 8 步 `smoke_frozen_app.py`：起进程、读日志锚点、
看首帧），**没有任何一步真的点过它**。链上唯一"点界面"的第 3.65 步走查跑的是源码 `import`
出来的窗口（`native_glass_probe.ProbeWindow`）。而本仓库反复踩过同一个坑：全量回归两年全绿、
装机版却坏（改名漏改 OTA 助手、菜单首点无反应、降级态徽标画不出来……）。用户手上那份是
打包字节码，不是源码。这个工具补的就是这一段：**跑的是 /Applications 里那个包**，
事件走系统 HID 通道（`CGEventPost(kCGHIDEventTap)`），与手按鼠标是同一条路。

判据只看可观测事实
------------------
  · 窗口出现/消失 → `CGWindowListCopyWindowInfo` 按**属主 pid** 收屏上窗口；
  · 菜单是否真被合成出来 → 在它自己的 bounds 中心取一个像素与菜单底色比
    （AGENTS §5.9 三问的第三问：建出来 ≠ 看得见）；
  · 拖动 / 拉伸 / 收展 → 窗口 bounds 的 X/Y/Height 实测变化量；
  · 换股票、目标价 → **本工具指定的临时 config** 里 `code` 与 `alerts` 的实际内容；
  · 右键这一下有没有落到主窗 → 产品自己写的 `ctx menu opened` / `ctx menu closed picked=…`。

四条诚实边界（不许放宽）
----------------------
1. **前提不成立就退 2，绝不退 0。** 屏幕锁定、显示器睡着、驱动进程没有"辅助功能"授权、
   产物不存在、没给 `--i-am-at-the-machine`（这条会接管你屏幕上的光标）—— 任一条都是
   "这里答不了"，不是"这里通过了"。
2. **一条都没真跑成就不给总结论**：全部 skip → `not_evaluated` + 退 1，与
   `test_discipline_scan.min_files`、`diag_coverage_check` 的"空输入不算通过"同口径。
3. **只动自己拉的进程、自己写的文件。** 全隔离环境（config / 日志 / 单实例 socket / 字典缓存
   一律改道临时目录），前后各拍一次用户真实配置目录的 (size, mtime) 快照 —— 变了就是本工具
   的隔离失效，当场判红。绝不杀装机版、绝不改用户目标价。
4. **键盘事件一律 `CGEventPostToPid`，绝不走 HID 通道。** 这个程序是常驻浮动窗，
   `frontmostApplication` 从来不是它（实测 `activate()`、`activateWithOptions_`、合成点击、
   `AXFrontmost` 四种办法都做不到，为它白等过 90 秒）；把键发到 HID 等于把 1688 与六位代码
   敲进用户此刻正在用的那个文档。2026-10-24 实测定向投递会正常落进它自己的输入框
   （截图里看得见 1688 与 sh6005190000），所以既不需要"先把应用带到前台"，也不会打扰别的程序。

⚠ 落点坐标是**由源码布局常量算出来的估算**（主窗是手绘控件，没有可枚举的子窗口），
菜单那一行的纵向落点**不靠估算**：截图量第一行文字的墨迹中心（bounds 含阴影，按底色找顶边
会把阴影当成内容，实测过）。每次点击用的具体坐标都写进 `result.json`：试运行若发现点偏了，
改的是坐标函数、**判据不动**。每个动作开头都重读一次主窗位置（`Ctx.refresh_main`）——
鼠标事件走 HID，期间用户自己把窗口拖走是常态（实测过一次 X 从 60 变 1086），
按旧坐标继续点等于后面全点空。

用法
----
    ./.venv/bin/python tools/post_release_click_walkthrough.py --report
    ./.venv/bin/python tools/post_release_click_walkthrough.py --self-test
    ./.venv/bin/python tools/post_release_click_walkthrough.py --i-am-at-the-machine
    ./.venv/bin/python tools/post_release_click_walkthrough.py --action menu --action drag

退出码：0=被执行的用例全过且至少执行了一条｜1=有用例判红、隔离被破坏或一条都没跑成｜
        2=前提不成立（未知参数也由 argparse 退 2）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Any

# 时间依赖只从这一处取：生产默认是真实 time，纯替身测试可替换整只时钟，
# 不得 patch 共享标准库模块的属性影响同进程其他测试或残留线程。
_CLOCK = time

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 工具既可被 `python tools/…` 直接运行，也会在测试主进程里 import；前置插入两条路径，
# 保证两种入口的顶层产品模块解析一致。重复路径无副作用，反而避免"测试因环境已插入而没走到"
# 这一行把覆盖与直接运行分成两套口径。
sys.path[:0] = [str(ROOT / "tools"), str(ROOT)]

import frozen_startup_stream as FSS      # noqa: E402  pid_window_bounds
import sf_constants                      # noqa: E402  APP_SUPPORT_DIR
import smoke_frozen_app as SMOKE         # noqa: E402  isolation_env / scan_markers
import stock_float_window as LAYOUT      # noqa: E402  主窗布局常量（唯一来源）
import target_panel as TP                # noqa: E402  目标价面板布局常量（唯一来源）
import verify_dmg_pyz as VD              # noqa: E402  bundle_executable

# ---- 取值与阈值：每条都标来源（AGENTS §6：数字必须能回答"这从哪来"）----
LAUNCH_TIMEOUT_SEC = 30.0        # 与第 8 步 smoke_frozen_app --timeout 30 同档
POLL_SEC = 0.05                  # 轮询窗口列表的节拍，比 §5.9 实测落地时刻 0.27s 细一个量级
POPUP_SETTLE_SEC = 1.5          # 弹层落地+画上 的等待上限（实测落地 0.27s，动画 0.2s）
SETTLE_TICK_SEC = 0.1          # "连续三拍一致"的取样节拍：比动画 0.2s 细，又不至于把过渡帧当稳定
ACTION_WAIT_SEC = 3.0            # 手势之后等窗口列表出现新状态的兜底时限（拖动/拉伸实测都在 1 秒内）
TOGGLE_WAIT_SEC = 5.0            # 收展是 200ms 动画 + 合成延迟，给到 5 秒仍属宽松
CLOSE_WAIT_SEC = 8.0             # 编辑窗关闭：要等它自己落盘再收窗
DRAG_STEPS = 12                  # 拖动步数：够 Qt 收到多次 moved，不做帧率断言
DRAG_STEP_SEC = 0.02
MOVE_TOLERANCE_PX = 6            # 位移容差：Retina 取整 + 窗口管理器边界
SIZE_TOLERANCE_PX = 3            # 按尺寸认出主窗时的宽度容差
STRETCH_MIN_DELTA_PX = 12        # 拉伸必须看得见的最小变化量
MENU_BG_RGB = (38, 43, 58)       # 与 contextMenuEvent 那条 rgba(38,43,58,.97) 同值（tests/test_menu_row_icons.py:44）
MENU_BG_TOLERANCE = 26           # 平均色差上限：留一截给毛玻璃随桌面浮动，但不足以放行空白截图
CLICK_DOWN_UP_SEC = 0.06         # 按下到松开的停顿；短于此可能被 WindowServer 合成双击
CLICK_SETTLE_SEC = 0.03          # 指针到位到按下的停顿（主窗这类普通控件够用）
MENU_CLICK_SETTLE_SEC = 0.25     # 菜单专用慢档：等 QMenu 拿到鼠标抓取（实测快档只把菜单收掉）
MENU_CLICK_HOLD_SEC = 0.20       # 菜单专用的按下时长，与上面同一条实测
TARGET_PRICE = "+1%"             # 面板原生支持的相对涨幅：避免固定绝对价撞当天涨停（实测 1688 > 1450.34 被拒）
# 包里带网络硬拒绝开关时，走查期间取不到行情，面板的 +N% 没有当前价可换算（提示「无法获取当前价」、
# 不加条目、面板不关），随后点编辑还会被产品自己拦下；v1.4.104 走查 target/stock 判红即此。
# 此时改敲不依赖行情的整数绝对价：无行情就没有涨停线可越，只要求是正数。键盘映射里没有小数点，所以只能是整数。
TARGET_PRICE_OFFLINE = "1000"
STOCK_CODE_DIGITS = "000001"     # 走查样本代码：平安银行，6 位纯数字，避开字典前缀分支
STOCK_PREFIXED_CODE = "sz000001"
SEED_X, SEED_Y = 140, 140        # 起点写死，免得走查窗口落在别的屏幕上
# 只对走系统代理的库有效；产品自建的请求通道（sf_http.safe_urlopen）不认它，
# 真正的硬拒绝是下面的 NETWORK_GUARD_ENV，而且只有包里带该开关时才生效。
NETWORK_BLOCK_PROXY = "http://127.0.0.1:1"
NETWORK_GUARD_ENV = "SF_REJECT_NETWORK"
EDITOR_MARGIN_X = 14       # sf_editor._build 的左右内边距
EDITOR_BOTTOM_MARGIN = 12  # sf_editor._build 的底部内边距
EDITOR_CONFIRM_W = 58      # 当前 Qt 样式下「确定」按钮实测宽度；点后仍以 config 变更反证
EDITOR_CONFIRM_H = 33      # 当前 Qt 样式下「确定」按钮实测高度
LEGACY_CONFIG_NAME = ".stock_float_config_targetprice.json"
DEFAULT_APP = pathlib.Path("/Applications/Skyrocket.app")
MENU_TEXT_LUMINANCE = 150        # 行内算"文字像素"的亮度下限（实测菜单底色最亮 66、文字 255）
MENU_TEXT_MIN_PIXELS = 8         # 一行至少要有几个亮像素才算文字行（实测每行 22~51 个）
MENU_TEXT_RUN_ROWS = 3           # 必须连续几行都成立，挡掉桌面/阴影的零星亮点
PANEL_PLUS_BAND = (165, 185)   # 真截图量到的加号中心区间（面板宽 206pt 时）
HOME_PREFIX = "sf_click_home_"   # 子进程家目录放在短临时路径：socket 路径有 104 字节上限
EXPECTED_MENU_PICK = "refresh"    # 菜单第一项「刷新行情」在产品侧的固定行名（tag=refresh）
LOG_MENU_OPEN = "ctx menu opened"
LOG_MENU_CLOSE = "ctx menu closed"

# 退出码三档（AGENTS §3.1 的口径：0=被执行的都过｜1=判红/隔离被破坏/一条没跑成｜
# 2=前提不成立）。写具名常量而不是裸数字：main() 与 overall_exit 各返回一次，
# 谁改动哪一档都能读出来，测试断言也不必写死 0/1/2。
EXIT_OK = 0
EXIT_RED = 1
EXIT_PRECONDITION = 2

# Carbon 虚拟键码：数字、Return、Esc。来源为 Carbon HIToolkey 常量表（非本仓推导）
KVK = {"0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
       "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
       "return": 0x24, "escape": 0x35, "down": 0x7D, "a": 0x00}


# ============================ 纯函数：认窗与取点 ============================
def pick_main(rows: list[dict], remembered: dict,
              tolerance: int = SIZE_TOLERANCE_PX) -> dict | None:
    """按**宽度**认出主窗。

    同宽的不止一只：主窗、它身后的玻璃层（§5.9 实测 258/273 那一族），以及替换股票弹窗
    （宽度刻意与主窗一致）。排序键因此是"离上次位置的距离 + 离上次高度的距离"，
    玻璃层与主窗同坐标时按高度更接近的那只取 —— 高度是收展/拉伸判据的分母，取错一层
    就等于量了别人的尺寸。
    """
    same_width = [r for r in rows
                  if abs(int(r["Width"]) - int(remembered["Width"])) <= tolerance]
    if not same_width:
        return None
    wanted_h = int(remembered.get("Height") or 0)

    def rank(r: dict) -> tuple[int, int]:
        distance = abs(int(r["X"]) - int(remembered["X"])) + abs(int(r["Y"]) - int(remembered["Y"]))
        return (distance, abs(int(r["Height"]) - wanted_h))

    return min(same_width, key=rank)


def pick_tool_window(rows: list[dict], main: dict, expected_width: int,
                     width_tolerance: int = SIZE_TOLERANCE_PX) -> dict | None:
    """工具窗（编辑器/面板）：宽度对得上，且**不是**主窗那块位置。"""
    cand = [r for r in rows if abs(int(r["Width"]) - expected_width) <= width_tolerance
            and abs(int(r["X"]) - int(main["X"])) > 20]
    if not cand:
        return None
    return max(cand, key=lambda r: int(r["Height"]))


def pick_fresh_window(rows: list[dict], known: set[tuple[int, int, int, int]],
                      main_width: int) -> dict | None:
    """手势之后新出现的窗口（右键菜单）：签名没见过，且不是主窗那一档宽度。"""
    fresh = [r for r in rows if _sig(r) not in known and int(r["Width"]) != main_width]
    if not fresh:
        return None
    return max(fresh, key=lambda r: int(r["Width"]) * int(r["Height"]))


def _sig(row: dict) -> tuple[int, int, int, int]:
    return (int(row["X"]), int(row["Y"]), int(row["Width"]), int(row["Height"]))


def top_button_point(rect: dict, name: str) -> dict:
    """主窗顶部按钮的屏幕坐标。口径与 `tests/test_ui.py::_btn_center` 同一条：
    按钮横位在 `W - BTN_X[name]`、纵位在 `BTN_CY`。主窗是手绘控件，只能按常量算。"""
    return {"x": int(rect["X"]) + int(rect["Width"]) - LAYOUT.BTN_X[name],
            "y": int(rect["Y"]) + LAYOUT.BTN_CY}


def chart_point(rect: dict) -> dict:
    """分时图热区内一点：起点按当前高度选（展开态 CHART_TOP，收起态 Y_PRICE）。"""
    expanded = int(rect["Height"]) >= LAYOUT.MIN_H
    top = LAYOUT.CHART_TOP if expanded else LAYOUT.Y_PRICE
    return {"x": int(rect["X"]) + int(rect["Width"]) // 2, "y": int(rect["Y"]) + top + 8}


def idle_point(rect: dict) -> dict:
    """既不在顶部按钮行、也不在分时图热区内的点：用来做纯拖动。"""
    return {"x": int(rect["X"]) + 12, "y": int(rect["Y"]) + LAYOUT.Y_PRICE - 6}


def stretch_point(rect: dict) -> dict:
    """下沿把手：距窗口底 4px、横向居中（mousePressEvent 判 `y >= height-8`，仅展开态）。"""
    return {"x": int(rect["X"]) + int(rect["Width"]) // 2,
            "y": int(rect["Y"]) + int(rect["Height"]) - 4}


def panel_points(rect: dict) -> dict:
    """目标价面板四枚可点件的坐标，全部由 target_panel 自己的布局常量推出。

    面板是 QVBoxLayout，内边距 MARGIN_X/MARGIN_Y；顶行是标题 + ✕（固定 20px），
    然后 TITLE_GAP、输入行（高 TARGET_INPUT_H，输入框宽 TARGET_INPUT_W，加号紧贴其右），
    再列表行、状态行，最后底排两枚等宽按钮（高 BUTTON_H、间距 BUTTON_GAP，
    「清空」在左、「立即生效」在右）。
    """
    content_w = int(rect["Width"]) - 2 * TP.MARGIN_X
    btn_w = (content_w - TP.BUTTON_GAP) / 2.0
    input_row_y = TP.MARGIN_Y + 20 + TP.TITLE_GAP + TP.TARGET_INPUT_H / 2.0
    bottom_row_y = int(rect["Height"]) - TP.MARGIN_Y - TP.BUTTON_H / 2.0
    input_x = TP.MARGIN_X + TP.DIRECTION_W + TP.ROW_SPACING + TP.TARGET_INPUT_W / 2.0
    # 加号在**输入框组的最右端**：TargetInputGroup 的布局是 edit + stretch(1) + button，
    # 所以按钮贴着组的右边界（= 内容右边界），不是紧贴输入框右边。
    # 2026-10-24 试运行实测：按"输入框右边 + 半个按钮"取点会落在组内的空白 stretch 上，
    # 面板开着、点也发了，但一条目标价都没加进去（真截图量到加号墨迹中心在 171~177pt）。
    plus_x = int(rect["Width"]) - TP.MARGIN_X - TP.TARGET_INPUT_H / 2.0
    return {
        "input": {"x": int(rect["X"]) + input_x, "y": int(rect["Y"]) + input_row_y},
        "plus": {"x": int(rect["X"]) + plus_x, "y": int(rect["Y"]) + input_row_y},
        "clear": {"x": int(rect["X"]) + TP.MARGIN_X + btn_w / 2.0,
                  "y": int(rect["Y"]) + bottom_row_y},
        "apply": {"x": int(rect["X"]) + int(rect["Width"]) - TP.MARGIN_X - btn_w / 2.0,
                  "y": int(rect["Y"]) + bottom_row_y},
    }


def editor_entry_point(rect: dict) -> dict:
    """替换股票输入框中心：sf_editor._build 的内边距 14/12、行距 8、顶行 ✕ 高 20。"""
    return {"x": int(rect["X"]) + int(rect["Width"]) // 2,
            "y": int(rect["Y"]) + 12 + 20 + 8 + 18}


def editor_confirm_point(rect: dict) -> dict:
    """替换股票「确定」的中心点。

    编辑器底排的布局是 stretch + 取消 + 确定，窗口的右内边距与底内边距都由
    `sf_editor._build` 固定。确认按钮点完仍必须由临时 config 的 code 变更判定，
    所以常量漂移会判红而不是被这条公式掩盖。
    """
    return {"x": int(rect["X"]) + int(rect["Width"]) - EDITOR_MARGIN_X - EDITOR_CONFIRM_W / 2.0,
            "y": int(rect["Y"]) + int(rect["Height"]) - EDITOR_BOTTOM_MARGIN - EDITOR_CONFIRM_H / 2.0}


def menu_row_point(rect: dict, index: int, rows: int) -> dict:
    """菜单第 index 行的等分点（备用档：需要按行数铺开探测时才用它）。"""
    if rows <= 0:
        raise ValueError("菜单行数必须为正")
    return {"x": int(rect["X"]) + int(rect["Width"]) // 2,
            "y": int(rect["Y"]) + int(rect["Height"]) * (index + 0.5) / rows}


def menu_first_row_point(rect: dict, center_pt: float) -> dict:
    """菜单第一行的落点：横向取中线，纵向用**截图量出来的文字中心**。

    第一行是「刷新行情」（stock_float_window 里第一个 addAction，tag=refresh）。
    刻意不按行数等分：行数取决于 face_guard 这个功能开关在装机版里开不开，
    菜单是 6 行还是 7 行会跟着漂，等分点就漂。
    点中的到底是什么，由产品写下的 picked=X 说话。
    """
    return {"x": int(rect["X"]) + int(rect["Width"]) // 2,
            "y": int(rect["Y"]) + int(round(center_pt))}


# ============================ 纯函数：判据 ============================
def judge_displacement(before: dict, after: dict | None, dx: int, dy: int,
                       tolerance: int = MOVE_TOLERANCE_PX) -> dict:
    """拖动判据：原点位移与手势位移一致（在容差内）。找不到窗就是没动。"""
    if after is None:
        return {"ok": False, "reason": "main_window_not_found_after_drag",
                "expected": [dx, dy], "actual": None, "tolerance": tolerance}
    got_dx = int(after["X"]) - int(before["X"])
    got_dy = int(after["Y"]) - int(before["Y"])
    return {"ok": abs(got_dx - dx) <= tolerance and abs(got_dy - dy) <= tolerance,
            "expected": [dx, dy], "actual": [got_dx, got_dy], "tolerance": tolerance}


def judge_height(before_h: int, after_h: int | None, direction: str,
                 min_delta: int = STRETCH_MIN_DELTA_PX) -> dict:
    """高度判据。direction 取 smaller / bigger / same；`same` 是负向对照。"""
    if after_h is None:
        return {"ok": False, "reason": "window_not_found", "before": before_h,
                "after": None, "expected": direction}
    delta = int(after_h) - int(before_h)
    if direction == "smaller":
        ok = delta <= -min_delta
    elif direction == "bigger":
        ok = delta >= min_delta
    elif direction == "same":
        ok = abs(delta) <= 2
    else:
        raise ValueError("未知的高度口径：%s" % direction)
    return {"ok": ok, "before": before_h, "after": after_h, "delta": delta,
            "expected": direction, "min_delta": min_delta}


def judge_alert_written(config: dict, code: str) -> dict:
    """目标价落盘判据：临时 config 里有一条**涨到方向**的有效条目，且归属当前股票。

    走查输入默认用产品原生的相对写法 `+1%`（包带网络硬拒绝开关时改用离线整数价，见
    `pick_target_price`），落盘值取决于当时真实行情或所选样本，不能拿某只股票的固定
    绝对价硬比；方向、归属与正数目标价才是这条交互真正要证明的事实。
    """
    alerts = [a for a in (config.get("alerts") or []) if isinstance(a, dict)]
    hit = []
    for alert in alerts:
        try:
            valid_target = float(alert.get("target", 0)) > 0
        except (TypeError, ValueError):
            valid_target = False
        if (alert.get("direction") == TP.TARGET_DIR_ABOVE and valid_target
                and alert.get("code", code) == code):
            hit.append(alert)
    return {"ok": bool(hit), "stored_total": len(alerts),
            "targets": [a.get("target") for a in alerts],
            "directions": [a.get("direction") for a in alerts]}


def judge_alert_cleared(config: dict, code: str) -> dict:
    alerts = [a for a in (config.get("alerts") or []) if isinstance(a, dict)]
    mine = [a for a in alerts if a.get("code", code) == code]
    return {"ok": not mine, "stored_total": len(alerts), "own_total": len(mine)}


def judge_code_written(config: dict, digits: str) -> dict:
    """换股票判据：config 的 code 以这 6 位数字结尾（sh/sz 前缀由产品自己补）。"""
    code = str(config.get("code", ""))
    return {"ok": code.endswith(digits), "code": code, "typed": digits}


def judge_menu_pick(log_text: str, expected: str = EXPECTED_MENU_PICK) -> dict:
    """菜单点击判据：开过、收起过、而且点中的就是期望那一行。

    落点是估算，所以必须把实际点中的名字拿出来比对 —— 只判"菜单关掉了"就会把
    "点到分隔线/空白/别的行"也算通过，那正是这道关卡要防的事。
    """
    verdict = judge_menu_log(log_text)
    verdict["expected"] = expected
    verdict["ok"] = bool(verdict["ok"] and verdict["picked"] == expected)
    if not verdict["ok"] and verdict["closes"]:
        verdict["reason"] = "missed_or_wrong_row"
    return verdict


def judge_menu_log(log_text: str) -> dict:
    """右键与菜单的日志判据：出现过 ctx menu opened，且收起了（picked 留在证据里）。"""
    lines = log_text.splitlines()
    opens = [l for l in lines if LOG_MENU_OPEN in l]
    closes = [l for l in lines if LOG_MENU_CLOSE in l]
    picked = ""
    for line in reversed(closes):
        for token in line.split():
            if token.startswith("picked="):
                picked = token.split("=", 1)[1].rstrip(",")
    return {"ok": bool(opens) and bool(closes), "opens": len(opens), "closes": len(closes),
            "picked": picked}


def judge_pixel_is_menu(rgb: Any) -> dict:
    """取色判据：菜单那块窗真的被合成出来了。截不到 / 空白一律判红，不算通过。"""
    if rgb is None:
        return {"ok": False, "reason": "screenshot_unreadable"}
    diff = sum(abs(int(a) - int(b)) for a, b in zip(rgb, MENU_BG_RGB)) / 3.0
    return {"ok": diff <= MENU_BG_TOLERANCE, "sampled": [int(c) for c in rgb],
            "expected": list(MENU_BG_RGB), "mean_channel_diff": round(diff, 1)}


def judge_startup(markers: dict) -> dict:
    """冷启动三件事：起来了、画完首帧、没有未捕获异常。SWALLOWED 只记录不判红（本仓常态）。"""
    return {"ok": bool(markers.get("startup")) and bool(markers.get("first_frame"))
            and not markers.get("uncaught"),
            "startup": len(markers.get("startup") or []),
            "first_frame": len(markers.get("first_frame") or []),
            "uncaught": len(markers.get("uncaught") or []),
            "swallowed": len(markers.get("swallowed") or [])}


def judge_user_data_untouched(before: dict, after: dict) -> dict:
    """隔离反证：用户真实目录任何文件的 size/mtime_ns 改动都必须逐条点名。"""
    changed: list[str] = []

    def walk(prefix: str, left: Any, right: Any) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child = "%s/%s" % (prefix, key) if prefix else str(key)
                walk(child, left.get(key), right.get(key))
        elif left != right:
            changed.append(prefix)

    walk("", before, after)
    return {"ok": not changed, "changed": changed}


def preflight_reasons(app: pathlib.Path, screen: dict, trusted: Any,
                      human_present: bool, actions: list[str],
                      installed_running: bool = False) -> list[str]:
    """前提条件 → 阻塞原因列表。空列表才允许真发事件。"""
    reasons = []
    if not app.exists():
        reasons.append("app_missing:%s" % app)
    if installed_running:
        reasons.append("installed_app_running")
    if screen.get("locked") is not False:
        reasons.append("screen_locked_or_unknown:%s" % screen.get("locked"))
    if screen.get("display_asleep") is not False:
        reasons.append("display_asleep_or_unknown:%s" % screen.get("display_asleep"))
    if trusted is not True:
        reasons.append("accessibility_not_granted:%s" % trusted)
    if not human_present:
        reasons.append("need_flag:--i-am-at-the-machine")
    if not actions:
        reasons.append("no_action_selected")
    return reasons


def summarize(results: list[dict]) -> dict:
    """结论口径：skipped 不算通过；一条都没真跑成时给 not_evaluated。"""
    ran = [r for r in results if r.get("status") in ("pass", "fail")]
    return {"ran": len(ran),
            "passed": sum(1 for r in ran if r["status"] == "pass"),
            "failed": sum(1 for r in ran if r["status"] == "fail"),
            "skipped": sum(1 for r in results if r.get("status") == "skip"),
            "not_evaluated": not ran,
            "ok": bool(ran) and all(r["status"] == "pass" for r in ran)}


def overall_exit(summary: dict, blocked: list[str]) -> int:
    if blocked:
        return EXIT_PRECONDITION
    if summary["not_evaluated"] or summary["failed"]:
        return EXIT_RED
    return EXIT_OK


# ============================ 系统与产物：真发事件 ============================
def accessibility_trusted() -> Any:  # pragma: no cover - 真查系统授权，无断言价值
    """驱动进程有没有"辅助功能"授权。读不到返回 None —— 一律当不成立，不当通过。"""
    try:
        import ctypes
        lib = ctypes.CDLL("/System/Library/Frameworks/"
                          "ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def list_windows(pid: int) -> list[dict]:  # pragma: no cover - 真查 WindowServer
    return FSS.pid_window_bounds(pid)


def wait_rows(pid: int, predicate, timeout: float) -> Any:
    """轮询屏上窗口直到 predicate 给出非空结果。"""
    deadline = _CLOCK.monotonic() + timeout
    while _CLOCK.monotonic() < deadline:
        found = predicate(list_windows(pid))
        if found:
            return found
        _CLOCK.sleep(POLL_SEC)
    return None


def wait_stable(read_value, timeout: float, tick: float = SETTLE_TICK_SEC,
                needed: int = 3) -> Any:
    """连续 `needed` 次读到同一个值才算"到位了"（默认三拍 = 0.3 秒）。

    为什么不能取到就走：收展是 200ms 动画、弹层落地要 0.2~0.3 秒（§5.9 实测右键到
    看见菜单 0.27 秒）。2026-10-24 试运行就是取到 218→206 这个**过渡高度**就去比
    阈值，判成"只变了 12pt"，第二次又只涨回 11pt —— 量的其实是动画中间帧，不是结果。
    """
    deadline = _CLOCK.monotonic() + timeout
    previous = read_value()
    same = 1
    while _CLOCK.monotonic() < deadline:
        _CLOCK.sleep(tick)
        current = read_value()
        if current is not None and current == previous:
            same += 1
            if same >= needed:
                return current
        else:
            previous, same = current, 1
    return previous


def capture_region(rect: dict, out: pathlib.Path) -> Any:
    """把菜单窗口整块截下来。CGWindowList 的 bounds 含窗口阴影，所以不能直接按 top 点。"""
    try:
        subprocess.run(["screencapture", "-x",
                        "-R%d,%d,%d,%d" % (int(rect["X"]), int(rect["Y"]),
                                           int(rect["Width"]), int(rect["Height"])),
                        str(out)], check=True, capture_output=True, timeout=10)
    except Exception:
        return None
    return out


def first_text_band_center(image_path: Any, rect: dict,
                          luminance: int = MENU_TEXT_LUMINANCE,
                          min_pixels: int = MENU_TEXT_MIN_PIXELS,
                          run_rows: int = MENU_TEXT_RUN_ROWS) -> float | None:
    """在截图里找**第一行文字**的垂直中心，返回相对 bounds 顶边的 pt；没有文字给 None。

    两个用处：① 菜单第一行的落点（阴影会把按底色的算法骗过去）；② 弹层"真的画出来了"
    的证据 —— 窗口在列表里出现 ≠ 已合成到屏上（§5.9 三问），量不到一行字就是还没画上。

    为什么量文字、不量底色：CGWindowList 的 bounds 含窗口阴影，而阴影就是"桌面被压暗"，
    在深色桌面上它与菜单底色 (38,43,58) 的平均色差只有 6 —— 按底色找顶边会把阴影第一行
    当成内容顶边（2026-10-24 试运行实测 content_top=0）。行高、字号、阴影宽度都会随
    系统外观与 Qt 版本漂，"第一行文字的墨迹中心"不会：它就是要点的那一点。

    判据：一行里亮像素（亮度 > luminance）不少于 min_pixels，且连续 run_rows 行都成立
    —— 阴影/桌面区要么暗、要么只有零星几个亮点（实测桌面那一行最多 9 个且逐行衰减）。
    找不到就返回 None，由调用方判红，绝不盲点。
    """
    if image_path is None:
        return None
    try:
        from PyQt6.QtGui import QImage
        image = QImage(str(image_path))
        if image.isNull():
            return None
        image = image.convertToFormat(QImage.Format.Format_RGB32)
        scale = image.width() / float(rect["Width"]) if rect.get("Width") else 0.0
        if scale <= 0:
            return None
        left, right = int(image.width() * 0.08), int(image.width() * 0.92)
        counts = []
        for y in range(image.height()):
            hit = 0
            for x in range(left, right, max(1, int(scale))):     # 按 DPR 抽样，1x/2x 同一口径
                color = image.pixelColor(x, y)
                if (color.red() + color.green() + color.blue()) / 3.0 > luminance:
                    hit += 1
            counts.append(hit)
        start = None
        for y in range(len(counts) - run_rows + 1):
            if all(counts[y + i] >= min_pixels for i in range(run_rows)):
                start = y
                break
        if start is None:
            return None
        end = start
        while end + 1 < len(counts) and counts[end + 1] >= min_pixels:
            end += 1
        return (start + end) / 2.0 / scale
    except Exception:
        return None


def capture_pixel(point: dict, out: pathlib.Path) -> Any:  # pragma: no cover - 真调 screencapture
    """在指定屏幕点取一个像素：screencapture 截 3x3，再用 QImage 读中心。"""
    try:
        subprocess.run(["screencapture", "-x",
                        "-R%d,%d,3,3" % (int(point["x"]) - 1, int(point["y"]) - 1), str(out)],
                       check=True, capture_output=True, timeout=10)
    except Exception:
        return None
    try:
        from PyQt6.QtGui import QImage
        image = QImage(str(out))
        if image.isNull():
            return None
        color = image.convertToFormat(QImage.Format.Format_RGB32).pixelColor(1, 1)
        return (color.red(), color.green(), color.blue())
    except Exception:
        return None


def _post_mouse(kind: int, x: float, y: float, button) -> None:  # pragma: no cover - 真发 HID
    import Quartz
    event = Quartz.CGEventCreateMouseEvent(None, kind, (float(x), float(y)), button)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def hid_click(x: float, y: float, right: bool = False, settle_sec: float = CLICK_SETTLE_SEC,
              hold_sec: float = CLICK_DOWN_UP_SEC) -> None:  # pragma: no cover - 真发 HID
    """真实鼠标点击（走 HID 通道，与手按一下同一条路）。

    settle_sec 是"指针到位到按下"之间的停顿，hold_sec 是按下到松开。菜单那一行必须用
    慢档（0.25/0.2，实测）：QMenu 弹出后要先拿到系统鼠标抓取，快档那一下会被当成
    "点到外面"而只把菜单收掉 —— 产品留痕写成 picked=none, reason=unknown。
    """
    import Quartz
    button = Quartz.kCGMouseButtonRight if right else Quartz.kCGMouseButtonLeft
    down = Quartz.kCGEventRightMouseDown if right else Quartz.kCGEventLeftMouseDown
    up = Quartz.kCGEventRightMouseUp if right else Quartz.kCGEventLeftMouseUp
    _post_mouse(Quartz.kCGEventMouseMoved, x, y, button)
    _CLOCK.sleep(settle_sec)
    _post_mouse(down, x, y, button)
    _CLOCK.sleep(hold_sec)
    _post_mouse(up, x, y, button)


def hid_drag(x: float, y: float, dx: float, dy: float) -> None:  # pragma: no cover - 真发 HID
    import Quartz
    button = Quartz.kCGMouseButtonLeft
    _post_mouse(Quartz.kCGEventMouseMoved, x, y, button)
    _CLOCK.sleep(0.03)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x, y, button)
    _CLOCK.sleep(0.05)
    for i in range(1, DRAG_STEPS + 1):
        _post_mouse(Quartz.kCGEventLeftMouseDragged, x + dx * i / DRAG_STEPS,
                    y + dy * i / DRAG_STEPS, button)
        _CLOCK.sleep(DRAG_STEP_SEC)
    _CLOCK.sleep(0.15)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x + dx, y + dy, button)


def hid_key(keycode: int, pid: int, command: bool = False,
            shift: bool = False) -> None:  # pragma: no cover - 真发 HID
    """键盘事件**必须定向投递给被走查的那个 pid**。

    2026-10-24 实测：这类浮动窗的应用从来不是前台应用（`frontmostApplication` 一直是
    别的程序），但 `CGEventPostToPid` 的键会正常落进它自己的输入框 —— 所以既不需要
    "先把应用带到前台"（试过四种办法都做不到，白等 90 秒），也**绝不能**把键事件发到
    HID 通道：那会把 1688 与六位代码敲进用户此刻正在用的那个文档里。
    """
    import Quartz
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        flags = 0
        if command:
            flags |= Quartz.kCGEventFlagMaskCommand
        if shift:
            flags |= Quartz.kCGEventFlagMaskShift
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPostToPid(int(pid), event)
        _CLOCK.sleep(0.02)


def pick_target_price(guard_present: bool | None) -> str:
    """按被测包有没有网络硬拒绝开关选目标价样本。

    只有明确读到“带开关”才用离线整数价；没开关（旧包会真联网）或读不出（None）都沿用 `+1%`，
    不把“读不出”猜成带开关：猜错的代价是在能联网的包上敲一个可能越过涨停线的绝对价。
    """
    return TARGET_PRICE_OFFLINE if guard_present is True else TARGET_PRICE


def hid_type(text: str, pid: int) -> None:  # pragma: no cover - 真发 HID
    """定向输入走查样本：六码数字、目标价相对写法 `+1%` 或离线整数价。"""
    for ch in text:
        code = KVK.get(ch)
        if code is not None:
            hid_key(code, pid)
        elif ch == "+":
            hid_key(0x18, pid, shift=True)       # Carbon kVK_ANSI_Equal + Shift
        elif ch == "%":
            hid_key(KVK["5"], pid, shift=True)   # Shift+5
        else:
            raise ValueError("走查样本不支持字符 %r" % ch)


def hid_select_all(pid: int) -> None:  # pragma: no cover - 真发 HID
    """Cmd+A 全选。替换股票输入框**预填着当前代码**（实测 sh600519），
    直接敲六位会变成 sh600519000001 —— 那是用户自己也会踩的操作，
    所以走查按"先全选再输入"这条真实路线走。"""
    hid_key(KVK["a"], pid, command=True)


def launch_frozen(app: pathlib.Path, home: pathlib.Path, seed: dict) -> subprocess.Popen:
    """按 bundle 里的可执行文件直接起进程（不经 LaunchServices），环境全隔离。

    SF_* 只覆盖产品的主要持久化路径；图标、旧版迁移配置和任何第三方库的 `Path.home()`
    仍只会看 HOME。没有 HOME 隔离时，所谓"临时 config"会同时改写真实 icon.png，
    走查还因旧快照只看 json/log 而可能假绿。因此 HOME 必须和 cwd 一起指向同一个临时根。
    """
    exe = VD.bundle_executable(app)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(seed), encoding="utf-8")
    env = SMOKE.isolation_env(app, home)
    env.update({
        "HOME": str(home),
        "QT_QPA_PLATFORM": "cocoa",
        "SF_UNIVERSE_DISABLE": "1",
        NETWORK_GUARD_ENV: "1",
        "http_proxy": NETWORK_BLOCK_PROXY,
        "https_proxy": NETWORK_BLOCK_PROXY,
        "all_proxy": NETWORK_BLOCK_PROXY,
        "HTTP_PROXY": NETWORK_BLOCK_PROXY,
        "HTTPS_PROXY": NETWORK_BLOCK_PROXY,
        "ALL_PROXY": NETWORK_BLOCK_PROXY,
        "no_proxy": "",
        "NO_PROXY": "",
    })
    return subprocess.Popen([str(exe)], env=env, cwd=str(home),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def read_log(home: pathlib.Path) -> str:
    path = home / "debug.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def wait_startup(home: pathlib.Path, proc: subprocess.Popen,
                 timeout: float = LAUNCH_TIMEOUT_SEC) -> dict:
    """等到冻结包真的画出首帧，或明确证明它没能启动。

    不能像旧实现那样起进程后立即读一次日志：那一刻通常尚未写 STARTUP / first-frame，
    却仍继续发送 HID。首帧是任何点击动作的前提；没拿到就直接给 launch 记 fail。
    """
    deadline = _CLOCK.monotonic() + timeout
    markers: dict = {}
    while _CLOCK.monotonic() < deadline:
        markers = SMOKE.scan_markers(read_log(home))
        verdict = judge_startup(markers)
        if markers.get("first_frame") or proc.poll() is not None:
            verdict["process_returncode"] = proc.poll()
            return verdict
        _CLOCK.sleep(POLL_SEC)
    verdict = judge_startup(markers)
    verdict["process_returncode"] = proc.poll()
    verdict["reason"] = "first_frame_timeout"
    return verdict


def seed_config() -> dict:
    """起点写死：与 sf_config 的落盘契约同键名，alerts 从空开始，高度给默认档。"""
    return {"code": "sh600519", "x": SEED_X, "y": SEED_Y, "h": LAYOUT.MIN_H,
            "version": 1, "alerts": []}


def read_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def snapshot_tree(root: pathlib.Path) -> dict[str, list[int]]:
    """递归拍目录快照：文件相对路径 → (size, mtime_ns)。

    不按后缀筛选：走查隔离的契约是"真实用户目录一字节不动"，icon.png、sqlite -wal、
    新增的临时文件恰恰都可能不是 json/log。符号链接只记录自身元数据，不跟到目录外。
    """
    if not root.exists():
        return {}
    out: dict[str, list[int]] = {}
    for item in sorted(root.rglob("*")):
        try:
            stat = item.lstat()
        except OSError:
            continue
        if not (item.is_file() or item.is_symlink()):
            continue
        out[str(item.relative_to(root))] = [stat.st_size, stat.st_mtime_ns]
    return out


def user_data_stat() -> dict:
    """给用户真实支持目录与遗留家目录配置拍递归快照，用于隔离反证。"""
    support = pathlib.Path(sf_constants.APP_SUPPORT_DIR)
    home = pathlib.Path.home()
    out = {"support": snapshot_tree(support)}
    legacy = home / LEGACY_CONFIG_NAME
    if legacy.exists():
        stat = legacy.stat()
        out["legacy_config"] = {LEGACY_CONFIG_NAME: [stat.st_size, stat.st_mtime_ns]}
    return out


def bundle_version(app: pathlib.Path) -> str:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as fh:
            return str(plistlib.load(fh).get("CFBundleShortVersionString", ""))
    except Exception:
        return ""


def string_literals(code: Any) -> set[str]:
    """递归收集 code 对象里的全部字符串常量（含嵌套函数）。

    网络硬拒绝开关只以字符串字面量出现在 `os.environ.get("SF_REJECT_NETWORK")` 里，
    不进 `co_names`，所以不能用 `verify_dmg_pyz.names()` 查。
    """
    out: set[str] = set()
    stack = [code]
    while stack:
        current = stack.pop()
        for const in current.co_consts:
            if hasattr(const, "co_consts"):
                stack.append(const)
            elif isinstance(const, str):
                out.add(const)
    return out


def judge_network_guard(literals: set[str] | None) -> dict:
    """被测包的网络入口有没有 `SF_REJECT_NETWORK` 硬拒绝开关。

    只披露、不判成败：没有守卫的旧包照样能走查，用户目录隔离靠 HOME 改道与前后快照，
    与网络无关。但“产品网络请求被硬拒绝”这句话只有在包里真有该开关时才成立 ——
    已装 v1.4.103 实测没有（它随更晚的提交才进 `sf_network.http_get`），那轮 6/6 的日志里
    行情监控 `total=1 ok=1` 就是真联网取过行情。`None` 表示读不出包内网络入口，不得猜成有。
    """
    if literals is None:
        return {"present": None,
                "note": "读不出包内网络入口，不断言行情请求是否被拒绝"}
    if NETWORK_GUARD_ENV in literals:
        return {"present": True,
                "note": "包内带 %s：网络统一入口会硬拒绝，走查期间产品不出网" % NETWORK_GUARD_ENV}
    return {"present": False,
            "note": ("包内没有 %s（旧版本）：行情与分时请求会真实发出（只读 GET），"
                     "代理环境变量拦不住产品自建的请求通道；用户目录隔离结论不受影响"
                     % NETWORK_GUARD_ENV)}


def network_guard_state(app: pathlib.Path) -> dict:
    """读被测包里 `sf_network` 的字符串常量并判定；任何读取失败都落到“读不出”。"""
    try:
        bundle = VD.Bundle(app)
    except (SystemExit, Exception):
        return judge_network_guard(None)
    try:
        code = bundle.code("sf_network")
        return judge_network_guard(string_literals(code) if code is not None else None)
    except Exception:
        return judge_network_guard(None)
    finally:
        bundle.dispose()


# ============================ 动作 ============================
class Ctx:
    """一轮走查的现场：进程、临时目录、主窗当前位置、证据落点。"""

    def __init__(self, pid: int, home: pathlib.Path, shot: pathlib.Path, main: dict,
                 target_price: str = TARGET_PRICE):
        self.pid = pid
        self.home = home
        self.shot = shot
        self.main = dict(main)
        self.target_price = target_price

    def windows(self) -> list[dict]:
        return list_windows(self.pid)

    def refresh_main(self, timeout: float = ACTION_WAIT_SEC) -> dict:
        found = wait_rows(self.pid, lambda rows: pick_main(rows, self.main), timeout)
        if found:
            self.main = dict(found)
        return self.main

    def config(self) -> dict:
        return read_json(self.home / "config.json")

    def log(self) -> str:
        path = self.home / "debug.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def settle_popup(ctx: Ctx, expected_width: int, shot: pathlib.Path,
                 timeout: float = POPUP_SETTLE_SEC) -> tuple[dict | None, bool]:
    """等弹层落地并确认它真的画上了，返回 (bounds, 画上没有)。

    两条都是实测换来的：① 窗口出现在列表里 ≠ 已合成到屏上（§5.9 三问）；
    ② 面板刚建出来那 ~0.3 秒里点它的输入框，六位码/价格会敲不进去
    （2026-10-24 试运行 target 那条就是这么红的，截图里输入框是空的）。
    "画上没有"用的还是量第一行文字那把尺子 —— 弹层里必然有文字。
    """
    rect = wait_stable(lambda: pick_tool_window(ctx.windows(), ctx.main, expected_width),
                       timeout)
    if rect is None:
        return None, False
    return rect, first_text_band_center(capture_region(rect, shot), rect) is not None


def settled_height(ctx: Ctx, timeout: float = TOGGLE_WAIT_SEC) -> int | None:
    """取主窗**动画停下来之后**的高度，而不是过渡帧。"""
    return wait_stable(lambda: _sync_main(ctx), timeout)


def wait_config(ctx: Ctx, predicate, timeout: float = CLOSE_WAIT_SEC) -> dict:
    """等待临时 config 达到可观测状态，再做目标价保存/清空判定。

    目标价的 `_set_targets` 会走落盘协调器，`_apply` 随即关面板；2026-10-24 真机探针
    证明点完「立即生效」后立刻读盘会暂时仍是空 alerts，而 0.7 秒后已写入 `above` 条目。
    这里等真实文件变成期望状态，不能用一段猜测 sleep 充数。
    """
    deadline = _CLOCK.monotonic() + timeout
    latest = ctx.config()
    while _CLOCK.monotonic() < deadline:
        if predicate(latest):
            return latest
        _CLOCK.sleep(POLL_SEC)
        latest = ctx.config()
    return latest


def wait_popup_gone(ctx: Ctx, expected_width: int, timeout: float = CLOSE_WAIT_SEC) -> bool:
    """等已提交的面板真的从窗口列表消失，避免下一次铃铛点击只把旧面板收起。"""
    deadline = _CLOCK.monotonic() + timeout
    while _CLOCK.monotonic() < deadline:
        if pick_tool_window(ctx.windows(), ctx.main, expected_width) is None:
            return True
        _CLOCK.sleep(POLL_SEC)
    return False


def _sync_main(ctx: Ctx) -> int | None:
    row = pick_main(ctx.windows(), ctx.main)
    if row is None:
        return None
    ctx.main = dict(row)
    return int(row["Height"])


def wait_menu_trace(ctx: Ctx, timeout: float = ACTION_WAIT_SEC) -> dict:
    """等产品写下"菜单收起"那一行再判。

    留痕是菜单自己收起来时才写的，读一次就下结论会把"还没写"误判成"没点到"
    （2026-10-24 试运行就是这么红的：点完立刻读，closes=0）。
    """
    deadline = _CLOCK.monotonic() + timeout
    trace = judge_menu_pick(ctx.log())
    while not trace["closes"] and _CLOCK.monotonic() < deadline:
        _CLOCK.sleep(POLL_SEC)
        trace = judge_menu_pick(ctx.log())
    return trace


def action_menu(ctx: Ctx) -> dict:
    """真右键 → 菜单出现（新窗口 + 取色 + 量第一行文字）→ 真点一行 → 读 picked 留痕。"""
    ctx.refresh_main()
    known = {_sig(r) for r in ctx.windows()}
    rect = ctx.main
    point = {"x": rect["X"] + rect["Width"] // 2, "y": rect["Y"] + rect["Height"] - 30}
    hid_click(point["x"], point["y"], right=True)
    menu = wait_rows(ctx.pid, lambda rows: pick_fresh_window(rows, known, LAYOUT.W),
                     LAUNCH_TIMEOUT_SEC)
    if menu is None:
        return {"status": "fail",
                "evidence": {"reason": "menu_window_not_on_screen", "click": point}}
    colour = judge_pixel_is_menu(capture_pixel(
        {"x": menu["X"] + menu["Width"] // 2, "y": menu["Y"] + menu["Height"] // 2}, ctx.shot))
    strip = capture_region(menu, ctx.shot.with_name("menu.png"))
    center_pt = first_text_band_center(strip, menu)
    if center_pt is None:
        hid_key(KVK["escape"], ctx.pid)
        return {"status": "fail", "evidence": {"reason": "menu_first_row_not_found",
                                               "menu_bounds": menu, "pixel": colour}}
    row = menu_first_row_point(menu, center_pt)
    # 慢档：QMenu 要先拿到系统鼠标抓取，快档那一下只会把菜单收掉（实测 picked=none）
    hid_click(row["x"], row["y"], settle_sec=MENU_CLICK_SETTLE_SEC,
              hold_sec=MENU_CLICK_HOLD_SEC)
    trace = wait_menu_trace(ctx)
    method = "click"
    if not trace["ok"] and not trace["closes"]:
        # 菜单一直挂着 = 鼠标那一下根本没被接住；键盘选择是同一功能的另一条真实路线。
        # 但菜单已经"picked=none"收起来时**不许**偷偷改用键盘救场 —— 那是坐标偏了，
        # 必须判红并把实际落点留在证据里，改的是取点函数。
        hid_key(KVK["down"], ctx.pid)
        _CLOCK.sleep(0.15)
        hid_key(KVK["return"], ctx.pid)
        retry = wait_menu_trace(ctx)
        if retry["ok"]:
            trace = retry
            method = "keyboard"
    still_open = bool(pick_fresh_window(ctx.windows(), known, LAYOUT.W))
    if still_open:
        hid_key(KVK["escape"], ctx.pid)          # 只补给自己走查的进程，别把 Esc 送给用户的前台程序
        wait_rows(ctx.pid, lambda rows: {} if not pick_fresh_window(rows, known, LAYOUT.W)
                  else None, ACTION_WAIT_SEC)
    return {"status": "pass" if colour["ok"] and trace["ok"] else "fail",
            "evidence": {"menu_bounds": menu, "pixel": colour, "first_row_center": center_pt,
                         "clicked_row": row, "method": method, "trace": trace}}


def action_drag(ctx: Ctx) -> dict:
    """顶部空白按住拖动：窗口原点必须跟着手势走。"""
    ctx.refresh_main()
    before = dict(ctx.main)
    point = idle_point(before)
    dx, dy = 60, 40
    hid_drag(point["x"], point["y"], dx, dy)
    after = wait_rows(ctx.pid, lambda rows: _moved_row(rows, before, dx, dy), ACTION_WAIT_SEC)
    # 没搬到位就把 None 交给判据 —— 它报 "找不到窗"，而不是编一个 (0,0) 位移出来
    verdict = judge_displacement(before, after, dx, dy)
    if after:
        ctx.main = dict(after)
    return {"status": "pass" if verdict["ok"] else "fail",
            "evidence": {**verdict, "point": point}}


def _moved_row(rows: list[dict], before: dict, dx: int, dy: int) -> dict | None:
    """找"已经搬到预期位置"的那一帧：按尺寸认主窗，再看离目标点多近。"""
    target = {"X": int(before["X"]) + dx, "Y": int(before["Y"]) + dy,
              "Width": int(before["Width"]), "Height": int(before["Height"])}
    row = pick_main(rows, target)
    if row and abs(int(row["X"]) - target["X"]) <= MOVE_TOLERANCE_PX \
            and abs(int(row["Y"]) - target["Y"]) <= MOVE_TOLERANCE_PX:
        return row
    return None


def action_chart(ctx: Ctx) -> dict:
    """点分时图热区：先收起，再点回来。两次都只用窗口高度这个可观测事实。"""
    rect = ctx.refresh_main()
    h0 = int(rect["Height"])
    if h0 < LAYOUT.MIN_H:
        return {"status": "skip", "evidence": {"reason": "already_collapsed", "height": h0}}
    point = chart_point(rect)
    hid_click(point["x"], point["y"])
    collapsed = settled_height(ctx)
    first = judge_height(h0, collapsed, "smaller")
    # 第二拍的"变更前"必须在这一拍之前抄下来：settled_height 会顺手把 ctx.main 刷新，
    # 事后再读就等于拿结果比结果（delta 恒 0，用例实测抓到过）。
    before_expand = int(ctx.main["Height"])
    point2 = chart_point(ctx.main)
    hid_click(point2["x"], point2["y"])
    back = settled_height(ctx)
    second = judge_height(before_expand, back, "bigger")
    return {"status": "pass" if first["ok"] and second["ok"] else "fail",
            "evidence": {"collapse": first, "expand": second,
                         "points": [point, point2], "h0": h0}}




def action_stretch(ctx: Ctx) -> dict:
    """展开态下沿拉伸：高度必须变。收起态的负向对照由回归里的 test_ui 负责。"""
    rect = ctx.refresh_main()
    # 展开稳态实测 217、MIN_H=218、收起 146：用 MIN_H 当阈值会把“已展开”误判成收起。
    if int(rect["Height"]) < int(LAYOUT.collapsed_height()) + 20:
        return {"status": "skip", "evidence": {"reason": "needs_expanded",
                                               "height": int(rect["Height"]),
                                               "collapsed": int(LAYOUT.collapsed_height())}}
    point = stretch_point(rect)
    hid_drag(point["x"], point["y"], 0, 40)
    after = wait_rows(ctx.pid, lambda rows: _height_above(rows, int(rect["Height"])), ACTION_WAIT_SEC)
    ctx.refresh_main()
    verdict = judge_height(int(rect["Height"]), after, "bigger")
    return {"status": "pass" if verdict["ok"] else "fail",
            "evidence": {"point": point, "verdict": verdict}}


def _height_above(rows: list[dict], h0: int) -> int | None:
    heights = [int(r["Height"]) for r in rows
               if abs(int(r["Width"]) - LAYOUT.W) <= SIZE_TOLERANCE_PX]
    grew = [h for h in heights if h >= h0 + STRETCH_MIN_DELTA_PX]
    return max(grew) if grew else None


def action_stock(ctx: Ctx) -> dict:
    """真点主窗编辑按钮 → 真点输入框 → 敲 6 位数字 → **真点确定** → config 的 code 变了。"""
    ctx.refresh_main()
    point = top_button_point(ctx.main, "edit")
    hid_click(point["x"], point["y"])
    # 编辑窗的宽度**刻意与主窗一致**（sf_editor._build 尾部那条），所以按"新签名"找会永远
    # 找不到它 —— 必须按位置区分：与主窗不在同一处的同宽窗口才是它。
    editor, painted = settle_popup(ctx, LAYOUT.W, ctx.shot.with_name("editor.png"))
    if editor is None:
        return {"status": "fail", "evidence": {"reason": "editor_window_not_on_screen",
                                               "click": point, "rows": ctx.windows()}}
    if not painted:
        hid_key(KVK["escape"], ctx.pid)
        return {"status": "fail", "evidence": {"reason": "editor_not_painted",
                                               "editor_bounds": editor}}
    entry = editor_entry_point(editor)
    confirm = editor_confirm_point(editor)
    hid_click(entry["x"], entry["y"])
    # 输入框**预填着当前代码**（实测点一下之后光标停在末尾，直接敲会变成
    # sh600519000001 → 搜索失败、窗也不关）。真人换股票同样是"全选→输入"，
    # 所以这里先 Cmd+A 再敲六位，而不是把清空当成产品的缺陷。
    hid_select_all(ctx.pid)
    hid_type(STOCK_CODE_DIGITS, ctx.pid)
    typed_shot = capture_region(editor, ctx.shot.with_name("editor-typed.png"))
    # Return 与 QPushButton.clicked 是两条独立接线；用户要求的是“输入代码 + 点确定”，
    # 所以不能拿 Return 代替。点后仍以窗口关闭 + 临时 config 实际 code 双重反证。
    hid_click(confirm["x"], confirm["y"])
    gone = wait_rows(ctx.pid, lambda rows: {"closed": True}
                     if not pick_tool_window(rows, ctx.main, LAYOUT.W) else None,
                     CLOSE_WAIT_SEC)
    saved = force_save_by_resize(ctx)
    verdict = judge_code_written(ctx.config(), STOCK_CODE_DIGITS)
    verdict["editor_closed_seen"] = bool(gone)
    verdict["save_triggered"] = saved
    return {"status": "pass" if verdict["ok"] and verdict["editor_closed_seen"] else "fail",
            "evidence": {"editor_bounds": editor, "entry_point": entry,
                         "confirm_point": confirm,
                         "typed_shot": str(typed_shot) if typed_shot else None,
                         "config": verdict}}


def force_save_by_resize(ctx: Ctx) -> dict:
    """用一次真实的下沿拉伸触发产品的 save_config（换股本身不落盘）。

    产品只在三处写 config：拉伸松手、关窗、退出。SIGTERM 不走 Qt 收尾，所以不能靠
    “结束进程再读盘”。这里拖下沿 10px，松手即落盘；高度变化本身也进证据。
    """
    rect = ctx.refresh_main()
    if int(rect["Height"]) < int(LAYOUT.collapsed_height()) + 20:
        return {"ok": False, "reason": "needs_expanded"}
    point = stretch_point(rect)
    hid_drag(point["x"], point["y"], 0, 14)
    after = wait_rows(ctx.pid, lambda rows: _height_above(rows, int(rect["Height"])),
                      ACTION_WAIT_SEC)
    ctx.refresh_main()
    return {"ok": bool(after), "before": int(rect["Height"]), "after": after}


def action_target(ctx: Ctx) -> dict:
    """铃铛开面板 → 点输入框 → 敲价 → 点加号 → 点「立即生效」→ 读临时 config → 再开面板清空两下。"""
    ctx.refresh_main()
    bell = top_button_point(ctx.main, "bell")
    hid_click(bell["x"], bell["y"])
    # 面板宽度 206、主窗 184：靠宽度就能区分，但仍按"不在主窗那一处"再排除一次玻璃层
    panel, painted = settle_popup(ctx, TP.PANEL_W, ctx.shot.with_name("panel.png"))
    if panel is None:
        return {"status": "fail", "evidence": {"reason": "target_panel_not_on_screen",
                                               "click": bell}}
    if not painted:
        hid_key(KVK["escape"], ctx.pid)
        return {"status": "fail", "evidence": {"reason": "target_panel_not_painted",
                                               "panel_bounds": panel}}
    pts = panel_points(panel)
    hid_click(pts["input"]["x"], pts["input"]["y"])
    hid_type(ctx.target_price, ctx.pid)
    # 敲完先留一张图：这一条失败时唯一能回答"字到底进框了没有"的证据
    typed_shot = capture_region(panel, ctx.shot.with_name("panel-typed.png"))
    hid_click(pts["plus"]["x"], pts["plus"]["y"])
    # _add 成功会插入一条完整列表行，面板从 193pt 重排到约 244pt；底部「立即生效」
    # 也会随之下移。还按旧面板坐标点，只会打在新增行/状态区，永远没有落盘。
    panel_after, after_painted = settle_popup(ctx, TP.PANEL_W,
                                              ctx.shot.with_name("panel-after-add.png"))
    if panel_after is None or not after_painted:
        hid_key(KVK["escape"], ctx.pid)
        return {"status": "fail", "evidence": {"reason": "target_panel_not_repainted_after_add",
                                                   "panel_bounds": panel,
                                                   "typed_shot": str(typed_shot) if typed_shot else None}}
    pts_after = panel_points(panel_after)
    hid_click(pts_after["apply"]["x"], pts_after["apply"]["y"])
    saved_config = wait_config(ctx, lambda config: judge_alert_written(
        config, config.get("code", ""))["ok"])
    saved = judge_alert_written(saved_config, saved_config.get("code", ""))
    # 保存成功即关闭：必须等旧面板消失，不能拿下一下铃铛把它当"重新打开"又收起。
    applied_closed = wait_popup_gone(ctx, TP.PANEL_W)
    if not applied_closed:
        return {"status": "fail", "evidence": {"reason": "target_panel_did_not_close_after_apply",
                                                   "panel_bounds": panel_after,
                                                   "typed_shot": str(typed_shot) if typed_shot else None,
                                                   "saved": saved}}
    # 保存成功即关面板（target_panel._apply 尾部），所以清空要重新开一次
    hid_click(bell["x"], bell["y"])
    panel2, painted_again = settle_popup(ctx, TP.PANEL_W, ctx.shot.with_name("panel2.png"))
    if panel2 is None or not painted_again:
        hid_key(KVK["escape"], ctx.pid)
        return {"status": "fail", "evidence": {
            "reason": "target_panel_not_repainted_for_clear",
            "panel_bounds": panel_after,
            "typed_shot": str(typed_shot) if typed_shot else None,
            "saved": saved,
        }}
    pts2 = panel_points(panel2)
    hid_click(pts2["clear"]["x"], pts2["clear"]["y"])       # 第一下：进「确认清空」
    hid_click(pts2["clear"]["x"], pts2["clear"]["y"])       # 第二下：真清空
    cleared_config = wait_config(ctx, lambda config: judge_alert_cleared(
        config, config.get("code", ""))["ok"])
    cleared = judge_alert_cleared(cleared_config, cleared_config.get("code", ""))
    hid_key(KVK["escape"], ctx.pid)                           # 面板留在屏幕上会挡住下一次走查
    return {"status": "pass" if saved["ok"] and cleared["ok"] else "fail",
            "evidence": {"panel_bounds": panel, "points": pts, "points2": pts2,
                         "typed_shot": str(typed_shot) if typed_shot else None,
                         "saved": saved, "cleared": cleared}}


ACTIONS: list[tuple[str, str]] = [
    ("menu", "action_menu"),
    ("drag", "action_drag"),
    ("chart", "action_chart"),
    ("stretch", "action_stretch"),
    # 目标价必须排在换股之前：编辑窗仍未关时，产品自己会把铃铛按成 btn blocked。
    ("target", "action_target"),
    # 换股只改内存，产品在“拉伸松手/退出”时才写 config，所以它必须最后跑，并在
    # 判定前用一次真实拉伸触发落盘。
    ("stock", "action_stock"),
]
ACTION_NAMES = [name for name, _ in ACTIONS]
_TABLE = dict(ACTIONS)


# ============================ 编排 ============================
def run_walkthrough(app: pathlib.Path, out_dir: pathlib.Path, names: list[str]) -> dict:  # pragma: no cover - 真起产物并发事件
    """真跑一轮。调用前必须已经过了 preflight。"""
    # 子进程 socket 路径有 104 字节上限：证据目录在主树 build/ 下太长，家目录必须放短路径。
    home = pathlib.Path(tempfile.mkdtemp(prefix=HOME_PREFIX))
    before_user = user_data_stat()
    guard = network_guard_state(app)              # 只取一次：样本选择与证据里记的是同一份
    target_price = pick_target_price(guard["present"])
    proc = launch_frozen(app, home, seed_config())
    results: list[dict] = []
    startup: dict = {}
    ctx: Ctx | None = None
    try:
        if proc.poll() is not None:
            results.append({"name": "launch", "status": "fail",
                            "evidence": {"exited_early": proc.poll()}})
        else:
            startup = wait_startup(home, proc)
            if not startup["ok"]:
                results.append({"name": "launch", "status": "fail",
                                "evidence": {"reason": "startup_marker_missing_or_uncaught",
                                             "startup": startup}})
            else:
                first = wait_rows(proc.pid,
                                  lambda rows: pick_main(rows, {"X": SEED_X, "Y": SEED_Y,
                                                                "Width": LAYOUT.W,
                                                                "Height": LAYOUT.MIN_H}),
                                  LAUNCH_TIMEOUT_SEC)
                if first is None:
                    results.append({"name": "launch", "status": "fail",
                                    "evidence": {"reason": "main_window_never_appeared",
                                                 "startup": startup}})
                else:
                    ctx = Ctx(proc.pid, home, out_dir / "pixel.png", first, target_price)
                    acted = False
                    for name in names:
                        acted = True
                        started = _CLOCK.monotonic()
                        try:
                            result = getattr(sys.modules[__name__], _TABLE[name])(ctx)
                        except Exception as exc:    # 单条炸掉不带走整轮，但必须留下名字与原因
                            result = {"status": "fail", "evidence": {"error": repr(exc)}}
                        result["name"] = name
                        result["seconds"] = round(_CLOCK.monotonic() - started, 2)
                        results.append(result)
                        print("  [%-4s] %s｜%.1fs" % (result["status"], name, result["seconds"]))
                    final_startup = judge_startup(SMOKE.scan_markers(read_log(home)))
                    if acted and not final_startup["ok"]:
                        results.append({"name": "runtime", "status": "fail",
                                        "evidence": {"reason": "uncaught_or_startup_anchor_lost",
                                                     "startup": final_startup}})
    finally:
        _terminate(proc)
    summary = summarize(results)
    evidence = {"app": str(app), "version": bundle_version(app),
                "network_guard": guard, "target_price_sample": target_price,
                "pid": proc.pid, "startup": startup,
                "results": results,
                "summary": summary,
                "user_data": judge_user_data_untouched(before_user, user_data_stat()),
                "home": str(home), "main_final": (ctx.main if ctx else {})}
    copy_runtime_evidence(home, out_dir)
    (out_dir / "result.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return evidence


def copy_runtime_evidence(home: pathlib.Path, out_dir: pathlib.Path) -> None:
    """把临时家目录里的日志与 config 抄进证据目录，然后删掉临时目录，不留垃圾。"""
    kept = out_dir / "runtime"
    try:
        kept.mkdir(parents=True, exist_ok=True)
        for name in ("debug.log", "config.json"):
            source = home / name
            if source.exists():
                (kept / name).write_bytes(source.read_bytes())
    except Exception:
        pass
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _terminate(proc: subprocess.Popen) -> None:  # pragma: no cover - 只收尾自己拉的进程
    """只终止本工具自己拉起的 pid：先 SIGTERM 让它自己落盘退出，等不到再 SIGKILL。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    deadline = _CLOCK.monotonic() + 10
    while _CLOCK.monotonic() < deadline and proc.poll() is None:
        _CLOCK.sleep(0.1)
    if proc.poll() is None:
        proc.kill()


# ============================ 自检（不碰屏幕、不发事件）============================
def self_test() -> int:
    """种错核验：每条判据都要"改坏必红"，包括判据自己。返回 0 才算这道闸会红。"""
    problems: list[str] = []

    def check(label: str, ok: bool) -> None:
        if not ok:
            problems.append(label)

    remembered = {"X": 100, "Y": 100, "Width": LAYOUT.W, "Height": 260}
    main_row = {"X": 100, "Y": 100, "Width": LAYOUT.W, "Height": 260}
    glass_row = {"X": 100, "Y": 100, "Width": LAYOUT.W, "Height": 262}
    other_row = {"X": 300, "Y": 100, "Width": 999, "Height": 9}
    check("按宽度认主窗：宽度不符的不能算", pick_main([other_row], remembered) is None)
    check("按宽度认主窗：应当认出来", pick_main([other_row, main_row], remembered) is not None)
    moved_back = pick_main([{"X": 160, "Y": 140, "Width": LAYOUT.W, "Height": 260}],
                           {"X": 158, "Y": 138, "Width": LAYOUT.W, "Height": 260})
    check("主窗移动后按最近位置认回", moved_back is not None and moved_back["X"] == 160)
    layer_picked = pick_main([glass_row, main_row], remembered)
    check("同宽两只（主窗+玻璃层）取高度更接近记住值的一层",
          layer_picked is not None and layer_picked["Height"] == 260)
    editor_rival = pick_main([{"X": 340, "Y": 100, "Width": LAYOUT.W, "Height": 300}, main_row],
                             remembered)
    check("替换股票窗与主窗同宽：认主窗仍按位置最近取",
          editor_rival is not None and editor_rival["X"] == 100)
    far_layer = pick_main([{"X": 100, "Y": 100, "Width": LAYOUT.W, "Height": 900}, glass_row],
                          remembered)
    check("同坐标两只：高度取更接近记住值的那一层",
          far_layer is not None and far_layer["Height"] == 262)
    check("工具窗必须排除主窗所在位置",
          pick_tool_window([main_row], main_row, LAYOUT.W) is None)
    check("工具窗按位置区分得开",
          pick_tool_window([main_row, {"X": 340, "Y": 100, "Width": LAYOUT.W, "Height": 300}],
                           main_row, LAYOUT.W) is not None)
    check("新窗口判据：签名没变过就不算新", pick_fresh_window([main_row],
                                                            {_sig(main_row)}, LAYOUT.W) is None)
    check("新窗口判据：新出现的窄窗口才算菜单", pick_fresh_window(
        [main_row, {"X": 90, "Y": 200, "Width": 152, "Height": 173}],
        {_sig(main_row)}, LAYOUT.W) is not None)

    check("位移 60/40 应判通过",
          judge_displacement({"X": 100, "Y": 100}, {"X": 160, "Y": 140}, 60, 40)["ok"])
    check("窗口没动却判通过 —— 拖动判据恒真",
          not judge_displacement({"X": 100, "Y": 100}, {"X": 100, "Y": 100}, 60, 40)["ok"])
    check("找不到窗要判红不是异常",
          judge_displacement({"X": 0, "Y": 0}, None, 10, 10)["ok"] is False)
    check("收起应判通过", judge_height(260, 150, "smaller")["ok"])
    check("只降 2px 也算收起 —— 阈值没牙", not judge_height(260, 258, "smaller")["ok"])
    check("展开应判通过", judge_height(150, 260, "bigger")["ok"])
    check("被拉高了还判「高度没变」—— 负向对照失效",
          not judge_height(260, 300, "same")["ok"])
    check("高度读不到要判红", judge_height(260, None, "bigger")["ok"] is False)
    try:
        judge_height(1, 2, "sideways")
        problems.append("未知高度口径必须抛错")
    except ValueError:
        pass

    own = {"code": "sz000001", "alerts": [{"target": 101.0,
                                             "direction": TP.TARGET_DIR_ABOVE,
                                             "code": "sz000001"}]}
    check("已写入的涨到目标价应判通过", judge_alert_written(own, "sz000001")["ok"])
    check("别的股票条目不算数",
          not judge_alert_written({"code": "sz000001",
                                   "alerts": [{"target": 101.0, "direction": TP.TARGET_DIR_ABOVE,
                                               "code": "sh600519"}]}, "sz000001")["ok"])
    check("跌到条目不许冒充涨到已写入",
          not judge_alert_written({"alerts": [{"target": 99.0, "direction": "below",
                                                "code": "sz000001"}]}, "sz000001")["ok"])
    check("没写入却判通过 —— 落盘判据恒真",
          not judge_alert_written({"alerts": []}, "sz000001")["ok"])
    check("还留着条目却判「已清空」", not judge_alert_cleared(own, "sz000001")["ok"])
    check("清空后应判通过", judge_alert_cleared({"alerts": []}, "sz000001")["ok"])
    check("带前缀的代码应判通过", judge_code_written({"code": "sz000001"}, "000001")["ok"])
    check("换股没落盘却判通过", not judge_code_written({"code": "sh600519"}, "000001")["ok"])
    check("config 读空时不能算已换股", not judge_code_written({}, "000001")["ok"])

    trace = judge_menu_log("[10:00:00] INFO: ctx menu opened, app_state=active\n"
                           "[10:00:01] INFO: ctx menu closed after 270ms, "
                           "picked=target, reason=picked\n")
    check("菜单留痕应解析出 picked=target", trace["ok"] and trace["picked"] == "target")
    check("点中的不是期望那一行必须判红", not judge_menu_pick(
        "[a] ctx menu opened\n[a] ctx menu closed, picked=feedback, reason=picked")["ok"])
    check("点空处（picked=none）必须判红", not judge_menu_pick(
        "[a] ctx menu opened\n[a] ctx menu closed, picked=none, reason=outside_click")["ok"])
    check("点中期望那一行必须判通过", judge_menu_pick(
        "[a] ctx menu opened\n[a] ctx menu closed, picked=refresh, reason=picked")["ok"])
    first = menu_first_row_point({"X": 10, "Y": 20, "Width": 152, "Height": 173}, 21.4)
    check("第一行落点要横向居中、纵向用量出来的文字中心",
          first["x"] == 86 and first["y"] == 41)
    check("日志里没有菜单行却判通过", not judge_menu_log("nothing here")["ok"])
    check("只开不关不算成对", not judge_menu_log("ctx menu opened")["ok"])
    check("菜单底色应判通过", judge_pixel_is_menu(MENU_BG_RGB)["ok"])
    check("截到白底却判通过 —— 取色判据恒真", not judge_pixel_is_menu((255, 255, 255))["ok"])
    check("截不到图必须判红", judge_pixel_is_menu(None)["ok"] is False)
    check("首帧没画出来不能算起来",
          not judge_startup({"startup": ["STARTUP v1.4.104"], "first_frame": [],
                             "uncaught": []})["ok"])
    check("有 UNCAUGHT 必须判红",
          not judge_startup({"startup": ["a"], "first_frame": ["b"],
                             "uncaught": ["UNCAUGHT"]})["ok"])
    check("用户配置被改动却没报出来",
          not judge_user_data_untouched({"config.json": [10, 1]},
                                        {"config.json": [11, 1]})["ok"])
    check("用户配置没动应判通过",
          judge_user_data_untouched({"config.json": [10, 1]}, {"config.json": [10, 1]})["ok"])

    ok_screen = {"locked": False, "display_asleep": False}
    here = ROOT / "sf_constants.py"
    check("产物不存在必须拦住", bool(preflight_reasons(pathlib.Path("/definitely/not/here"),
                                                    ok_screen, True, True, ["menu"])))
    check("屏幕锁定必须拦住", bool(preflight_reasons(here, {"locked": True,
                                                       "display_asleep": False},
                                                      True, True, ["menu"])))
    check("屏幕状态读不出（None）也当不成立", bool(preflight_reasons(
        here, {"locked": None, "display_asleep": None}, True, True, ["menu"])))
    check("没有辅助功能授权必须拦住", bool(preflight_reasons(here, ok_screen, False,
                                                          True, ["menu"])))
    check("授权读不出来（None）必须拦住", bool(preflight_reasons(here, ok_screen, None,
                                                             True, ["menu"])))
    check("没允许占屏必须拦住", bool(preflight_reasons(here, ok_screen, True, False, ["menu"])))
    check("一条动作都没选必须拦住", bool(preflight_reasons(here, ok_screen, True, True, [])))
    check("前提齐备时不该编出阻塞项",
          preflight_reasons(here, ok_screen, True, True, ["menu"]) == [])

    check("全部 skip 时应给 not_evaluated",
          summarize([{"status": "skip"}])["not_evaluated"] is True)
    check("一条没跑成不能退 0", overall_exit(summarize([{"status": "skip"}]), []) == EXIT_RED)
    check("前提不成立必须退 2",
          overall_exit(summarize([{"status": "pass"}]), ["blocked"]) == EXIT_PRECONDITION)
    check("有用例判红必须退 1",
          overall_exit(summarize([{"status": "pass"}, {"status": "fail"}]), []) == EXIT_RED)
    check("全过才退 0", overall_exit(summarize([{"status": "pass"}]), []) == EXIT_OK)

    check("菜单首行等分点算错",
          menu_row_point({"X": 0, "Y": 0, "Width": 100, "Height": 200}, 0, 4)["y"] == 25)
    try:
        menu_row_point({"X": 0, "Y": 0, "Width": 1, "Height": 1}, 0, 0)
        problems.append("行数为 0 必须抛错")
    except ValueError:
        pass
    pts = panel_points({"X": 0, "Y": 0, "Width": TP.PANEL_W, "Height": 220})
    check("「立即生效」在右、「清空」在左 —— 两者算反了", pts["apply"]["x"] > pts["clear"]["x"])
    check("同排两枚按钮纵向坐标必须一致", abs(pts["apply"]["y"] - pts["clear"]["y"]) < 1e-6)
    # 加号中心按**真截图量出来的带**判，不按公式判：2026-10-24 试运行里
    # "在输入框右边、没越过右边界"这两条都成立，落点却仍在按钮左边的空白 stretch 上
    # （实测加号墨迹中心 171~177pt / 面板宽 206pt），于是面板开着、点发了、一条没加进去。
    check("加号中心必须落在实测的按钮带内(165~185pt)",
          PANEL_PLUS_BAND[0] <= pts["plus"]["x"] <= PANEL_PLUS_BAND[1])
    check("加号不许退回到输入框里", pts["plus"]["x"] > TP.MARGIN_X + TP.DIRECTION_W
          + TP.ROW_SPACING + TP.TARGET_INPUT_W)
    check("输入行必须在底排之上", pts["input"]["y"] < pts["apply"]["y"])
    check("主窗顶部按钮坐标要在窗内",
          0 < top_button_point({"X": 0, "Y": 0, "Width": LAYOUT.W, "Height": 260},
                              "bell")["x"] < LAYOUT.W)
    check("分时图热区点在展开态要低于收起态起点",
          chart_point({"X": 0, "Y": 0, "Width": LAYOUT.W, "Height": 260})["y"] > LAYOUT.CHART_TOP)
    check("收起态取点走 Y_PRICE 一档",
          chart_point({"X": 0, "Y": 0, "Width": LAYOUT.W, "Height": 120})["y"]
          >= LAYOUT.Y_PRICE)
    check("下沿把手要在窗口底部 8px 内",
          stretch_point({"X": 0, "Y": 0, "Width": LAYOUT.W, "Height": 260})["y"] >= 252)

    check("走查清单里每个名字都能找到实现",
          all(hasattr(sys.modules[__name__], fn) for _, fn in ACTIONS))
    check("动作清单不许重名", len(set(ACTION_NAMES)) == len(ACTION_NAMES))

    # 网络守卫披露：有开关/没开关/读不出三态必须各自说对，且“读不出”不许被猜成有。
    check("包内带守卫字面量必须判 present=True",
          judge_network_guard({"x", NETWORK_GUARD_ENV})["present"] is True)
    check("包内没有守卫字面量必须判 present=False（旧版本）",
          judge_network_guard({"x", "y"})["present"] is False)
    check("读不出包内网络入口必须是 None，不许猜成有或没有",
          judge_network_guard(None)["present"] is None)
    check("没有守卫时说明必须点名旧版本会真实出网",
          "真实发出" in judge_network_guard(set())["note"])
    check("字符串常量收集必须钻进嵌套函数",
          NETWORK_GUARD_ENV in string_literals(
              compile("def f():\n    def g():\n        return '%s'\n" % NETWORK_GUARD_ENV,
                      "<selftest>", "exec")))

    # 目标价样本：带开关用离线价；没开关沿用 +1%；读不出不许猜成带开关（猜错会在能联网的包上敲绝对价）。
    check("包带网络开关时必须用离线整数价样本",
          pick_target_price(True) == TARGET_PRICE_OFFLINE)
    check("包没有开关时必须沿用相对涨幅样本",
          pick_target_price(False) == TARGET_PRICE)
    check("读不出开关状态时必须沿用相对涨幅样本，不许猜成带开关",
          pick_target_price(None) == TARGET_PRICE)
    check("离线样本必须是纯数字且每个字符键盘映射里都有",
          TARGET_PRICE_OFFLINE.isdigit() and all(ch in KVK for ch in TARGET_PRICE_OFFLINE))
    check("离线样本必须是正数且不同于相对样本",
          int(TARGET_PRICE_OFFLINE) > 0 and TARGET_PRICE_OFFLINE != TARGET_PRICE)

    if problems:
        print("自检 %d 项判红：" % len(problems))
        for line in problems:
            print("  !! %s" % line)
        return 1
    print("自检全部通过（不发事件、不碰屏幕、不写用户数据）")
    return 0


# ============================ CLI ============================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="post_release_click_walkthrough.py",
                                 description="发布后验收：真鼠标真键盘点装机版")
    ap.add_argument("--app", default=str(DEFAULT_APP), help="装机包路径")
    ap.add_argument("--out", default="", help="证据目录（默认 build/click-<版本>-<时间>）")
    ap.add_argument("--action", dest="actions", action="append", default=[],
                    choices=ACTION_NAMES, help="只跑点名的动作，可重复")
    ap.add_argument("--report", action="store_true", help="只打印清单与前提，不发事件")
    ap.add_argument("--self-test", action="store_true", help="离线种错核验")
    ap.add_argument("--i-am-at-the-machine", action="store_true", dest="human_present",
                    help="确认此刻屏幕解锁，且允许本工具接管鼠标")
    return ap


def _screen_state() -> dict:
    try:
        import commit_preflight
        return commit_preflight.screen_state()
    except Exception:
        return {"locked": None, "display_asleep": None}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)   # 未知参数由 argparse 退 2
    if args.self_test:
        return self_test()
    names = args.actions or ACTION_NAMES
    app = pathlib.Path(args.app)
    print("装机包: %s（版本 %s）" % (app, bundle_version(app) or "读不出"))
    print("动作清单: %s" % "、".join(names))
    if app.exists():
        print("网络隔离: %s" % network_guard_state(app)["note"])
    screen = _screen_state()
    trusted = accessibility_trusted()
    reasons = preflight_reasons(app, screen, trusted, args.human_present, names,
                                FSS.installed_app_running())
    if args.report:
        print("前提: 屏幕=%s 显示器=%s 辅助功能=%s"
              % (screen.get("locked"), screen.get("display_asleep"), trusted))
        print("阻塞项: %s" % ("、".join(reasons) if reasons else "无"))
        return EXIT_OK
    if reasons:
        print("!! 前提不成立，本轮**不判通过也不判失败**：%s" % "、".join(reasons))
        print("   先解决上面每一条，再加 --i-am-at-the-machine 重跑")
        return EXIT_PRECONDITION
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (pathlib.Path(args.out) if args.out
               else ROOT / "build" / ("click-%s-%s" % (bundle_version(app) or "unknown", stamp)))
    out_dir.mkdir(parents=True, exist_ok=True)
    print("证据目录: %s（接下来约 %d 秒会接管鼠标）"
          % (out_dir, max(20, 25 * len(names))))
    evidence = run_walkthrough(app, out_dir, names)
    summary = evidence["summary"]
    print("结论: 执行 %d 条，通过 %d，判红 %d，跳过 %d"
          % (summary["ran"], summary["passed"], summary["failed"],
             summary.get("skipped", 0)))
    print("网络隔离: %s" % evidence.get("network_guard", {}).get("note", "未记录"))
    if not evidence["user_data"]["ok"]:
        print("!! 用户真实配置被改动了：%s —— 这是本工具的隔离缺陷，必须修"
              % evidence["user_data"]["changed"])
        return EXIT_RED
    print("明细见 %s" % (out_dir / "result.json"))
    return overall_exit(summary, [])


if __name__ == "__main__":  # pragma: no cover - CLI 脚本入口，测试走 main(argv)
    sys.exit(main())
