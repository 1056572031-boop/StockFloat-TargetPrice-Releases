# -*- coding: utf-8 -*-
"""实时股价 v1.2.55 首次安装默认高度 = 最低高度 回归测试。

需求：首次安装（无 config.json）打开程序，悬浮窗默认高度即为窗口最低高度
（MIN_H，紧凑态）；旧默认 H(192) 低于窗口最小高度限制，靠 Qt 钳制碰巧垫到
MIN_H，默认值本身失真。同步：配置存在但高度字段缺失/非法时的降级值一致。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sf_config
from sf_constants import APP_VERSION, VERSION_TEXT, BG_MODE_DEFAULT, DEFAULT_STOCK, MIN_H




class TestFirstInstallDefaultHeight(unittest.TestCase):
    """load_config 默认高度决策（临时目录隔离，不触碰真实配置）。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_h_")
        self._p = os.path.join(self._td, "config.json")
        self._bak = self._p + ".bak"
        self._patch_path = patch.object(sf_config, "CONFIG_PATH", self._p)
        self._patch_bak = patch.object(sf_config, "CONFIG_BACKUP_PATH", self._bak)
        self._patch_split = patch.object(
            sf_config, "SPLIT_SOURCE_CONFIG_PATH", os.path.join(self._td, "missing-formal.json"))
        self._patch_legacy = patch.object(
            sf_config, "LEGACY_CONFIG_PATH", os.path.join(self._td, "missing-legacy.json"))
        self._patch_path.start()
        self._patch_bak.start()
        self._patch_split.start()
        self._patch_legacy.start()

    def tearDown(self):
        self._patch_legacy.stop()
        self._patch_split.stop()
        self._patch_bak.stop()
        self._patch_path.stop()
        shutil.rmtree(self._td, ignore_errors=True)

    def _write(self, d):
        with open(self._p, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_no_config_returns_min_height(self):
        """首次安装（无配置文件）：默认高度 == 最低高度 MIN_H"""
        code, x, y, h, bg = sf_config.load_config()
        self.assertEqual(code, DEFAULT_STOCK)
        self.assertEqual(h, MIN_H, "首装默认高度应为最低高度")
        self.assertEqual(bg, BG_MODE_DEFAULT)

    def test_config_without_h_falls_back_to_min_height(self):
        """配置存在但缺高度字段：降级值同为最低高度"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "bg": "glass"})
        code, x, y, h, bg = sf_config.load_config()
        self.assertEqual(h, MIN_H)

    def test_config_with_invalid_h_falls_back_to_min_height(self):
        """高度字段非法（低于下限/类型错误）：降级值同为最低高度"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 100, "bg": "glass"})
        _c, _x, _y, h, _b = sf_config.load_config()
        self.assertEqual(h, MIN_H)
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": "tall", "bg": "glass"})
        _c, _x, _y, h, _b = sf_config.load_config()
        self.assertEqual(h, MIN_H)

    def test_valid_config_height_preserved(self):
        """合法高度不受影响（老用户既有高度不被覆盖）"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 300, "bg": "glass"})
        _c, _x, _y, h, _b = sf_config.load_config()
        self.assertEqual(h, 300)

    def test_min_height_within_window_limits(self):
        """默认高度必须落在窗口允许的 [MIN_H, MAX_H] 区间（self-evident 防御）"""
        from sf_constants import MAX_H
        _c, _x, _y, h, _b = sf_config.load_config()
        self.assertTrue(MIN_H <= h <= MAX_H)


if __name__ == "__main__":
    unittest.main(verbosity=2)
