"""快速选股：以指定交易日(默认=库内最后交易日)收盘价为准，输出 Top N。

用法:
    python scripts/pick_top.py                # 用库内最后交易日
    python scripts/pick_top.py --date 2026-09-03
"""
from __future__ import annotations

import argparse
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.screen.scorer import compute_and_select  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="快速选股 TopN")
    ap.add_argument("--date", default=None, help="基准交易日(YYYY-MM-DD)，默认库内最后交易日")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    setup_logger("pick_top", cfg_get(cfg, "paths.log"))
    conn = S.open_db(db)

    if args.date:
        date = args.date
    else:
        row = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()
        date = row[0]
        if not date:
            print("daily_bar 无数据，请先 backfill")
            conn.close()
            return 1

    names = {r[0]: r[1] for r in conn.execute("SELECT code,name FROM stock_meta")}
    selected = compute_and_select(conn, cfg, date, persist=True)

    print(f"\n===== {date} 选股结果（Top{len(selected)}，纯形态打分） =====")
    if not selected:
        print("当日无符合条件的股票（可能 min_score 阈值偏高或无模板匹配）")
    for s in selected:
        name = names.get(s["code"], "")
        ref = f"{s.get('best_tpl_code')}@{s.get('best_tpl_anchor')}" if s.get("best_tpl_code") else "-"
        hits = ",".join(s.get("hits") or []) or "-"
        print(f"#{s['rank']}  {s['code']} {name:<8} 得分={s['score']}  "
              f"形态分={s.get('sim_score')}  最像模板={ref}  命中={hits}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
