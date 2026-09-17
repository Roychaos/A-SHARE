"""系统体检：数据库/模板/选股结果/产物/代码 一屏看全（纯标准库，不联网）。

用法: python scripts/audit.py [--db data/screener.db]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from glob import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def main() -> int:
    ap = argparse.ArgumentParser(description="系统体检")
    ap.add_argument("--db", default="data/screener.db", help="数据库路径")
    ap.add_argument("--output", default="output", help="产物目录")
    args = ap.parse_args()

    print("=" * 62)
    print("A股量价选股 Agent —— 系统体检")
    print("=" * 62)

    # 1) 数据库文件
    if not os.path.exists(args.db):
        print(f"[数据库] 不存在: {args.db}")
        return 1
    size_mb = os.path.getsize(args.db) / 1024 / 1024
    print(f"\n[数据库] {args.db}  {size_mb:.1f} MB")
    conn = sqlite3.connect(args.db)
    for t in ("stock_meta", "trade_cal", "daily_bar", "template", "scan_result", "run_log", "fetch_log"):
        try:
            n = _q(conn, f"SELECT COUNT(*) FROM {t}")[0]
            print(f"  - {t:<12} {n:>9}")
        except sqlite3.Error as exc:
            print(f"  - {t:<12} 读取失败: {exc}")

    # 2) 行情覆盖
    codes, bars = _q(conn, "SELECT COUNT(DISTINCT code), COUNT(*) FROM daily_bar")
    dmin, dmax = _q(conn, "SELECT MIN(date), MAX(date) FROM daily_bar")
    print(f"\n[行情] 股票 {codes} 只 / K线 {bars} 根")
    print(f"  日期范围: {dmin} ~ {dmax}")
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT 12")]
    print(f"  最近12个交易日: {', '.join(reversed(dates))}")
    if len(dates) >= 2:
        import datetime as dt
        prev = dt.date.fromisoformat(dates[1])
        cur = dt.date.fromisoformat(dates[0])
        gap = (cur - prev).days
        flag = "⚠ 最近两日间隔偏大(可能有缺口)" if gap > 4 else "ok"
        print(f"  最近两日间隔: {gap} 天  {flag}")

    # 3) 模板库
    tpl_n = _q(conn, "SELECT COUNT(*) FROM template")[0]
    if tpl_n:
        tmin, tmax = _q(conn, "SELECT MIN(anchor_date), MAX(anchor_date) FROM template")
        fwd = _q(conn, "SELECT AVG(fwd_ret_10d) FROM template")[0] or 0
        print(f"\n[模板库] {tpl_n} 条  锚点 {tmin} ~ {tmax}  平均未来10日收益 {fwd*100:.1f}%")
    else:
        print("\n[模板库] 空（需先跑 update_templates 或 run_daily）")

    # 4) 选股结果
    rows = conn.execute(
        "SELECT date, COUNT(*) FROM scan_result GROUP BY date ORDER BY date DESC LIMIT 5").fetchall()
    if rows:
        print("\n[选股结果] 最近几天:")
        for d, n in rows:
            picks = [r[0] for r in conn.execute(
                "SELECT code FROM scan_result WHERE date=? ORDER BY rank", (d,))]
            print(f"  - {d}: {n} 只  {', '.join(picks)}")
    else:
        print("\n[选股结果] 空")

    # 5) 运行日志
    logs = conn.execute("SELECT date,status,msg FROM run_log ORDER BY id DESC LIMIT 5").fetchall()
    if logs:
        print("\n[运行日志] 最近5条:")
        for d, s, m in logs:
            print(f"  - {d} [{s}] {m[:60]}")

    # 6) 产物
    print(f"\n[产物] {args.output}/")
    dirs = sorted([d for d in glob(os.path.join(args.output, "*")) if os.path.isdir(d)],
                  reverse=True)[:6]
    if not dirs:
        print("  (无)")
    for d in dirs:
        pngs = [p for p in glob(os.path.join(d, "*.png")) if not p.endswith("_kline.png")]
        md = os.path.exists(os.path.join(d, "summary.md"))
        print(f"  - {os.path.basename(d)}: 卡片 {len(pngs)} 张, 摘要{'有' if md else '无'}")

    conn.close()
    print("\n" + "=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
