# -*- coding: utf-8 -*-
"""右键菜单与「反馈问题」入口的会话记录（诊断包 runtime.menu_trace，schema menu_trace.v1）。

起因：用户反馈「右键里的反馈问题要点好几次才弹出来」。点丢的那几下到不了任何动作入口，日志里
什么都没有；诊断包又是点「上传」那一刻才采的，写描述期间再开关菜单会把出问题的那几次挤出环。
所以这里记三类东西：
  ① 菜单会话：开了多久、程序是否在前台、按下/松开各落在哪一项（固定名字，不记行号与文案）、
     最后悬停哪一项、怎么收的；
  ② 右键对账：右键按下次数 vs 菜单打开次数（按下数多于打开数 = 有按下没等到菜单）；
  ③ 反馈开窗：排队、采集、建窗、显示、置顶各到第几毫秒，开窗后回看窗口是否真在这块桌面上。
新建反馈窗那一刻把当时的记录冻结一份（frozen_at_open）随诊断包带走。

只记枚举名、计数和时长：不记屏幕名、绝对坐标、别的程序的窗口标题。数据只在内存里，进程退出即没。
数据里不写「多少毫秒算异常」这类结论，判断留给分析器。
"""
from __future__ import annotations

import copy
import time
import weakref
from typing import Any
from collections import deque
from datetime import datetime, timezone

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from sf_glass import (POPUP_SPACE_MAX_TICKS, POPUP_SPACE_TICK_MS, content_ns_window,
                      native_cocoa_active)
from sf_logging import log_line, log_swallow

SCHEMA = "menu_trace.v1"
# 两个环各留 12 条：设计取值，不是量出来的。单条会话不到 1KB，两个环加冻结一份合计约 20KB，
# 远小于诊断包单个 JSON 的 256KB 上限（runtime.json 现约 39KB）。
RING_SIZE = 12
# 回看时刻。300ms 来自 v1.4.70 装机版实测「右键到看见菜单 0.27 秒」；1500ms 是产品自己给弹层
# 补齐到活动 Space 的上限（节拍 × 最大拍数），过了这个点还不在，就是产品自己也放弃了。
READBACK_FIRST_MS = 300
READBACK_SECOND_MS = POPUP_SPACE_MAX_TICKS * POPUP_SPACE_TICK_MS

_T: dict[str, Any] = {"sessions": deque(maxlen=RING_SIZE), "attempts": deque(maxlen=RING_SIZE), "seq": 0,
      "attempt_seq": 0, "right_presses": 0, "menus_opened": 0, "right_pending": False,
      "feedback_dropped": 0, "feedback_pending": None, "open_session": None, "frozen": None,
      "activated_at": None, "deactivated_at": None}
_TRACKER: dict[str, Any] = {"apps": []}   # 已接过的应用；同一个只接一次，否则每次清空记录再接会让处理函数叠加


def clear():
    """清空全部记录（判据与自检用；产品路径不调）。"""
    _T["sessions"].clear()
    _T["attempts"].clear()
    _T.update(seq=0, attempt_seq=0, right_presses=0, menus_opened=0, right_pending=False,
              feedback_dropped=0, feedback_pending=None, open_session=None, frozen=None,
              activated_at=None, deactivated_at=None)


def _ms_since(t0):
    return int(round((time.monotonic() - t0) * 1000))


def _age_s(t):
    return None if t is None else round(time.monotonic() - t, 1)


def _on_app_state(state):
    name = state.name
    now = time.monotonic()
    _T["activated_at" if name == "ApplicationActive" else "deactivated_at"] = now
    open_session = _T["open_session"]
    if open_session is not None and len(open_session["app_changes"]) < 8:
        open_session["app_changes"].append({"to": name, "at_ms": _ms_since(open_session["_t0"])})


def install_app_tracker(app):
    """记下程序最近一次切到前台/后台的时刻；对同一个 QApplication 只接一次。"""
    if app is None or any(seen is app for seen in _TRACKER["apps"]):
        return False
    _TRACKER["apps"].append(app)
    if app.applicationState() == Qt.ApplicationState.ApplicationActive:
        _T["activated_at"] = time.monotonic()      # 装之前就在前台：只知道不晚于此刻
    app.applicationStateChanged.connect(_on_app_state)
    return True


def note_right_press():
    """主窗收到一次右键按下。Qt 在 macOS 上先送按下、再送右键菜单事件，菜单打开时清掉待配对标记。"""
    _T["right_presses"] += 1
    _T["right_pending"] = True


def native_readback(widget):
    """窗口此刻的真实状态；绝不外抛。非 Cocoa 进程不碰 AppKit（那是不可捕获的段错误）。"""
    out: dict[str, Any] = {"visible": bool(widget.isVisible()), "active": bool(widget.isActiveWindow())}
    if not native_cocoa_active():
        out["native"] = "unavailable:not_cocoa"
        return out
    if not out["visible"]:
        out["native"] = "unavailable:not_visible"
        return out
    try:
        nsw = content_ns_window(widget)
        if nsw is None:
            out["native"] = "unavailable:no_nswindow"
            return out
        out.update(native="ok", on_active_space=bool(nsw.isOnActiveSpace()), level=int(nsw.level()),
                   behavior=int(nsw.collectionBehavior()),
                   occlusion_visible=bool(int(nsw.occlusionState()) & 2))
        screen = QGuiApplication.screenAt(widget.frameGeometry().center())
        out["on_a_screen"] = screen is not None
    except Exception as exc:
        log_swallow("sf_menu_trace.native_readback", exc)
        out["native"] = "error:%s" % type(exc).__name__
    return out


def _schedule_readbacks(record, widget, delays):
    """开窗/开菜单后按时刻回看。只留弱引用，不替窗口续命；窗口没了就记 gone。"""
    ref = weakref.ref(widget)

    def make(tag):
        def run():
            target = ref()
            if target is None or sip.isdeleted(target):
                record["readbacks"][tag] = {"gone": True}
                return
            try:
                record["readbacks"][tag] = native_readback(target)
            except Exception as exc:
                log_swallow("sf_menu_trace.readback", exc)
        return run

    for tag, delay in delays:
        QTimer.singleShot(delay, make(tag))


def key_of(action):
    """菜单项的固定名字（创建时用 setData 打的标）；没标的记 untagged，分隔线记 separator。"""
    if action is None:
        return "none"
    if action.isSeparator():
        return "separator"
    return str(action.data() or "untagged")


def action_key_at(menu, pos):
    """pos 是菜单本地坐标。菜单外记 outside。"""
    if not menu.rect().contains(pos):
        return "outside"
    return key_of(menu.actionAt(pos))


class _MenuFilter(QObject):
    """只观察、不拦截：永远返回 False，菜单行为不受影响。"""

    def __init__(self, session):
        super().__init__()
        self.s = session

    def eventFilter(self, obj, event):
        kind = event.type()
        s = self.s
        if kind == QEvent.Type.MouseButtonPress:
            s["presses"] += 1
            if s["first_press"] is None:
                s["first_press"] = action_key_at(obj, event.position().toPoint())
        elif kind == QEvent.Type.MouseButtonRelease:
            s["releases"] += 1
            s["last_release"] = action_key_at(obj, event.position().toPoint())
        elif kind == QEvent.Type.KeyPress:
            s["keys"] += 1
            s["escape"] = s["escape"] or event.key() == Qt.Key.Key_Escape
        return False


def _close_reason(s):
    if s["picked"] != "none":
        return "picked"
    if s["escape"]:
        return "escape"
    # 只认程序前台状态真的变成非活动。菜单窗自己的「失去激活」事件每次收起都会来（选中、Esc 也一样，
    # 2026-10-23 离屏实测四种收法全部触发），不带信息，不能当失焦证据。
    if any(c["to"] != "ApplicationActive" for c in s["app_changes"]):
        return "app_deactivated"
    if s["first_press"] == "outside":
        return "outside_click"
    # 鼠标停在某一项上（hover 有名字），但过滤器一次按下、一次按键都没收到，程序前台也没变：
    # 与「真的没有任何线索」分开记，方便区分「按下根本没送到菜单」和「什么都没发生」。
    if s["hover"] != "none" and s["presses"] == 0 and s["keys"] == 0 and s["first_press"] is None:
        return "hover_no_press"
    return "unknown"


def _finalize(session, reason=None):
    if session["closed_after_ms"] is not None:
        return
    elapsed = _ms_since(session["_t0"])
    session["closed_after_ms"] = elapsed
    session["reason"] = reason or _close_reason(session)
    if _T["open_session"] is session:
        _T["open_session"] = None
    log_line("ctx menu closed after %dms, picked=%s, reason=%s"
             % (elapsed, session["picked"], session["reason"]))


def begin_menu(menu, context=None):
    """右键菜单刚建好、popup 之前调用；绝不外抛。返回会话编号（出错返回 None）。

    context 是可选的零参函数，返回宿主才知道的少量现场（例如主窗是否在全屏桌面）。"""
    try:
        s = _T["open_session"]
        if s is not None:
            _finalize(s, "replaced")
        _T["seq"] += 1
        _T["menus_opened"] += 1
        matched, _T["right_pending"] = _T["right_pending"], False
        install_app_tracker(QApplication.instance())
        extra = None
        if context is not None:
            try:
                extra = context()
            except Exception as exc:
                log_swallow("sf_menu_trace.context", exc)
                extra = {"error": type(exc).__name__}
        session: dict[str, Any] = {"id": _T["seq"], "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "_t0": time.monotonic(), "after_right_press": matched,
                   "app_state": QApplication.applicationState().name,
                   "since_activated_s": _age_s(_T["activated_at"]),
                   "since_deactivated_s": _age_s(_T["deactivated_at"]), "context": extra,
                   "first_turn_ms": None, "presses": 0, "releases": 0, "keys": 0,
                   "first_press": None, "last_release": None, "hover": "none", "escape": False,
                   "app_changes": [], "picked": "none",
                   "closed_after_ms": None, "reason": None, "readbacks": {}}
        _T["sessions"].append(session)
        _T["open_session"] = session
        menu.hovered.connect(lambda act: session.__setitem__("hover", key_of(act)))
        menu.triggered.connect(lambda act: session.__setitem__("picked", key_of(act)))
        # 「选中」与「收起」两个信号谁先谁后由 Qt 内部决定，收起时延后一拍再结算。
        menu.aboutToHide.connect(lambda: QTimer.singleShot(0, lambda: _finalize(session)))
        watcher = _MenuFilter(session)
        watcher.setParent(menu)
        menu.installEventFilter(watcher)
        QTimer.singleShot(0, lambda: session.__setitem__("first_turn_ms", _ms_since(session["_t0"])))
        _schedule_readbacks(session, menu, [("t300", READBACK_FIRST_MS)])
        log_line("ctx menu opened, app_state=%s" % session["app_state"])
        return session["id"]
    except Exception as exc:
        log_swallow("sf_menu_trace.begin_menu", exc)
        return None


def open_session_id():
    """此刻还没结算的菜单会话编号（点中菜单项的那一拍，会话还开着）；没有就是 None。"""
    session = _T["open_session"]
    return None if session is None else session["id"]


def feedback_request(session_id=None, dropped=False):
    """有人要开反馈窗（排队那一刻）。上一次还没处理完时再来的会被丢掉，这里记一笔。"""
    if dropped:
        _T["feedback_dropped"] += 1
        return None
    _T["attempt_seq"] += 1
    attempt: dict[str, Any] = {"id": _T["attempt_seq"], "session_id": session_id, "_t0": time.monotonic(),
               "marks": {}, "outcome": None, "error": None, "reused": None, "readbacks": {}}
    _T["attempts"].append(attempt)
    _T["feedback_pending"] = attempt
    return attempt


def feedback_begin():
    """_open_feedback 开始执行；没有排队记录（直接调用）也补一条，保证每次开窗都有迹可查。"""
    attempt = _T["feedback_pending"] or feedback_request()
    _T["feedback_pending"] = None
    attempt["marks"]["queued_ms"] = _ms_since(attempt["_t0"])
    return attempt


def feedback_mark(attempt, name):
    attempt["marks"][name] = _ms_since(attempt["_t0"])


def feedback_finish(attempt, outcome, dialog=None, reused=None, error=None):
    """outcome：opened / capture_failed。新建的窗在这里冻结当时的记录，并安排开窗后回看。"""
    attempt.update(outcome=outcome, reused=reused, error=error)
    feedback_mark(attempt, "total_ms")
    if dialog is None:
        return
    _schedule_readbacks(attempt, dialog, [("t300", READBACK_FIRST_MS), ("t1500", READBACK_SECOND_MS)])
    if not reused:
        _T["frozen"] = {"at_attempt": attempt["id"], "right_presses": _T["right_presses"],
                        "menus_opened": _T["menus_opened"],
                        "sessions": [_export(s) for s in _T["sessions"]]}


def _export(record):
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_")})


def diagnostics():
    """runtime.menu_trace 的内容；值全是 JSON 安全的原生类型。"""
    return {"schema": SCHEMA, "available": True, "ring_size": RING_SIZE,
            "right_presses": _T["right_presses"], "menus_opened": _T["menus_opened"],
            "right_press_pending_now": _T["right_pending"], "feedback_dropped": _T["feedback_dropped"],
            "app_active_age_s": _age_s(_T["activated_at"]),
            "app_inactive_age_s": _age_s(_T["deactivated_at"]),
            "sessions": [_export(s) for s in _T["sessions"]],
            "feedback_attempts": [_export(a) for a in _T["attempts"]],
            "frozen_at_open": copy.deepcopy(_T["frozen"])}
