"""Read public identity literals without importing Qt or executing product code."""
from __future__ import annotations

import ast
from pathlib import Path


def load_identity(path: Path | None = None) -> dict[str, str]:
    path = path or Path(__file__).resolve().parents[1] / "sf_constants.py"
    required = {"RELEASE_REPO", "RELEASE_TAG", "SUPPORT_EMAIL", "UPDATE_MANIFEST_NAME"}
    values = {}
    for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in required:
                value = ast.literal_eval(node.value)
                if not isinstance(value, str) or not value:
                    raise ValueError("Public identity must be a nonempty string: " + target.id)
                if target.id in values:
                    raise ValueError("Duplicate public identity: " + target.id)
                values[target.id] = value
    if required - values.keys():
        raise ValueError("Missing public identity: " + ", ".join(sorted(required - values.keys())))
    return values


IDENTITY = load_identity()
REPO = IDENTITY["RELEASE_REPO"]
TAG = IDENTITY["RELEASE_TAG"]
SUPPORT_EMAIL = IDENTITY["SUPPORT_EMAIL"]
MANIFEST_PATH = IDENTITY["UPDATE_MANIFEST_NAME"]
# 本版更新说明字数上限（含标题行、去空白）。2026-10-23 用户把永久规则从 100 字放宽到 200 字；
# 全仓只有这一处数值，闸门、公开资料检查、自检边界值都从这里取或按它推算。
NOTES_MAX_CHARS = 200


def count_notes_chars(text: str) -> int:
    """本版更新说明的字数：去掉所有空白后的字符数，标题行和编号“1.”一起算。

    闸门（make_update_assets）与公开资料检查（public_docs_check）各自截取本版段落，
    但数字数只许有这一份实现：两处口径一旦不同，同一份说明会在一处过、另一处红。
    超限时两处的报错都带实际字数、上限和超出几字，不必再另写脚本去数。
    """
    return len("".join(text.split()))


def notes_over_limit_detail(count: int, limit: int = NOTES_MAX_CHARS) -> str:
    """超限报错共用的一句话：实际字数、上限、超出几字，并写明哪些内容计入。"""
    return ("%d characters, limit %d, over by %d "
            "(whitespace is not counted; the title line and item numbers are)"
            % (count, limit, count - limit))


# 同一版本号再发布（例如把已发的 Beta 同号转正）时，上一轮的归档文件夹 outputs/release-candidates/vX.Y.Z
# 已经在主线上，归档工具不覆盖同名异字节文件。再发布那一轮带一个标签，证据放进并列的 vX.Y.Z-标签。
# 全仓只有这一处决定文件夹叫什么：post-release 记录、归档、完成记录的范围三处都从这里取。
ARCHIVE_LABEL_MAX = 24


def validate_archive_label(label: str) -> str:
    """小写字母、数字和单个连字符，首尾是字母或数字；不许路径分隔、点号、空白或大写。"""
    if (not isinstance(label, str) or not label or len(label) > ARCHIVE_LABEL_MAX
            or not all(part and part.isalnum() and part.isascii() and part == part.lower()
                       for part in label.split("-"))):
        raise ValueError("归档标签只能是小写字母、数字和单个连字符（最长 %d 个字符）：%r"
                         % (ARCHIVE_LABEL_MAX, label))
    return label


def archive_folder_name(version: str, label: str | None = None) -> str:
    """归档文件夹名：不带标签是 vX.Y.Z（与以往逐字相同），带标签是 vX.Y.Z-标签。"""
    return "v" + version if label is None else "v%s-%s" % (version, validate_archive_label(label))
