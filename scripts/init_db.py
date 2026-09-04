"""Phase 0：初始化数据库（幂等，可重复执行）。

用法:
    python scripts/init_db.py [--db data/screener.db]
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化 SQLite 数据库（幂等）")
    ap.add_argument("--db", default=None, help="数据库路径（默认取配置 paths.db）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认自动查找）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = args.db or cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("init_db", cfg_get(cfg, "paths.log"))

    conn = S.open_db(db)
    logger.info("数据库就绪: %s", db)
    for tbl in ("stock_meta", "trade_cal", "daily_bar", "template", "scan_result", "run_log", "fetch_log"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        logger.info("表 %-12s 行数: %d", tbl, n)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
