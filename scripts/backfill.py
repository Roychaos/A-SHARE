"""Phase 0：首次/增量回填历史日线（全A 每日收盘后也可直接复用做增量）。

用法:
    python scripts/backfill.py                    # 按配置回填（years_back 年，自动续传）
    python scripts/backfill.py --years 2          # 覆盖历史深度
    python scripts/backfill.py --limit 20         # 只处理前20只（联调）
    python scripts/backfill.py --codes 000001,600519
    python scripts/backfill.py --rebuild          # 清空日线后整库重建（历史深度不齐时用，耗时约1.5-2.5h）
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.data.fetcher import ensure_universe, fetch_incremental_all  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402
from src.utils.net import sanitize_proxy_env  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 A股日线历史（akshare）")
    ap.add_argument("--years", type=int, default=None, help="历史深度（年）；默认取配置 fetch.years_back")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只（联调用）")
    ap.add_argument("--codes", default=None, help="只处理指定代码，逗号分隔")
    ap.add_argument("--rebuild", action="store_true", help="清空日线/fetch_log后整库重建")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # 默认直连行情站：忽略系统代理（配置 fetch.direct: false 可关闭）
    if cfg_get(cfg, "fetch.direct", True):
        sanitize_proxy_env()
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("backfill", cfg_get(cfg, "paths.log"))
    logger.info("== 回填开始 ==")

    conn = S.open_db(db)

    # 1) 股票池（meta）
    boards = cfg_get(cfg, "universe.boards")
    meta = ensure_universe(conn, boards=boards)
    logger.info("股票池 %d 只", len(meta))

    # 2) 目标代码（范围裁剪）
    if args.codes:
        wanted = [c.strip() for c in args.codes.split(",") if c.strip()]
        meta = [m for m in meta if m["code"] in wanted]
        logger.info("裁剪到指定 %d 只", len(meta))
    target_codes = [m["code"] for m in meta]

    # 3) 覆盖目标日期范围：从(今天 - years)到 今天
    years = args.years if args.years else int(cfg_get(cfg, "fetch.years_back", 2))
    start_dt = dt.date.today() - dt.timedelta(days=365 * years + 30)  # 多留缓冲
    start_iso = start_dt.isoformat()
    logger.info("历史窗口: %s ~ %s（%.1f 年）", start_iso, dt.date.today(), years)

    # 3.5) 重建模式：清空日线再整库重拉（历史深度统一为 years 年）
    if args.rebuild:
        before = S.count_bars(conn)
        conn.execute("DELETE FROM daily_bar")
        conn.execute("DELETE FROM fetch_log")
        conn.execute("UPDATE stock_meta SET list_date=NULL")
        conn.commit()
        logger.info("已清空日线 %d 根，开始整库重建", before)

    # 4) 逐只回填（已覆盖的自动跳过 -> 幂等，可断点续跑）
    sleep_s = float(cfg_get(cfg, "fetch.sleep_s", 0.5))
    stats = fetch_incremental_all(
        conn, cfg,
        codes=target_codes,
        limit=args.limit,
        date=dt.date.today().isoformat(),
        sleep_s=sleep_s,
        fallback_start=start_iso,   # 无历史记录的股票从此起点回拉（此前缺陷：固定近370天）
    )

    conn.close()
    logger.info(
        "== 回填结束: 更新 %d 只 / %d 根, 失败 %d%s ==",
        stats["codes"], stats["bars"], len(stats["failed"]),
        "（熔断提前停止）" if stats.get("stopped_early") else "",
    )
    if stats["failed"]:
        for code, exc in stats["failed"][:10]:
            logger.warning("失败 %s: %s", code, exc)
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
