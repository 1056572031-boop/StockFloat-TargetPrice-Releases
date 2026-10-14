#!/usr/bin/env python3
"""实渲染像素核验：底部两行的真实视觉间距是否等于 BOTTOM_ROW_GAP，且不重叠。

方法：离屏 grab() 渲染 → 取底部区域 → 逐行统计"文字像素"数量 →
      从下往上找行2 的顶边，再往上找行1 的底边，二者之差即视觉净间距。

注意：必须调用 R.load_bundled_fonts()，否则 NUM_FONT 退化为替代字体，度量失真。

判据为什么不再**单向**跟着常量走（2026-10-09 改）：
    旧写法是 `0 < gap <= BOTTOM_ROW_GAP + 3` —— 上限只由被验对象的常量决定。
    谁把那个常量改成 40，这道关卡就跟着把 40px 的松垮间距判成"合理区间"，
    于是它变成一条**永不失败的检查**。本仓为同一件事栽过至少四次（台账 G01~G05：
    verify_dmg_pyz 无脑 PASS 约 54 个版本、探针把全屏前置做成永真、
    位图缓存核验空转却报"36 组零差异"、功耗打桩写死代码让交付记录的百分比是错的），
    判据自指是这个家族的共同根子（同类缺陷 D-30：旧断言拿 target_screen()
    自己的输出当基准，单屏恒成立）。
    现在有效上限 = `min(GAP_CEILING_PX, BOTTOM_ROW_GAP + 3)`：
    今天的判据强度**一点没放松**（BOTTOM_ROW_GAP=2 → 上限仍是 5px），
    但常量被改大时上限不再跟着放宽 —— 想放宽就得有人来改 `GAP_CEILING_PX`
    并写下理由（用户定的规矩：阈值必须标明来源）。
    `judge()` 是纯函数、不导入任何产品模块，这条解耦由 `--self-test` 断言，不靠注释自觉。

用法：
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/verify_bottom_gap_px.py   # 真渲染核验
    ./.venv/bin/python tools/verify_bottom_gap_px.py --self-test                 # 变异自检（不渲染、不建窗口）
"""
import os
import sys

# 净间距上限的**上限**（px）。真判据取 min(本值, BOTTOM_ROW_GAP + 3)。
# 来源：本机真渲染实测 gap=3px（行1 字形底 y=190、行2 字形顶 y=194），
# 8px 留给 抗锯齿边缘 ±1px + 不同 DPI 的字形浮动 + 保守模型的 descent 误差。
# 为什么还要这层绝对值：产品常量可以被人在一次改版里改大，
# 那会让旧的 `BOTTOM_ROW_GAP + 3` 把松垮间距判成合理；本值把这条放宽路径堵死。
GAP_CEILING_PX = 8
# 至少几条文字带才谈得上"两行间距"
MIN_BANDS = 2


def text_rows(img, x0, x1, y0, y1):
    """返回 {y: 该行文字像素数}。文字像素 = 与背景差异明显的非透明像素。"""
    rows = {}
    for y in range(y0, y1):
        n = 0
        for x in range(x0, x1):
            c = img.pixelColor(x, y)
            # 背景近黑且透明，把"够不透明 + 够亮"的像素近似当作文字像素
            if c.alpha() > 40 and (c.red() + c.green() + c.blue()) / 3 > 60:
                n += 1
        rows[y] = n
    return rows


def bands_from_rows(rows):
    """把"有文字的连续 y"合并成文字带；相邻 1px 视为同一条带（抗锯齿淡影不该拆行）。"""
    active = sorted(y for y, n in rows.items() if n > 0)
    if not active:
        return []
    bands = []
    start = prev = active[0]
    for y in active[1:]:
        if y - prev > 1:
            bands.append((start, prev))
            start = y
        prev = y
    bands.append((start, prev))
    return bands


def judge(bands, height, ceiling=GAP_CEILING_PX):
    """纯判据：返回问题清单，空表=通过。不读任何产品常量，所以它不会跟着常量一起变钝。"""
    problems = []
    if len(bands) < MIN_BANDS:
        problems.append("只检出 %d 条文字带，无法判定两行间距"
                        "（画布尺寸取错、字体退化、或底部区根本没画上都会走到这里）"
                        % len(bands))
        return problems
    row1_bottom = bands[-2][1]
    row2_top = bands[-1][0]
    # -1：两端都是"有像素的那一行"，算净间距要把行1 的底边本身扣掉
    gap = row2_top - row1_bottom - 1
    if row1_bottom >= row2_top:
        problems.append("两行重叠（行1 字形底 y=%d ≥ 行2 字形顶 y=%d）"
                        % (row1_bottom, row2_top))
    if not (0 < gap <= ceiling):
        problems.append("净间距 %dpx 超出 (0, %d] 的绝对上限"
                        "（上限来源见 GAP_CEILING_PX，与产品常量无关）" % (gap, ceiling))
    if bands[-1][1] >= height:
        problems.append("行2 越出窗口底部（字形底 y=%d，窗口高 %d）"
                        % (bands[-1][1], height))
    return problems


def self_test():
    """变异自检：五种"改坏"的形态必须全被抓到，两种"合法"的形态必须放行。

    为什么这里一次渲染都不做：自检要断言的是**判据本身**会不会变钝，
    而判据是纯函数；把它和真渲染分开，CI 与没有窗口服务的机器也照样能跑这道证明。
    """
    bad = []
    # 1) 干净样本必须放行（本机实测的形态：两条 12px 带、净间距 3px）
    if judge([(179, 190), (194, 205)], 224):
        bad.append("实测通过的形态被判红了（判据过严会一路拦人，最后被绕过）")
    # 2) 两行重叠必须判红
    if not judge([(179, 196), (194, 205)], 224):
        bad.append("行1 底 196 ≥ 行2 顶 194（真重叠）没被判出")
    # 3) 只检出一条带必须判红（画布取 _height_collapsed 而非窗口实际尺寸时就是这样，
    #    那正是 AGENTS §3.1 记的那条"表现成未检出文字"的坑）
    if not judge([(194, 205)], 224):
        bad.append("只有一条文字带却判成通过")
    if not judge([], 224):
        bad.append("一条带都没检出（整段空转）却判成通过")
    # 4) 行2 越出窗口底必须判红
    if not judge([(179, 190), (194, 205)], 200):
        bad.append("行2 画到窗口外（窗口高 200）没被判出")
    # 5) 间距超出**绝对**上限必须判红 —— 这条是本次改造的目标
    if not judge([(150, 160), (194, 205)], 224):
        bad.append("净间距 33px 却判成合理（上限又被常量带跑了吧）")
    # 6) 上限边界锁死：恰好等于上限放行，超 1px 判红
    if judge([(179, 185), (194, 205)], 224):
        bad.append("净间距恰好 %dpx 被判红（边界口径漂移）" % GAP_CEILING_PX)
    if not judge([(179, 184), (194, 205)], 224):
        bad.append("净间距超上限 1px（=9px）却没判红")
    # 7) 调用方可以把上限收得更紧（真渲染那侧传的是 min(绝对上限, 常量+3)）：
    #    净间距 6px 在 ceiling=5 下必须判红，在默认 8px 下必须放行
    if not judge([(179, 187), (194, 205)], 224, ceiling=5):
        bad.append("净间距 6px 在上限 5px 下却判成通过（收紧失效）")
    if judge([(179, 187), (194, 205)], 224):
        bad.append("净间距 6px 在默认上限 8px 下被判红（收紧过头会一路拦人）")
    # 8) 判据不许依赖产品模块：解耦是这条判据可信的前提
    if "sf_ui_render" in sys.modules or "stock_float_window" in sys.modules:
        bad.append("自检进程里已经导入了产品模块 —— 判据与产品常量的解耦无法证明")
    # 9) 分带规则：相邻 1px 合并成同一条带，隔 2px 才拆成两条
    if bands_from_rows({10: 5, 11: 4, 12: 3}) != [(10, 12)]:
        bad.append("抗锯齿淡影把一行拆成了两条带（相邻 1px 必须合并）")
    if bands_from_rows({10: 5, 13: 4}) != [(10, 10), (13, 13)]:
        bad.append("真正分开的两行被合并了")
    if bands_from_rows({10: 0, 11: 0}) != []:
        bad.append("零像素的行被当成了文字带")
    for line in bad:
        print("FAIL", line)
    if bad:
        return 1
    print("self-test 通过：9 类判定各归各位（重叠/单带/空转/越界/间距 33px/超上限 1px/上限收紧到 5px "
          "全部判红；实测形态、恰好 8px 与 6px 在默认上限下放行；分带规则锁死；判据不碰产品模块）")
    return 0


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from unittest.mock import patch

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtWidgets import QApplication

    import sf_quote
    import sf_ui_render as R
    import stock_float_window as m

    def _fake_http_get(url, **kw):
        if "hq.sinajs.cn" in url:
            return 'var hq_str_sh600519="贵州茅台,1500.0,1490.0,1510.0,1520.0,1480.0";'
        if "web.ifzq.gtimg.cn" in url:
            return ('{"data":{"sh600519":{"data":{"date":"20260821",'
                    '"data":["0930 1510.0 100 1000", "1000 1511.0 200 2000"]}}}}')
        return 'v_sh600519="1~贵州茅台~600519~1510.0~1490.0~0.1~1.01~10.5~9.8";'

    # 必须持有 QApplication 引用：后续 grab() 离屏渲染全程依赖它，被 GC 会直接崩
    app = QApplication.instance() or QApplication([])
    R.load_bundled_fonts()

    patches = [
        patch.object(m, "load_config",
                     return_value=("sh600519", 100, 100, 200, "translucent")),
        patch.object(m, "save_config", lambda *a, **k: None),
        patch.object(sf_quote, "http_get", _fake_http_get),
        patch.object(m, "start_icon_worker", lambda: None),
        patch.object(m.FloatWindow, "_force_top", lambda self: None),
    ]
    for p in patches:
        p.start()
    try:
        win = m.FloatWindow()
        win._expanded_h = None
        win.resize(240, win._height_collapsed())
        QApplication.processEvents()
        # FloatWindow 会锁定尺寸，resize 未必生效 —— 画布必须按**窗口实际尺寸**建，
        # 否则 _paint_bottom 内部用 self.height() 算出的 y 会落到画布外，什么也画不上。
        W, H = win.width(), win.height()
        print("窗口实际尺寸: %dx%d（_height_collapsed=%d）"
              % (W, H, win._height_collapsed()))
        # quote 字段与真实行情一致：数值一律为字符串（解析侧负责转 float）
        win.quote = {
            "name": "贵州茅台", "code": "600519", "price": "1510.00",
            "pct": "1.34", "change": "20.00", "pre_close": "1490.00",
            "high": "1520.00", "low": "1480.00",
            "volume": "12345", "limit_up": "1639.00", "limit_down": "1341.00",
        }
        win.minute_points = []

        # 只画底部区：直接调 _paint_bottom 到干净 QImage，
        # 绕过 paintEvent 的 try/except 兜底（否则异常被吞，只留一行日志）
        img = QImage(W, H, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        win._paint_bottom(painter, W)
        painter.end()

        print("常量: BOTTOM_ROW_GAP=%d  BOTTOM_ROW_H=%d  BOTTOM_SECTION_HIDDEN=%d"
              % (R.BOTTOM_ROW_GAP, R.BOTTOM_ROW_H, R.BOTTOM_SECTION_HIDDEN))
        ceiling = min(GAP_CEILING_PX, R.BOTTOM_ROW_GAP + 3)
        print("判据: 有效净间距上限 = min(GAP_CEILING_PX=%d, BOTTOM_ROW_GAP+3=%d) = %d px"
              % (GAP_CEILING_PX, R.BOTTOM_ROW_GAP + 3, ceiling))
        print()

        # 扫描底部区域（行1 字形顶之上 6px 起，到窗口底）
        top = H - R.BOTTOM_SECTION_HIDDEN - 6
        rows = text_rows(img, R.PAD_X, W - R.PAD_X, max(0, top), H)
        bands = bands_from_rows(rows)
        if not bands:
            print("!! 底部区未检出文字像素，无法判定")
            return 1

        print("检出文字带（y 范围，自下而上）:")
        for b in reversed(bands):
            print("   y %d..%d  (高 %d px)" % (b[0], b[1], b[1] - b[0] + 1))

        problems = judge(bands, H, ceiling=ceiling)
        if len(bands) >= 2:
            gap = bands[-1][0] - bands[-2][1] - 1
            print()
            print("行1 字形底 y = %d" % bands[-2][1])
            print("行2 字形顶 y = %d" % bands[-1][0])
            print("实测视觉净间距 = %d px   (常量 BOTTOM_ROW_GAP = %d)"
                  % (gap, R.BOTTOM_ROW_GAP))
        print()
        # 注：实测间距与常量**不必严格相等**，原因有二：
        #   1) 算术模型按最坏情况取 descent=3（有下伸部的字形 g/y/p）；
        #      底部实际内容是数字（无下伸部），故实测比模型更宽 —— 方向安全。
        #   2) 抗锯齿会在字形边缘产生 1px 淡影，收进/排除哪一侧有 ±1px 浮动。
        # 因此像素层只断言**平台无关的不变量**（不重叠 / 不越界 / 间距为正且不超绝对上限），
        # 精确相等由纯算术测试 test_v164 保证（不经过真实字体）。
        for msg in problems:
            print("   [FAIL]", msg)
        if not problems:
            print("   [PASS] 两行不重叠")
            print("   [PASS] 间距在合理区间（保守模型 %dpx，实测更宽，安全）"
                  % R.BOTTOM_ROW_GAP)
            print("   [PASS] 行2 未越出窗口底部（底部留白 %d px）"
                  % (H - 1 - bands[-1][1]))

        win.close()
        print("\n== 结论:", "像素核验通过 ==" if not problems else "像素核验失败 ==")
        return 0 if not problems else 1
    finally:
        for p in patches:
            p.stop()
        del app


if __name__ == "__main__":
    # 自检必须在**任何产品模块被导入之前**分流：judge() 的可信度正来自它不依赖被验对象，
    # 一旦自检顺手 import 了 sf_ui_render，"上限跟着常量变钝"这类缺陷就再也测不出来。
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    sys.exit(main())


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        # 自检必须在**任何产品模块导入之前**分流：判据的可信度来自它不依赖被验对象
        sys.exit(self_test())
    sys.exit(main())
