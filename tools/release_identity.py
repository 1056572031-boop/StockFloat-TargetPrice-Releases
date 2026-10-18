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
