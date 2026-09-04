"""Phase 0 离线冒烟测试：仅用标准库（sqlite3/os/sys），无需安装 akshare/pandas。

运行: python tests/test_phase0.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data import store as S  # noqa: E402
from src.data import universe as U  # noqa: E402
from src.utils.retry import retry_call  # noqa: E402
from src.utils import calendar as cal  # noqa: E402
from src.config import cfg_get, env_secret  # noqa: E402

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


def test_store():
    conn = mem_conn()
    # 建表幂等
    S.init_db(conn)

    # meta 写入与 upsert 覆盖
    S.upsert_stock_meta(conn, [
        {"code": "600519", "name": "贵州茅台", "board": "SH主板", "is_st": False},
        {"code": "000001", "name": "平安银行", "board": "SZ主板", "is_st": False},
        {"code": "000004", "name": "*ST国农", "board": "SZ主板", "is_st": True},
    ])
    check("meta upsert", len(S.list_stock_meta(conn)) == 3)
    S.upsert_stock_meta(conn, [{"code": "600519", "name": "贵州茅台", "board": "SH主板", "is_st": False}])
    check("meta no-dup", len(S.list_stock_meta(conn)) == 3)

    # daily_bar 写入、去重替换、最新日期
    bars = [
        {"date": "2026-01-05", "open": 10.0, "high": 11.0, "low": 9.8, "close": 10.8,
         "volume": 1_000_000, "amount": 1.08e8, "pct_chg": 2.0},
        {"date": "2026-01-06", "open": 10.8, "high": 10.9, "low": 10.5, "close": 10.6,
         "volume": 800_000, "amount": 8.5e7, "pct_chg": -1.9},
    ]
    check("bar insert", S.upsert_daily_bars(conn, "600519", bars) == 2)
    bars[0]["close"] = 10.9  # 模拟当日数据被行情源修正后重拉 -> INSERT OR REPLACE
    S.upsert_daily_bars(conn, "600519", bars[:1])
    cur = conn.execute("SELECT close FROM daily_bar WHERE code='600519' AND date='2026-01-05'").fetchone()
    check("bar replace", cur[0] == 10.9)
    check("latest date", S.latest_bar_date(conn, "600519") == "2026-01-06")
    check("bar count", S.count_bars(conn) == 2)

    # list_date 回填
    S.backfill_list_date(conn, "600519")
    m = conn.execute("SELECT list_date FROM stock_meta WHERE code='600519'").fetchone()
    check("list_date backfill", m[0] == "2026-01-05")

    # fetch_log / run_log
    S.set_fetch_log(conn, "600519", "ok", last_ok_date="2026-01-06", bars=2)
    S.set_fetch_log(conn, "600519", "error", error="boom")
    row = conn.execute("SELECT status FROM fetch_log WHERE code='600519'").fetchone()
    check("fetch_log upsert", row[0] == "error")
    S.add_run_log(conn, "2026-01-06", "ok", "smoke")
    check("run_log", conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 1)
    conn.close()


def test_universe_pure():
    cases = {
        "600519": "SH主板", "601398": "SH主板", "603288": "SH主板", "605117": "SH主板",
        "000001": "SZ主板", "002594": "SZ主板", "003816": "SZ主板",
        "300750": "创业板", "301269": "创业板",
        "688981": "科创板", "689009": "科创板",
        "430047": None, "920002": None, "200002": None, "900901": None, "510300": None,
    }
    for code, want in cases.items():
        got = U.board_of(code)
        check(f"board_of {code}", got == want, f"got={got}")
    check("ST by name", U.is_st_by_name("*ST西发") is True)
    check("non-ST by name", U.is_st_by_name("平安银行") is False)
    metas = U.build_meta([{"code": "600519", "name": "贵州茅台"}, {"code": "300750", "name": "宁德时代"},
                          {"code": "830799", "name": "艾融软件"}, {"code": "000004", "name": "ST国农"}])
    check("build_meta drops non-board", len(metas) == 3)
    filt = U.filter_by_board(metas, ["创业板"])
    check("filter_by_board", [m["code"] for m in filt] == ["300750"])


def test_retry():
    calls = {"n": 0}

    def flaky_twice(x):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return x * 2

    check("retry then ok", retry_call(flaky_twice, 21, times=4, base_delay=0, sleep=lambda s: None) == 42)
    check("retry attempts", calls["n"] == 3)

    def always_fail():
        raise ValueError("always")

    try:
        retry_call(always_fail, times=2, base_delay=0, sleep=lambda s: None)
        check("retry raise", False)
    except ValueError:
        check("retry raise", True)


def test_calendar():
    conn = mem_conn()
    S.upsert_trade_cal_dates(conn, ["2026-01-05", "2026-01-06", "2026-01-07"])  # 周一~周三
    check("trading day true", cal.is_trading_day(conn, "2026-01-06") is True)
    check("holiday false", cal.is_trading_day(conn, "2026-01-10") is False)  # 周六
    check("iso str ok", cal.is_trading_day(conn, "2026-01-07") is True)
    conn.close()


def test_config_helpers():
    cfg = {"scoring": {"top_n": 5, "weights": {"a": 0.4}}, "push": {}}
    check("cfg_get deep", cfg_get(cfg, "scoring.top_n") == 5)
    check("cfg_get default", cfg_get(cfg, "nope.x", 7) == 7)
    check("cfg_get nested default", cfg_get(cfg, "push.channels", ["console"]) == ["console"])
    os.environ["ASHARE_TEST_SECRET"] = "abc"
    try:
        check("env_secret", env_secret("ASHARE_TEST_SECRET") == "abc")
        check("env_secret missing", env_secret("ASHARE_NO_SUCH") is None)
    finally:
        os.environ.pop("ASHARE_TEST_SECRET", None)


def test_backfill_list_date_order():
    # daily_bar 无日期排序假设：latest_bar_date 用 MAX(date) 字符串比较（ISO 序=时间序）
    conn = mem_conn()
    rows = [{"date": "2026-01-09", "close": 1.0}, {"date": "2026-01-02", "close": 1.0},
            {"date": "2026-01-20", "close": 1.0}]
    S.upsert_daily_bars(conn, "000001", rows)
    check("iso max date", S.latest_bar_date(conn, "000001") == "2026-01-20")
    conn.close()


def main():
    print("== Phase 0 离线冒烟测试 ==")
    test_store()
    test_universe_pure()
    test_retry()
    test_calendar()
    test_config_helpers()
    test_backfill_list_date_order()
    print(f"\n全部通过: {len(PASS)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
