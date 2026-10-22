# -*- coding: utf-8 -*-
"""改名之前装的旧回退包也要被更新助手清掉（判据是目录树）。

背景：v1.4.55 把程序改名，旧回退包叫 `.实时股价-目标价版.app.rollback-<epoch>`，而
助手只扫当前包名，于是改名前攒下的回退包永远没人清（实测一台机器 30 份、约 5.2GB）。
现在名字不同的隐藏回退包也进候选，但必须读得到 Info.plist 的包标识且是本程序两代之一。

抽段法：从助手里按 BEGIN/END 标记取出函数体，在 /bin/sh 里真跑一遍，只喂 TARGET 与
prune_dir 两个助手对外本来就有的输入。
"""
import pathlib
import plistlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from _base import SFCase                  # noqa: E402
except ImportError:                           # 以包形式被 discover 导入时
    from tests._base import SFCase            # noqa: E402

HELPER = ROOT / "updater" / "update_helper.sh"
CURRENT = ".Skyrocket.app.rollback-%s"
LEGACY = ".实时股价-目标价版.app.rollback-%s"


class LegacyRollbackPrune(SFCase):
    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # 目录名里带空格：路径里有空格是用户机器上的常态，别让函数只在干净路径上成立
        self.apps = pathlib.Path(holder.name) / "Apps dir"
        self.apps.mkdir()
        self.prune = pathlib.Path(holder.name) / "pruned"

    def _body(self):
        text = HELPER.read_text(encoding="utf-8")
        begin, end = "# BEGIN prune_rollbacks", "# END prune_rollbacks"
        self.assertIn(begin, text)
        self.assertIn(end, text)
        body = text.split(begin, 1)[1].split(end, 1)[0]
        self.assertIn("prune_rollbacks()", body, "标记之间不再是那个函数，测试前提变了")
        return body

    def _run(self):
        script = ("TARGET='%s'\nprune_dir='%s'\n%s\nprune_rollbacks\n"
                  % (self.apps / "Skyrocket.app", self.prune, self._body()))
        proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])

    def _make(self, name, bundle_id=None):
        d = self.apps / name
        d.mkdir()
        (d / "marker").write_text(name, encoding="utf-8")
        if bundle_id is not None:
            (d / "Contents").mkdir()
            (d / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
        return name

    def _left(self):
        return sorted(p.name for p in self.apps.iterdir())

    def _moved(self):
        return sorted(p.name for p in self.prune.iterdir()) if self.prune.is_dir() else []

    def test_old_named_rollbacks_are_pruned_and_only_the_newest_survives(self):
        old_a = self._make(LEGACY % 100, "com.stockfloat.targetprice")
        old_b = self._make(LEGACY % 200, "com.stockfloat.targetprice")
        newest = self._make(CURRENT % 300)
        self._run()
        self.assertEqual(self._left(), [newest])
        self.assertEqual(self._moved(), sorted([old_a, old_b]), "旧名回退包应被挪走而不是删除")

    def test_order_is_numeric_across_names_not_alphabetical(self):
        """跨名字比时间：epoch 位数不同时字典序会把 999 排在 1000 后面。"""
        older_current = self._make(CURRENT % 999)
        newer_legacy = self._make(LEGACY % 1000, "com.stockfloat.targetprice")
        self._run()
        self.assertEqual(self._left(), [newer_legacy], "留下的必须是数值最大的那份")
        self.assertEqual(self._moved(), [older_current])

    def test_both_generations_of_bundle_id_are_recognised(self):
        a = self._make(LEGACY % 10, "com.stockfloat.targetprice")
        b = self._make(".Another.app.rollback-20", "com.skyrocket.app")
        keep = self._make(CURRENT % 30)
        self._run()
        self.assertEqual(self._left(), [keep])
        self.assertEqual(self._moved(), sorted([a, b]))

    def test_other_vendors_hidden_rollback_directories_are_never_touched(self):
        foreign = self._make(".Other.app.rollback-5", "com.example.other")
        no_plist = self._make(".Mystery.app.rollback-6")
        mine_old = self._make(CURRENT % 100)
        mine_new = self._make(CURRENT % 200)
        self._run()
        self.assertEqual(self._left(), sorted([foreign, no_plist, mine_new]))
        self.assertEqual(self._moved(), [mine_old])

    def test_a_suffix_that_is_not_a_plain_epoch_is_not_ours(self):
        odd = self._make(".实时股价-目标价版.app.rollback-12ab", "com.stockfloat.targetprice")
        keep = self._make(CURRENT % 7)
        self._run()
        self.assertEqual(self._left(), sorted([odd, keep]))
        self.assertEqual(self._moved(), [])

    def test_a_lone_old_named_rollback_is_left_alone(self):
        """只有一份时它就是唯一的回退路径，哪怕名字是旧的。"""
        only = self._make(LEGACY % 42, "com.stockfloat.targetprice")
        self._run()
        self.assertEqual(self._left(), [only])
        self.assertEqual(self._moved(), [])
