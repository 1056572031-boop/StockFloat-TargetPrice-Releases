# -*- coding: utf-8 -*-
"""v1.2.46 快速拉伸顶部背景变形/闪烁修复回归测试

缺陷：快速拉伸悬浮窗底部时顶部背景层变形闪烁（缓慢拉伸正常）。
根因：v1.2.26/27 在 resizeEvent 里"动画/拉伸逐帧强制重建玻璃圆角 mask"，mask 更新
     与主窗 Qt 内容（paintEvent 异步重绘 + AppKit 合成）走两条渲染流水线，快速拉伸
     时 mask 光栅化与主窗重绘帧错位 → 顶部背景层变形/闪烁；慢速拉伸逐帧 Δ≤1px
     被 should_rebuild_glass_mask 尺寸复用跳过、mask 不重建反而不触发。
     另：v1.2.43 的 should_rebuild_glass_mask 降频判定已定义但 _sync_glass_frame
     从未接线（逐帧重建依旧），降频收益实际未落地。
修复：mask 更新决策收敛为三态计划 glass_mask_update_plan
     （immediate=force 立即重建 / skip=Δ≤1px 复用零重建 / pending=尺寸变化，
      交由下一次 Qt 主窗 paintEvent 同帧 _glass_mask_flush 重建），
     mask 与主窗圆角同一几何进入合成帧，消除错位变形/闪烁，同时保留 v1.2.43
     中间帧降频收益。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sf_glass import glass_mask_update_plan, should_rebuild_glass_mask
from sf_constants import APP_VERSION

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLASS_SRC = os.path.join(REPO, "sf_glass.py")
RENDER_SRC = os.path.join(REPO, "sf_ui_render.py")
WINDOW_SRC = os.path.join(REPO, "stock_float_window.py")


class TestGlassMaskUpdatePlan(unittest.TestCase):
    """v1.2.46：三态更新计划纯函数 —— 快速拉伸对齐 / 中间帧降频 / 关键节点强对齐"""

    def test_force_mask_immediate(self):
        # force_mask（拉伸结束/动画完成帧）→ 立即重建精确对齐
        self.assertEqual(glass_mask_update_plan(286, 250, 286, 250, 1.0, 1.0,
                                                force_mask=True), 'immediate')
        self.assertEqual(glass_mask_update_plan(300, 260, 286, 250, 1.0, 1.0,
                                                force_mask=True), 'immediate')

    def test_same_size_reuse_skip(self):
        # 目标尺寸与上次重建一致（≤1px）→ 复用缓存零重建（中间帧降频，v1.2.43 收益）
        now = 100.0
        self.assertEqual(glass_mask_update_plan(286, 250, 286, 250, now, now - 5.0),
                         'skip')
        # ≤1px 抖动视作同尺寸
        self.assertEqual(glass_mask_update_plan(287, 250, 286, 250, now, now - 5.0),
                         'skip')
        self.assertEqual(glass_mask_update_plan(286, 251, 286, 250, now, now - 5.0),
                         'skip')

    def test_size_changed_pending(self):
        # 尺寸确实变化 → pending，交由下一次 Qt 绘制帧同帧 flush（不错位）
        now = 100.0
        self.assertEqual(glass_mask_update_plan(300, 250, 286, 250, now, now - 0.001),
                         'pending')
        # 快速拉伸典型：高度大幅跳变（Δ>1px）→ pending，不等时间节流立即登记同帧重建
        self.assertEqual(glass_mask_update_plan(286, 280, 286, 250, now, now - 0.001),
                         'pending')

    def test_no_history_pending(self):
        # 无历史尺寸（首次）→ pending（首帧由绘制帧建立并重建）
        self.assertEqual(glass_mask_update_plan(286, 250, 0, 0, 0.0, 0.0), 'pending')

    def test_plan_is_consistent_with_throttle_pure_fn(self):
        # 与 v1.2.43 should_rebuild_glass_mask 语义不冲突：
        # 尺寸复用场景两者都判定"不重建"；force 场景两者都判定"重建"
        now = 100.0
        self.assertFalse(should_rebuild_glass_mask(286, 250, 286, 250, now - 5.0, now))
        self.assertEqual(glass_mask_update_plan(286, 250, 286, 250, now, now - 5.0),
                         'skip')
        self.assertTrue(should_rebuild_glass_mask(286, 250, 286, 250, 1.0, 1.0,
                                                  force_mask=True))
        self.assertEqual(glass_mask_update_plan(286, 250, 286, 250, 1.0, 1.0,
                                                force_mask=True), 'immediate')


class TestGlassSyncWiring(unittest.TestCase):
    """v1.2.46：源码级接线 —— 降频纯函数正式接入 _sync_glass_frame"""

    def test_sync_uses_plan_and_not_legacy_forced_branch(self):
        with open(GLASS_SRC, encoding="utf-8") as f:
            src = f.read()
        # 已接线三态计划
        self.assertIn("glass_mask_update_plan", src)
        # 旧逐帧强制重建分支（animating/resizing 无条件每帧重建）已移除
        self.assertNotIn("animating or resizing", src)
        self.assertNotIn("_resizing, False", src)
        # pending 标记与同帧 flush 存在
        self.assertIn("_glass_mask_pending", src)
        self.assertIn("def _glass_mask_flush", src)

    def test_paint_flushes_mask_same_frame(self):
        with open(RENDER_SRC, encoding="utf-8") as f:
            src = f.read()
        # Qt 主窗实际绘制帧内同帧 flush 玻璃 mask
        self.assertIn("self._glass_mask_flush()", src)

    def test_release_force_mask_kept(self):
        # 拉伸结束/动画完成帧仍强制立即重建对齐（关键节点 force_mask 保留）
        with open(WINDOW_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_sync_glass_frame(force_mask=True)", src)




if __name__ == "__main__":
    unittest.main()
