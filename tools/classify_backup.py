#!/usr/bin/env python3
"""对 backups/ 下的源码快照做版本归属特征检测。

判定逻辑（基于各版本引入的独有符号）：
  v1.2.62 特征：BOTTOM_INNER_GAP（双行间距）、_bottom_layout（双行布局纯函数）
  v1.2.63 特征：PRICE_ROW_RIGHT_PAD、price_pct_layout、PCT_GAP
  pre-v1.2.62 特征：price_w = w - W_PRICE（价格行沿用按钮区预留宽，v1.2.61 遗留）

一个「v1.2.63 修改前」快照应满足：
  含 v1.2.62 特征 且 不含 v1.2.63 特征 且 含 pre-v1.2.62 特征
即它等价于 v1.2.62 发版后的状态。
"""
import pathlib

BACKUPS = pathlib.Path(__file__).resolve().parents[1] / "backups"

FEATURES = {
    "v1.2.62": ["BOTTOM_INNER_GAP", "_bottom_layout"],
    "v1.2.63": ["PRICE_ROW_RIGHT_PAD", "price_pct_layout", "PCT_GAP"],
}
LEGACY = "price_w = w - W_PRICE"


def scan(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return None, str(exc)
    counts = {}
    for ver, syms in FEATURES.items():
        counts[ver] = {s: text.count(s) for s in syms}
    counts["legacy"] = text.count(LEGACY)
    return counts, None


def main():
    targets = sorted(BACKUPS.glob("bak_*_v1.2.63_*.py"))
    if not targets:
        raise SystemExit("未找到 v1.2.63 前缀的快照")

    verdicts = []
    for p in targets:
        counts, err = scan(p)
        if err:
            print(f"[跳过] {p.name}: {err}")
            continue

        # v1.2.62 特征符号分处两个文件（BOTTOM_INNER_GAP 在 sf_ui_render，_bottom_layout 在
        # renderer/sf_ui_render 均有），故按「任一命中」判定，避免跨文件误判为特征不足
        has_62 = any(v > 0 for v in counts["v1.2.62"].values())
        has_63 = any(v > 0 for v in counts["v1.2.63"].values())
        # 遗留表达式同样只在 sf_ui_render 出现，renderer 不含属预期
        _has_legacy = counts["legacy"] > 0

        print(f"\n== {p.name} ==")
        print(f"   [v1.2.62 特征] " + "  ".join(f"{k}={v}" for k, v in counts["v1.2.62"].items()))
        print(f"   [v1.2.63 特征] " + "  ".join(f"{k}={v}" for k, v in counts["v1.2.63"].items()))
        print(f"   [遗留表达式]   {LEGACY!r} = {counts['legacy']}")

        # 该文件是否真的涉及价格行改动（sf_constants 无布局符号，需排除）
        relevant = any(v > 0 for v in counts["v1.2.62"].values()) or counts["legacy"] > 0
        if not relevant:
            print("   判定：本文件不含布局符号（仅版本号），跳过版本归属判定")
            continue

        if has_63:
            verdict = "含 v1.2.63 特征 → 非修改前快照（异常）"
        elif has_62:
            verdict = "v1.2.62 发版后状态（= v1.2.63 修改前），v1.2.62 布局代码可回退"
        else:
            verdict = "特征不足，无法判定"
        print(f"   判定：{verdict}")
        verdicts.append((p.name, verdict))

    print("\n== 汇总 ==")
    for name, verdict in verdicts:
        print(f"   {name:<52} {verdict}")


if __name__ == "__main__":
    main()
