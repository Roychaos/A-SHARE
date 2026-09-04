"""Phase 2 离线冒烟测试：信号触发/打分/选股/scan_result 存储（纯标准库合成数据）。

运行: python tests/test_phase2.py
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data import store as S  # noqa: E402
from src.patterns.similarity import pearson, template_sim  # noqa: E402
from src.screen.scorer import build_ctx, score_components, select_top  # noqa: E402
from src.signals import rules as R  # noqa: E402

PASS = []


def check(name: str, cond: bool, detail: str = ""):
    if not cond:
        raise AssertionError(f"[FAIL] {name} {detail}")
    PASS.append(name)
    print(f"  ok - {name}")


def rows_builder(n: int, close_fn, vol=1000.0, start="2024-01-01") -> list[dict]:
    d0 = dt.date.fromisoformat(start)
    out = []
    for i in range(n):
        c = float(close_fn(i))
        out.append({"date": (d0 + dt.timedelta(days=i)).isoformat(),
                    "open": c * 0.995, "high": c * 1.01, "low": c * 0.99,
                    "close": c, "volume": vol})
    return out


def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    S.init_db(conn)
    return conn


def test_pearson_sim():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    check("pearson identity", abs(pearson(a, a) - 1.0) < 1e-9)
    check("pearson anti", abs(pearson(a, [5.0, 4.0, 3.0, 2.0, 1.0]) + 1.0) < 1e-9)
    flat = [1.0, 1.0, 1.0]
    check("pearson flat None", pearson(flat, flat) is None)
    tpl = [{"id": 1, "w_close": "[1,2,3,4,5]", "w_vol": "[1,1,1,1,1]"}]
    res = template_sim([1, 2, 3, 4, 5], [1, 1, 1, 1, 1], tpl)
    check("tpl sim high", res["pattern_score"] is not None and res["pattern_score"] > 95)
    res_none = template_sim([1, 2, 3], [], [])
    check("no templates None", res_none["pattern_score"] is None)


def test_warm_volume_signal():
    rows = rows_builder(79, lambda i: 10.0)
    rows.append({"date": "2024-03-20", "open": 10.1, "high": 10.45, "low": 10.05,
                 "close": 10.4, "volume": 1600.0})
    ctx = build_ctx(rows)
    check("ctx built", ctx is not None and ctx["i"] == len(rows) - 1)
    sc = R.warm_volume_rise(ctx, {})
    check("warm_volume triggers", sc > 0, f"score={sc}")
    # 涨停区(涨幅>6%)不算温和放量
    rows[-1]["close"] = 11.0
    ctx2 = build_ctx(rows)
    check("warm excludes big jump", R.warm_volume_rise(ctx2, {}) == 0)


def test_platform_and_ma():
    rows = rows_builder(79, lambda i: 10.0)
    rows.append({"date": "2024-03-20", "open": 10.1, "high": 10.6, "low": 10.2,
                 "close": 10.6, "volume": 2000.0})
    ctx = build_ctx(rows)
    check("platform triggers", R.platform_breakout(ctx, {}) > 0)
    # 纯横盘序列：均线重叠、无金叉
    ctx_flat = build_ctx(rows_builder(80, lambda i: 10.0))
    check("ma cross none on flat", R.ma_bullish_init(ctx_flat, {}) == 0)


def test_obv_signal():
    rows = rows_builder(70, lambda i: 10.0 * (1.01 ** i))
    ctx = build_ctx(rows)
    check("obv high triggers", R.obv_high(ctx, {}) > 0)


def test_score_components_and_select():
    cfg = {"learning": {"window": 25}, "signals": {}, "scoring": {"min_score": 60.0, "top_n": 2,
           "max_per_industry": 2, "weights": {"pattern_sim": 0.4, "signal": 0.35, "trend": 0.25}}}
    rows = rows_builder(80, lambda i: 10.0 * (1.002 ** i))
    comp = score_components(rows, cfg, {"pattern_score": None})
    check("components dict", comp is not None and "total" in comp)
    scored = [
        {"code": "000001", "industry": "A", "total": 90.0, "signal_max": 90.0},
        {"code": "000002", "industry": "A", "total": 85.0, "signal_max": 80.0},
        {"code": "000003", "industry": "B", "total": 80.0, "signal_max": 70.0},
        {"code": "000004", "industry": "C", "total": 70.0, "signal_max": 90.0},
    ]
    top = select_top(scored, cfg)
    check("top_n=2 order", [s["code"] for s in top] == ["000001", "000002"])
    cfg["scoring"]["max_per_industry"] = 1
    cfg["scoring"]["top_n"] = 3
    top2 = select_top(scored, cfg)
    check("industry dedupe", [s["code"] for s in top2] == ["000001", "000003", "000004"])


def test_store_scan_and_close():
    conn = mem_conn()
    rows = [
        {"date": "2026-09-01", "code": "600519", "rank": 1, "score": 88.0,
         "sim_score": 90.0, "sig_score": 85.0, "trend_score": 80.0,
         "hits": ["platform_breakout"], "matched_tpl_id": 7,
         "best_tpl_code": "000858", "best_tpl_anchor": "2026-01-05"},
    ]
    n = S.replace_scan_results(conn, "2026-09-01", rows)
    check("scan persisted", n == 1)
    n2 = S.replace_scan_results(conn, "2026-09-01", rows)  # 幂等覆盖
    check("scan idempotent", n2 == 1 and conn.execute(
        "SELECT COUNT(*) FROM scan_result WHERE date='2026-09-01'").fetchone()[0] == 1)
    # close helpers
    S.upsert_daily_bars(conn, "600519", [
        {"date": "2026-09-01", "close": 10.0}, {"date": "2026-09-02", "close": 10.5}])
    check("close_on", S.close_on(conn, "600519", "2026-09-01") == 10.0)
    check("close_or_before", S.close_on_or_before(conn, "600519", "2026-09-05") == 10.5)
    check("close_or_before none", S.close_on_or_before(conn, "600519", "2026-08-01") is None)
    conn.close()


def main():
    print("== Phase 2 离线冒烟测试 ==")
    test_pearson_sim()
    test_warm_volume_signal()
    test_platform_and_ma()
    test_obv_signal()
    test_score_components_and_select()
    test_store_scan_and_close()
    print(f"\n全部通过: {len(PASS)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
