# -*- coding: utf-8 -*-
"""「目标价」功能：macOS 通知模块（v6 §5.2 / Step 4）。

投递主路线：UserNotifications 框架（横幅 + 点击回调恢复窗口）。
降级路线：osascript display notification（无点击回调，已知损失）。
授权状态：每次触发前查询 authorizationStatus；denied 则只走彩虹、不弹窗不打扰。
跨线程：UN 的 didReceive 回调不在 Qt 主线程，经 dispatch_async(main_queue) 回主线程
        后再调恢复窗口回调；delegate 必须模块级单例持久持有，否则回调丢失。
        （v1.4.57 起这两个名字由本模块用 NSOperationQueue.mainQueue() 实现 ——
         pyobjc 12 已经把 Foundation 里的 dispatch_* 拿掉了，见 _ensure_un。）
语义键："target-{direction}-{target}"，避免列表 index 漂移导致旧通知被错误替换。

注意：本模块在 dev 态（无桌面会话 / UN 不可达）会自动降级 osascript，
      且 v6 明文：UN 能否在**打包后 .app** 上投递是硬验收门，需真机实测。
"""

import collections
import subprocess
import threading
import time

from sf_logging import log_line
from sf_quote import _beijing_now, price_decimals

# ---- UserNotifications 可用性探测（导入失败 → 整条 UN 路线降级） ----
# 注意命名：本标志为 True 表示「UN 不可用」（导入失败 → 整条路线降级）。
# 原名为 _UN_AVAILABLE，但赋值却是"导入失败 → True"，与字面义相反，
# 极易在后续维护中被误改成反的。v1.2.80 更名，使名称与赋值一致。
#
# v1.3.45 冷启动优化：pyobjc 的 UserNotifications / Foundation 改为**延迟导入**。
# 实测：模块级同步 import 这两个框架需 54~62 ms，占冷启动 import 阶段约 1/3，
# 而通知只在「目标价命中」时才用得到 —— 为了一个可能整天都不发生的事件，
# 让每次双击启动都多等 60ms 不划算。
# 做法：模块级只放 None 占位（保持可被测试桩替换），首次真正要用时才 _ensure_un()
# 导入并填充。首次通知因此会多花 ~55ms，但那是非关键路径，且只有一次。
UNUserNotificationCenter = None
UNMutableNotificationContent = None
UNNotificationRequest = None
NSObject = None
NSString = None
dispatch_get_main_queue = None
dispatch_async = None

_UN_UNAVAILABLE = False   # 乐观初值；_ensure_un() 首次调用时按真实导入结果修正
_UN_PROBED = False


def _ensure_un():
    """首次使用 UN 前完成 pyobjc 导入；已探测过或已被置为不可用时直接返回。

    判断顺序刻意如此：
      1. _UN_PROBED / _UN_UNAVAILABLE 命中 → 不动（测试桩可先把 _UN_UNAVAILABLE
         置 True 强制走降级路线，不能被探测结果覆盖）；
      2. 模块级名字已被外部填好（测试桩 mock）→ 视为就绪，不覆盖桩；
      3. 其余情况才真正 import，失败则整条 UN 路线降级 osascript。
    """
    global _UN_UNAVAILABLE, _UN_PROBED
    if _UN_PROBED or _UN_UNAVAILABLE:
        return
    _UN_PROBED = True
    if UNUserNotificationCenter is not None:
        return
    try:
        from UserNotifications import (
            UNUserNotificationCenter as _center,
            UNMutableNotificationContent as _content,
            UNNotificationRequest as _request,
        )
        from Foundation import (NSObject as _NSObject, NSString as _NSString,
                                NSOperationQueue as _ops_queue)
    except Exception:
        _UN_UNAVAILABLE = True
        return
    g = globals()
    g["UNUserNotificationCenter"] = _center
    g["UNMutableNotificationContent"] = _content
    g["UNNotificationRequest"] = _request
    g["NSObject"] = _NSObject
    g["NSString"] = _NSString
    # v1.4.57：pyobjc 12 起 Foundation **不再导出** dispatch_get_main_queue /
    # dispatch_async（本机实测 `ImportError: cannot import name 'dispatch_async'`）。
    # 上面那句 import 是一体的，一个名字取不到就整条 UN 路线被判不可用 ——
    # 表现是横幅改走 osascript（身份不是本 app）、点击横幅恢复窗口和 on_click
    # 回调一起失效，而日志里只有孤零零一行 "notify delegate install error"。
    # 这条事故由新加的 tools/pyobjc_sig_check.py 扫出来（35 个 pyobjc 符号里
    # 就这 2 个取不到）。队列语义改用 NSOperationQueue.mainQueue()：实测在 Qt 的
    # cocoa 事件循环里 addOperationWithBlock_ 确实会被执行（探针打印 BLOCK-RAN）。
    # 两个名字**保留原样**，测试桩仍然可以直接替换。
    def _main_queue():
        return _ops_queue.mainQueue()

    def _post_to_queue(queue, fn):
        queue.addOperationWithBlock_(fn)

    g["dispatch_get_main_queue"] = _main_queue
    g["dispatch_async"] = _post_to_queue
    _UN_UNAVAILABLE = False
    global _UN_REAL_CLASS
    _UN_REAL_CLASS = _center       # 记住"真类"是谁：只有它需要过身份守卫

# v1.4.66 复核（2026-10-12）：**没有 app bundle 身份的进程里绝不能碰 center**。
# 裸解释器 / 离线探针 / 测试进程里 `UNUserNotificationCenter.currentNotificationCenter()`
# 抛的是 ObjC 异常 NSInternalInconsistencyException('bundleProxyForCurrentProcess is nil') ——
# PyObjC 接不住 ObjC 异常，`except Exception` 也拦不到，进程当场 SIGABRT
# （实测：随机顺序档全量被这一枚打断；同族的 NSAlert 构造反而没问题）。
# 所以守卫只圈在"取 center"这一个动作上：**不**改 `_ensure_un` 的整路线判定 ——
# 那条路线的可用性由 v1.4.57 的两条真框架用例守着（import 与 delegate 装配都得真跑）。
_BUNDLE_IDENTITY = None
_UN_REAL_CLASS = None        # 自己 import 到的那个类对象；被测试桩换掉后身份就不等了


def has_bundle_identity() -> bool:
    """本进程是不是以一个 app bundle 的身份在跑（装机版 True；裸 python / 探针 False）。"""
    global _BUNDLE_IDENTITY
    if _BUNDLE_IDENTITY is None:
        try:
            from Foundation import NSBundle
            _BUNDLE_IDENTITY = NSBundle.mainBundle().bundleIdentifier() is not None
        except Exception as exc:
            # 探测本身失败按"没有身份"处理：宁可降级 osascript，也不拿一次 abort 换一次横幅
            log_line("notify: bundle 身份探测失败，按不可用处理: %r" % (exc,), "WARNING")
            _BUNDLE_IDENTITY = False
    return _BUNDLE_IDENTITY


def _un_center():
    """取 UN center 的唯一入口；真框架且没有 bundle 身份时返回 None，调用方一律降级。

    为什么条件是 `_UN_IS_REAL_IMPORT and not has_bundle_identity()`：ObjC 异常只在
    **真的 UserNotifications 框架**上抛（PyObjC 接不住，进程当场 abort），而测试桩替换的是
    模块级名字 —— 桩在的时候没有 abort 风险，守卫若照拦会把整族通知用例拖去降级路径，
    于是"看起来测过了"实际测的是 osascript 分支（AGENTS §6 第 12 条那一族）。
    """
    if UNUserNotificationCenter is None:
        return None
    # 身份比对而不是布尔标记：全量里 v1.4.57 那条真框架用例会先把真类 import 进来，
    # 之后 test_notify 换成自己的替身 —— 布尔标记是模块级状态，它不会随替身复位，
    # 于是替身也被判成"真框架 + 裸进程"，整条 UN 路线被降级成 osascript（实测两条用例
    # 因此在用户桌面上要弹真通知，被弹窗守卫拦停）。比对对象身份就没有这个泄漏。
    if UNUserNotificationCenter is _UN_REAL_CLASS and not has_bundle_identity():
        return None
    return UNUserNotificationCenter.currentNotificationCenter()


# 实机探测：UN 投递失败（授权正常但 delivery 报错/超时无展示）一次性切 osascript，
# 不做运行时反复横跳（v6 §5.2）。
_USE_OSASCRIPT = False
# 投递通道计数（诊断导出用）：deliver() 明确返回 'un'/'osascript'/'denied'/'error'，
# 但调用点（目标价命中提醒）只关心成败，返回值过去被丢弃 —— 于是"横幅到底发出去没"
# 在诊断包里零证据（台账 S34：整条 UN 路线静默坏死几十个版本，现场只有一行
# 会被轮转挤掉的 WARNING）。计数与末次通道在投递那一刻就地落账。
_DELIVER_COUNTS = {"un": 0, "osascript": 0, "denied": 0, "error": 0}
_DELIVER_LAST = ""
_DELIVER_LAST_AT = 0.0
_DELIVERY_LOCK = threading.Lock()
# 下面两个 OrderedDict 都按插入序淘汰到 256 条：identifier 里带日期与价位，
# 组合数随使用天数无上限增长，不封顶就是一条长挂机下的内存泄漏。
_FALLBACK_KEYS = collections.OrderedDict()
_FALLBACK_KEYS_MAX = 256
# delegate 为模块级单例，必须按通知标识保存点击回调；否则多条横幅并存时，
# 点击较早通知会错误执行最后一条通知的回调。
_CLICK_HANDLERS = collections.OrderedDict()
_CLICK_HANDLERS_MAX = 256


def reset_delivery_probe():
    """复位「UN 投递失败 → 永久降级 osascript」的探测状态（测试专用）。

    R48：该标志置真后永不复位，测试进程内一条用例探测失败会污染后续全部用例，
    让它们全部走 osascript 分支 —— 看起来"通知逻辑测过了"，实际测的是降级路径。
    """
    global _USE_OSASCRIPT
    with _DELIVERY_LOCK:
        _USE_OSASCRIPT = False
        _FALLBACK_KEYS.clear()
        _CLICK_HANDLERS.clear()

# 模块级单例 delegate：防止回调丢失（v6 §5.2）。
_DELEGATE = None


def target_notification_title(name, code):
    """目标价提醒标题：优先股票名称，名称缺失时回退股票代码。"""
    label = str(name or "").strip() or str(code or "").strip() or "股票"
    return "%s 目标价" % label


def authorization_status():
    """返回 'authorized' / 'denied' / 'notDetermined' / 'provisional' / 'unknown'。
    UN 不可用时视 'denied' 处理（只走彩虹，不弹窗）。"""
    _ensure_un()
    if _UN_UNAVAILABLE:
        return "denied"
    try:
        center = _un_center()
        if center is None:
            return "denied"          # 没有 bundle 身份：只走彩虹，不弹横幅（与 UN 不可用同口径）
        st = center.authorizationStatus()
        mapping = {0: "notDetermined", 1: "denied", 2: "authorized",
                   3: "provisional", 4: "ephemeral"}
        return mapping.get(int(st), "unknown")
    except Exception as exc:
        log_line("notify auth status error: %r" % (exc,), "WARNING")
        return "unknown"


def delivery_stats():
    """诊断导出用：通知这一路此刻的现场（台账 S34 的远程取证通路）。

    回答三类过去只能靠用户复现的问题：
      · "到价没横幅" —— counts 里 un/osascript 各走了几次、last_channel 是哪条；
      · "我是不是关了通知" —— authorization 的系统侧真实状态；
      · "为什么横幅长得不一样/点了没反应" —— un_available / degraded_osascript
        两个永久降级标记（它们此前只活在会被轮转挤掉的一行 WARNING 里）。
    """
    with _DELIVERY_LOCK:
        counts = dict(_DELIVER_COUNTS)
        last = _DELIVER_LAST
        last_at = _DELIVER_LAST_AT
    # 没有 bundle 身份时**绝不问 UN**：`+[UNUserNotificationCenter ...]` 在那里抛的是
    # ObjC 异常，Python 兜不住（见 sf_logging.has_bundle_identity 的实测说明）。
    # 源码态/测试报 "no_bundle"，与"问了系统、系统说没授权"是两件事，不许混成 unknown。
    if not has_bundle_identity():
        auth = "no_bundle"
    else:
        try:
            auth = authorization_status()
        except Exception as exc:
            log_line("notify delivery_stats auth failed: %r" % (exc,), "WARNING")
            auth = "unknown"
    return {
        "authorization": auth,
        "un_available": bool(_UN_UNAVAILABLE),
        "degraded_osascript": bool(_USE_OSASCRIPT),
        "counts": counts,
        "last_channel": last,
        "last_at": last_at,
    }


def request_authorization():
    """首次保存目标价时异步请求授权；结果不回溯阻断保存（v6 §5.2）。"""
    _ensure_un()
    if _UN_UNAVAILABLE:
        return
    center = _un_center()
    if center is None:
        log_line("notify: 无 bundle 身份，跳过 UN 授权请求", "WARNING")
        return
    try:
        _install_delegate(center)

        def _handler(granted, error):
            log_line("UN authorization granted=%s error=%r" % (granted, error))
        try:
            center.requestAuthorizationWithOptions_completionHandler_(
                7, _handler)  # alert(1) | sound(2) | badge(4)
        except Exception as exc:
            log_line("UN requestAuthorization error: %r" % (exc,), "WARNING")
    except Exception as exc:
        log_line("notify request auth error: %r" % (exc,), "WARNING")


def _notification_identifier(response):
    """从 UN 点击响应读取通知标识；兼容 PyObjC 方法与测试替身属性。"""
    # 下面三处 except TypeError 同理：真实 PyObjC 对象上这三者是方法，而单测替身
    # 把它们做成属性 —— 属性不可调用抛 TypeError，用 getattr 兜住可共用一条路径。
    try:
        notification = response.notification()
    except TypeError:
        notification = getattr(response, "notification", None)
    try:
        request = notification.request()
    except TypeError:
        request = getattr(notification, "request", None)
    try:
        return str(request.identifier())
    except TypeError:
        return str(getattr(request, "identifier", ""))
    except Exception:
        return ""


def _install_delegate(center):
    """持久持有 delegate 单例，接管点击通知 → 主线程恢复窗口（v6 §5.2）。"""
    global _DELEGATE
    if _DELEGATE is not None:
        return
    try:
        # delegate 类必须定义在函数内：基类 NSObject 要等 _ensure_un() 才被填充，
        # 模块级 `class X(None)` 在导入期就 TypeError，整条 UN 路线再也装不上。
        class _NotifyDelegate(NSObject):
            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                    self, center, response, handler):
                try:
                    def _restore():
                        ident = _notification_identifier(response)
                        try:
                            with _DELIVERY_LOCK:
                                cb = _CLICK_HANDLERS.pop(str(ident), None)
                            if cb is not None:
                                cb()
                        except Exception:
                            pass
                        try:
                            if handler is not None:
                                handler()
                        except Exception:
                            pass
                    dispatch_async(dispatch_get_main_queue(), _restore)
                except Exception:
                    try:
                        if handler is not None:
                            handler()
                    except Exception:
                        pass

        _DELEGATE = _NotifyDelegate.alloc().init()
        center.setDelegate_(_DELEGATE)
    except Exception as exc:
        log_line("notify delegate install error: %r" % (exc,), "WARNING")


def deliver(title, subtitle, body, semantic_key=None, on_click=None):
    """投递一条通知。返回 'un' / 'osascript' / 'denied' / 'error'。

    - UN 不可用或已探测到 UN 投递失败 → osascript 降级；
    - 授权 denied → 直接 'denied'（只走彩虹，不弹窗）；
    - on_click：点击回调（仅 UN 路线有效，需主线程调用恢复窗口）。

    每一次返回都会计入 `delivery_stats()`（通道计数 + 末次通道 + 时刻）——
    调用点只看成败，返回值过去被丢弃，"到价没横幅"因此无法远程回答。
    """
    global _DELIVER_LAST, _DELIVER_LAST_AT
    try:
        result = _deliver_impl(title, subtitle, body, semantic_key, on_click)
    except Exception:
        with _DELIVERY_LOCK:
            _DELIVER_COUNTS["error"] = _DELIVER_COUNTS.get("error", 0) + 1
            _DELIVER_LAST = "error"
            _DELIVER_LAST_AT = time.time()
        raise
    channel = result if result in _DELIVER_COUNTS else "error"
    with _DELIVERY_LOCK:
        _DELIVER_COUNTS[channel] = _DELIVER_COUNTS.get(channel, 0) + 1
        _DELIVER_LAST = channel
        _DELIVER_LAST_AT = time.time()
    return result


def _deliver_impl(title, subtitle, body, semantic_key=None, on_click=None):
    """deliver() 的原始实现，行为与拆分前逐位一致（计时/计数在 deliver 外层）。"""
    global _USE_OSASCRIPT
    _ensure_un()
    if _UN_UNAVAILABLE:
        # UN 框架缺失（PyObjC 不可用）：osascript 是唯一的横幅出路。
        # 这不是"用户拒绝"，不应被下面的 denied 分支挡掉。
        return _deliver_osascript(title, subtitle, body)
    status = authorization_status()
    if status == "denied":
        # R48：授权被拒**必须**优先于降级判定。原实现先判 `_USE_OSASCRIPT or
        # _UN_UNAVAILABLE`，于是 UN 投递失败永久降级后，即使用户在系统设置里
        # 关掉了通知，仍会继续弹 osascript 横幅 —— 等于绕过用户的明确选择。
        return "denied"
    if status not in ("authorized", "provisional", "ephemeral"):
        # 只有这三种状态才真的能出横幅。`notDetermined`（首次设目标价时
        # requestAuthorizationWithOptions 是**异步**的，授权框还没答复就命中价位
        # 是超短线的常态）与 `unknown`（探测本身抛错）走 UN 会被系统静默丢弃：
        # addRequest 不报错、也不显示，deliver 照旧返回 'un'，调用方据此把条目
        # 置为已触发 → **这一天的提醒彻底消失，且日志里一行痕迹都没有**。
        # 未授权 ≠ 已拒绝，用户没表态时不能替他选"不打扰"，所以补发 osascript。
        log_line("notify: UN 尚未授权(status=%s)，本次改走 osascript 补发" % status,
                 "WARNING")
        return _deliver_osascript(title, subtitle, body)
    if _USE_OSASCRIPT:
        return _deliver_osascript(title, subtitle, body)
    try:
        center = _un_center()
        if center is None:
            raise RuntimeError("本进程无 bundle 身份，UN 取不到 center")
        _install_delegate(center)
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(NSString.stringWithString_(title))
        if subtitle:
            content.setSubtitle_(NSString.stringWithString_(subtitle))
        content.setBody_(NSString.stringWithString_(body))
        content.setSound_(None)  # 静默提醒，不打扰
        # identifier 用语义键，避免列表 index 漂移误替换旧通知（v6 §5.2）
        ident = semantic_key or "target-unknown"
        # delegate 装不上说明点击回调根本收不到（已走 osascript 或静默），此时注册
        # 只会让 _CLICK_HANDLERS 白白堆一个永不触发、也永不被弹出的条目。
        if on_click is not None and _DELEGATE is not None:
            with _DELIVERY_LOCK:
                _CLICK_HANDLERS[ident] = on_click
                _CLICK_HANDLERS.move_to_end(ident)
                while len(_CLICK_HANDLERS) > _CLICK_HANDLERS_MAX:
                    _CLICK_HANDLERS.popitem(last=False)
        req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            ident, content, None)
        fallback_key = ident

        def _done(error):
            if error is None:
                return
            log_line("UN deliver error: %r; fallback osascript" % (error,), "WARNING")
            global _USE_OSASCRIPT
            with _DELIVERY_LOCK:
                _USE_OSASCRIPT = True
                if fallback_key in _FALLBACK_KEYS:
                    return
                _FALLBACK_KEYS[fallback_key] = None
                while len(_FALLBACK_KEYS) > _FALLBACK_KEYS_MAX:
                    _FALLBACK_KEYS.popitem(last=False)
            # completionHandler 异步返回时，业务层已把该目标标成 hit；必须立刻
            # 对这一次通知补发，而不能只让后续通知降级，否则本次会永久漏掉。
            _deliver_osascript(title, subtitle, body)

        center.addNotificationRequest_withCompletionHandler_(req, _done)
        return "un"
    except Exception as exc:
        log_line("notify deliver error: %r; fallback osascript" % (exc,), "WARNING")
        _USE_OSASCRIPT = True
        return _deliver_osascript(title, subtitle, body)


def _deliver_osascript(title, subtitle, body):
    """osascript 降级路线：无点击回调（已知损失）。

    R35-4：必须在**后台线程**执行。deliver() 由行情 tick 在 UI 线程调用，
    一旦 _USE_OSASCRIPT 被置真（UN 投递失败后永久降级），每条目标价通知都会
    同步跑一次 subprocess（最长 5s 超时）——界面会明显卡顿。改为投递即返回。
    """
    head = "%s %s" % (title, subtitle) if subtitle else title
    # 转义双引号，避免脚本注入/语法错误
    head_s = head.replace('"', "'")
    body_s = body.replace('"', "'")
    script = 'display notification "%s" with title "%s"' % (body_s, head_s)

    def _run():
        try:
            result = subprocess.run(["/usr/bin/osascript", "-e", script], check=False,
                                    capture_output=True, timeout=5)
            if result.returncode != 0:
                log_line("osascript deliver failed returncode=%s stderr=%r" %
                         (result.returncode, result.stderr), "WARNING")
        except Exception as exc:
            log_line("osascript deliver error: %r" % (exc,), "WARNING")

    try:
        # daemon：osascript 最长可能阻塞 5s，非守护线程会让退出流程等它，
        # 表现为点了退出进程却迟迟不结束。
        t = threading.Thread(target=_run, name="sf-notify-osascript", daemon=True)
        t.start()
        return "osascript"
    except Exception as exc:
        log_line("osascript thread start error: %r" % (exc,), "WARNING")
        return "error"


def semantic_key(direction, target, code=None):
    """语义化通知键：target-600519-above-1500.00-20260907（v6 §5.2）。

    R35-2：必须带日期。UN 用 identifier 判重，相同 identifier 的请求会被系统
    **替换**已投递的那条而不是重新弹横幅。目标价每天 15:00 清空、次日常会重设
    同一个价位，若键只有「方向 + 价位」，第二次触发就会静默替换旧通知、不再提醒。
    带上生效日即可保证每天一次全新提醒，同时仍避免 index 漂移误替换。

    R35-1：target 非数字时不再抛异常（异常会打断整条通知链路）。
    """
    try:
        t = ("%%.%df" % price_decimals(code)) % float(target)
    except (TypeError, ValueError):
        t = "unknown"
    try:
        day = _beijing_now().strftime("%Y%m%d")
    except Exception:
        day = "unknown"
    code_part = str(code or "unknown").strip().lower() or "unknown"
    return "target-%s-%s-%s-%s" % (code_part, direction, t, day)
