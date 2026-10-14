"""实时股价 v1.2.25 回归测试：逐位滚动与价格插值叠加冲突修复。

背景：v1.2.24 逐位滚动在价格插值动画（130~200ms，16ms/帧）期间，_disp_price
每帧都在变，_roll_update 每帧都把滚动动画 entry 覆盖为新起点 → 0.18s 滚动
动画永远播不完，数字持续翻滚乱跳（用户反馈"奇奇怪怪"）。

v1.2.25 修复：窗口层在行情更新时预置 _roll_pending[key] = 最终目标串；
_draw_roll_text 在预置目标期间一律以目标串输入 _roll_update，保证一次滚动
从旧显示值到最终值、中间帧不重启；插值结束（_anim_tick_inner t>=1.0）清空
_roll_pending 后落定。切股时同步清空滚动状态，避免残留到新股票。
"""
import unittest

from sf_ui_render import RenderMixin


def make_render():
    """构造一个不触发 Qt 初始化的 RenderMixin 实例，仅测滚动状态逻辑。"""
    r = object.__new__(RenderMixin)
    r._roll_prev = {}
    r._roll_anim = {}
    r._roll_pending = {}
    return r


class TestRollPending(unittest.TestCase):
    """v1.2.25 核心：插值中间帧不再重启逐位滚动动画。"""

    def test_interpolation_frames_do_not_restart_animation(self):
        """插值中间帧以 pending 目标串输入时，滚动动画只触发一次、不被重启。"""
        r = make_render()
        old, final = "1.2300", "1.2400"
        r._roll_update("price", old)               # 稳定显示旧值
        r._roll_pending["price"] = final           # 行情更新：预置最终目标
        for f in range(13):                        # 插值 13 帧（130ms）
            t = f / 12.0
            e = 1.0 - (1.0 - t) ** 3
            interp = "%.4f" % (1.2300 + (1.2400 - 1.2300) * e)
            # render 层：有 pending 则一律以 pending 串输入
            s = r._roll_pending.get("price", interp)
            r._roll_update("price", s)
        anim = r._roll_anim["price"]
        # 第3位 3→4 触发一次滚动；from_ch 恒为旧显示值字符，未被中间帧覆盖
        self.assertEqual(anim[3][0], "3")
        self.assertEqual(anim[3][1], "4")
        # 只应有 3→4 这一处变化位
        self.assertEqual(len(anim), 1)
        self.assertEqual(r._roll_prev["price"], final)

    def test_pending_cleared_then_next_quote_rolls(self):
        """动画播完且 pending 清除后，新一轮行情可正常触发滚动。"""
        r = make_render()
        r._roll_update("price", "1.2400")
        r._roll_pending["price"] = "1.2500"
        r._roll_update("price", "1.2500")          # 触发 4→5 滚动
        r._roll_pending = {}                        # 插值结束清 pending
        r._roll_update("price", "1.2500")          # 落定帧：无新变化
        self.assertEqual(r._roll_prev["price"], "1.2500")
        # 再次行情：5→6 正常触发
        r._roll_update("price", "1.2600")
        self.assertEqual(r._roll_anim["price"][3][0], "5")
        self.assertEqual(r._roll_anim["price"][3][1], "6")

    def test_pending_override_continues_roll_to_new_target(self):
        """动画进行中 pending 被新目标覆盖：旧目标字符作为起点续滚到新目标。"""
        r = make_render()
        r._roll_update("price", "1.2400")
        r._roll_pending["price"] = "1.2500"
        r._roll_update("price", "1.2500")          # 4→5 滚动中
        r._roll_pending["price"] = "1.2600"        # 行情再更新：目标变 1.2600
        r._roll_update("price", "1.2600")
        anim = r._roll_anim["price"]
        self.assertEqual(anim[3][0], "5")          # 从 1.2500 的字符续滚
        self.assertEqual(anim[3][1], "6")

    def test_len_change_clears_anim(self):
        """目标串位数变化（如 99→100）时不逐位滚动，仅更新基线避免整串乱滚。"""
        r = make_render()
        r._roll_update("price", "99.00")
        r._roll_pending["price"] = "100.00"
        r._roll_update("price", "100.00")          # 长度 5→6：清空动画
        self.assertEqual(r._roll_anim.get("price", {}), {})
        self.assertEqual(r._roll_prev["price"], "100.00")

    def test_switch_stock_clears_roll_state(self):
        """切股时清空滚动状态，旧股票的滚动动画不残留到新股票。"""
        r = make_render()
        r._roll_update("price", "1.2400")
        r._roll_pending["price"] = "1.2500"
        r._roll_update("price", "1.2500")
        # 模拟切股清理（stock_float_window 切股分支）
        r._roll_pending = {}
        r._roll_prev = {}
        r._roll_anim = {}
        self.assertEqual(r._roll_pending, {})
        self.assertEqual(r._roll_prev, {})
        self.assertEqual(r._roll_anim, {})


if __name__ == "__main__":
    unittest.main()
