"""选股门槛单测：分位数门槛（min_percentile）与绝对分门槛（min_score）的行为。

背景：price_sim=(corr+1)/2*100 把全市场分数压在 67~95 分，
      旧配置 min_score=60 实测 5202/5202 只全部通过（等于没有门槛），
      所以改为「当日截面分位数」控制候选池大小。
纯标准库，离线可跑：python tests/test_score_gate.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.screen.scorer import select_top  # noqa: E402

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


def mk(n: int, base: float = 95.0, step: float = 0.01, industry=None) -> list[dict]:
    out = []
    for i in range(n):
        d = {"code": "%06d" % i, "score": base - i * step}
        if industry:
            d["industry"] = industry[i % len(industry)]
        out.append(d)
    return out


def main() -> int:
    print("== 分位数门槛 ==")
    cfg = {"scoring": {"min_percentile": 0.02, "top_n": 5, "max_per_industry": 2}}
    scored = mk(1000)
    sel = select_top(scored, cfg)
    check("只取 top_n=5 只", len(sel) == 5, f"得到 {len(sel)}")
    check("取的是分数最高的 5 只",
          [s["code"] for s in sel] == ["000000", "000001", "000002", "000003", "000004"])
    check("分位数不改变排序（第1名仍是最高分）", sel[0]["score"] == 95.0)

    # 池子只是"上界"：比例算出来比 top_n 还小时，仍保证 top_n（宁可从池子边界取满，也不饿死）
    cfg_big = {"scoring": {"min_percentile": 0.01, "top_n": 50, "max_per_industry": 99}}
    sel_big = select_top(mk(1000), cfg_big)
    check("top_n=50 且 1%*1000=10 → 仍按 top_n 取 50 只（top_n 优先）",
          len(sel_big) == 50, f"得到 {len(sel_big)}")

    # ★ 重要事实：当"总是取 Top N"时，任何股票级门槛（分位数/绝对分）都不会改变结果 ——
    #   从"前 2%"里取前 5 只，和从全市场取前 5 只，得到的完全一样。
    cfg_small = {"scoring": {"min_percentile": 0.0001, "top_n": 5, "max_per_industry": 99}}
    no_gate = {"scoring": {"min_score": None, "top_n": 5, "max_per_industry": 99}}
    check("门槛不改变 Top5 的结果（数学上必然）",
          [s["code"] for s in select_top(mk(1000), cfg_small)]
          == [s["code"] for s in select_top(mk(1000), no_gate)])

    print("== 绝对分门槛（旧行为，保持向后兼容） ==")
    cfg_ms = {"scoring": {"min_score": 60.0, "top_n": 5, "max_per_industry": 2}}
    check("min_score=60 时高票全通过", len(select_top(mk(1000), cfg_ms)) == 5)
    cfg_hi = {"scoring": {"min_score": 99.0, "top_n": 5, "max_per_industry": 2}}
    check("min_score=99 时被全部过滤（返回空）", select_top(mk(1000), cfg_hi) == [])
    cfg_none = {"scoring": {"min_score": None, "top_n": 3, "max_per_industry": 2}}
    check("min_score=None 时不设门槛", len(select_top(mk(100), cfg_none)) == 3)

    print("== 行业分散仍然生效 ==")
    cfg_ind = {"scoring": {"min_percentile": 0.2, "top_n": 5, "max_per_industry": 2}}
    sel_ind = select_top(mk(100, industry=["银行", "医药"]), cfg_ind)
    banks = sum(1 for s in sel_ind if s["industry"] == "银行")
    check("同一行业不超过 2 只", banks <= 2, f"银行 {banks} 只")

    print("== 边界 ==")
    check("空输入返回空", select_top([], cfg) == [])
    check("score 缺失的票被忽略",
          len(select_top([{"code": "x"}, {"code": "y", "score": 90.0}], cfg)) == 1)

    print(f"\n全部通过: {PASS} 项" + (f"，失败 {FAIL} 项" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
