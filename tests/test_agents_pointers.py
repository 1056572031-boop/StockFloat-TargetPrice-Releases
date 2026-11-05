"""AGENTS.md 拆分后，指引必须有效、移出的内容不随公开源码发布、总览不再长回去。

2026-11-05 之前 AGENTS.md 有八万多字符，每轮都进上下文，而其中大半是“动对应代码前再读”的
参考材料。拆开后靠三件事守住：指引指向的文件真的存在；同号小节在两边都找得到
（仓库里有一百多处写着“AGENTS §6 第 N 条”之类的引用）；总览超过预算就红，
逼着新增内容走“放进 docs/agents/ 再留一行指引”的路。
"""
from pathlib import Path
import re
import sys

from tests._base import SFCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import public_release as public  # noqa: E402

AGENTS = ROOT / "AGENTS.md"
WORKFLOW = ROOT / "docs" / "RELEASE_PREPARATION_WORKFLOW.md"
AGENTS_DIR = ROOT / "docs" / "agents"

# 每轮都进上下文的总览预算（字符数）。超了就把新增内容放进 docs/agents/ 并在这里留一行指引；
# 确需调高，改这个数并在提交说明里写明为什么。
AGENTS_MAX_CHARS = 30000

# (移出后的文件, 该文件里的小节标题前缀, AGENTS.md 里留下的对应行前缀)
MOVED_HEADINGS = (
    [("RELEASE_TOOLS.md", "## 3.1 ", "## 3.1 ")]
    + [("LESSONS.md", "## 5.%d " % n, "- 5.%d " % n) for n in range(2, 11)]
    + [("TESTING.md", "## 6. ", "## 6. "), ("NAMING.md", "## 7. ", "## 7. ")]
)


def pointer_names(text):
    return set(re.findall(r"agents/([A-Za-z_]+\.md)", text))


class AgentsPointersStayValid(SFCase):
    def test_every_pointer_in_the_overview_and_the_workflow_resolves(self):
        found_in_agents = pointer_names(AGENTS.read_text(encoding="utf-8"))
        found_in_workflow = pointer_names(WORKFLOW.read_text(encoding="utf-8"))
        # 正向对照：指引表自己取到空，“全部存在”就会无条件成立
        self.assertEqual(found_in_agents, {"RELEASE_TOOLS.md", "LESSONS.md", "TESTING.md", "NAMING.md"})
        self.assertEqual(found_in_workflow, {"RELEASE_LESSONS.md"})
        missing = sorted(name for name in found_in_agents | found_in_workflow
                         if not (AGENTS_DIR / name).is_file())
        self.assertEqual(missing, [], "指引指向了不存在的文件")

    def test_same_numbered_headings_exist_on_both_sides(self):
        agents_lines = AGENTS.read_text(encoding="utf-8").splitlines()
        for name, moved_prefix, stub_prefix in MOVED_HEADINGS:
            with self.subTest(file=name, heading=moved_prefix):
                moved_lines = (AGENTS_DIR / name).read_text(encoding="utf-8").splitlines()
                self.assertTrue(any(line.startswith(moved_prefix) for line in moved_lines),
                                "移出的文件里找不到小节 " + moved_prefix)
                self.assertTrue(any(line.startswith(stub_prefix) for line in agents_lines),
                                "AGENTS.md 里没留下同号指引 " + stub_prefix)

    def test_moved_files_are_never_part_of_the_public_snapshot(self):
        moved = sorted(AGENTS_DIR.glob("*.md"))
        self.assertTrue(moved, "docs/agents/ 下一个文件都没有，下面的“全部不外发”会无条件成立")
        for path in moved:
            with self.subTest(file=path.name):
                self.assertTrue(public.export_excluded("docs/agents/" + path.name))

    def test_overview_stays_within_its_budget(self):
        size = len(AGENTS.read_text(encoding="utf-8"))
        self.assertLessEqual(
            size, AGENTS_MAX_CHARS,
            "AGENTS.md 已有 %d 字符，超过预算 %d：新增内容放进 docs/agents/，这里只留一行指引" % (size, AGENTS_MAX_CHARS))
