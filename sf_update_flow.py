"""Update interaction decisions, separate from native/Qt dialog rendering.

The host port supplies update state and UI commands. All methods execute on the
UI thread; blocking transport remains in UpdateController.
"""

import os
import threading
import time
from sf_constants import UPDATE_CHECK_STALE_SEC


def update_flow_diagnostics(host, field, install_result):
    """Read the existing update snapshot on the UI thread, one field at a time."""
    return {
        "downloading": field("update.downloading", lambda: bool(host._update_downloading), None),
        "transition_pending": field("update.transition",
                                    lambda: bool(host._update_transition_pending), None),
        "manual_wait": field("update.manual_wait", lambda: bool(host._update_manual_wait), None),
        "last_check_at": field("update.last_check_at", lambda: float(host._update_last_check_at), None),
        "stale_resets": field("update.stale_resets", lambda: int(host._update_stale_resets), None),
        "last_result": field("update.last_result", lambda: host._update_last_result, None),
        "install_result": field("update.install_result", lambda: install_result(), None),
    }


class UpdateFlowController:
    def __init__(self, backend, log, swallow):
        self._backend = backend
        self.log = log
        self.swallow = swallow

    def check(self, host, manual=False):
        """检查更新只占用独立线程，绝不与行情轮询共用连接池或阻塞界面。

        v1.4.43：手动点击【必须有可见反馈】，并把「能立刻答复的」排到守卫之前。
        为什么要改（离屏探针按真实分支实测）：右键悬浮窗的那一下就把应用切成了
        活跃，而「变为活跃」本身就会触发一次静默补查 —— 用户接着点「检查更新」时
        几乎必然撞上那次还在飞的检查。旧代码在这里直接 return，连「正在检查」的框
        都不给；5 秒后那次检查又按静默处理什么都不弹。用户看到的就是「点了没反应」，
        而日志里一行都不留。
        """
        # ① 已知有新版本：手动检查直接弹确认框，不看任何在飞状态。自动检查发现的
        #    新版则会在回包后自动下载，下载完成再提示用户，避免让用户先确认一次、
        #    下载结束又确认一次。
        pending = getattr(host, "_update_pending", None)
        if manual and pending is not None:
            if getattr(host, "_update_downloading", False):
                host._show_update_message("更新进行中", "正在下载更新，请稍候。")
                return
            host._prompt_update(pending)
            return
        if getattr(host, "_threads_stopped", False):
            return
        in_flight = bool(host._update_checking
                         or getattr(host, "_update_transition_pending", False))
        if in_flight and host._update_check_stale():
            # v1.4.44：在飞太久就当它已经死了。回包是有界队列（32 格，满时丢最旧）里的
            # 一次性事件，一旦被丢掉，这两个标记会**永久锁真**：此后任何检查都起不来，
            # 那张「正在检查更新」也永远关不掉，只能重启程序。放开重来比从此不问好。
            host._update_checking = False
            host._update_transition_pending = False
            host._update_manual_wait = False
            # 只收这张「正在检查」框：整包收口会连带关掉下载进度框，那是另一件事。
            host._dispose_update_dialog(getattr(host, "_update_check_dialog", None))
            self.log("update check stale over %ds, state reset"
                     % UPDATE_CHECK_STALE_SEC, "WARNING")
            # 诊断留痕：复位过一次就说明有一轮检查的回包被丢掉过（那正是"点了没
            # 反应、框永远关不掉"的成因）。此前只有一行会被挤掉的 WARNING。
            host._update_stale_resets = int(getattr(host, "_update_stale_resets", 0)) + 1
            in_flight = False
        if in_flight:
            # ② 已有一次检查在飞：不再另起第二个请求，但这次点击要记在账上，由那次
            #    检查的回包按「用户主动要求」兑现（见 _handle_update_check）。
            if manual:
                host._update_manual_wait = True
                # A new explicit click renews the request, even after cancelling its wait box.
                host._update_check_aborted = False
                # 与下面发起新检查的分支同理：等待框只是提示，它出错不能让这次点击没有回音。
                try:
                    host._show_checking_dialog()
                except Exception as exc:
                    self.swallow("sf_update_flow.show_checking_dialog", exc)
                self.log("update check click reused in-flight check")
            return
        if host._update_downloading:
            # ③ 正在下载：更新已经在跑，说清楚在跑什么，别让人以为这次点击无效。
            if manual:
                host._show_update_message("更新进行中", "正在下载更新，请稍候。")
            return
        host._update_checking = True
        host._update_transition_pending = True
        # 上一次「取消」只作废它那一次，不许跟着下一次检查一路走下去。
        host._update_check_aborted = False
        # 墙钟：既用于激活补查的节流，也用于回包时算出这次检查实际花了多久。
        host._update_last_check_at = time.time()
        # 只有手动检查才弹「正在检查」：启动后 15 秒的自检、切回程序的补查、
        # 6 小时复查与失败退避都必须是静默的 —— 系统弹窗会把应用带到最前，
        # 看盘时抢一次焦点比慢一点更糟。（加强后的既有用例当场逮到过这个。）
        if manual:
            # 等待框只是提示，不是检查的前提：它内部会惰性 import 系统弹窗模块，
            # 安装包在程序运行中被换掉时归档读出坏数据（实测 zlib "Error -3"），
            # 异常若冒出去，检查既没发起、上面两个状态位又留在 True 直到被判过期。
            # 失败只留痕，照常往下发起检查（AGENTS §6 第 9 条，不许静默）。
            try:
                host._show_checking_dialog()
            except Exception as exc:
                self.swallow("sf_update_flow.show_checking_dialog", exc)

        self._backend(host).check(manual)

    def check_stale(self, host):
        """这一次在飞检查是不是已经过期（起算时刻 + UPDATE_CHECK_STALE_SEC）。

        单独成方法是为了让调用点只问一句"它还活着吗"，也便于用例直接驱动时间。
        """
        started = getattr(host, "_update_last_check_at", 0.0) or 0.0
        return (time.time() - started) > UPDATE_CHECK_STALE_SEC

    def checked(self, host, manual, update, error):
        host._update_checking = False
        host._update_transition_pending = False
        # v1.4.43：点击撞上在飞检查时记的账，由这里兑现 —— 结果按「用户主动要求」
        # 汇报（该弹确认框/该说「已是最新」/该说失败都照常弹）。销账放在最前面：
        # 一次点击只兑一次，不许隔几个小时后再莫名弹出一个框。
        manual = bool(manual) or bool(getattr(host, "_update_manual_wait", False))
        host._update_manual_wait = False
        # v1.4.44：用户在这一次结束前按了「取消」→ 状态、日志、退避照常走，
        # 但界面不再弹任何东西（销账在这里，不留到下一次检查）。
        if getattr(host, "_update_check_aborted", False):
            host._update_check_aborted = False
            manual = False
        # v1.4.43：每一次检查都留一行结果。此前**只有失败**才写日志，成功与
        # 「点击被复用」全是空白 —— 于是「今天到底查了几次、查没查到」无法回答，
        # 本轮这个 bug 就是这么被藏住的。
        started = getattr(host, "_update_last_check_at", 0.0) or 0.0
        self.log("update check done: asked=%s result=%s elapsed=%.1fs"
                 % ("manual" if manual else "auto",
                    "error" if error is not None else
                    ("new" if update is not None else "latest"),
                    (time.time() - started) if started else -1.0))
        # 诊断留痕（runtime.json 的 update_last_result）：asked/result/elapsed 与
        # 远端版本、阶段此前只活在上面这行会被轮转挤掉的日志里 —— "点了没反应"
        # "还停在 Beta""已是最新但通道没推上去"三件事在包里全部同形。
        try:
            host._update_last_result = {
                "asked": "manual" if manual else "auto",
                "outcome": ("error" if error is not None
                            else ("new" if update is not None else "latest")),
                "elapsed_sec": round(time.time() - started, 1) if started else None,
                "remote_version": (getattr(update, "version", "") or ""
                                   if update is not None else ""),
                "remote_stage": (getattr(update, "stage", "") or ""
                                 if update is not None else ""),
                "error": (str(error)[:200] if error is not None else ""),
                "at": time.time(),
            }
        except Exception as _rec_exc:
            # 记账失败不影响更新回包，但必须留痕（AGENTS §6 第 9 条）
            self.swallow("stock_float_window._update_last_result", _rec_exc)
        if getattr(host, "_quitting", False):
            host._close_update_flow_box()
            return
        if error is not None:
            host._close_update_flow_box()
            host._update_failure_count = getattr(host, "_update_failure_count", 0) + 1
            self.log("update check failed: %r" % (error,), "WARNING")
            host._schedule_update_retry()
            if manual:
                host._show_update_message("检查更新失败",
                                          "暂时无法检查更新。", "当前版本不受影响。")
            return
        host._close_update_flow_box()
        host._update_failure_count = 0
        timer = getattr(host, "_update_retry_timer", None)
        if timer is not None:
            timer.stop()
        if update is None:
            if manual:
                host._show_update_message("检查更新", "当前已是最新版本。")
            return
        host._update_prompted_version = update.version
        host._update_pending = update
        if manual:
            host._prompt_update(update)
        elif host._ready_update_archive(update) is None:
            # 自动检查不抢焦点：下载在后台完成，成功后再弹一次包含更新内容的确认框。
            host._download_update_background(update, prompt_when_ready=True,
                                             show_progress=False)

    def ready_archive(self, host, update):
        """返回仍存在的已校验更新包；版本变化或文件消失时清掉旧状态。"""
        archive = getattr(host, "_update_ready_archive", None)
        if (not archive or getattr(host, "_update_ready_version", None) != update.version
                or not os.path.isfile(archive)):
            if getattr(host, "_update_ready_version", None) != update.version:
                host._update_ready_archive = None
                host._update_ready_version = None
            elif archive and not os.path.isfile(archive):
                host._update_ready_archive = None
                host._update_ready_version = None
            return None
        return archive

    def answered(self, host, button):
        """主线程处理系统确认框的按钮结果；只认“立即更新”，其余视为放弃。"""
        dialog = getattr(host, "_update_prompt_dialog", None)
        host._update_prompt_dialog = None
        # 弹窗已随用户点击自行结束；这里只兜底收口异常未退出的子进程。
        host._dispose_update_dialog(dialog)
        if button == "暂不更新" or getattr(host, "_quitting", False):
            return
        update = getattr(host, "_update_pending", None)
        if update is None:
            return
        ready_archive = host._ready_update_archive(update)
        if button == "更新并重启应用":
            if ready_archive is not None:
                host._launch_downloaded_update(update, ready_archive)
            else:
                # 包被外部清理后，回到自动下载流程；不把旧路径交给安装助手。
                host._download_update_background(update, prompt_when_ready=True,
                                                 show_progress=False)
        elif button == "立即更新":
            host._accept_update_prompt(None, update)

    def download(self, host, update, prompt_when_ready=False, show_progress=True):
        if host._update_downloading or getattr(host, "_quitting", False):
            return
        host._update_downloading = True
        host._update_cancel = threading.Event()
        host._update_prompt_when_ready = bool(prompt_when_ready)
        # 检查提示已在 _prompt_update 中结束并释放后，才允许进入下载阶段。
        if show_progress:
            host._open_download_progress_box()

        self._backend(host).download(update, host._update_cancel)

    def cancel_download(self, host):
        cancel = getattr(host, "_update_cancel", None)
        if cancel is not None:
            cancel.set()

    def downloaded(self, host, update, archive, error):
        prompt_when_ready = bool(getattr(host, "_update_prompt_when_ready", False))
        host._update_prompt_when_ready = False
        host._update_downloading = False
        host._update_cancel = None
        host._close_update_flow_box()
        if getattr(host, "_quitting", False):
            return
        if error is not None:
            self.log("update download failed: %r" % (error,), "WARNING")
            if not prompt_when_ready:
                host._show_update_message("更新未完成",
                                          "下载或校验失败。", "当前版本未受影响。")
            return
        if prompt_when_ready:
            host._update_ready_archive = archive
            host._update_ready_version = update.version
            host._prompt_update(update)
            return
        host._launch_downloaded_update(update, archive)
