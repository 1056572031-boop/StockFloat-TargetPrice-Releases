# -*- coding: utf-8 -*-
"""实时股价 v1.2.0 界面显示层渲染测试（17.0 新增，补齐"显示层"空白）。

覆盖：
  1) 涨停/跌停/停牌三种状态徽标真实渲染为对应色块（像素采样断言，非宽度数值）；
  2) _trend_color 涨/跌/平 趋势色逻辑（A股红涨绿跌）；
  3) 断网界面：名称栏"网络异常"文案（拦截 QPainter.drawText）+ _last_ok_time 记录；
  4) 保存失败兜底：写文件/替换失败时 save_config 不抛异常、不崩溃。

关键点：
  - 本测试在真实 GUI 会话（cocoa）下运行；QT_QPA_PLATFORM=offscreen 的字体 fallback
    与真机不同，断言一律用像素采样而非字体宽度数值。
  - 徽标为 34x12 实心圆角矩形（v1.2.72 起），像素远多于文字/折线等细元素：用「匹配
    像素数 + 单行最大连续段」双重阈值区分"色块"与"文字细线"，确保徽标真的画出来了。

运行方式（在 StockFloat 目录）：
    python3 -m unittest discover -s tests -v
"""
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QColor, QImage, QPainter, QFontInfo, QFontMetrics, QFont
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QApplication

import stock_float_window as m
import sf_config
import sf_quote
import sf_ui_render

try:
    from _base import SFCase                  # noqa: E402
except ImportError:                           # 以包形式被 discover 导入时
    from tests._base import SFCase            # noqa: E402


# ---- 行情数据构造：三态 quote（价格/涨跌幅为字符串，_apply_quote 依赖该形态） ----
def _quote(**over):
    q = {
        "name": "贵州茅台",
        "code": "600519",
        "price": "403.50",
        "pct": "0.00",
        "change": "0.00",
        "pre_close": "403.50",
        "high": "403.50",
        "low": "403.50",
        "volume": "12345",
        "limit_up": "443.85",
        "limit_down": "363.15",
    }
    q.update(over)
    return q


QUOTE_LIMIT_UP = _quote(price="443.85", pct="10.00", change="40.35", high="443.85")
QUOTE_LIMIT_DOWN = _quote(price="363.15", pct="-10.00", change="-40.35", low="363.15")
QUOTE_SUSPENDED = _quote(price="0", pct="0.00", change="0.00", volume="0")
# 停牌 + 价格取不到（显示 "--"）：v1.4.62 当时它**不能**当徽标用例的夹具 ——
# 降级分支把价格带顶当涨跌幅顶传给 badge_vertical，恒返回 None，于是那条支路
# 一次都没画出过徽标（台账 S49）。v1.4.64 修完之后它成了那一条的夹具，
# 见 test_suspended_badge_renders_in_degraded_price。
# 停牌 + 挂着昨收价继续展示的真实组合仍是下面这一枚。
QUOTE_SUSPENDED_TRADING = _quote(price="10.00", pct="0.00", change="0.00", volume="0")
QUOTE_NORMAL = _quote(price="403.50", pct="0.00", change="0.00")   # 平盘，用于"无徽标"基线


class TestMaterialBellPath(unittest.TestCase):
    """用户指定 SVG 轮廓必须等比映射进原 12×12 图标框。"""

    def test_path_uses_existing_icon_box_and_material_proportions(self):
        rect = QRectF(10, 11, sf_ui_render.BTN_ICON_SZ, sf_ui_render.BTN_ICON_SZ)
        bounds = sf_ui_render.material_bell_path(rect).boundingRect()
        self.assertAlmostEqual(bounds.center().x(), rect.center().x(), delta=0.01)
        self.assertAlmostEqual(bounds.center().y(), rect.center().y(), delta=0.01)
        # V1.3.12：仅缩小铃铛轮廓以对齐右侧三图标视觉重量，中心/热区不变。
        self.assertAlmostEqual(bounds.width(), 6.72, delta=0.05)
        self.assertAlmostEqual(bounds.height(), 8.40, delta=0.05)
        self.assertLessEqual(bounds.width(), rect.width())
        self.assertLessEqual(bounds.height(), rect.height())


def assert_solid_block_found(img, color, expect_w, expect_h, label, tol=30,
                             min_span_x=0.80, min_span_y=0.70, min_fill=0.50):
    """在整图里找到 color 的**最大实心块**，按相对量断言它确实是徽标（位置无关）。

    旧判据两处都在说谎，v1.4.62 一起更正：
      ① 整图扫描取「最大连续段」量到的是徽标内文字两侧的背景缝（内边距仅
         BADGE_PAD_X=4px），云端 runner（macOS 26.6.2、1x 屏）字形略宽就把
         run 压到 5 而误报——徽标本身画得好好的；本地则相反，缝宽 10 勉强过线，
         全凭运气。
      ② 更早的问题：整图 count 也全是噪点——FG_DIM 系的灰色文字元素在任何
         一帧里都有几百个像素（实测 809，徽标自身只有 312px），「count ≥ 150」
         从来没验证过徽标。停牌用例旧夹具（price="0"）走「--」降级路径，
         badge_vertical(Y_PRICE) 返回 None，徽标**根本不会被画出来**，用例
         照样绿——假门一枚。
    现改为：8 连通找最大匹配簇（徽标是全图最大的该色实心块；灰字、细线都
    是小簇），断言三个相对量——宽/高铺展 ≥ 期望徽标的 80%/70%（期望值用与
    产品相同的 badge_width/字体度量算出），簇内填充率 ≥ 50%（白字居中、
    圆角、抗锯齿都算扣减，描边/细线达不到）。全部成比例缩放，1x/2x 屏同判，
    位置无关。
    """
    dpr = img.devicePixelRatio() or 1.0
    w_img, h_img = img.width(), img.height()
    rgb = (color.red(), color.green(), color.blue())

    def _match(x, y):
        c = img.pixelColor(x, y)
        return (abs(c.red() - rgb[0]) <= tol and abs(c.green() - rgb[1]) <= tol
                and abs(c.blue() - rgb[2]) <= tol)

    seen = bytearray(w_img * h_img)
    best = None
    for y0 in range(h_img):
        base = y0 * w_img
        for x0 in range(w_img):
            idx = base + x0
            if seen[idx] or not _match(x0, y0):
                continue
            stack = [(x0, y0)]
            seen[idx] = 1
            n = 0
            xmn = xmx = x0
            ymn = ymx = y0
            while stack:
                x, y = stack.pop()
                n += 1
                if x < xmn:
                    xmn = x
                elif x > xmx:
                    xmx = x
                if y < ymn:
                    ymn = y
                elif y > ymx:
                    ymx = y
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1),
                               (x - 1, y - 1), (x + 1, y - 1),
                               (x - 1, y + 1), (x + 1, y + 1)):
                    if 0 <= nx < w_img and 0 <= ny < h_img:
                        j = ny * w_img + nx
                        if not seen[j] and _match(nx, ny):
                            seen[j] = 1
                            stack.append((nx, ny))
            if best is None or n > best[0]:
                best = (n, xmn, xmx, ymn, ymx)
    if best is None:
        raise AssertionError("%s: 整图没有任何匹配像素，徽标未绘制" % label)
    n, xmn, xmx, ymn, ymx = best
    span_x = (xmx - xmn + 1) / dpr
    span_y = (ymx - ymn + 1) / dpr
    fill = n / max(1, (xmx - xmn + 1) * (ymx - ymn + 1))
    if span_x < min_span_x * expect_w:
        raise AssertionError(
            "%s: 最大匹配簇横向只铺到 %.1f/%.1f px（<%d%%），疑似没画徽标或画小了"
            % (label, span_x, expect_w, int(min_span_x * 100)))
    if span_y < min_span_y * expect_h:
        raise AssertionError(
            "%s: 最大匹配簇纵向只铺到 %.1f/%.1f px（<%d%%），疑似一条细线而非色块"
            % (label, span_y, expect_h, int(min_span_y * 100)))
    if fill < min_fill:
        raise AssertionError(
            "%s: 匹配簇实心度 %.2f < %.2f（bbox %dx%d 内只有 %d px），疑似描边"
            % (label, fill, min_fill, xmx - xmn + 1, ymx - ymn + 1, n))


class TestRenderBase(unittest.TestCase):
    """公共夹具：与 test_ui.py 相同的 patch 模式，构造 FloatWindow 后停掉后台线程。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        # 度量前提（v1.4.62）：本模块的像素断言全部假设**产品启动后**的字体环境
        # ——正式版启动必调 load_bundled_fonts()，而测试进程里此前靠「全量套件中
        # 恰好有别的模块先注册过」这个副作用。cocoa 冒烟子集（CI 只跑
        # test_smoke/test_render/test_quit_lifecycle）没有这个副作用，云端 runner
        # 上 D-DIN 解析不到，度量整体漂到兜底字体。这里显式注册并当场验证解析
        # 成功；不成立就大声停，不许悄悄退化成一套假数字。
        sf_ui_render.load_bundled_fonts()
        fam = QFontInfo(QFont(sf_ui_render.NUM_FONT, 12)).family()
        if fam != sf_ui_render.NUM_FONT:
            raise AssertionError(
                "测试前提不成立：内置字体 %s 注册后仍解析到「%s」而不是它自己。"
                "本模块所有像素断言都假设产品字体环境，继续跑只会量出假数字。"
                % (sf_ui_render.NUM_FONT, fam))

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_render_")
        self._patches = [
            patch.object(m, "load_config",
                         return_value=("sh600519", 100, 100, 200, "translucent")),
            patch.object(m, "load_api_urls", lambda: None),
            patch.object(m, "save_config", lambda *a, **k: None),
            patch.object(sf_quote, "http_get", lambda *a, **k: ""),
            patch.object(m, "start_icon_worker", lambda: None),
            patch.object(m.FloatWindow, "_force_top", lambda self: None),
            patch.object(m.FloatWindow, "_setup_reopen_handler", lambda self: None),
            patch("PyQt6.QtWidgets.QMenu.popup", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.win = m.FloatWindow()
        # 停掉后台行情/分时线程与定时器，保证测试进程可退出、无真实网络
        self.win.running = False
        self.win._wake.set()
        self.win.timer.stop()
        self.win.top_timer.stop()
        self.win._anim_timer.stop()
        self.win._flash_timer.stop()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()
        for p in self._patches:
            p.stop()

    # ---- 渲染辅助 ----
    def _inject_and_render(self, quote):
        """走真实 _apply_quote 注入 quote（同时记录 _last_ok_time），强制重绘并抓帧。"""
        self.win._apply_quote(quote)
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()
        pm = self.win.grab()
        QApplication.processEvents()
        return pm.toImage()

    def _badge_rect(self):
        """按布局常量计算徽标逻辑像素区域 (x, y, w, h)，与 paintEvent 绘制位置一致。

        v1.2.72 样式调整：徽标左对齐涨跌幅区域（非居中）。这里用停牌
        (fallback) 路径的确定性坐标——价格区显示 '--' 时徽标所在区域，也是窗口
        最右侧、最易与价格/涨跌幅误判的区域，作为"无徽标"负向检查的代表区域。
        """
        w = self.win.width()
        fm = QFontMetrics(QFont(sf_ui_render.NUM_FONT, sf_ui_render.FONT_PRICE_MAX,
                                QFont.Weight.Bold))
        text_w = fm.horizontalAdvance("--")
        pct_x_fb = sf_ui_render.PAD_X + text_w + sf_ui_render.PCT_GAP
        max_pct_x = w - sf_ui_render.PRICE_ROW_RIGHT_PAD
        pct_avail_fb = max(0, max_pct_x - pct_x_fb)
        bx = pct_x_fb   # 左对齐涨跌幅区域
        by = sf_ui_render.Y_PRICE - sf_ui_render.STATUS_BADGE_H - sf_ui_render.BADGE_ABOVE_PCT_GAP
        # v1.2.72：徽标宽度走 R.badge_width 唯一实现（常量 STATUS_BADGE_W 已删除 ——
        # 布局按它判定、绘制按文字算实际宽，同一物理量两份定义会让徽标被误丢弃）。
        bfm = QFontMetrics(QFont(sf_ui_render.FONT, sf_ui_render.BADGE_FONT_SIZE,
                                 QFont.Weight.Light))
        bw = sf_ui_render.badge_width("停牌", bfm)
        return (bx, by, bw, sf_ui_render.STATUS_BADGE_H)

    def _count_in_region(self, img, rect, rgb, tol=30):
        """统计指定逻辑矩形区域（按 devicePixelRatio 换算）内匹配色像素数。"""
        dpr = img.devicePixelRatio()
        x0 = max(int(rect[0] * dpr), 0)
        y0 = max(int(rect[1] * dpr), 0)
        x1 = min(int((rect[0] + rect[2]) * dpr), img.width())
        y1 = min(int((rect[1] + rect[3]) * dpr), img.height())
        count = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                c = img.pixelColor(x, y)
                if (abs(c.red() - rgb[0]) <= tol and abs(c.green() - rgb[1]) <= tol
                        and abs(c.blue() - rgb[2]) <= tol):
                    count += 1
        return count


class TestRenderBadges(TestRenderBase):
    """涨停/跌停/停牌徽标真实渲染色块断言。

    v1.3.53：涨跌幅改为相对价格垂直居中，徽标随之上移约 4px。
    v1.4.62：三个用例全部改走 assert_solid_block_found（整图找最大实心簇 +
    相对量断言）。原因：旧判据的 min_px/count 在整图上量到的主要是 FG_DIM 系
    文字噪点（实测 809 px，徽标自身仅 312px），min_run 量到的是徽标内文字两侧
    仅 4px 宽的背景缝 —— 前者从未真正验证过徽标，后者换台机器就翻红（云端
    runner 实测 run=5 误报）。
    """

    @staticmethod
    def _muted(color):
        """与生产代码一致：标签背景色 = trend_color.darker(140)。"""
        return color.darker(140)

    @staticmethod
    def _badge_dims(badge):
        """期望徽标尺寸 (w, h)：宽走产品唯一实现 badge_width，高走布局常量。

        字体度量来自**本进程已注册**的内置字体环境（TestRenderBase.setUpClass
        有前提断言），与产品绘制同源——期望值跟着字体走，判据不用写死像素。
        """
        fm = QFontMetrics(QFont(sf_ui_render.FONT, sf_ui_render.BADGE_FONT_SIZE,
                                QFont.Weight.Light))
        return (sf_ui_render.badge_width(badge, fm), sf_ui_render.STATUS_BADGE_H)

    def test_limit_up_badge_renders_red_block(self):
        """涨停：涨跌幅正上方渲染红色徽标色块（RED 压暗版）。"""
        img = self._inject_and_render(QUOTE_LIMIT_UP)
        assert_solid_block_found(img, self._muted(m.RED),
                                 *self._badge_dims("涨停"), "涨停徽标")

    def test_limit_down_badge_renders_green_block(self):
        """跌停：空间充足时在涨跌幅正上方渲染绿色徽标色块。"""
        # 160px 窄版按既有降级优先级允许隐藏徽标，避免挤压价格和涨跌幅；
        # 此处用 200px 验证徽标绘制能力本身未被宽度调整破坏。
        self.win.setFixedWidth(200)
        img = self._inject_and_render(QUOTE_LIMIT_DOWN)
        assert_solid_block_found(img, self._muted(m.GREEN),
                                 *self._badge_dims("跌停"), "跌停徽标")

    def test_suspended_badge_renders_gray_block_in_trading(self):
        """停牌：交易时段涨跌幅正上方渲染灰色徽标色块（FG_DIM 压暗版）。

        v1.4.62 两处更正：
        ① 夹具从 QUOTE_SUSPENDED（price="0"，走「--」降级路径）换成
           QUOTE_SUSPENDED_TRADING（停牌 + 价格有效）：降级路径下
           badge_vertical(Y_PRICE) 返回 None，徽标**根本不会被画出来**，
           旧判据是绿在整图灰色文字噪点上的假门；真实场景是停牌股挂着
           昨收价继续交易展示。
        ② 判据改走 assert_solid_block_found（位置无关的相对量）。
        """
        with patch.object(sf_quote, "_is_trading_time", return_value=True):
            img = self._inject_and_render(QUOTE_SUSPENDED_TRADING)
        assert_solid_block_found(img, self._muted(m.FG_DIM),
                                 *self._badge_dims("停牌"), "停牌徽标")

    def test_suspended_badge_renders_in_degraded_price(self):
        """停牌 + 价格取不到（显示 "--"）：徽标必须照样画出来（台账 S49，v1.4.64 修）。

        v1.2.30 立这条就是为了让用户分得清"停牌"与"断网/加载中"，而 v1.3.42 起
        这条支路恒不绘制 —— 最需要标识的时刻恰好看不见，且全量一直是绿的
        （旧判据量整图灰色文字噪点，809 px ≫ 徽标 312 px，假门）。
        位置换了：降态没有涨跌幅行，徽标在价格带内垂直居中、左对齐涨跌幅区域。
        """
        # 停牌判定只在交易时段生效（sf_quote.quote_status 里那条），
        # 非交易时段的 price=0 是正常收盘态，不误报停牌 —— 所以夹具要注入时段。
        with patch.object(sf_quote, "_is_trading_time", return_value=True):
            img = self._inject_and_render(QUOTE_SUSPENDED)
        assert_solid_block_found(img, self._muted(m.FG_DIM),
                                 *self._badge_dims("停牌"), "降级态停牌徽标")

    def test_degraded_badge_does_not_touch_the_two_rows_around_it(self):
        """徽标画在价格带里，不许压到代码行底或涨跌幅行顶（真渲染取像素）。"""
        top = sf_ui_render.badge_in_band_top(sf_ui_render.Y_PRICE,
                                             sf_ui_render.PRICE_BAND_H)
        self.assertIsNotNone(top, "降级态位置算不出来，上一条用例就没有意义")
        self.assertGreater(top, sf_ui_render.Y_CODE + sf_ui_render.H_CODE,
                           "徽标顶边压进代码行了")
        self.assertLess(top + sf_ui_render.STATUS_BADGE_H - 1, sf_ui_render.Y_PCT,
                        "徽标底边压进涨跌幅行了")

    def test_normal_quote_no_badge(self):
        """正常行情（平盘）：徽标区域内不渲染任何红/绿/灰实心色块"""
        img = self._inject_and_render(QUOTE_NORMAL)
        rect = self._badge_rect()
        for color, label in ((m.RED, "红"), (m.GREEN, "绿"), (m.FG_DIM, "灰")):
            count = self._count_in_region(
                img, rect, (color.red(), color.green(), color.blue()))
            self.assertLess(
                count, 60,
                "%s色在徽标区域出现 %d 像素，正常行情不应有徽标" % (label, count))

    def test_trend_color_up_down_flat(self):
        """_trend_color：涨→RED / 跌→GREEN / 平→FG_NAME（A股红涨绿跌）。

        说明：生产代码 v1.2.0 平盘返回 FG_NAME(#ffffff) 而非 FG_DIM，
        这里按生产真实行为断言，不改生产代码。
        """
        self.win.quote = QUOTE_LIMIT_UP
        self.assertEqual(self.win._trend_color(), m.RED)
        self.win.quote = QUOTE_LIMIT_DOWN
        self.assertEqual(self.win._trend_color(), m.GREEN)
        self.win.quote = QUOTE_NORMAL
        self.assertEqual(self.win._trend_color(), sf_ui_render.FG_NAME)


class TestBadgeBlockJudge(SFCase):
    """assert_solid_block_found 自身可证伪：不碰产品、不开窗口，用合成图喂判据。

    「闸门不许说谎」这一族的基本功——如果这个判据怎么画都绿，它守的就不是徽标。
    全部用 setPixel 逐像素作画，不经过字体，任何平台上都 deterministic：
      全白 / 只描边 / 一条线 / 尺寸不足 → 红；
      实心块挖白字（真徽标形状）、画在画布任意位置 → 绿。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = cls.ensure_qapp()
        cls.color = sf_ui_render.FG_DIM.darker(140)
        # 期望尺寸与真实徽标同源（badge_width 唯一实现 + 布局常量）
        fm = QFontMetrics(QFont(sf_ui_render.FONT, sf_ui_render.BADGE_FONT_SIZE,
                                QFont.Weight.Light))
        cls.badge_w = sf_ui_render.badge_width("停牌", fm)
        cls.badge_h = sf_ui_render.STATUS_BADGE_H

    def _img(self, w, h, rgb):
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor(*rgb))
        return img

    def _rgb(self):
        return (self.color.red(), self.color.green(), self.color.blue())

    def _draw_block(self, img, ox, oy, hole=False):
        """在 (ox, oy) 画一块 badge_w×badge_h 实心色块；hole=True 挖中央白块。"""
        col = QColor(*self._rgb())
        for y in range(self.badge_h):
            for x in range(self.badge_w):
                if hole and 6 <= x < self.badge_w - 6 and 2 <= y < self.badge_h - 2:
                    continue
                img.setPixel(ox + x, oy + y, col.rgba())

    def test_blank_image_is_rejected(self):
        """什么都没画 → 判红（守「徽标未绘制」）。"""
        img = self._img(60, 40, (255, 255, 255))
        with self.assertRaises(AssertionError):
            assert_solid_block_found(img, self.color,
                                     self.badge_w, self.badge_h, "空白")

    def test_outline_only_is_rejected(self):
        """只画 1px 描边、内部空心 → 实心度判红（守「细线冒充实心块」）。"""
        img = self._img(60, 40, (255, 255, 255))
        col = QColor(*self._rgb())
        w, h = self.badge_w, self.badge_h
        for x in range(w):
            img.setPixel(x, 0, col.rgba())
            img.setPixel(x, h - 1, col.rgba())
        for y in range(h):
            img.setPixel(0, y, col.rgba())
            img.setPixel(w - 1, y, col.rgba())
        with self.assertRaises(AssertionError):
            assert_solid_block_found(img, self.color,
                                     self.badge_w, self.badge_h, "描边")

    def test_single_line_is_rejected(self):
        """只有一条 2px 横线 → 纵向铺展不足，必须判红。"""
        img = self._img(60, 40, (255, 255, 255))
        col = QColor(*self._rgb())
        for x in range(self.badge_w):
            img.setPixel(x, 20, col.rgba())
            img.setPixel(x, 21, col.rgba())
        with self.assertRaises(AssertionError):
            assert_solid_block_found(img, self.color,
                                     self.badge_w, self.badge_h, "细线")

    def test_undersized_block_is_rejected(self):
        """画了一块但只有一半宽 → 横向铺展判红（守「徽标被截短」）。"""
        img = self._img(60, 40, (255, 255, 255))
        col = QColor(*self._rgb())
        for y in range(self.badge_h):
            for x in range(self.badge_w // 2):
                img.setPixel(x, y, col.rgba())
        with self.assertRaises(AssertionError):
            assert_solid_block_found(img, self.color,
                                     self.badge_w, self.badge_h, "画小了")

    def test_solid_block_with_text_hole_passes(self):
        """实心块中央挖一块「白字」形状 → 必须绿（真徽标就是这个形状）。"""
        img = self._img(60, 40, (255, 255, 255))
        self._draw_block(img, 10, 14, hole=True)
        assert_solid_block_found(img, self.color,
                                 self.badge_w, self.badge_h, "实心带字")  # 不抛即绿

    def test_position_is_irrelevant(self):
        """色块画在画布任意位置都能找到（判据位置无关，跨机布局差异不误报）。"""
        img = self._img(80, 60, (255, 255, 255))
        self._draw_block(img, 40, 30, hole=True)
        assert_solid_block_found(img, self.color,
                                 self.badge_w, self.badge_h, "挪位")  # 不抛即绿


class TestFiredTargetPlacement(TestRenderBase):
    """到达目标价提示应固定在涨跌幅上方，而不是铃铛操作区。"""

    def test_fired_target_text_not_drawn_inside_main_card(self):
        """目标价提示改由铃铛系统气泡承载，主卡片内不得常驻绘制。"""
        import inspect
        src = inspect.getsource(sf_ui_render.RenderMixin)
        self.assertNotIn("_paint_fired_target", src)
        self.assertNotIn("_paint_bell_popup", src)

    def test_fired_target_does_not_hide_price_status_badge(self):
        """提示移出卡片后，涨停/跌停徽标不应再被目标价状态隐藏。"""
        import inspect
        src = inspect.getsource(self.win._paint_price)
        self.assertNotIn("badge = None if has_fired_target", src)


class TestMinuteStaleLabel(TestRenderBase):
    def test_label_draws_in_header_and_disappears_after_recovery(self):
        self.win._apply_quote(QUOTE_NORMAL)  # discipline-ok(R5): quote delivery is the production setup path
        image = QImage(self.win.width(), self.win.height(), QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        real = QPainter.drawText
        labels = []
        header_rects = []

        def record(p, *args):
            header_rects.append(QRectF(args[0]))
            if args[-1] == "分时滞后·重试中":
                labels.append(QRectF(args[0]))
            return real(p, *args)

        try:
            self.win._minute_stale = True  # discipline-ok(R5): inject the unavailable-minute state
            with patch.object(QPainter, "drawText", record):
                self.win._paint_header(painter, self.win.width())  # discipline-ok(R5): real header geometry
                pen_before, brush_before, font_before = painter.pen(), painter.brush(), painter.font()
                self.win._paint_stale_label(painter, self.win.width())  # discipline-ok(R5): real status label pixels
                self.assertEqual(painter.pen(), pen_before)
                self.assertEqual(painter.brush(), brush_before)
                self.assertEqual(painter.font(), font_before)
            self.assertEqual(len(labels), 1)
            rect = labels[0]
            self.assertGreaterEqual(rect.left(), sf_ui_render.PAD_X)
            self.assertEqual(rect.right(), self.win.width() - sf_ui_render.PAD_X)
            self.assertGreater(rect.top(), sf_ui_render.BTN_ICON_Y + sf_ui_render.BTN_ICON_SZ)
            self.assertLess(rect.bottom(), sf_ui_render.Y_PRICE)
            self.assertEqual(rect.center().y(), sf_ui_render.Y_NAME + sf_ui_render.NAME_TOP_PAD
                             + (sf_ui_render.H_NAME - sf_ui_render.NAME_TOP_PAD) / 2)
            self.assertLess(header_rects[0].right(), rect.left(),
                            "股票名称必须为右侧标签留出间距")
            self.assertTrue(any(image.pixelColor(x, y).alpha() > 0
                                for x in range(int(rect.left()), int(rect.right()))
                                for y in range(int(rect.top()), int(rect.bottom()))),
                            "标签必须实际渲染出像素")
            labels.clear()
            self.win._minute_stale = False  # discipline-ok(R5): inject successful recovery
            with patch.object(QPainter, "drawText", record):
                self.win._paint_stale_label(painter, self.win.width())  # discipline-ok(R5): recovery hides status label
            self.assertEqual(labels, [])
        finally:
            painter.end()


class TestRenderNetBad(TestRenderBase):
    """断网界面表现：名称栏"网络异常" + 最近成功时间。"""

    def _render_net_bad(self):
        """先注入正常行情（记录 _last_ok_time），再模拟断网渲染。"""
        self.win._apply_quote(QUOTE_NORMAL)
        self.win._net_ok = False
        self.win._need_redraw = True
        self.win.update()
        QApplication.processEvents()

    def test_network_bad_draws_name_text(self):
        """断网：绘制名称栏"网络异常"（拦截 QPainter.drawText 记录文本）"""
        texts = []

        def _draw_text(self_, *args, **kw):
            real(self_, *args, **kw)
            for a in args:
                if isinstance(a, str):
                    texts.append(a)

        real = m.QPainter.drawText
        self._render_net_bad()
        with patch.object(QPainter, "drawText", _draw_text):
            self.win._need_redraw = True
            # Observe a real synchronous render, not whether Cocoa happens to
            # dispatch a queued paint before one processEvents call returns.
            frame = self.win.grab()
            self.assertFalse(frame.isNull(), "网络异常状态必须有实际渲染帧")
        self.assertTrue(
            any("网络异常" in t for t in texts),
            "未绘制'网络异常'文案，drawText 文本: %r" % (texts,))
        self.assertNotIn("网…", texts, "160px 窄版仍应完整显示网络异常，不得只剩单字省略")

    def test_network_bad_observer_rejects_missing_header_render(self):
        # Mutation control: a missing production header must remain a failure;
        # synchronous frame acquisition is not permission to accept no text.
        with patch.object(self.win, "_paint_header", return_value=None), \
                self.assertRaisesRegex(AssertionError, "未绘制'网络异常'"):
            self.test_network_bad_draws_name_text()

    def test_last_ok_time_recorded(self):
        """断网：_apply_quote 成功后记录 _last_ok_time（HH:MM:SS 格式）"""
        self.assertIsNone(self.win._last_ok_time)
        self.win._apply_quote(QUOTE_NORMAL)
        self.assertIsNotNone(self.win._last_ok_time)
        self.assertTrue(
            re.fullmatch(r"\d{2}:\d{2}:\d{2}", self.win._last_ok_time),
            "最近成功时间格式异常: %r" % (self.win._last_ok_time,))


class TestSaveConfigFallback(unittest.TestCase):
    """保存失败兜底：写文件/替换失败时 save_config 不抛异常、不崩溃。

    说明：save_config 内部以 try/except 静默兜底（无 log_line 调用），
    本测试验证"不抛异常、不残留半截配置"的核心兜底语义。CONFIG_PATH
    指向临时目录，避免污染真实配置。
    """

    def setUp(self):
        self._orig_path = m.CONFIG_PATH
        self._orig_path_sfc = sf_config.CONFIG_PATH
        self._td = tempfile.mkdtemp(prefix="stockfloat_cfg_")
        p = os.path.join(self._td, "config.json")
        m.CONFIG_PATH = p
        sf_config.CONFIG_PATH = p

    def tearDown(self):
        m.CONFIG_PATH = self._orig_path
        sf_config.CONFIG_PATH = self._orig_path_sfc

    def test_save_success_writes_file(self):
        """正常保存：写入 config.json 且含 code/version（兜底基线）"""
        m.save_config("sh600000", 1, 2, 200, "glass")
        self.assertTrue(os.path.exists(m.CONFIG_PATH))
        import json
        with open(m.CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["code"], "sh600000")
        self.assertEqual(d["version"], sf_config.CONFIG_SCHEMA_VERSION)

    def test_open_failure_does_not_raise(self):
        """磁盘读/写全失败（open 抛 OSError）：save_config 不抛异常"""
        with patch("builtins.open", side_effect=OSError(28, "No space left on device")):
            m.save_config("sh600000", 1, 2, 200, "glass")  # 不应抛
        self.assertTrue(True)

    def test_replace_failure_does_not_raise_and_cleans_tmp(self):
        """原子替换失败（os.replace 抛 OSError）：不抛异常且清理临时文件"""
        tmp = m.CONFIG_PATH + ".tmp"
        with patch.object(os, "replace", side_effect=OSError(5, "I/O error")):
            m.save_config("sh600000", 1, 2, 200, "glass")  # 不应抛
        self.assertFalse(
            os.path.exists(tmp), "替换失败后残留半截临时文件: %s" % tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
