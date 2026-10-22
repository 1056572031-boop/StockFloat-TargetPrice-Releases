# -*- coding: utf-8 -*-
"""诊断里的通知授权缓存：从未查询过时不许报出一个编造的年龄。

真实案例：用户机器上导出的诊断包写着 `age_sec: 885193.9`（约 10.2 天），恰好等于那一刻
的开机时长 —— 缓存时间戳初值是 0.0，而 monotonic 从开机算起，两者一减就是开机时长。
读诊断的人会以为"10 天前问过一次系统"，实际上一次都没问过。
"""
import sys
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_notify  # noqa: E402

try:
    from _base import SFCase                  # noqa: E402
except ImportError:                           # 以包形式被 discover 导入时
    from tests._base import SFCase            # noqa: E402

UPTIME_SEC = 885193.9   # 真实诊断包里那个数，用它当"开机已久"的时钟


class NeverQueriedAuthorizationCache(SFCase):
    def setUp(self):
        super().setUp()
        sf_notify.reset_delivery_probe()
        self.addCleanup(sf_notify.reset_delivery_probe)
        for patcher in (mock.patch.object(sf_notify.time, "monotonic", return_value=UPTIME_SEC),
                        mock.patch.object(sf_notify, "has_bundle_identity", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_age_is_unknown_not_the_uptime_when_nothing_was_ever_cached(self):
        cache = sf_notify.delivery_stats()["authorization_cache"]
        self.assertIsNone(cache["age_sec"], "从未缓存过就没有年龄；写出开机时长会误导读诊断的人")
        self.assertFalse(cache["fresh"])
        self.assertEqual(cache["value"], "unknown")
        self.assertEqual(sf_notify.delivery_stats()["authorization"], "unknown")

    def test_a_real_cached_answer_still_reports_its_true_age(self):
        # 证明上面那条不是把年龄一概写成 None：真缓存过的答案照常给出有限的年龄。
        with mock.patch.object(sf_notify, "_AUTH_STATUS_CACHE", "authorized"), \
                mock.patch.object(sf_notify, "_AUTH_STATUS_CACHE_AT", UPTIME_SEC - 2.0):
            cache = sf_notify.delivery_stats()["authorization_cache"]
        self.assertAlmostEqual(cache["age_sec"], 2.0, places=3)
        self.assertTrue(cache["fresh"])


if __name__ == "__main__":
    unittest.main()
