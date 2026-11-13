"""分档带选择 + 持仓追踪单测。

固化两条设计决策（2026-11-13）：
  1) 选股只在"分数分位 [2%, 10%) 带"内取 —— 刻意跳过最顶尖的 2%
     （全样本分档：top0.5% +0.57% | 2~5% +2.23% | 5~10% +1.06%）
  2) 周度批次：T+1 开盘买入，持有 hold 个交易日后到期卖出

纯标准库，离线可跑：python tests/test_band_and_tracker.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data import store as S  # noqa: E402
from src.portfolio import tracker  # noqa: E402
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


def make_scored(n: int, base: float = 95.0, step: float = 0.01) -> list[dict]:
    return [{"code": f"{i:06d}", "score": base - i * step} for i in range(n)]


def test_band() -> None:
    print("== 分档带选择 ==")
    cfg = {"scoring": {"band": {"low": 0.02, "high": 0.10}, "top_n": 5,
                       "max_per_industry": 99}}
    scored = make_scored(1000)          # 1000 只，分数递减
    sel = select_top(scored, cfg)
    check("选出 5 只", len(sel) == 5, f"得到 {len(sel)}")
    codes = [s["code"] for s in sel]
    # 2% ~ 10% 带 = 索引 20..99（1000 只时），按分数从高到低取前 5
    check("跳过最顶尖的 2%（第1名不在结果里）", codes[0] == "000020", f"首位 {codes[0]}")
    check("带内按分数取前 5", codes == ["000020", "000021", "000022", "000023", "000024"],
          str(codes))
    top_score = scored[0]["score"]
    check("入选分数低于全市场最高分", all(s["score"] < top_score for s in sel))

    # 没有 band 时退回旧行为（取最顶尖）
    cfg2 = {"scoring": {"top_n": 5, "max_per_industry": 99}}
    sel2 = select_top(make_scored(1000), cfg2)
    check("无 band 时取最顶尖（兼容旧行为）", sel2[0]["code"] == "000000")

    # 极端情形：样本很少时 band 不会选出空
    check("样本很少时仍有输出", len(select_top(make_scored(12), cfg)) > 0)

    # 行业上限在带内仍然生效
    cfg3 = {"scoring": {"band": {"low": 0.0, "high": 1.0}, "top_n": 4,
                        "max_per_industry": 2}}
    scored3 = [{"code": f"{i:06d}", "score": 95 - i * 0.01,
                "industry": "银行" if i < 3 else "医药"} for i in range(10)]
    sel3 = select_top(scored3, cfg3)
    banks = sum(1 for s in sel3 if s["industry"] == "银行")
    check("同行业最多 2 只", banks <= 2, f"银行 {banks} 只")


def test_tracker() -> None:
    print("== 持仓追踪 ==")
    conn = sqlite3.connect(":memory:")
    S.init_db(conn)
    # 造 25 个交易日
    days = [f"2026-01-{i:02d}" for i in range(1, 26)]
    S.upsert_trade_cal_dates(conn, days)
    conn.commit()
    codes = ["600000", "600001", "600002"]
    for c in codes:
        conn.execute("INSERT OR REPLACE INTO stock_meta(code,name,is_st) VALUES(?,?,0)", (c, c))
        for k, d in enumerate(days):
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar(code,date,open,high,low,close,volume) "
                "VALUES(?,?,?,?,?,?,?)", (c, d, 10.0 + k * 0.1, 11.0 + k * 0.1,
                                          9.5 + k * 0.1, 10.5 + k * 0.1, 1000.0))
    conn.commit()

    decision = days[0]
    sel = [{"code": c, "score": 95.0 - i} for i, c in enumerate(codes)]
    res = tracker.record_batch(conn, decision, sel, hold=20)
    check("记录了 3 只持仓", res["records"] == 3, str(res))
    check("买入日为下一交易日", res["buy_date"] == days[1], str(res.get("buy_date")))
    check("到期日为买入后第 20 个交易日", res["due_date"] == days[20], str(res.get("due_date")))
    check("当前持仓数 = 3", S.open_position_count(conn) == 3)

    # 未到期不结算
    check("未到期时不结算", tracker.settle_due(conn, days[10]) == [])
    check("仍为 3 只持仓", S.open_position_count(conn) == 3)

    # 到期结算
    sold = tracker.settle_due(conn, days[20])
    check("到期结算 3 只", len(sold) == 3, str(len(sold)))
    check("结算后持仓归零", S.open_position_count(conn) == 0)
    check("收益已计算且为正（造的数据是单边上涨）",
          all((x.get("ret") or 0) > 0 for x in sold), str([x.get("ret") for x in sold]))

    # 幂等：同一天重复记录会覆盖而不是翻倍
    tracker.record_batch(conn, decision, sel, hold=20)
    tracker.record_batch(conn, decision, sel, hold=20)
    check("重复记录同一批次不会重复计数", S.open_position_count(conn) == 3)
    conn.close()


def main() -> int:
    test_band()
    test_tracker()
    print(f"\n全部通过: {PASS} 项" + (f"，失败 {FAIL} 项" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
