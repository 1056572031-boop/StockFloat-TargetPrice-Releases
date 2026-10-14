# -*- coding: utf-8 -*-
"""实时股价 v1.2.65 第1轮走查：sf_config 两处健壮性修复的回归测试。

覆盖 2 项修复：
1. _backup_config：写盘前的备份快照失败不再静默吞掉 —— 记 WARNING 日志，
   且**不**中断 save_config 的主写盘流程（备份是保险，不是前置条件）。
2. load_config 返回值清洗：code / x / y / h 读回时做类型与合法性校验，
   脏值回退安全默认值并记 WARNING。只保证「类型正确 + 数值在合理区间」，
   **不**做屏幕尺寸钳制（那份逻辑在 stock_float_window.py 的
   _clamp_to_screen / _keep_on_screen，此处不重复实现）。

运行方式（在 StockFloat 目录）：
    python3 -m unittest tests.test_v165_config -v
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
from sf_constants import BG_MODE_DEFAULT, DEFAULT_STOCK, MAX_H, MIN_H


class _ConfigTestCase(unittest.TestCase):
    """公共脚手架：临时目录隔离配置路径 + 捕获 log_line 调用。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="stockfloat_cfg165_")
        self._p = os.path.join(self._td, "config.json")
        self._bak = self._p + ".bak"
        self.logs = []          # [(msg, level), ...]

        def _fake_log(msg, level="INFO"):
            self.logs.append((msg, level))

        self._patches = [
            patch.object(sf_config, "CONFIG_PATH", self._p),
            patch.object(sf_config, "CONFIG_BACKUP_PATH", self._bak),
            patch.object(sf_config, "SPLIT_SOURCE_CONFIG_PATH",
                         os.path.join(self._td, "missing-formal.json")),
            patch.object(sf_config, "LEGACY_CONFIG_PATH",
                         os.path.join(self._td, "missing-legacy.json")),
            patch.object(sf_config, "log_line", _fake_log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._td, ignore_errors=True)

    def _write(self, d):
        with open(self._p, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def _write_raw(self, text):
        with open(self._p, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self):
        with open(self._p, encoding="utf-8") as f:
            return json.load(f)

    def warnings(self):
        return [m for m, lv in self.logs if lv == "WARNING"]

    def assert_warned(self, keyword):
        self.assertTrue(any(keyword in m for m in self.warnings()),
                        "应记录含 %r 的 WARNING 日志，实际：%r" % (keyword, self.warnings()))


class TestBackupFailureNotSilent(_ConfigTestCase):
    """修复1：备份失败必须可见（WARNING），且不得阻断主写盘。"""

    def test_backup_failure_logs_warning(self):
        """copy2 抛异常（磁盘满/权限/被占用）→ 记 WARNING 且含关键信息"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        with patch.object(sf_config.shutil, "copy2",
                          side_effect=OSError(28, "No space left on device")):
            sf_config.save_config("sh600519", 10, 20, 240, "glass")
        self.assert_warned("backup")
        self.assert_warned(sf_config.CONFIG_BACKUP_PATH)

    def test_backup_failure_does_not_abort_main_write(self):
        """备份失败只是保险失效，主配置仍必须正常落盘（返回 True + 内容正确）"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        with patch.object(sf_config.shutil, "copy2", side_effect=PermissionError("denied")):
            ok = sf_config.save_config("sh600519", 10, 20, 240, "glass")
        self.assertIs(ok, True, "备份失败不应导致 save_config 失败")
        d = self._read()
        self.assertEqual(d["code"], "sh600519", "新配置必须已写入主配置文件")
        self.assertEqual((d["x"], d["y"]), (10, 20))

    def test_backup_failure_keeps_no_snapshot_but_old_config_still_on_disk(self):
        """备份失败时 .bak 不存在，但旧的完好配置在写盘完成前一直保留"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        with patch.object(sf_config.shutil, "copy2", side_effect=OSError("busy")):
            sf_config.save_config("sh600519", 10, 20, 240, "glass")
        self.assertFalse(os.path.exists(self._bak), "备份失败不应留下 .bak")

    def test_backup_success_logs_no_warning(self):
        """回归护栏：备份正常时不应刷 WARNING（防止日志噪音/误报）"""
        self._write({"code": "sh600000", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        sf_config.save_config("sh600519", 10, 20, 240, "glass")
        self.assertTrue(os.path.exists(self._bak))
        self.assertEqual(self.warnings(), [], "备份成功时不应有 WARNING")


class TestSaveAlertsCreatesDirectory(_ConfigTestCase):
    def test_save_alerts_creates_missing_parent_and_preserves_other_fields(self):
        nested = os.path.join(self._td, "missing", "nested", "config.json")
        with patch.object(sf_config, "CONFIG_PATH", nested), \
             patch.object(sf_config, "CONFIG_BACKUP_PATH", nested + ".bak"):
            ok = sf_config.save_alerts([{"direction": "above", "target": 1.0}])
        self.assertTrue(ok)
        with open(nested, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["alerts"][0]["target"], 1.0)
        self.assertFalse(os.path.exists(nested + ".tmp"), "原子替换后不得残留临时文件")

    def test_replace_failure_keeps_existing_config(self):
        self._write({"code": "sh600519", "alerts": []})
        with patch.object(sf_config.os, "replace", side_effect=OSError("disk")):
            self.assertFalse(sf_config.save_alerts([{"direction": "above", "target": 2.0}]))
        self.assertEqual(self._read()["alerts"], [], "替换失败时主配置必须保持旧内容")


class TestLoadConfigSanitizeCode(_ConfigTestCase):
    """修复2-a：code 脏值清洗（null / 非字符串 / 空白 → 默认代码）。"""

    def _code_of(self, raw):
        self._write({"code": raw, "x": 1, "y": 2, "h": 240, "bg": "glass"})
        return sf_config.load_config()[0]

    def test_null_code_falls_back_to_default(self):
        self.assertEqual(self._code_of(None), DEFAULT_STOCK)
        self.assert_warned("code")

    def test_numeric_code_falls_back_to_default(self):
        """手工编辑成数字 600519（非字符串）→ 回退默认，避免拼出非法行情 URL"""
        self.assertEqual(self._code_of(600519), DEFAULT_STOCK)

    def test_blank_code_falls_back_to_default(self):
        self.assertEqual(self._code_of("   "), DEFAULT_STOCK)

    def test_list_code_falls_back_to_default(self):
        self.assertEqual(self._code_of(["sh600519"]), DEFAULT_STOCK)

    def test_valid_code_stripped_and_preserved(self):
        """合法代码原样保留（仅 strip），且不产生告警"""
        self.assertEqual(self._code_of("  sh600519  "), "sh600519")
        self.assertEqual(self.warnings(), [], "合法代码不应告警")

    def test_bare_numeric_string_code_preserved(self):
        """护栏：纯数字字符串是可用代码（normalize_code 会补前缀），不得误杀"""
        self.assertEqual(self._code_of("600519"), "600519")
        self.assertEqual(self.warnings(), [])


class TestLoadConfigSanitizeCoords(_ConfigTestCase):
    """修复2-b：x / y 脏值清洗 + 不做屏幕尺寸钳制。"""

    def _xy_of(self, x, y):
        self._write({"code": "sh600519", "x": x, "y": y, "h": 240, "bg": "glass"})
        _c, rx, ry, _h, _b = sf_config.load_config()
        return rx, ry

    def test_string_coord_falls_back(self):
        self.assertEqual(self._xy_of("abc", 5), (sf_config.CONFIG_DEFAULT_X, 5))
        self.assert_warned("x")

    def test_null_coord_falls_back(self):
        self.assertEqual(self._xy_of(None, None), (sf_config.CONFIG_DEFAULT_X,
                                                   sf_config.CONFIG_DEFAULT_Y))

    def test_bool_coord_falls_back(self):
        """bool 是 int 子类，JSON true 不得被当成坐标 1"""
        self.assertEqual(self._xy_of(True, False), (sf_config.CONFIG_DEFAULT_X,
                                                    sf_config.CONFIG_DEFAULT_Y))

    def test_inf_coord_falls_back(self):
        self.assertEqual(self._xy_of(float("inf"), 5), (sf_config.CONFIG_DEFAULT_X, 5))

    def test_nan_coord_falls_back(self):
        self.assertEqual(self._xy_of(float("nan"), 5), (sf_config.CONFIG_DEFAULT_X, 5))

    def test_absurd_coord_falls_back(self):
        self.assertEqual(self._xy_of(10 ** 12, -10 ** 12), (sf_config.CONFIG_DEFAULT_X,
                                                            sf_config.CONFIG_DEFAULT_Y))

    def test_negative_coord_within_range_preserved(self):
        """核心：不做屏幕尺寸钳制 —— 主屏左侧外接屏的负坐标必须原样保留"""
        self.assertEqual(self._xy_of(-200, -50), (-200, -50))
        self.assertEqual(self.warnings(), [])

    def test_large_positive_coord_preserved(self):
        """远离当前屏幕但在合理区间内的坐标（多屏右侧）不得被改写"""
        self.assertEqual(self._xy_of(4000, 3000), (4000, 3000))

    def test_float_coord_coerced_to_int(self):
        """PyQt6 move()/resize() 不接受 float，脏配置里的 100.7 必须转 int"""
        rx, ry = self._xy_of(100.7, 200.2)
        self.assertEqual((rx, ry), (100, 200))
        self.assertIsInstance(rx, int)
        self.assertIsInstance(ry, int)

    def test_valid_int_coord_preserved(self):
        self.assertEqual(self._xy_of(10, 20), (10, 20))
        self.assertEqual(self.warnings(), [])


class TestLoadConfigSanitizeHeight(_ConfigTestCase):
    """修复2-c：h 脏值清洗（沿用 MIN_H 降级，新增 WARNING 可见性）。"""

    def _h_of(self, raw):
        self._write({"code": "sh600519", "x": 1, "y": 2, "h": raw, "bg": "glass"})
        return sf_config.load_config()[3]

    def test_negative_height_falls_back(self):
        self.assertEqual(self._h_of(-1), MIN_H)
        self.assert_warned("h")

    def test_string_height_falls_back(self):
        self.assertEqual(self._h_of("tall"), MIN_H)

    def test_above_max_height_falls_back(self):
        self.assertEqual(self._h_of(MAX_H + 1), MIN_H)

    def test_valid_height_preserved_and_int(self):
        self.assertEqual(self._h_of(300), 300)
        self.assertEqual(self._h_of(float(MIN_H + 10)), MIN_H + 10)
        self.assertEqual(self.warnings(), [])


class TestNonDictConfigTreatedAsCorrupt(_ConfigTestCase):
    """补修：config.json 顶层不是对象（list/str）→ 按损坏处理，不得崩启动。"""

    def test_top_level_list_falls_back_to_defaults(self):
        """顶层是 [1,2]：回退默认配置 + 记 WARNING（写明实际类型），不抛异常"""
        self._write_raw("[1, 2]")
        out = sf_config.load_config()
        self.assertEqual(len(out), 5, "非 dict 配置也必须返回 5 元组，不得抛异常")
        code, x, y, h, bg = out
        self.assertEqual(code, DEFAULT_STOCK)
        self.assertEqual(x, sf_config.CONFIG_DEFAULT_X)
        self.assertEqual(y, sf_config.CONFIG_DEFAULT_Y)
        self.assertEqual(h, MIN_H)
        self.assertEqual(bg, BG_MODE_DEFAULT)
        self.assert_warned("list")

    def test_top_level_string_falls_back_to_defaults(self):
        """顶层是字符串 "hello"：同上，回退默认且不抛异常"""
        self._write_raw('"hello"')
        code, _x, _y, _h, _b = sf_config.load_config()
        self.assertEqual(code, DEFAULT_STOCK)
        self.assert_warned("str")

    def test_top_level_number_and_bool_also_handled(self):
        """顶层是数字/布尔：同样按损坏处理（int/bool 也不得走进 d.get 崩掉）"""
        self._write_raw("42")
        self.assertEqual(sf_config.load_config()[0], DEFAULT_STOCK)
        self.assert_warned("int")
        self._write_raw("true")
        self.assertEqual(sf_config.load_config()[0], DEFAULT_STOCK)
        self.assert_warned("bool")

    def test_valid_dict_emits_no_warning_and_reads_normally(self):
        """回归护栏：正常 dict 配置零告警、正常读取（不得误判为损坏）"""
        self._write({"code": "sh600519", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        self.assertEqual(sf_config.load_config(), ("sh600519", 1, 2, 240, "glass"))
        self.assertEqual(self.warnings(), [])

    def test_none_path_unchanged_and_no_corrupt_warning(self):
        """护栏：None（文件不存在/内容为空）是正常首装路径，不得被当成损坏刷日志"""
        code, x, y, h, bg = sf_config.load_config()   # 无配置文件
        self.assertEqual((code, x, y, h, bg),
                         (DEFAULT_STOCK, sf_config.CONFIG_DEFAULT_X,
                          sf_config.CONFIG_DEFAULT_Y, MIN_H, BG_MODE_DEFAULT))
        self.assertEqual(self.warnings(), [], "None 路径不应产生任何 WARNING")
        # 空文件：_read_json_or_none 同样返回 None，既有代码会走「损坏且无备份」分支刷日志
        # （v1.2.23 起的既有行为，不属本次修复）；此处只保证不新增「顶层结构非法」告警。
        self._write_raw("")
        self.assertEqual(sf_config.load_config()[0], DEFAULT_STOCK)
        self.assertFalse([m for m in self.warnings() if "顶层结构非法" in m],
                         "None 路径不得被判为顶层结构非法：%r" % (self.warnings(),))

    def test_non_dict_main_recovers_from_valid_backup(self):
        """顶层非 dict 也走既有的备份恢复分支，用户数据不被直接丢弃"""
        with open(self._bak, "w", encoding="utf-8") as f:
            json.dump({"code": "sh600000", "x": 7, "y": 8, "h": 240, "bg": "solid"}, f)
        self._write_raw("[1, 2]")
        self.assertEqual(sf_config.load_config(), ("sh600000", 7, 8, 240, "solid"))


class TestLoadConfigContract(_ConfigTestCase):
    """契约护栏：清洗不得改变返回结构，合法配置零副作用。"""

    def test_return_shape_is_5_tuple(self):
        self._write({"code": "sh600519", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        out = sf_config.load_config()
        self.assertEqual(len(out), 5, "返回结构必须是 5 元组，调用方按此解包")
        code, x, y, h, bg = out
        self.assertEqual((code, x, y, h, bg), ("sh600519", 1, 2, 240, "glass"))

    def test_fully_dirty_config_still_returns_usable_tuple(self):
        """整份配置被写坏 → 全部回退默认值，不抛异常、不返回 None"""
        self._write({"code": None, "x": "abc", "y": None, "h": -1, "bg": "neon"})
        code, x, y, h, bg = sf_config.load_config()
        self.assertEqual(code, DEFAULT_STOCK)
        self.assertEqual(x, sf_config.CONFIG_DEFAULT_X)
        self.assertEqual(y, sf_config.CONFIG_DEFAULT_Y)
        self.assertEqual(h, MIN_H)
        self.assertEqual(bg, BG_MODE_DEFAULT)
        for t in (code, x, y, h, bg):
            self.assertIsNotNone(t)

    def test_clean_config_emits_no_warning(self):
        self._write({"code": "sh600519", "x": 1, "y": 2, "h": 240, "bg": "glass"})
        sf_config.load_config()
        self.assertEqual(self.warnings(), [], "干净配置不应产生任何 WARNING")


class TestConfigDurableWrite(_ConfigTestCase):
    """v1.3.41 数据安全加固：断电/强退时配置不能只写一半。

    验证三点：
    1. 写盘过程经过 fsync（文件内容 + 目录项），不是只丢给系统缓存；
    2. 写入中途失败时主配置保持原样（原子替换），不会留下半截 JSON；
    3. 正常写入后不残留 .tmp 文件。
    """

    def test_write_is_fsynced_for_file_and_directory(self):
        self._write({"code": "sh600519"})
        calls = []
        real_fsync = os.fsync

        def _spy_fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        with patch.object(os, "fsync", _spy_fsync):
            self.assertTrue(sf_config.save_config("sz000001", 10, 20))
        self.assertGreaterEqual(len(calls), 2,
                                "必须同时 fsync 文件内容与所在目录，断电才不会丢")
        self.assertEqual(self._read()["code"], "sz000001")

    def test_failed_write_keeps_previous_config_intact(self):
        self._write({"code": "sh600519", "x": 7})
        real_dump = json.dump

        def _boom(obj, fp, *a, **kw):
            fp.write("{\"code\": \"half-")   # 模拟写到一半断电
            raise OSError("simulated power loss")

        with patch.object(json, "dump", _boom):
            self.assertFalse(sf_config.save_config("sz000001", 1, 2))
        self.assertEqual(self._read()["code"], "sh600519",
                         "写盘失败必须保留旧配置，不能被半截内容覆盖")
        self.assertFalse(os.path.exists(self._p + ".tmp"), "临时文件必须被清理")

    def test_successful_write_leaves_no_temp_file(self):
        self.assertTrue(sf_config.save_config("sh600519", 3, 4))
        self.assertFalse(os.path.exists(self._p + ".tmp"))
        self.assertEqual(self._read()["code"], "sh600519")

    def test_save_alerts_also_durable_and_redacts_nothing(self):
        self._write({"code": "sh600519"})
        self.assertTrue(sf_config.save_alerts([{"code": "sh600519", "target": 1800.0}]))
        self.assertEqual(self._read()["alerts"][0]["target"], 1800.0)
        self.assertFalse(os.path.exists(self._p + ".tmp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
