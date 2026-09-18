"""相似度批量算分单测：template_sim_batch（numpy）必须与逐只 template_sim 结果一致。

这是"给回测提速"的前提 —— 换实现不能换结果。
纯标准库 + 可选 numpy，离线可跑：python tests/test_similarity_batch.py
"""
from __future__ import annotations

import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.patterns.similarity import template_sim, template_sim_batch  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_templates(n: int, win: int, seed: int = 11) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        close = [round(rng.gauss(0, 1), 3) for _ in range(win)]
        vol = [round(rng.uniform(0.5, 2.0), 3) for _ in range(win)]
        out.append({"id": i + 1, "code": f"T{i:04d}", "anchor_date": f"2026-01-{(i % 28) + 1:02d}",
                    "fwd_ret_10d": 0.1 + i * 0.001,
                    "w_close": json.dumps(close), "w_vol": json.dumps(vol)})
    return out


def main() -> int:
    win = 25
    templates = make_templates(60, win)
    rng = random.Random(7)
    now_close = [[round(rng.gauss(0, 1), 3) for _ in range(win)] for _ in range(40)]
    now_vol = [[round(rng.uniform(0.5, 2.0), 3) for _ in range(win)] for _ in range(40)]

    print("== 批量 vs 逐只：数值一致性 ==")
    single = [template_sim(now_close[i], now_vol[i], templates, top_matches=3,
                           w_price=0.7, w_vol=0.3) for i in range(len(now_close))]
    batch = template_sim_batch(now_close, now_vol, templates, top_matches=3,
                              w_price=0.7, w_vol=0.3)

    check("返回条数一致", len(batch) == len(single))
    max_diff = max(abs((a["pattern_score"] or 0) - (b["pattern_score"] or 0))
                   for a, b in zip(single, batch))
    check(f"pattern_score 最大偏差 ≤0.05（实测 {max_diff:.4f}）", max_diff <= 0.05)
    same_best = sum(1 for a, b in zip(single, batch) if a["best_tpl_id"] == b["best_tpl_id"])
    check(f"最像模板一致（{same_best}/{len(single)}）", same_best == len(single),
          f"不一致 {len(single) - same_best} 条")
    check("best_score 偏差 ≤0.05",
          max(abs((a["best_score"] or 0) - (b["best_score"] or 0)) for a, b in zip(single, batch)) <= 0.05)

    print("== 边界情形 ==")
    check("模板为空 → 全部 None",
          all(v["pattern_score"] is None for v in template_sim_batch(now_close, now_vol, [])))
    check("股票为空 → 空列表", template_sim_batch([], [], templates) == [])

    # 模板无 w_vol：逐只走 vol_sim=50 分支，批量必须一致
    no_vol = [dict(t, w_vol="") for t in templates]
    s2 = [template_sim(now_close[i], now_vol[i], no_vol) for i in range(5)]
    b2 = template_sim_batch(now_close[:5], now_vol[:5], no_vol)
    check("模板缺量能时一致（vol_sim=50 分支）",
          max(abs(a["pattern_score"] - b["pattern_score"]) for a, b in zip(s2, b2)) <= 0.05)

    # 模板长度与窗口不一致：逐只会跳过，批量也必须跳过
    short = make_templates(10, win - 3)
    b3 = template_sim_batch(now_close[:3], now_vol[:3], short)
    check("长度不匹配的模板被跳过（返回 None）",
          all(v["pattern_score"] is None for v in b3))

    # 分块边界：chunk 小于总数时结果不变
    b4 = template_sim_batch(now_close, now_vol, templates, chunk=3)
    check("分块计算不影响结果",
          max(abs(a["pattern_score"] - b["pattern_score"]) for a, b in zip(batch, b4)) <= 0.05)

    print(f"\n全部通过: {PASS} 项" + (f"，失败 {FAIL} 项" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
