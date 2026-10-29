# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_editor —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import threading

from PyQt6.QtCore import (QThread, Qt, QTimer, pyqtSignal, QRectF)
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen)
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)
from sf_constants import FG_BRIGHT_HEX, FG_HINT_HEX, FG_TEXT_HEX
from sf_glass import (GlassMixin, PopupLineEdit, paint_popup_surface, popup_stylesheet,
                      prepare_popup_surface, POPUP_INPUT_H)
from sf_icons import _ICON_SVG, svg_pixmap
from sf_logging import log_swallow
from sf_quote import search_by_digits, search_stocks
from sf_universe import code_shape, fold_name, prefixed_digits
from sf_utils import market_prefix, normalize_code
from sf_ui_render import (HOVER_BG_ALPHA, HOVER_STROKE_ALPHA, HOVER_RADIUS,
                          HOVER_PRESSED_BG_ALPHA, HOVER_PRESSED_STROKE_ALPHA)


def _canonical_code(text):
    """输入若是完整 6 位代码（可带 sh/sz/bj），返回规范化后的带前缀代码，否则返回 None。

    全角数字、大小写和中间空白先由 code_shape 收成标准形态；这样输入法在全角状态下
    敲出的 ６００５１９ 不会绕过归一化，被当成一个"代码"原样写进配置。
    """
    shape = code_shape(text)
    if shape is None:
        return None
    return normalize_code(shape[0] + shape[1])


def verify_bare_code(keyword):
    """字典只确认存在，不用缺页或过期名单证明不存在；联网仅在工作线程执行。

    显式写了交易所前缀、但和代码段对不上的（sh000001、bj600519），明确判"无此代码"：
    悄悄纠正成另一个交易所的同号股票，等于替用户换了一只他没点名的股票。
    """
    import sf_universe
    from sf_quote import QuoteDataError, fetch_quote
    shape = code_shape(keyword)
    if shape is None:
        sf_universe.note_reject("invalid_code_shape")
        return []
    prefix, digits = shape
    expected = market_prefix(digits)
    if prefix and expected and prefix != expected:
        sf_universe.note_reject("prefix_mismatch")
        return []
    code = normalize_code(prefix + digits)
    try:
        sf_universe.ensure_ready()
        local = sf_universe.match_confident(code)
        if any(normalize_code(candidate) == code for candidate, _ in local):
            return [(code, local[0][1])]
    except Exception as exc:
        log_swallow("sf_editor.verify_bare_code.dictionary", exc)
    try:
        quote = fetch_quote(code)
    except QuoteDataError:
        sf_universe.note_reject("probe_says_missing")
        return []
    except Exception as exc:
        log_swallow("sf_editor.probe_offsegment", exc)
        return [(code, code)]
    if (not isinstance(quote, dict) or not str(quote.get("name") or "").strip()
            or (quote.get("code") and normalize_code(str(quote["code"])) != code)):
        sf_universe.note_reject("probe_empty_or_wrong_code")
        return []
    return [(code, quote["name"])]


def _is_code_input(text):
    """输入是代码一类（完整 6 位，或写了交易所前缀的数字）还是名称。"""
    if code_shape(text) is not None:
        return True
    shape = prefixed_digits(text)
    return bool(shape and shape[0])


def resolve_code_input(keyword):
    """把代码类输入解析成候选列表；窗口按条数决定：一条直接替换，多条列出来选。

    交易所前缀写错（sh000001、bj600519）不再直接判"无此代码"：按数字去找同号的股票、
    ETF/LOF 和指数。同号有多条（上证指数与平安银行）就全部列出来让用户选；只有一条
    （bj600519 只对得上沪市茅台）就直接替换，与其他搜索一致，不再多问一次。
    写对了前缀（或没写）的完整代码照旧：字典/行情确认存在就直接用；确认不存在时，
    同号也可能是另一个交易所的指数（000300 只在沪市是指数），同样按同号候选处理。
    """
    import sf_universe
    shape = code_shape(keyword)
    if shape is None:
        partial = prefixed_digits(keyword)
        if not (partial and partial[0]):
            return []
        found = search_by_digits(partial[1], partial[0])
        if not found:
            sf_universe.note_reject("prefix_no_match")
        return found
    prefix, digits = shape
    expected = market_prefix(digits)
    if prefix and expected and prefix != expected:
        found = search_by_digits(digits, prefix)
        if not found:
            sf_universe.note_reject("prefix_mismatch")
        return found
    found = verify_bare_code(keyword)
    if found:
        return found
    return search_by_digits(digits, prefix or expected)

# H-1 v1.2.50：zombie 搜索线程登记表改为模块级 + 软上限。
# 背景：原实现把关闭时仍在运行的 SearchThread 挂在窗口实例 _zombie_threads 上，
# 窗口销毁后线程失去引用 → QThread 运行中被 GC 析构直接 abort。现改为模块级登记表，
# 线程 finished 时经 _prune_zombie_threads 自动移除，既防止运行中析构崩溃，又杜绝积累。
# v1.4.18 走查澄清：_ZOMBIE_THREADS_MAX 是**软上限**——仅用于观测/测试，prune 只在
# 线程 finished 后移除，**绝不丢弃仍在运行的线程**（那恰恰会触发它要避免的 QThread
# 运行中析构 abort）。列表长度由同时在运行的搜索数天然封顶，不是靠这个数硬截断。
_ZOMBIE_THREADS_MAX = 4
_zombie_threads = []  # 模块级：持有关闭时仍在运行的搜索线程引用，线程结束自清
# 第1轮走查修复：登记表锁 —— QThread.finished 在工作线程发射，纯 Python 槽按直接连接
# 在工作线程执行清理，与主线程的 append/遍历并发，无锁时可能丢失/误删条目。
_zombie_lock = threading.Lock()


def _prune_zombie_threads():
    """仅清理已结束的 zombie 线程；运行中的线程即使暂时超过软上限也保留引用。"""
    with _zombie_lock:
        for t in list(_zombie_threads):
            try:
                running = t.isRunning()
            except RuntimeError:
                running = False
            if not running:
                try:
                    _zombie_threads.remove(t)
                except ValueError:
                    pass


class SearchThread(QThread):
    # v1.2.71：回包携带"搜索序号"（发起时分配）。窗口侧用序号判断这份结果是否
    # 仍是最新一次搜索的产物，旧序号的回包一律丢弃。
    done = pyqtSignal(list, int)

    def __init__(self, keyword, seq=0, parent=None):
        super().__init__(parent)
        self.keyword = keyword
        self.seq = seq
        self.verify_code = False

    def run(self):
        try:
            if self.verify_code:
                results = resolve_code_input(self.keyword)
            else:
                results = search_stocks(self.keyword)
        except Exception as exc:
            # 搜索失败按"无结果"回报，但必须留痕：否则"搜不到"与"搜索代码出错"在日志里长得一样。
            log_swallow("sf_editor.SearchThread.run", exc)
            results = []
        # 第1轮走查修复：emit 也包在 try 内 —— 信号投递异常（如接收方已析构的极端
        # 时序）不应让 QThread 以未捕获异常终止且无日志。
        try:
            self.done.emit(results, self.seq)
        except Exception as exc:
            log_swallow("sf_editor.SearchThread.emit", exc)


_CAND_SIDE_PAD = 52      # 弹窗左右边距 28 + 按钮内边距 20 + 余量 4
_CAND_FALLBACK_W = 132   # 取不到窗口宽度时按主窗最窄的 160 推（160 - 52 + 24）


def _candidate_label(name, code, avail_px):
    """候选按钮文字：放得下就是"名称  代码"，放不下只省略名称，代码永远完整。"""
    font = QFont()
    font.setPixelSize(12)             # 与样式表 QPushButton#cand 的 font-size 一致
    metrics = QFontMetrics(font)
    full = "%s  %s" % (name, code)
    if metrics.horizontalAdvance(full) <= avail_px:
        return full
    shown = name
    while shown and metrics.horizontalAdvance("%s…  %s" % (shown, code)) > avail_px:
        shown = shown[:-1]
    return "%s…  %s" % (shown, code) if shown else code


class _CandidateButton(QPushButton):
    """候选按钮：回车可选中；上下键交还给编辑窗在候选间移动焦点。"""

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
        elif e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            e.ignore()
        else:
            super().keyPressEvent(e)


class MainCloseButton(QPushButton):
    """与主卡共用图标及悬浮令牌，底板使用抗锯齿绘制。"""
    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._close_img = svg_pixmap(_ICON_SVG["close"], 24)
        self._close_images = {24: self._close_img}
        self.setStyleSheet(
            "QPushButton{background:transparent;border:none;padding:0;}")

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if self.isEnabled() and (self.underMouse() or self.isDown()):
                pressed = self.isDown()
                p.setBrush(QColor(255, 255, 255,
                                 HOVER_PRESSED_BG_ALPHA if pressed else HOVER_BG_ALPHA))
                p.setPen(QPen(QColor(255, 255, 255,
                                    HOVER_PRESSED_STROKE_ALPHA if pressed else HOVER_STROKE_ALPHA), 1))
                p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                  HOVER_RADIUS, HOVER_RADIUS)
            size = 12
            # Render at the destination's device scale, including QWidget.render
            # redirection, so both 1x and Retina avoid bitmap resampling.
            pixels = max(1, round(size * abs(p.deviceTransform().m11())))
            if pixels not in self._close_images:
                self._close_images[pixels] = svg_pixmap(_ICON_SVG["close"], pixels)
            image = self._close_images[pixels]
            if image is not None:
                if not self.isEnabled():
                    p.setOpacity(0.3)
                p.drawImage(QRectF((self.width() - size) / 2, (self.height() - size) / 2,
                                  size, size), image)
        finally:
            p.end()


class EditorWindow(GlassMixin, QWidget):
    _prefer_liquid_glass = True

    def __init__(self, code, on_apply, anchor, anchor_w):
        super().__init__()
        self._init_glass_state()    # v1.2.87：复用毛玻璃（非 macOS 自动降级）
        self.code = code
        self.on_apply = on_apply
        self.on_closed = None    # 关闭时回调（主窗用它同步清空 _editor 引用）
        self._anchor = anchor
        self._anchor_w = anchor_w
        self._search_thread = None
        # v1.2.71：搜索序号 —— 每次发起搜索递增，回包携带；窗口用它丢弃过期结果
        # （回包竞态下旧关键词的结果会覆盖新搜索，详见 do_search 注释）
        self._search_seq = 0
        self._search_keyword = None
        # zombie 线程由模块级登记表持有，结束后清理；软上限只作观测，不丢弃运行中线程。

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)   # 关闭即销毁，配合 destroyed 信号可靠回收 _editor
        self.setStyleSheet(popup_stylesheet(
            ("QPushButton#cand{background:transparent;color:%s;text-align:left;"
             "padding:6px 10px;border-radius:6px;font-size:12px;}"
             "QPushButton#cand:hover{background:rgba(255,255,255,0.10);}"
             "QPushButton#cand:pressed{background:rgba(255,255,255,0.16);}")
            % FG_TEXT_HEX))
        self._build()
        self._move_near()
        # Only configure the native shadow here; no desktop capture is needed.
        self.prepare_surface()

    def prepare_surface(self):
        prepare_popup_surface(self)

    def _move_near(self):
        """定位到悬浮窗右侧（按主窗口实际宽度偏移，避免重叠）；
        超出屏幕时自动回折到左侧并钳制在屏幕内"""
        x = self._anchor.x() + self._anchor_w + 12
        y = self._anchor.y()
        try:
            # v1.2.71：按**悬浮窗所在的那块屏幕**取可用区域，而不是固定取主屏。
            # 悬浮窗停在副屏时（尤其副屏排在主屏下方、或顶部高于主屏的排列），
            # 原实现会把副屏虚拟坐标（如 y=1200）的锚点拉回主屏范围内，
            # 编辑器因此弹到另一块屏幕，与悬浮窗相距数百像素。
            scr = QGuiApplication.screenAt(self._anchor) or QApplication.primaryScreen()
            g = scr.availableGeometry()
            if x + self.width() > g.right() - 8:
                x = max(g.left() + 8, self._anchor.x() - self.width() - 12)
            if y + self.height() > g.bottom() - 8:
                y = max(g.top() + 8, g.bottom() - self.height() - 8)
            x = max(g.left() + 8, x)
            y = max(g.top() + 8, y)
        except Exception as _swallow_exc:
            log_swallow("sf_editor._move_near", _swallow_exc)
        self.move(x, y)

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("替换股票")
        # v1.2.87：标题字号与主窗股票名称一致（12pt 加粗）
        title.setStyleSheet("font-size: 12px; font-weight: bold; color:%s; "
                        "background: transparent;" % FG_BRIGHT_HEX)
        head.addWidget(title)
        head.addStretch()
        close = MainCloseButton()
        close.setFixedSize(20, 20)
        close.setAccessibleName("关闭替换股票窗口")
        close.clicked.connect(self._close)
        head.addWidget(close)
        lay.addLayout(head)

        self.entry = PopupLineEdit(self.code)
        # 替换股票输入框占满弹窗内容区；仅高度与目标价输入框保持统一。
        self.entry.setFixedHeight(POPUP_INPUT_H)
        self.entry.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.entry.setAccessibleName("股票代码或名称输入框")
        self.entry.setMaxLength(12)   # v1.2.22：A股代码最长 6 位数字+前缀（如 sh600519），限制防超长粘贴/输入
        self.entry.returnPressed.connect(self.do_search)
        # v1.2.33 修复：connect textChanged -> _on_text_changed（v1.2.31 只写了处理函数
        # 漏接信号，导致用户输入新股票后确定按钮仍保持初始置灰，无法替换）
        self.entry.textChanged.connect(self._on_text_changed)
        self.entry.selectAll()
        lay.addWidget(self.entry)

        self.hint = QLabel("")
        # 提示小字灰与 sf_glass.POPUP_FG_DIM 同一份来源（v1.4.64，复核 F6）
        self.hint.setStyleSheet("font-size:10px;color:%s;background:transparent;"
                                % FG_HINT_HEX)
        self.hint.setFixedHeight(14)  # 只承载搜索进度/错误反馈，空白时布局不跳动
        lay.addWidget(self.hint)

        self.cand_box = QVBoxLayout()
        self.cand_box.setSpacing(2)
        lay.addLayout(self.cand_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok = QPushButton("确定")
        ok.setAccessibleName("确定替换股票")
        ok.setObjectName("ok")
        ok.clicked.connect(self.do_search)
        self.ok = ok
        self.ok.setEnabled(False)   # 初始（未输入新内容）置灰
        cancel = QPushButton("取消")
        cancel.setAccessibleName("取消替换股票")
        cancel.setObjectName("cancel")
        cancel.clicked.connect(self._close)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        lay.addLayout(btn_row)

        # v1.2.87：宽度与主窗保持一致（_anchor_w 即主窗实际宽度）。
        # 原实现硬编码 230，主窗按内容自适应变宽后，替换弹窗明显窄一截、与
        # 主窗右边缘对不齐；目标价面板已同样跟随主窗宽度（target_panel._panel_w）。
        # 主窗宽度取不到（调用方未传/为 0）时退回历史值 230，保证不塌缩。
        self.setFixedWidth(int(self._anchor_w) if self._anchor_w else 230)
        self.adjustSize()
        # v1.2.71 第4轮走查：初始焦点必须落在输入框（实测原实现 setFocus 零调用）。
        # Qt 的 Tab 链默认按**创建顺序**取第一个可聚焦控件，而本窗口在 head 布局里
        # 先创建了右上角「✕ 关闭」按钮 —— 焦点就落在它上面，后果有三：
        #   ① 打开编辑器后直接打字，字符不进输入框；
        #   ② 此时按回车/空格激活的是**已聚焦的 ✕** → 窗口直接关闭，
        #      搜索一次都不会执行（用户会以为"搜索坏了"）；
        #   ③ entry.selectAll() 期待的"整段覆写"同时失效。
        self.entry.setFocus()
        # Tab 顺序按视觉排布：输入框 → 取消（左）→ 确定（右）
        self.setTabOrder(self.entry, cancel)
        self.setTabOrder(cancel, ok)

    def showEvent(self, e):
        """v1.2.87：首帧后延迟注入毛玻璃（复用 GlassMixin，非 macOS 自动降级）。"""
        super().showEvent(e)
        if getattr(self, "_glass_pending", False):
            self._glass_pending = False
            QTimer.singleShot(0, self._inject_glass)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._sync_glass_frame(force_mask=True)

    def hideEvent(self, e):
        try:
            self._close_glass()
        except Exception as exc:
            log_swallow("sf_editor.hideEvent", exc)
        super().hideEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._sync_glass_frame(force_mask=True)

    def paintEvent(self, e):
        # v1.2.87：同帧 flush 玻璃层圆角 mask（pending 时），与主窗一致
        self._glass_mask_flush()
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Native material lives below Qt in this same window; dimming is shared.
            paint_popup_surface(self, p)
        finally:
            # v1.4.18 建议#6：与主窗 sf_ui_render 同样用 try/finally 收口 ——
            # paint_popup_surface 抛错时若不 end()，一个活动的 QPainter 会留在
            # 底图缓存上，后续绘制行为未定义（主窗已加固、弹窗此前漏了）。
            p.end()

    def _detach_search_thread(self):
        """第1轮走查修复：搜索线程安全收尾抽出为幂等方法，供 _close 与 closeEvent 共用。

        背景：原收尾逻辑只写在 _close()，但窗口存在不走 _close 的销毁路径
        （open_editor 开关式 close()、退出时关闭、deleteLater），WA_DeleteOnClose
        下窗口销毁后运行中的 SearchThread 失去最后引用 → QThread 运行中析构直接
        abort；未断开的 done 信号投递到已删除对象同样 qFatal。现任何关闭途径都先断信号 +
        登记 zombie，线程引用由模块级表持有至自然结束。"""
        if self._search_thread is None or not self._search_thread.isRunning():
            return
        # H-2 v1.2.51：先断开运行中线程的 done 信号连接，杜绝窗口关闭/替换后
        # 迟到回包仍触发 _on_search_done → on_apply 二次切股，把用户刚替换的
        # 股票覆盖回旧结果（实测 5s 后回退原股现象根因）
        try:
            self._search_thread.done.disconnect(self._on_search_done)
        except (TypeError, RuntimeError):
            pass
        _prune_zombie_threads()
        with _zombie_lock:
            if self._search_thread not in _zombie_threads:
                _zombie_threads.append(self._search_thread)
        # finished 从工作线程发射，清理已持锁，不会与主线程并发破坏登记表
        try:
            self._search_thread.finished.connect(
                lambda t=self._search_thread: _prune_zombie_threads())
        except (TypeError, RuntimeError):
            pass

    def closeEvent(self, e):
        """第1轮走查修复：统一关闭入口 —— 任何途径的 close()（含开关式关闭/退出/
        系统关窗）都先做线程安全收尾，再交给默认关闭（WA_DeleteOnClose 销毁）。"""
        self._closed = True
        try:
            self._close_glass()      # v1.2.87：回收毛玻璃 NSWindow，避免原生窗口泄漏
        except Exception:
            pass
        try:
            self._detach_search_thread()
        except Exception:
            pass
        super().closeEvent(e)

    def _close(self):
        self._closed = True
        # 同步通知主窗清空 _editor 引用（避免依赖异步 destroyed 造成悬空引用）
        cb = self.on_closed
        self.on_closed = None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
        # 不再阻塞等待搜索线程（http 最长 4s 会卡 UI）；若仍在运行则移入模块级
        # zombie 登记表持有引用（防窗口关闭后 QThread 被 GC 崩溃），线程结束后
        # finished 连接触发 _prune_zombie_threads 自动移除（线程一结束即出表）
        self._detach_search_thread()
        self._anchor = None
        self.close()

    def _on_text_changed(self, text):
        """未输入新内容（输入为空，或仍等于当前代码）时置灰确定按钮"""
        if getattr(self, "_search_keyword", None) is not None:
            self._search_seq += 1
            self._search_keyword = None
            self._detach_search_thread()
            self._search_thread = None
            self.hint.setText("")
            self._clear_candidates()
        try:
            txt = text.strip()
            if not txt:
                self.ok.setEnabled(False)
                return
            same = False
            try:
                # 全角数字、大小写、中间空白先收成标准代码再比，否则输入法全角状态下
                # 敲出的当前代码会被当成"新内容"，确定按钮不置灰。
                typed = _canonical_code(txt) or normalize_code(txt)
                same = typed == normalize_code(self.code or "")
            except Exception:
                same = txt == (self.code or "")
            self.ok.setEnabled(not same)
        except Exception:
            # 判定出错时放行：宁可让用户多点一次，也不能把"确定"永久锁死
            self.ok.setEnabled(True)

    def do_search(self):
        # 搜索进行中忽略重复触发（防止旧线程被覆盖引用导致 Qt abort）
        if self._search_thread is not None and self._search_thread.isRunning():
            return
        if not self.ok.isEnabled():
            return  # 确定按钮置灰（未输入新内容）时，回车同样不执行
        txt = self.entry.text().strip()
        if not txt:
            return
        typed_code = _canonical_code(txt)
        if typed_code is not None:
            # v1.2.23：直接输入代码且与当前股票重复 → 明确提示，不做静默替换
            if self._is_dup(typed_code):
                self.hint.setText("该股票已在显示中，无需替换")
                return
            try:
                import sf_universe
                local = sf_universe.match_confident(typed_code)
            except Exception as _swallow_exc:
                log_swallow("sf_editor.guard_bare_code", _swallow_exc)
                local = []
            if local:
                self._choose_candidate(typed_code)
                return
        self.hint.setText("搜索中...")
        self._clear_candidates()
        # v1.2.71：发起新搜索前先断开旧线程的 done 连接，并递增搜索序号。
        # 背景（回包竞态）：done 是跨线程信号，走 QueuedConnection —— 线程 run()
        # 结束后 isRunning() 已变 False（上面的守卫拦不住），但槽调用可能仍排在
        # 主线程事件队列里。此时用户再回车发新搜索，旧槽出队执行就会用**旧关键词**
        # 的结果刷新界面；若旧结果恰好只有 1 条，会直接 on_apply(旧代码) 并关闭
        # 窗口 —— 表现是"搜了 B，结果切到了刚才的 A"。
        # 序号校验是最后一道闸：即便某个连接没断开，过期回包也会被丢弃。
        if self._search_thread is not None:
            try:
                self._search_thread.done.disconnect(self._on_search_done)
            except (TypeError, RuntimeError):
                pass
        self._search_seq += 1
        self._search_keyword = fold_name(txt)
        self._search_thread = SearchThread(txt, self._search_seq)
        self._search_thread.verify_code = _is_code_input(txt)
        self._search_thread.done.connect(self._on_search_done)
        self._search_thread.start()

    def _is_dup(self, code):
        """添加/替换的股票与当前显示股票是否为同一只（重复）。
        代码统一经 normalize_code 归一化后比较，兼容带前缀/纯数字两种输入形式。"""
        try:
            candidate_code = normalize_code(str(code).strip())
            return bool(candidate_code) and candidate_code == normalize_code(str(self.code).strip())
        except Exception:
            return False

    def _choose_candidate(self, code):
        """候选点击统一入口：重复股票明确提示，不静默替换；否则应用并关闭。"""
        if self._is_dup(code):
            self.hint.setText("该股票已在显示中，无需替换")
            return
        # 第1轮走查修复：on_apply 异常保护（同 do_search 直码分支）
        try:
            self.on_apply(code)
        except Exception:
            self.hint.setText("替换失败，请重试")
            return
        self._close()

    def _on_search_done(self, results, seq=0):
        # H-2 v1.2.51：双保险 —— 窗口已关闭/已替换时，迟到的回包直接丢弃，
        # 避免在 _close 断开信号前已进入排队槽调用窗口的竞态仍执行 on_apply
        # _closed 只在关闭路径上赋值、__init__ 里没有初始化，故用 getattr 兜底
        if getattr(self, '_closed', False):
            return
        # v1.2.71：过期回包丢弃 —— 序号小于当前搜索序号，说明这是上一次搜索
        # 的结果（用户已经发起了新搜索），用它刷新界面会覆盖新结果。
        if seq < self._search_seq:
            return
        keyword = getattr(self, "_search_keyword", None)
        if keyword is not None and fold_name(self.entry.text()) != keyword:
            return
        if not results:
            import sf_universe
            sf_universe.note_reject("search_no_match")
            is_code = bool(keyword and _is_code_input(keyword))
            self.hint.setText("未找到该代码，请核对后重试" if is_code
                              else "未找到匹配的 A 股")
            return
        if len(results) == 1:
            # v1.2.23：唯一结果与当前股票重复 → 明确提示，不做静默替换
            if self._is_dup(results[0][0]):
                self.hint.setText("该股票已在显示中，无需替换")
                return
            # 第1轮走查修复：on_apply 异常保护（同 do_search 直码分支）
            try:
                self.on_apply(results[0][0])
            except Exception:
                self.hint.setText("替换失败，请重试")
                return
            self._close()
            return
        self._clear_candidates()
        self.hint.setText("")
        shown = 0
        for code, name in results:
            if shown >= 8:
                # 提示行只有一行高、不换行：主窗收窄到 184 时可用宽约 156px，
                # 原文案「结果过多，已显示前 8 条，请输入更精确的关键词」要 223px，会被截断。
                self.hint.setText("结果过多，仅显示前 %d 条" % shown)
                break
            # 候选名超长防护：按像素省略名称、保留代码。同名的 ETF/指数靠代码区分，
            # 旧写法按 24 个字符一刀切，长名称会把代码裁掉，两条候选就看不出区别了。
            try:
                avail = int(self.width()) - _CAND_SIDE_PAD
            except Exception:
                avail = _CAND_FALLBACK_W
            label = _candidate_label(name, code, avail)
            button = _CandidateButton(label)
            if label != "%s  %s" % (name, code):
                button.setToolTip("%s  %s" % (name, code))
            button.setObjectName("cand")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, c=code: self._choose_candidate(c))
            self.cand_box.addWidget(button)
            shown += 1
        self.adjustSize()
        self._move_near()

    def _clear_candidates(self):
        had = self.cand_box.count() > 0
        while self.cand_box.count():
            item = self.cand_box.takeAt(0)
            widget = item.widget()
            if widget:
                # deleteLater 要等下一轮事件循环才真正销毁；先隐藏，
                # 否则旧按钮在新结果画出来之前还会留在窗口上。
                widget.hide()
                widget.deleteLater()
        if had:
            self._fit_height()

    def _fit_height(self):
        """候选收起后把窗口收回内容所需的高度并回到锚点位置。

        只在 _on_search_done 里 adjustSize 过一次的话，继续输入或搜到 0 条后窗口
        仍保持 8 条候选时的高度（实测 421px，内容只需 151px），底部留下大片空白。
        关闭后 _anchor 已清空，此时不再定位。
        """
        self.adjustSize()
        if self._anchor is not None:
            self._move_near()

    def _step_candidate(self, delta):
        """上下键在输入框与候选之间移动焦点；没有候选时不处理。"""
        items = (self.cand_box.itemAt(i) for i in range(self.cand_box.count()))
        buttons = [item.widget() for item in items if item is not None]
        if not buttons:
            return False
        focused = self.focusWidget()
        index = buttons.index(focused) if focused in buttons else -1
        target = index + delta
        if target < 0:
            self.entry.setFocus()
        else:
            buttons[min(target, len(buttons) - 1)].setFocus()
        return True

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._close()
        elif e.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._step_candidate(
                1 if e.key() == Qt.Key.Key_Down else -1):
            e.accept()
        else:
            super().keyPressEvent(e)
