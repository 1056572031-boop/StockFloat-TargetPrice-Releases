# -*- coding: utf-8 -*-
"""身份字段"两代都认"的漂移锁（v1.4.64，全方位复核）。

AGENTS §3 把"改名要两代都认"列了七处落点，并写明漏一处的后果都看得见
（漏第六处 → 1.4.55 起已装用户一次都没自更新成功；漏第七处 → 装好的新版被回滚）。
问题是**没有任何东西回答"还有第几处没进清单"**：这些名字散在 spec、shell、
以及不能提前 import sf_constants 的工具里，各写一份字面量。

这一条用例做两件事：
  1. 把"当前这一代"的每个字面量与 sf_constants 对拍 —— 改了唯一来源而漏了某处，红；
  2. 把"必须两代都认"的那几处按名字点出来 —— 少认一代，红。
两类都配了种错必红的自证（CriteriaCanFail），否则这又是一枚恒真判据。

数据目录（~/Library/Application Support/StockFloatTargetPrice）**故意不改名**
（AGENTS §3：换目录等于让用户重录目标价）。但 tools/ 里几枚探针工具在
import sf_constants **之前**就要把落盘物隔离到临时目录，所以它们必须写死路径 ——
那是设计约束，不是抄漏，于是这一族锁"目录名与 APP_SUPPORT_DIR 尾部一致"。
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_constants as C  # noqa: E402

try:
    from _base import SFCase  # noqa: E402
except ImportError:  # 以包形式被 discover 导入时
    from tests._base import SFCase  # noqa: E402

# 这些文件里的身份字面量是**故意**写死的：spec 在打包期不能 import 产品模块，
# shell 助手跑在用户机器上（不保证有解释器），双击工具自包含，
# 探针类工具必须在 import sf_constants 之前把落盘物隔离掉。
CURRENT_GEN_SITES = (
    "StockFloat.spec", "run_tests.sh", "build_release.sh",
    "updater/update_helper.sh", "tools/cleanup_temp_copies.command",
    "tools/verify_responsiveness.command", "sf_bootstrap.py",
)
# 必须同时认"当前这一代可执行名 + 旧那一代"的地方：漏任一代都有实测后果
BOTH_GEN_EXEC_SITES = ("run_tests.sh", "sf_logging.py", "sf_crashreport.py")
DATA_DIR = "StockFloatTargetPrice"     # 数据目录名，故意不随包改名
# 归档件与回退副本不参与：AGENTS §3.1/§5 写明它们不代表当前需求
SCAN_SKIP = ("backups/", "tools/archive/", ".venv/", "build/", "dist/", "tests/")
# 拆分前的老配置文件位置：只读迁移源，有 os.path.exists 守卫，正当保留
DATA_DIR_ALLOWED = ("StockFloat",)
# This directory contains publisher-only keys, not the application's user data.
BUILD_ONLY_DATA_DIRS = {"tools/stable_signing.py": {"SkyrocketBuildSigning"}}


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def stale_bundle_tokens(text: str) -> list[str]:
    """文中出现的 com.* 包标识里，既不是当前值也不是旧值的那些（= 写歪了）。"""
    out = []
    for token in sorted(set(re.findall(r"com\.[a-z0-9.\-]+", text))):
        if token == C.BUNDLE_ID or token in C.LEGACY_BUNDLE_IDS:
            continue
        if "github" in token or token.endswith((".app.bundle", ".apple", ".example")):
            continue
        out.append(token)
    return out


# 从 sf_constants 派生名字的写法（import 了这些常量）算"已经认了两代"：
# 字面量不该是唯一的合规形式，否则等于逼着每个模块都抄一份字符串。
DERIVED_MARKERS = ("LEGACY_EXECUTABLE_NAMES", "LEGACY_APP_DISPLAY_NAMES",
                   "LEGACY_BUNDLE_IDS", "CRASH_NAME_PREFIXES", "CRASH_PREFIXES")


def missing_current_gen(text: str) -> list[str]:
    """认得旧名字、却没写当前这一代 → 返回缺的字段名。"""
    out = []
    derived = any(m in text for m in DERIVED_MARKERS)
    pools = {"APP_DISPLAY_NAME": [*C.LEGACY_APP_DISPLAY_NAMES,
                                  *C.LEGACY_EXECUTABLE_NAMES],
             "BUNDLE_ID": list(C.LEGACY_BUNDLE_IDS)}
    current = {"APP_DISPLAY_NAME": C.APP_DISPLAY_NAME, "BUNDLE_ID": C.BUNDLE_ID}
    for field, legacy in pools.items():
        mentions = any(n in text for n in legacy) or derived
        if mentions and current[field] not in text and not derived:
            out.append(field)
    return out


def missing_exec_generation(text: str) -> list[str]:
    need = [C.APP_DISPLAY_NAME, *C.LEGACY_EXECUTABLE_NAMES]
    return [n for n in need if n not in text]


def data_dir_tokens(text: str) -> list[str]:
    return re.findall(r"Library/Application Support/([A-Za-z0-9_\-]+)", text)


class CurrentGenerationIsConsistent(SFCase):
    """写死当前身份的字面量必须真的等于 sf_constants 里的值。"""

    def test_no_site_names_a_bundle_id_that_is_neither_generation(self):
        bad = []
        for rel in CURRENT_GEN_SITES:
            for token in stale_bundle_tokens(read(rel)):
                bad.append("%s 里的包标识 %s 既不是当前值也不是旧值" % (rel, token))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_every_site_that_knows_the_old_name_knows_the_new_one(self):
        gaps = []
        for rel in CURRENT_GEN_SITES:
            gaps += ["%s 缺 %s" % (rel, f) for f in missing_current_gen(read(rel))]
        self.assertEqual(gaps, [],
                         "改了 sf_constants 却没同步这些写死点：\n   " + "\n   ".join(gaps))

    def test_data_dir_literals_match_the_single_source(self):
        """几枚工具在 import sf_constants 之前就要用路径，所以写死；这里锁一致。"""
        self.assertTrue(C.APP_SUPPORT_DIR.endswith("/" + DATA_DIR),
                        "sf_constants.APP_SUPPORT_DIR 尾部变了，这些写死点要同步")
        users = []
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if any(p in rel for p in SCAN_SKIP):
                continue
            for hit in data_dir_tokens(
                    path.read_text(encoding="utf-8", errors="replace")):
                if (hit != DATA_DIR and hit not in DATA_DIR_ALLOWED
                        and hit not in BUILD_ONLY_DATA_DIRS.get(rel, set())):
                    users.append("%s 用了 Application Support/%s" % (rel, hit))
        self.assertEqual(users, [], "\n".join(users))

    def test_publisher_signing_keys_are_not_application_user_data(self):
        from tools import stable_signing
        self.assertFalse(stable_signing.STATE.resolve().is_relative_to(
            pathlib.Path(C.APP_SUPPORT_DIR).resolve()))
        self.assertFalse(stable_signing.STATE.resolve().is_relative_to(ROOT))


class BothGenerationsAreRecognized(SFCase):
    """"两代都认"的落点：少认一代就是 v1.4.55 那批事故的形状。"""

    def test_exec_name_sites_recognize_both_generations(self):
        gaps = []
        for rel in BOTH_GEN_EXEC_SITES:
            text = read(rel)
            if any(m in text for m in DERIVED_MARKERS):
                continue        # 派生自 sf_constants，字面量本来就不该在这里
            gaps += ["%s 没写到 %s" % (rel, n) for n in missing_exec_generation(text)]
        self.assertEqual(gaps, [],
                         "这些落点必须两代可执行名都认（漏一处 = 旧崩溃记录翻不到，"
                         "或旧副本被 LaunchServices 重新拉起）：\n   " + "\n   ".join(gaps))

    def test_log_show_recipe_names_the_real_process(self):
        """sf_logging 教人用 log show 查崩溃，那句里的进程名必须是真名。"""
        import sf_logging
        doc = sf_logging.log_line.__doc__ or ""
        self.assertIn(C.APP_DISPLAY_NAME, doc,
                      "log show 那行还写着旧进程名 —— 照抄的人查不到任何记录")


class CriteriaCanFail(SFCase):
    """判据本身要能红：把三种真实漏法各种一处。"""

    def test_legacy_only_bundle_is_caught(self):
        text = 'BUNDLE_ID="%s"\n' % C.LEGACY_BUNDLE_IDS[0]
        self.assertIn("BUNDLE_ID", missing_current_gen(text),
                      "只写旧包标识却没报缺当前这一代 → 这条锁是恒真的")

    def test_wrong_bundle_token_is_caught(self):
        self.assertIn("com.not.ours.app",
                      stale_bundle_tokens('id="com.not.ours.app"'),
                      "冒出来的第三个包标识没被发现 → 对拍失真")

    def test_missing_legacy_exec_generation_is_caught(self):
        text = 'x = "%s"\n' % C.APP_DISPLAY_NAME
        self.assertEqual(missing_exec_generation(text), list(C.LEGACY_EXECUTABLE_NAMES),
                         "只认当前一代（正是本轮修掉的那处漏法）却没报")

    def test_wrong_data_dir_is_caught(self):
        hits = data_dir_tokens('p = "~/Library/Application Support/OtherDir/config.json"')
        self.assertNotEqual(hits, [DATA_DIR],
                            "目录名写错了却没被发现 → 尾部一致性那条判据恒真")

    def test_clean_writings_pass(self):
        """反向：两代都写全的文本必须放行，否则这条锁会变成每轮必红的死闸。"""
        text = ("%s %s %s %s ~/Library/Application Support/%s"
                % (C.APP_DISPLAY_NAME, C.BUNDLE_ID, C.LEGACY_BUNDLE_IDS[0],
                   C.LEGACY_EXECUTABLE_NAMES[0], DATA_DIR))
        self.assertEqual(missing_current_gen(text), [], "两代都认却被判缺")
        self.assertEqual(stale_bundle_tokens(text), [], "合法包标识被判歪")
        self.assertEqual(missing_exec_generation(text), [], "两代都在却报缺")
        self.assertEqual(data_dir_tokens(text), [DATA_DIR])
