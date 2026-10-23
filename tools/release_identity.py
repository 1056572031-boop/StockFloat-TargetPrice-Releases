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
