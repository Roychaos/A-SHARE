"""Phase 1 离线冒烟测试：锚点定义/模板提取/落库幂等（纯标准库 + 合成数据）。

运行: python tests/test_phase1.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data import store as S  # noqa: E402
from src.patterns.templates import (  # noqa: E402
    _rolling_max, _rolling_mean, _slope_pct, _zscore,
    anchor_date_bounds, extract_templates,
)

PASS = []


def check(name: str, cond: bool, detail: str = ""):
    if not cond:
        raise AssertionError(f"[FAIL] {name} {detail}")
    PASS.append(name)
    print(f"  ok - {name}")


def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    S.init_db(conn)
    return conn


def fake_dates(n: int = 300, start: str = "2024-01-01") -> list[str]:
    d = dt.date.fromisoformat(start)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


def add_stock(conn, code: str, dates: list[str], close_fn, vol: float = 1000.0, is_st: bool = False):
    rows = [
        {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": vol,
         "amount": c * vol * 100, "pct_chg": 0.0}
        for d, c in ((d, float(close_fn(i))) for i, d in enumerate(dates))
    ]
    S.upsert_daily_bars(conn, code, rows)
    name = "*ST测试" if is_st else f"测试{code}"
    S.upsert_stock_meta(conn, [{"code": code, "name": name, "board": "SZ主板", "is_st": is_st}])


def test_helpers():
    z = _zscore([1.0, 2.0, 3.0, 4.0])
    check("zscore mean0", abs(sum(z)) < 1e-9)
    check("zscore std1", abs(sum(x * x for x in z) / len(z) - 1.0) < 1e-9)
    rm = _rolling_mean([1.0, 2.0, 3.0, 4.0], 2)
    check("rolling_mean", rm == [None, 1.5, 2.5, 3.5])
    rmax = _rolling_max([1.0, 5.0, 3.0, 2.0], 2)
    check("rolling_max", rmax[1] == 5.0 and rmax[2] == 5.0 and rmax[3] == 3.0)
    slope = _slope_pct([10.0, 11.0, 12.0, 13.0])
    check("slope positive", slope is not None and slope > 0)


def _base_cfg(**learning_overrides) -> dict:
    lr = {
        "lookback_days": 250, "forward_days": 10, "min_fwd_return": 0.09,
        "anchor": {"day_gain_min": 0.02, "prior10_gain_max": 0.03,
                   "prior60_range": [-0.05, 0.25]},
        "window": 25, "max_templates_per_day": 20,
    }
    lr.update(learning_overrides)
    return {"learning": lr}


def test_extract_anchor_and_exclusions():
    conn = mem_conn()
    dates = fake_dates(300)
    S.upsert_trade_cal_dates(conn, dates)

    # 600001: 前85日横盘(0%) -> 第85日 +2.5% 启动 -> 之后10日每日+1.5%（未来10日≈16%）
    def spike(i: int) -> float:
        if i < 85:
            return 10.0
        if i == 85:
            return 10.25
        if i <= 95:
            return 10.25 * (1.015 ** (i - 85))
        return 10.25 * 1.015 ** 10 * (1.001 ** (i - 95))  # 之后微涨，不再触发

    add_stock(conn, "600001", dates, spike)

    # 600002: 全程单边上涨（前60日涨幅超限 + 日涨幅可能<2%均不达锚点）
    def trending(i: int) -> float:
        return 10.0 * (1.01 ** i)

    add_stock(conn, "600002", dates, trending)

    # 600003: 形态同600001 但标记为 ST -> 应被剔除
    add_stock(conn, "600003", dates, spike, is_st=True)

    cfg = _base_cfg()
    res = extract_templates(conn, cfg, quiet=True)
    stats, tpl = res["stats"], res["templates"]

    check("only one anchor", stats["anchors_total"] == 1, f"got {stats['anchors_total']}")
    check("anchor is 600001", tpl[0]["code"] == "600001", tpl[0]["code"])
    check("anchor date", tpl[0]["anchor_date"] == dates[85])
    w_close = json.loads(tpl[0]["w_close"])
    w_vol = json.loads(tpl[0]["w_vol"])
    check("w_close len=W", len(w_close) == 25, f"len={len(w_close)}")
    check("w_vol len=W", len(w_vol) == 25)
    check("fwd ~16%", abs(tpl[0]["fwd_ret_10d"] - (1.015 ** 10 - 1)) < 0.005,
          f"got {tpl[0]['fwd_ret_10d']}")
    feat = json.loads(tpl[0]["feat"])
    check("feat has slope20", "slope20" in feat)
    conn.close()


def test_daily_cap():
    conn = mem_conn()
    dates = fake_dates(300)
    S.upsert_trade_cal_dates(conn, dates)

    def mk(mod: float, post: float):
        def f(i: int) -> float:
            if i < 85:
                return 10.0
            if i == 85:
                return 10.0 * (1 + mod)
            if i <= 95:
                return 10.0 * (1 + mod) * ((1 + post) ** (i - 85))
            return 10.0 * (1 + mod) * ((1 + post) ** 10)
        return f

    add_stock(conn, "600100", dates, mk(0.025, 0.01))   # 启动温和 -> 未来10日≈10.5%
    add_stock(conn, "600101", dates, mk(0.03, 0.02))    # 启动更强 -> 未来10日≈21.9%
    cfg = _base_cfg(max_templates_per_day=1)
    res = extract_templates(conn, cfg, quiet=True)
    tpl = res["templates"]
    check("cap keeps 1/day", len(tpl) == 1, f"got {len(tpl)}")
    check("cap keeps stronger", tpl[0]["code"] == "600101", tpl[0]["code"])
    conn.close()


def test_replace_idempotent():
    conn = mem_conn()
    row = {"code": "600001", "anchor_date": "2025-06-01", "fwd_ret_10d": 0.16,
           "w_close": "[1,2]", "w_vol": "[3,4]", "feat": "{}"}
    n1 = S.replace_templates_since(conn, "2025-01-01", [row, dict(row, code="600002")])
    n2 = S.replace_templates_since(conn, "2025-01-01", [row])
    check("replace counts", n1 == 2 and n2 == 1)
    check("no duplicates after rerun", S.count_templates(conn) == 1,
          f"got {S.count_templates(conn)}")
    conn.close()


def test_anchor_bounds():
    conn = mem_conn()
    dates = fake_dates(300)
    S.upsert_trade_cal_dates(conn, dates)
    params = {"lookback_days": 250, "forward_days": 10, "window": 25}
    load_from, start, end = anchor_date_bounds(conn, params)
    check("anchor_end excludes future window", end == dates[-11])
    check("anchor_start back 260", start == dates[40])
    check("load_from <= anchor_start", load_from <= start)
    conn.close()


def main():
    print("== Phase 1 离线冒烟测试 ==")
    test_helpers()
    test_extract_anchor_and_exclusions()
    test_daily_cap()
    test_replace_idempotent()
    test_anchor_bounds()
    print(f"\n全部通过: {len(PASS)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
