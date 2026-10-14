# -*- coding: utf-8 -*-
"""「目标价」设置面板（v6 §5.6；v1.2.77 四维度自审修复版 + v1.2.81 优化）。

独立 QWidget，参考 EditorWindow 结构与定位。行式输入 + 当前目标价列表
（含距目标价差% + 逐条删除）+ 风险/生效日说明 + 高亮开关 + 底部 [清空][立即生效]。
所有保存经由 on_apply 回调（主窗统一落盘/武装定时器/请求通知授权），
面板本身只持有"待生效"内存列表，不直写 config。

v1.2.81 优化（P1/P2）：
  P1-2  列表实时显示距目标价差%（需当前价）
  P1-3  支持「百分比/相对涨跌幅」快捷输入（如 +5% / -3% / 5%）
  P1-4  允许同方向多条不同价位目标价（不再强制覆盖为一条）
  P1-5  增加风险提示与生效日说明
  P2-1  支持逐条删除单条目标价（每行 ✕）
  P2-2  支持关闭「触发时窗户高亮」（持久化）

早期自审修复要点（详见审查报告）：
  D1  回车即可添加（returnPressed）
  D2  Esc 关闭且不保存（不再"被困住"）
  D3  「清空」二次确认（避免误触即丢）
  D5  打开即聚焦首个输入框
  D8  保存成功后直接关闭，不展示成功提示
  D9  独立状态行显示错误，不再覆盖目标价列表
  D10 列表区分"已触发"
  D13/D21 校验提示改为用户语言
  D15 保存/清空失败有明确提示且保持面板打开
  D17 生效日在"点击立即生效"时计算，不在添加时冻结
  D18 同方向覆盖给出"已更新 X → Y"提示（v1.2.81 起改为追加，见 P1-4）
  D25 股票已切换时拒绝保存

实现约定（自查补充）：
  - 状态行（status_label）只承载一次性提示，高度恒定留白；
    Esc/回车提示常驻在底部 hint_label，不会被状态覆盖。
  - 「清空」必须先落盘成功再动内存与界面，失败时保持原样并保留确认态可重试。
  - 落盘成功后同步 _saved_sig，避免关闭延迟内重开面板误报"有未生效修改"。
  - 删除/添加均只改内存，需点「立即生效」才落盘（统一暂存模型，避免误删即丢）。
"""

import unicodedata

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QSizePolicy)

from sf_constants import (TARGET_DIR_ABOVE, TARGET_DIR_BELOW,
                         UP_RED_HEX, DOWN_GREEN_HEX,
                         FG_BRIGHT_HEX, FG_FIELD_HEX, FG_INFO_HEX, FG_HINT_HEX,
                         FG_ERROR_HEX, FG_WARN_HEX, FG_DISABLED_HEX,
                         DANGER_BG_HEX, DANGER_HOVER_HEX, DANGER_PRESSED_HEX)
from sf_config import load_alerts
from targets import (make_alert, validate_alert, _valid_price,
                     distance_pct, STATE_PENDING, STATE_HIT, _same_slot)
from sf_glass import (GlassMixin, PopupLineEdit, paint_popup_surface, popup_stylesheet,
                      prepare_popup_surface, POPUP_INPUT_H, POPUP_TARGET_INPUT_W)
from sf_logging import log_line, log_swallow
from sf_quote import price_decimals
from sf_editor import MainCloseButton

TARGET_INPUT_W = POPUP_TARGET_INPUT_W  # 与替换股票共用 100px 输入宽度
PANEL_W = 206          # 容纳 100px 输入框、标签、加号与左右留白的最小面板宽度
MARGIN_X = 14          # 编辑窗左右内边距；与主窗内容留白语义一致
MARGIN_Y = 12
ROW_DEL_W = 22         # 逐条删除按钮宽
ROW_SPACING = 4        # 单行内：文字与删除按钮的水平间距
LIST_ROW_GAP = 3       # 框选区：目标价条目之间的垂直间距（收回过大的留白）
LIST_STATUS_GAP = 4    # 框选区：目标价列表与底部提示语之间的垂直间距
MAX_PER_DIR = 3        # v1.2.84：每个方向（涨到/跌到）最多允许的目标价条数
# v1.2.87：与主界面保持一致的视觉令牌
NUM_FONT = "D-DIN"         # 主界面数字字体（Datto 官方 D-DIN，启动已注册；数字按定宽格绘制）
# v1.4.62：面板五处字号此前各写各的 `font-size:12px`（标题、涨、跌、输入框、
# 面板里的数字行），改一处就得肉眼数四遍 —— 收拢成一个常量，唯一来源在这里。
# 为什么留 px 而不改成主卡那套 pt：本机离屏实测 96dpi 下 `12px` 与 `9pt` 的
# QFontMetrics 完全等宽（'8' 都是 8px），也就是说这不是"单位错了"，只是两种写法；
# 而主卡的 27pt/13pt 是**字号档位表**的一部分（会被 price_pct_layout 逐级降档），
# 面板不参与那套降档 —— 混成一个数反而会把两侧的行为绑死。像素观感零变化是这条
# 收拢的硬约束，判据见 tests/test_v1462_tail_cleanup.py。
PANEL_FONT_PX = 12
# v1.4.62：已触发标记的文字，唯一来源在这里（与上面 PANEL_FONT_PX 同一个道理：
# 散在函数里的字面量既没法被产物核验查到，也没法被用例引用）。
# 它必须带在**行首**：列表行按右省略截断，排在尾部的字段是宽度不够时第一个消失的。
HIT_MARKER_PREFIX = "已触发 "
# v1.4.59（走查 S8）：值必须与主卡 **同一份来源**（sf_constants 的涨跌色）。
# 旧的 "#ff6b6b" 与主卡 #ff5252 同屏并存，而这里注释却写着"与主界面涨色一致"。
UP_RED = UP_RED_HEX        # 涨相关文本（面板"涨到"标签、列表条目、输入框前景）
DOWN_GREEN = DOWN_GREEN_HEX  # 跌相关文本

# 面板提示语固定白/灰，不跟随涨跌红绿或警告色变化；颜色只表达信息层级，
# 避免与行情红涨绿跌和彩虹动效混淆。
# v1.4.64（全方位复核 F6）：四个值一律从 sf_constants 取，样式表里不再手写 hex ——
# 判据见 tests/test_style_hex_tokens.py（出现裸 hex 直接红，不留基线）。
STATUS_STYLE = {
    "info": "color:%s;font-size:11px;" % FG_INFO_HEX,
    "error": "color:%s;font-size:11px;" % FG_ERROR_HEX,
    "warn": "color:%s;font-size:11px;" % FG_WARN_HEX,
    "success": "color:%s;font-size:11px;" % FG_BRIGHT_HEX,
}


class AddIconButton(QPushButton):
    """灰底自绘细线加号，几何不依赖字体，始终精确居中。"""
    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setFixedSize(POPUP_INPUT_H, POPUP_INPUT_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 同一个灰阶也走 token：这颗加号的禁用态原先手写 #5a6072（2.66:1），
        # 与样式表里那两处是同一个缺陷，提亮后与它们同值。
        col = QColor(FG_DISABLED_HEX) if not self.isEnabled() else QColor(FG_FIELD_HEX)
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        c = self.rect().center()
        r = 4
        p.drawLine(c.x() - r, c.y(), c.x() + r, c.y())
        p.drawLine(c.x(), c.y() - r, c.x(), c.y() + r)


class TargetPanel(GlassMixin, QWidget):
    def __init__(self, code, name, on_apply, on_clear,
                 current_code_fn=None, current_price_fn=None,
                 parent=None, width=None, apply_blocked_fn=None,
                 limits_fn=None):
        super().__init__(parent)
        self._init_glass_state()    # v1.2.87：复用毛玻璃（非 macOS 自动降级）
        # 无边框、置顶、工具窗（与悬浮窗一致风格）
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        # v1.2.84：透明背景 + 自定义绘制，实现与主窗一致的超椭圆圆角，而非方形深蓝块
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # v1.2.87 补丁（M4）：关闭即销毁。原实现缺少该属性，而主窗每次打开都
        # 新建一个面板（旧面板只 close 不释放），于是每开一次就常驻一整棵
        # QWidget 子树；v1.2.87 起每个面板还持有独立毛玻璃 NSWindow 与
        # CAShapeLayer，长时间使用会持续累积。与 EditorWindow 对齐。
        # 主窗侧已接 destroyed 信号同步清空 _target_panel 引用，不会悬空。
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.code = code
        self.name = name or ""
        self._price_decimals = price_decimals(code)
        self.on_apply = on_apply          # callback(list_of_alerts) -> bool
        self.on_clear = on_clear          # callback() -> bool（清空，不请求授权）
        self.current_code_fn = current_code_fn  # callable() -> 当前股票代码
        self.current_price_fn = current_price_fn  # callable() -> 当前价（P1-2/3）
        # 待生效列表：打开时从 config 载入；+/- 与清空只改内存，[立即生效] 才落盘
        self._pending = []
        self._close_timer = None           # 兼容关闭事件与既有测试的可取消定时器引用
        self._locked = False               # 兼容后台同步与既有锁定态保护
        self._saved_sig = ()              # 打开时的磁盘签名（判断是否有未生效修改）
        self._clear_armed = False         # 「清空」二次确认态
        # v1.2.87：与主界面一致的视觉/行为令牌
        # 目标价输入须容纳完整的「价格或 ±5%」提示与价格数字；主窗现为窄版，
        # 因此只允许面板向更宽处扩展，绝不再随主窗被压至 184px。
        self._panel_w = max(PANEL_W, int(width) if width else PANEL_W)
        self._apply_blocked_fn = apply_blocked_fn         # callable()->bool（铃铛已提醒时禁用"立即生效"）
        self._apply_blocked = bool(apply_blocked_fn and apply_blocked_fn())
        self._limits_fn = limits_fn                      # callable()->(limit_up,limit_down) 或 None
        self._build()
        self._reload()
        self._refresh_list()
        # 状态行保留空白占位（高度恒定），避免出提示时面板跳高
        self._set_status("", "info")

    # ---------- 构建 ----------
    def _build(self):
        self.setFixedWidth(self._panel_w)
        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN_X, MARGIN_Y, MARGIN_X, MARGIN_Y)
        root.setSpacing(6)                       # v1.2.87：由 8 收窄到 6，缩短框选区域间距

        # 顶部行：左「目标价」标题（与主界面股票名称同字号 12pt）+ 右侧关闭按钮（✕）
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel("目标价")
        self.title_label.setStyleSheet("color:%s;font-size:%dpx;font-weight:bold;"
                                       % (FG_BRIGHT_HEX, PANEL_FONT_PX))
        top.addWidget(self.title_label)
        top.addStretch(1)
        self.btn_close_x = MainCloseButton()
        self.btn_close_x.setFixedSize(20, 20)
        self.btn_close_x.setAccessibleName("关闭目标价面板")
        self.btn_close_x.clicked.connect(self._on_close_x)
        top.addWidget(self.btn_close_x)
        root.addLayout(top)

        # 涨到（红色 #ff5252，与主界面涨色一致）
        row_up = QHBoxLayout()
        lab_up = QLabel("涨到")
        lab_up.setFixedWidth(30)
        lab_up.setStyleSheet("color:%s;font-size:%dpx;" % (UP_RED, PANEL_FONT_PX))
        self.edit_up = PopupLineEdit()
        # 输入框缩短为 36px；右侧 AddIconButton 同高同宽，保证正方形比例。
        self.edit_up.setFixedSize(TARGET_INPUT_W, POPUP_INPUT_H)
        self.edit_up.setAccessibleName("涨到目标价输入框")
        self.edit_up.setObjectName("up")        # 涨相关文本红色
        # 恢复占位提示：v1.2.87 为收紧布局删掉了它，但 S2 之后"引导用户先输入"
        # 的责任已由焦点转移到输入框文案上 —— 没有占位符时，"+5%" 这类相对
        # 涨跌幅快捷输入（P1-3）完全没有入口提示，新用户只会输绝对价。
        self.edit_up.setPlaceholderText("价格或 +5%")
        self.btn_up = AddIconButton()
        self.btn_up.setAccessibleName("添加涨到目标价")
        self.btn_up.setObjectName("plus")
        row_up.addWidget(lab_up)
        row_up.addWidget(self.edit_up)
        row_up.addWidget(self.btn_up)
        root.addLayout(row_up)

        # 跌到（绿色 #4caf50，与主界面跌色一致）
        row_dn = QHBoxLayout()
        lab_dn = QLabel("跌到")
        lab_dn.setFixedWidth(30)
        lab_dn.setStyleSheet("color:%s;font-size:%dpx;" % (DOWN_GREEN, PANEL_FONT_PX))
        self.edit_dn = PopupLineEdit()
        # 与“涨到”输入框保持同一紧凑高度，不改变文本字号或字重。
        self.edit_dn.setFixedSize(TARGET_INPUT_W, POPUP_INPUT_H)
        self.edit_dn.setAccessibleName("跌到目标价输入框")
        self.edit_dn.setObjectName("dn")        # 跌相关文本绿色
        self.edit_dn.setPlaceholderText("价格或 -5%")
        self.btn_dn = AddIconButton()
        self.btn_dn.setAccessibleName("添加跌到目标价")
        self.btn_dn.setObjectName("plus")
        row_dn.addWidget(lab_dn)
        row_dn.addWidget(self.edit_dn)
        row_dn.addWidget(self.btn_dn)
        root.addLayout(row_dn)

        # D1：回车即添加
        self.edit_up.returnPressed.connect(lambda: self._add(TARGET_DIR_ABOVE))
        self.edit_dn.returnPressed.connect(lambda: self._add(TARGET_DIR_BELOW))
        self.btn_up.clicked.connect(lambda: self._add(TARGET_DIR_ABOVE))
        self.btn_dn.clicked.connect(lambda: self._add(TARGET_DIR_BELOW))

        # D10/P1-2/P2-1：目标价列表（逐行：方向+价位+距目标% + 删除 ✕）
        self.list_box = QVBoxLayout()
        self.list_box.setContentsMargins(0, 0, 0, 0)
        self.list_box.setSpacing(LIST_ROW_GAP)
        root.addLayout(self.list_box)

        # D9：独立状态行（错误/成功/提示），不再复用列表行。
        # 仅拉开框选区域纵向层次：列表结束后增加固定间距，字号/颜色/左右位置不变。
        root.addSpacing(LIST_STATUS_GAP)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(STATUS_STYLE["info"])
        root.addWidget(self.status_label)

        # D12：底部按钮等宽（立即生效=蓝色主按钮 / 清空=灰色次按钮，与主线路一致）
        btns = QHBoxLayout()
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setAccessibleName("清空全部目标价")
        self.btn_clear.setObjectName("cancel")
        self.btn_apply = QPushButton("立即生效")
        self.btn_apply.setAccessibleName("立即生效保存目标价")
        self.btn_apply.setObjectName("ok")
        # 与「替换股票」弹窗按钮完全同构：内容宽度、7px 圆角、7x16 内边距，
        # 右对齐排列；不再拉伸成两块占满整行的大按钮。
        for b in (self.btn_clear, self.btn_apply):
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.btn_clear.clicked.connect(self._clear)
        self.btn_apply.clicked.connect(self._apply)
        btns.addStretch()
        btns.addWidget(self.btn_clear)
        btns.addWidget(self.btn_apply)
        root.addLayout(btns)
        self._update_tab_order()

        # 两个编辑窗口统一走共享视觉令牌；目标价仅追加业务色和紧凑图标按钮规则。
        # 涨跌前景色单独拼一段再串进去：这一段带 %s，而整条 QSS 里其余规则都含字面
        # 大括号与十六进制色值，末尾统一 % 会被这些字符带偏（也读不清是哪处在插值）。
        up_dn_qss = "QLineEdit#up{color:%s;}QLineEdit#dn{color:%s;}" % (UP_RED, DOWN_GREEN)
        # 危险态（"再点一次确认清空"那颗红钮）三态一组，颜色同样从 sf_constants 取。
        # 与 up_dn_qss 同一形状单独拼：整条 QSS 末尾统一 % 会被其余规则里的
        # 字面大括号带偏（下面 262 行那条注释说的就是这件事）。
        danger_qss = (
            "QPushButton#cancel[confirmClear=\"true\"]{background:%s;color:%s;}"
            "QPushButton#cancel[confirmClear=\"true\"]:hover{background:%s;color:%s;}"
            "QPushButton#cancel[confirmClear=\"true\"]:pressed{background:%s;color:%s;}"
            % (DANGER_BG_HEX, FG_BRIGHT_HEX, DANGER_HOVER_HEX, FG_BRIGHT_HEX,
               DANGER_PRESSED_HEX, FG_BRIGHT_HEX))
        del_qss = (
            "QPushButton#del{background:rgba(255,255,255,0.10);color:%s;"
            "padding:0;font-size:11px;}"
            "QPushButton#del:hover{background:rgba(255,255,255,0.16);color:%s;}"
            "QPushButton#del:pressed{background:rgba(255,255,255,0.22);color:%s;}"
            "QPushButton#del:disabled{background:rgba(255,255,255,0.04);color:%s;}"
            % (FG_HINT_HEX, FG_BRIGHT_HEX, FG_BRIGHT_HEX, FG_DISABLED_HEX))
        self.setStyleSheet(popup_stylesheet(
            "QLineEdit{font-family:'D-DIN';font-size:%dpx;}" % PANEL_FONT_PX
            + up_dn_qss + danger_qss +
            "QPushButton#plus{background:rgba(255,255,255,0.10);padding:0;}"
            "QPushButton#plus:hover{background:rgba(255,255,255,0.16);}"
            "QPushButton#plus:pressed{background:rgba(255,255,255,0.22);}"
            "QPushButton#plus:disabled{background:rgba(255,255,255,0.04);}"
            + del_qss))

    def _update_tab_order(self):
        """按视觉顺序组织键盘焦点，列表删除按钮动态补入。"""
        chain = [self.btn_close_x, self.edit_up, self.btn_up, self.edit_dn, self.btn_dn]
        for w in self.findChildren(QPushButton):
            if w.objectName() == "del":
                chain.append(w)
        chain.extend([self.btn_clear, self.btn_apply])
        for first, second in zip(chain, chain[1:]):
            self.setTabOrder(first, second)

    # ---------- 数据 ----------
    @staticmethod
    def _as_slot(a):
        """把 config 里的一条 alert 规整为面板用的槽位（保留 state 用于显示）。

        v1.4.66 复核（2026-10-12）：诊断包 targets.json 把非 dict 脏数据按 invalid 计数，
        说明这种形状真实存在；而这里 `a.get(...)` 会直接 AttributeError 并从构造函数
        冒出去 —— 表现是"点了铃铛没反应"。按同一口径当 invalid 丢弃。
        """
        if not isinstance(a, dict):
            return None
        raw = a.get("target")
        if raw is None:        # 缺 target 与"target 非法"同判：旧实现 float(None) 也是落到 None
            return None
        try:
            return {"direction": a.get("direction"),
                    "target": float(raw),
                    "state": a.get("state")}
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _signature(pending, decimals=6):
        """面板内容的签名（方向 + 按当前品种精度归一的价位）。"""
        sig = []
        for a in pending:
            if not isinstance(a, dict):
                continue
            try:
                sig.append((str(a.get("direction")),
                            round(float(a.get("target")), decimals)))
            except (TypeError, ValueError):
                continue
        # R34：用 str 做排序键 —— direction 可能为 None（历史/手工编辑的脏配置），
        # 直接比较 None 与 str 会抛 TypeError，导致 has_unsaved_changes() 崩溃。
        return tuple(sorted(sig, key=lambda x: (x[0], x[1])))

    def _reload(self):
        """从 config 载入当前目标价，并记录磁盘签名。

        v1.2.81（P1-4）：保留同方向多条不同价位的条目；仅去重「完全相同的槽位」
        （方向 + 价位，历史/手工编辑遗留的脏重复），不再强制收敛为每方向一条。
        """
        try:
            # v1.4.59（S4 彻底解）：存储里现在同时留着别的股票的条目，
            # 面板只许显示这一只的 —— 否则切过来会看见别家的目标价。
            saved = load_alerts(self.code) or []
        except Exception as exc:
            # 配置损坏/正在被写入时读不到：按空列表处理，面板照常打开。
            # 但必须留痕：这个兜底同样会吞掉"签名对不上"这一类写错，而它的表现是
            # 「面板凭空没有目标价」—— 用户会以为目标价又丢了（S4 就是这条通道）。
            saved = []
            log_swallow("target_panel._reload", exc)
        slots, seen = [], set()
        for a in saved:
            s = self._as_slot(a)
            if s is None:
                continue
            key = (s.get("direction"),
                   round(float(s.get("target", 0)), self._price_decimals))
            if key in seen:
                continue
            seen.add(key)
            slots.append(s)
        self._pending = slots
        self._saved_sig = self._signature(slots, self._price_decimals)

    def has_unsaved_changes(self):
        """是否有尚未点「立即生效」的修改（主窗重开前据此提示，避免静默丢弃）。"""
        return self._signature(self._pending, self._price_decimals) != self._saved_sig

    def _mark_saved(self):
        """落盘成功后同步签名：否则 320ms 关闭延迟内重开面板会误报"有未生效修改"。"""
        self._saved_sig = self._signature(self._pending, self._price_decimals)

    def _rollback(self):
        """丢弃未生效修改，回到已保存状态（Esc / 外部重置时调用）。

        R34-7：Esc 的承诺是「关闭且不保存」。原先只调用 close()，_pending 仍
        留着未生效内容，下次打开面板会把这些"已丢弃"的条目重新显示出来、
        还提示「有未生效的修改」，与承诺矛盾。故关闭前显式回滚到磁盘状态。
        """
        try:
            self._disarm_clear()
            self._reload()
            self._refresh_list()
            self._set_status("", "info")
        except Exception as _swallow_exc:
            # 留痕理由：回滚失败=Esc/✕ 承诺的「丢弃未生效修改」被打破，脏数据会留下（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._rollback", _swallow_exc)

    def warn_unsaved(self):
        self._set_status("有未生效的修改，请先点「立即生效」", "warn")

    # ---------- 显示 ----------
    def _row_text_width(self):
        """单行文本可用宽度（减去删除按钮与间距），用于省略号截断。"""
        return self._panel_w - 2 * MARGIN_X - ROW_DEL_W - ROW_SPACING

    def _refresh_list(self):
        """重绘目标价列表：逐行 [方向+价位+距目标%] + [删除 ✕]。

        无条目时列表区域保持为空；极端输入（如 1e308）按行宽省略号截断，保证面板高度恒定。
        """
        # 清空现有行（可能是 widget，也可能是 sub-layout）
        while self.list_box.count():
            item = self.list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
                continue
            lay = item.layout()
            if lay is not None:
                while lay.count():
                    ci = lay.takeAt(0)
                    cw = ci.widget()
                    if cw is not None:
                        cw.deleteLater()

        if not self._pending:
            self._update_buttons()
            self._update_inputs()
            return

        price = self.current_price_fn() if self.current_price_fn else None
        show_pct = _valid_price(price)
        avail = self._row_text_width()
        for a in self._pending:
            op = "↑" if a.get("direction") == TARGET_DIR_ABOVE else "↓"
            try:
                t = float(a.get("target", 0))
            except (TypeError, ValueError):
                continue
            txt = ("%s %%.%df" % (op, self._price_decimals)) % t
            # P1-2：实时距目标价差%（价格可信时才显示）
            if show_pct:
                d = distance_pct(price, t)
                if d is not None:
                    txt += " (%+.1f%%)" % d
            if a.get("state") == STATE_HIT:
                # 标记放行首，见 HIT_MARKER_PREFIX 那条注释。触发点：2026-10-10 云端
                # 复检机上字体度量略宽，实测被切成「↑ 1600.00 (+0.0%) 已触…」，
                # 本机恰好放得下，所以这个边界从没在用户面前露出过。
                txt = HIT_MARKER_PREFIX + txt
            # 此处算一次方向，下方删除按钮的“涨到/跌到”文案复用同一结果
            is_up = a.get("direction") == TARGET_DIR_ABOVE
            col = UP_RED if is_up else DOWN_GREEN
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(ROW_SPACING)
            lab = QLabel(txt)
            # v1.2.87：按方向红/绿着色（与主界面涨红跌绿一致）；数字用 D-DIN
            lab.setStyleSheet("color:%s;font-size:%dpx;font-family:\"D-DIN\";" % (col, PANEL_FONT_PX))
            lab.setWordWrap(False)
            # 单行省略号截断：防极端数值把面板撑出屏幕
            fm = lab.fontMetrics()
            lab.setText(fm.elidedText(txt, Qt.TextElideMode.ElideRight, avail))
            row.addWidget(lab, 1)
            # P2-1：逐条删除（沿用主按钮风格 #del）
            del_btn = QPushButton("✕")
            del_btn.setAccessibleName("删除%s目标价 %.{}f".format(self._price_decimals) % ("涨到" if is_up else "跌到", t))
            del_btn.setObjectName("del")
            del_btn.setFixedWidth(ROW_DEL_W)
            direction = a.get("direction")
            target = t
            del_btn.clicked.connect(
                lambda _checked=False, d=direction, tg=target: self._remove_one(d, tg))
            row.addWidget(del_btn, 0)
            self.list_box.addLayout(row)
        self._update_tab_order()
        self._update_buttons()
        self._update_inputs()

    def _list_text(self):
        """汇总列表可见文本（测试用，避免依赖内部 QLabel 数量）。"""
        parts = []
        for i in range(self.list_box.count()):
            item = self.list_box.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, QLabel):
                parts.append(w.text())
                continue
            lay = item.layout() if item is not None else None
            if lay is not None:
                for j in range(lay.count()):
                    cw = lay.itemAt(j).widget()
                    if isinstance(cw, QLabel):
                        parts.append(cw.text())
        return "\n".join(parts)

    def _set_controls_enabled(self, enabled):
        """统一启用/禁用面板内**全部**可交互控件（含列表行内的逐条删除 ✕）。

        R46：M6 原实现只禁用了 4 个主控件，漏掉列表行内的 ✕ —— 延迟关闭期点它
        会走 _update_buttons() 把「立即生效」重新启用，用户得以二次落盘，
        把刚确认的内容再写一遍。改为按类型遍历，新增控件自动纳入。
        """
        try:
            for w in self.findChildren(QPushButton):
                w.setEnabled(enabled)
            for w in self.findChildren(QLineEdit):
                w.setEnabled(enabled)
        except Exception as _swallow_exc:
            # 留痕理由：按钮/输入框该禁不禁：用户能在无效状态下继续编辑（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._set_controls_enabled", _swallow_exc)

    def sync_alerts(self, alerts):
        """把后台（行情 tick / 过期清理）改动过的目标价同步到面板显示。

        R46：面板只在打开时载入一次快照，之后完全不感知后台变化。某条在面板
        打开期间被触发时，列表仍显示"待触发 + 距目标%"，用户会以为提醒没生效、
        重复添加同价位目标（虽被 merge 折叠，但状态显示与现实不符）。

        只回写已存在条目的状态与距目标%，**不增删条目**，避免覆盖用户尚未
        生效的编辑。
        """
        if self._locked:
            return
        live = [a for a in (alerts or []) if isinstance(a, dict)]
        changed = False
        for i, item in enumerate(self._pending):
            for l in live:
                if _same_slot(item, l):
                    if item.get("state") != l.get("state"):
                        self._pending[i] = dict(item, state=l.get("state"))
                        changed = True
                    break
        if changed:
            self._refresh_list()

    def show_calendar_sync_hint(self, status):
        """显示交易日历的非阻断状态；用户仍可继续编辑和保存目标价。"""
        if status == "syncing":
            self._set_status("交易日历正在后台同步，暂按周末规则判断", "info")
        elif status == "degraded":
            self._set_status("交易日历同步失败，暂按周末规则判断；将自动重试", "warn")

    def _set_status(self, text, kind="info"):
        try:
            self.status_label.setText(self._elide_for(self.status_label, text))
            self.status_label.setStyleSheet(STATUS_STYLE.get(kind, STATUS_STYLE["info"]))
        except Exception:
            pass   # 状态行只是提示：写不进去（控件已随面板销毁）不影响任何业务分支

    def _elide_for(self, label, txt):
        """按标签可用宽度做省略号截断（单行，保证面板高度恒定、不被超长数值撑爆）。"""
        try:
            w = label.width() or (self._panel_w - 2 * MARGIN_X)
            fm = label.fontMetrics()
            return fm.elidedText(txt, Qt.TextElideMode.ElideRight, w)
        except Exception:
            return txt

    def prepare_surface(self):
        prepare_popup_surface(self)

    def paintEvent(self, event):
        """v1.2.84：自定义绘制圆角背景，与主窗超椭圆 R=26 保持一致；
        v1.2.87：毛玻璃生效时仅压一层暗化露出 frosted 背景，否则降级为近不透明深蓝卡。"""
        self._glass_mask_flush()    # 同帧 flush 玻璃层圆角 mask（pending 时）
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 与替换股票窗口统一使用主窗 translucent 同款半透明黑材质。
            paint_popup_surface(self, p)
        finally:
            # v1.4.18 建议#6：paint_popup_surface 抛错也要收口 QPainter，
            # 否则活动 painter 漏在 backing store 上（主窗 sf_ui_render 已加固）。
            p.end()
        super().paintEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._sync_glass_frame(force_mask=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_mask()
        self._sync_glass_frame(force_mask=True)

    def _update_mask(self):
        """取消整数像素 QRegion 硬裁剪，圆角由抗锯齿绘制与透明背景承担。"""
        try:
            self.clearMask()
        except Exception:
            pass

    def _update_buttons(self):
        """列表非空时启用「清空」与「立即生效」。

        “已触发”只是展示状态，不是编辑权限：用户到达一个目标价后，仍需打开
        面板继续新增、删除或修改下一档目标。防止重复提醒由 merge_alerts 保留同槽位
        hit 状态负责，不能通过锁死整个保存入口实现。
        """
        enabled = bool(self._pending)
        self._apply_blocked = False
        try:
            self.btn_clear.setEnabled(enabled)
            self.btn_apply.setEnabled(enabled)
            self.btn_apply.setToolTip("")
        except Exception as _swallow_exc:
            # 留痕理由：应用/清空按钮的可用性没改成，点了没反应也查不到线索（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._update_buttons", _swallow_exc)

    def _update_inputs(self):
        """v1.2.84：每个方向达到 3 条后隐藏 + 按钮并禁用对应输入框。"""
        up = sum(1 for a in self._pending if a.get("direction") == TARGET_DIR_ABOVE)
        dn = sum(1 for a in self._pending if a.get("direction") == TARGET_DIR_BELOW)
        up_full = up >= MAX_PER_DIR
        dn_full = dn >= MAX_PER_DIR
        try:
            self.edit_up.setEnabled(not up_full)
            self.btn_up.setVisible(not up_full)
            self.edit_dn.setEnabled(not dn_full)
            self.btn_dn.setVisible(not dn_full)
        except Exception as _swallow_exc:
            # 留痕理由：上下限输入框的可用与可见状态没落到界面上（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._update_inputs", _swallow_exc)

    def _price_example(self):
        return "1500." + ("0" * self._price_decimals)

    def _friendly_message(self, txt, reason):
        """把 validate_alert 的技术原因翻译成用户语言，并匹配当前品种精度。"""
        if not txt:
            return "请输入目标价，例如 %s" % self._price_example()
        if "必须大于 0" in reason:
            return "价格必须大于 0"
        return "请输入有效价格，例如 %s" % self._price_example()

    def _set_clear_confirm_visual(self, armed):
        """确认清空态：红色填充、白色文字；退出确认后恢复灰色。"""
        try:
            self.btn_clear.setProperty("confirmClear", bool(armed))
            self.btn_clear.style().unpolish(self.btn_clear)
            self.btn_clear.style().polish(self.btn_clear)
            self.btn_clear.update()
        except Exception as _swallow_exc:
            # 留痕理由：二次确认视觉态没切过去：看不出「再点一次才真的清空」（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._set_clear_confirm_visual", _swallow_exc)

    def _disarm_clear(self):
        """取消「清空」二次确认态。"""
        if self._clear_armed:
            self._clear_armed = False
            try:
                self.btn_clear.setText("清空")
                self._set_clear_confirm_visual(False)
            except Exception as _swallow_exc:
                # 留痕理由：确认态没解除，按钮停在「确认清空」，存在误清空风险（AGENTS §6 第 9 条：兜异常不许静默）
                log_swallow("target_panel._disarm_clear", _swallow_exc)

    # ---------- 交互 ----------
    # NFKC 不会把数学减号 U+2212（−）等转成 ASCII '-'，而从网页/文档复制
    # "−5%" 时拿到的大概率是它 —— 用户看到的和 ASCII 减号几乎一样，却会提示
    # 格式不正确。这里在 NFKC 之后再做一次显式替换。
    _DASH_CHARS = ("\u2212", "\u2010", "\u2011", "\u2012", "\u2013",
                   "\u2014", "\u2015", "\u2043", "\ufe63", "\uff0d")

    @classmethod
    def _normalize_numeric_text(cls, raw):
        """把各种全角/数学符号归一成 ASCII 后再解析。"""
        t = unicodedata.normalize("NFKC", raw or "")
        for ch in cls._DASH_CHARS:
            t = t.replace(ch, "-")
        return t.strip()

    def _resolve_target(self, direction, txt):
        """解析输入文本为目标价（P1-3：支持百分比/相对涨跌幅快捷输入）。

        返回 (target, error)；解析失败 error 非空、target 为 None。
        """
        # 中文输入法可能写入全角数字、全角小数点或全角正负号；显示几乎一样，
        # 但 Python float 无法解析，用户会看到“明明是 64.50 仍提示无效”。
        # 统一正规化为半角后再走现有绝对价/百分比校验，避免改变任何价格规则。
        t = self._normalize_numeric_text(txt)
        if not t:
            return None, "请输入目标价，例如 %s" % self._price_example()
        # 百分比：N% / +N% / -N%
        if t.endswith("%"):
            body = t[:-1].strip()
            try:
                pct = float(body)
            except (TypeError, ValueError):
                return None, "百分比格式不正确，例如 +5%"
            return self._apply_pct(pct)
        # 符号前缀：+N / -N 视作相对涨跌幅（百分比）
        if t[0] in "+-":
            try:
                pct = float(t)
            except (TypeError, ValueError):
                return self._resolve_absolute(direction, t)
            return self._apply_pct(pct)
        return self._resolve_absolute(direction, t)

    def _apply_pct(self, pct):
        """相对涨跌幅 → 绝对目标价（基于当前价）。"""
        price = self.current_price_fn() if self.current_price_fn else None
        if not _valid_price(price):
            return None, "无法获取当前价，请输入具体价格"
        target = round(float(price) * (1.0 + pct / 100.0), self._price_decimals)
        # M7：必须走 _valid_price（有限正数），不能只判 `target <= 0`。
        # 输入 "nan%" / "inf%" / "1e309%" 时 float() 解析成功，得到 nan/inf；
        # 而 nan <= 0 恒为 False，旧判断直接放行 → 落盘成 NaN/Infinity
        # （非标准 JSON），该条在 _valid_alert 里恒判非法 → 永不触发、
        # 永久占位，铃铛一直亮着，只能手改配置文件清除。
        if not _valid_price(target):
            return None, "计算后的价格无效，请输入具体价格"
        return target, ""

    def _resolve_absolute(self, direction, t):
        ok, reason = validate_alert(direction, t)
        if not ok:
            return None, self._friendly_message(t, reason)
        return round(float(t), self._price_decimals), ""

    def _check_limits(self, direction, target):
        """v1.2.87：目标价不得越过当日涨停价 / 跌停价。

        返回非空字符串表示越界（作为错误提示）；None / 空表示通过。
        limits_fn 未提供（如离线测试）时直接放行。
        """
        if self._limits_fn is None:
            return ""
        try:
            lim = self._limits_fn()
        except Exception as exc:
            # M8：原来是静默 return ""，面板"看起来有校验"实际一点没拦，
            # 且失败不可观测。补一条日志，便于区分"没取到"与"代码写错"。
            log_line("target limits_fn error: %r" % (exc,), "WARNING")
            return ""
        if not isinstance(lim, (tuple, list)) or len(lim) < 2:
            return ""
        up, dn = lim[0], lim[1]
        if direction == TARGET_DIR_ABOVE and _valid_price(up) and target > float(up):
            return ("「涨到」价不可超过涨停价 %%.%df" % self._price_decimals) % float(up)
        if direction == TARGET_DIR_BELOW and _valid_price(dn) and target < float(dn):
            return ("「跌到」价不可低于跌停价 %%.%df" % self._price_decimals) % float(dn)
        return ""

    def _add(self, direction):
        if self._locked:
            return
        edit = self.edit_up if direction == TARGET_DIR_ABOVE else self.edit_dn
        txt = edit.text().strip()
        target, err = self._resolve_target(direction, txt)
        if target is None:
            self._set_status(err, "error")
            return
        # v1.2.84：涨到必须高于现价、跌到必须低于现价，否则禁止添加
        if self.current_price_fn is not None:
            try:
                cur = float(self.current_price_fn())
            except (TypeError, ValueError):
                cur = None
            if _valid_price(cur):
                if direction == TARGET_DIR_ABOVE and target <= cur:
                    self._set_status(
                        ("「涨到」价需高于当前价 %%.%df" % self._price_decimals) % cur,
                        "error")
                    return
                if direction == TARGET_DIR_BELOW and target >= cur:
                    self._set_status(
                        ("「跌到」价需低于当前价 %%.%df" % self._price_decimals) % cur,
                        "error")
                    return
        # v1.2.87：目标价不得越过当日涨停价 / 跌停价
        lim_err = self._check_limits(direction, target)
        if lim_err:
            self._set_status(lim_err, "error")
            return
        self._disarm_clear()
        # v1.2.84：每个方向最多 3 条
        if sum(1 for a in self._pending if a.get("direction") == direction) >= MAX_PER_DIR:
            self._set_status("每个方向最多 3 个目标价", "warn")
            return
        # P1-4：同槽位（同方向 + 同价位）拒绝重复；同方向不同价位则追加（不覆盖）
        for a in self._pending:
            if (a.get("direction") == direction
                    and round(float(a.get("target", 0)), self._price_decimals) == target):
                if a.get("state") == STATE_HIT:
                    self._set_status(
                        "该目标价今日已触发，请换个价格或等 15:00 后自动清空", "warn")
                else:
                    self._set_status("该目标价已存在", "warn")
                return
        # 注意：此处不调用 make_alert，避免把生效日冻结在"添加"时刻（D17）
        self._pending.append({"direction": direction, "target": target,
                              "state": STATE_PENDING})
        edit.clear()
        self._refresh_list()

    def _remove_one(self, direction, target):
        """P2-1：删除单条目标价（仅改内存，需点「立即生效」落盘）。"""
        if self._locked:
            return
        before = len(self._pending)
        self._pending = [a for a in self._pending
                         if not (a.get("direction") == direction
                                 and round(float(a.get("target", 0)), self._price_decimals)
                                 == round(float(target), self._price_decimals))]
        if len(self._pending) == before:
            return
        self._disarm_clear()
        self._refresh_list()
        self._set_status("", "info")

    def _apply(self):
        if self._locked:
            return
        if not self._pending:
            self._set_status("", "info")
            return
        # D25：股票已切换则拒绝保存，避免把旧股目标价写到新股上
        if self.current_code_fn is not None:
            try:
                cur = self.current_code_fn()
            except Exception:
                cur = None
            if cur and cur != self.code:
                self._set_status("已切换股票，请重新设置", "error")
                return
        self._disarm_clear()
        try:
            # D17：生效日以"真正生效这一刻"计算
            desired = [make_alert(a.get("direction"), a.get("target"))
                       for a in self._pending]
            ok = self.on_apply(desired)
        except Exception as exc:
            log_line("target panel apply error: %r" % (exc,), "WARNING")
            self._set_status("保存失败，请重试", "error")
            return
        if not ok:
            # D15：保存失败要有明确提示，且保持面板打开
            self._set_status("保存失败，请重试", "error")
            return
        self._mark_saved()
        # 保存成功即关闭：不显示“已生效”或通知授权状态提示，避免成功反馈
        # 停留在编辑弹窗内干扰操作。保存失败、切股等阻断场景仍保持原有提示并留在面板。
        self.close()

    def _finish_and_close(self, delay_ms):
        """M6：保存/清空成功后先进入「已完成」只读态，再延迟关闭。

        原实现只改状态行就 QTimer.singleShot(close)，面板在 320~1600ms 的延迟里
        仍完全可交互：可重复点「立即生效」造成重复落盘，也能继续增删。
        另外 singleShot 是一次性顶层定时器，无法取消 —— 面板若被提前关闭
        （切股、Esc、点 ✕），定时器仍会触发 self.close()。
        改为持有实例成员，并在 closeEvent 里 stop()。
        """
        # R46：置只读锁定态，并禁用**所有**可交互控件（含列表行内 ✕），
        # 否则点击行内删除会走 _update_buttons() 把「立即生效」重新启用。
        self._locked = True
        self._set_controls_enabled(False)
        self._stop_close_timer()
        try:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self.close)
            t.start(int(delay_ms))
            self._close_timer = t
        except Exception:
            self._close_timer = None

    def _stop_close_timer(self):
        """停掉并丢弃延迟关闭定时器（面板被提前关闭时调用）。"""
        t = getattr(self, "_close_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        self._close_timer = None

    @staticmethod
    def _notify_blocked():
        """系统通知是否已关闭（授权 denied 或 UN 不可用）。"""
        try:
            import sf_notify
            return sf_notify.authorization_status() == "denied"
        except Exception:
            return False

    def _clear(self):
        if self._locked:
            return
        if not self._pending:
            self._set_status("", "info")
            return
        # D3：二次确认，避免误触即清空
        if not self._clear_armed:
            self._clear_armed = True
            self.btn_clear.setText("确认清空")
            self._set_clear_confirm_visual(True)
            self._set_status("再点一次「确认清空」将删除全部目标价", "warn")
            return
        # 先落盘、成功后再动内存与界面：否则落盘失败会出现
        # 「界面已空、磁盘还在」的不一致，且因 _pending 已空而无法重试。
        try:
            ok = self.on_clear() if self.on_clear else True
        except Exception as exc:
            log_line("target panel clear error: %r" % (exc,), "WARNING")
            self._set_status("清空失败，请重试", "error")
            return
        if ok is False:
            # 保持原样且保留「确认清空」态，用户可直接再点一次重试
            self._set_status("清空失败，请重试", "error")
            return
        self._pending = []
        self.edit_up.clear()
        self.edit_dn.clear()
        self._disarm_clear()
        self._mark_saved()
        self._refresh_list()
        self._set_status("", "info")
        # 清空后保持面板打开；空列表本身已足够表达结果，不再重复显示成功提示。
        try:
            self.edit_up.setEnabled(True)
            self.edit_dn.setEnabled(True)
            self.edit_up.setFocus()
        except Exception as _swallow_exc:
            # 留痕理由：清空后输入框状态/焦点没恢复，面板像卡住（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._clear", _swallow_exc)

    # ---------- 键盘 / 显示事件 ----------
    def keyPressEvent(self, event):
        """D2：Esc 关闭且不保存（丢弃未生效修改）。

        R34-7：必须真的丢弃 —— 先回滚到已保存状态再关闭，否则下次打开
        面板会看到"已丢弃"的条目并被提示"有未生效的修改"。
        """
        try:
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self._rollback()
                self.close()
                return
        except Exception as _swallow_exc:
            log_swallow("target_panel.keyPressEvent", _swallow_exc)
        super().keyPressEvent(event)

    def _on_close_x(self):
        """右上角关闭按钮：与 Esc 等价 —— 关闭并丢弃未生效修改。"""
        try:
            self._rollback()
        except Exception as _swallow_exc:
            # 留痕理由：点 ✕ 时整段回滚没执行，未生效修改会残留（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel._on_close_x", _swallow_exc)
        self.close()

    def closeEvent(self, event):
        """v1.2.87：关闭时回收毛玻璃 NSWindow，避免原生窗口泄漏。

        M6：同时停掉延迟关闭定时器 —— 否则面板被提前关闭（切股/Esc/✕）后，
        定时器仍会触发一次 self.close()。
        """
        try:
            self._stop_close_timer()
        except Exception:
            pass
        try:
            self._close_glass()
        except Exception:
            pass
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # v1.2.87：首帧后延迟注入毛玻璃（复用 GlassMixin，非 macOS 自动降级）
        if getattr(self, "_glass_pending", False):
            self._glass_pending = False
            QTimer.singleShot(0, self._inject_glass)
        try:
            # 布局此时才有真实宽度，重刷一次让省略号截断按实际宽度计算
            self._refresh_list()
            self._update_mask()
        except Exception as _swallow_exc:
            # 留痕理由：布局就绪后的重刷没做完：列表不刷新、省略号按错宽度截断（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel.showEvent", _swallow_exc)
        # v1.2.87 补丁（S2）：**始终聚焦输入框**，不再区分交易时段。
        #
        # 原实现在非交易时段把焦点给「+」按钮（btn_up）。但按钮拿到焦点等于
        # 抢走输入框的键盘通道：用户直接敲 1600 一个字符都进不去，按空格还会
        # 激活「+」而输入框是空的 → 报"请输入目标价"，体感是"输入框坏了"。
        # 而午休/收盘后/周末恰是设价最频繁的时段，影响面很大。
        # 引导作用改由输入框提示文案承担，不再用焦点表达。
        #
        # 兜底必须落在输入框上：若放任 setFocus 失败/抛异常，焦点会回落到创建
        # 顺序上第一个可聚焦控件 —— 即右上角「✕」，此时按空格/回车直接关窗并
        # 丢弃全部未生效输入。
        try:
            target = self.edit_up if self.edit_up.isEnabled() else self.edit_dn
            target.setFocus()
        except Exception:
            try:
                self.edit_up.setFocus()
            except Exception as _swallow_exc:
                # 留痕理由：布局就绪后的重刷没做完：列表不刷新、省略号按错宽度截断（AGENTS §6 第 9 条：兜异常不许静默）
                log_swallow("target_panel.showEvent", _swallow_exc)
        except Exception as _swallow_exc:
            # 留痕理由：布局就绪后的重刷没做完：列表不刷新、省略号按错宽度截断（AGENTS §6 第 9 条：兜异常不许静默）
            log_swallow("target_panel.showEvent", _swallow_exc)
