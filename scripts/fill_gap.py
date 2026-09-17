"""补齐库中缺失的交易日（历史空洞修复）。

背景（为什么必须单独做这件事）：
    fetch_incremental_all 的「整市场快照」只负责把 *今天* 一次性入库（1 次请求），
    随后逐股循环里只要 `latest_bar_date >= end` 就 continue 跳过。
    它保证的是「最新日期 >= 目标日期」，**不保证序列连续**。
    因此一旦某次运行没能接上历史库（例如 artifact 恢复失败、退回旧 seed.zip），
    库里就会出现多天空洞，而之后每天只补当天一根 —— 空洞**永远不会自愈**。

    空洞的后果：形态模板窗口(25 根)与相似度、K线图都跨着空洞计算，选股结果失真。

本脚本：
    1) 按 trade_cal 找出窗口内「疑似缺失」的交易日
       （当日入库股票数 < 同期正常水平的 50%，说明那天整体没入库）；
    2) 对所有股票，从第一个缺口日期(前留 5 天缓冲)重新拉取到库内最新日期并覆盖写入（幂等可续跑）。

用法:
    python scripts/fill_gap.py                      # 自动扫描最近 60 天
    python scripts/fill_gap.py --days 120           # 扫描更久
    python scripts/fill_gap.py --from 2026-09-05 --to 2026-09-17
    python scripts/fill_gap.py --dry-run            # 只报告缺口，不抓取
    python scripts/fill_gap.py --limit 20           # 只处理前 20 只（联调）
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.data.fetcher import fetch_incremental_all  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402
from src.utils.net import sanitize_proxy_env  # noqa: E402

BUFFER_DAYS = 5          # 缺口起点前留几天缓冲，保证形态窗口有上下文
MIN_TYPICAL = 200        # 同期"正常水平"的下限，低于此值不算正常基准
RATIO = 0.5              # 当日入库数 < 基准 * RATIO 即判为缺口


def date_counts(conn, start: str, end: str) -> dict[str, int]:
    sql = ("SELECT date, COUNT(*) FROM daily_bar WHERE date >= ? AND date <= ? "
           "GROUP BY date")
    return {d: int(c) for d, c in conn.execute(sql, (start, end))}


def open_days(conn, start: str, end: str) -> list[str]:
    sql = "SELECT date FROM trade_cal WHERE date >= ? AND date <= ? ORDER BY date"
    return [r[0] for r in conn.execute(sql, (start, end))]


def find_gaps(conn, start: str, end: str) -> tuple[list[str], int, dict[str, int]]:
    """返回 (缺口日期列表, 同期基准股票数, 每日入库数)。"""
    counts = date_counts(conn, start, end)
    days = open_days(conn, start, end)
    present = [counts[d] for d in days if counts.get(d)]
    typical = int(statistics.median(present)) if present else 0
    gaps = [d for d in days if counts.get(d, 0) < max(typical, MIN_TYPICAL) * RATIO]
    return gaps, typical, counts


def codes_missing(conn, gaps: list[str]) -> list[str]:
    """只返回「在这些缺口日期上仍缺数据」的股票 —— 让重跑具备真正的续跑能力。

    首次跑通常是全部股票；若上一次跑到一半被限流/超时中断，
    已经补齐的股票就会被排除，重跑只处理剩下的，不会从头再来。
    """
    if not gaps:
        return []
    ph = ",".join("?" * len(gaps))
    sql = (
        "SELECT m.code FROM stock_meta m LEFT JOIN ("
        "  SELECT code, COUNT(DISTINCT date) AS c FROM daily_bar "
        "  WHERE date IN (%s) GROUP BY code"
        ") b ON b.code = m.code WHERE COALESCE(b.c, 0) < ?"
    ) % ph
    return [r[0] for r in conn.execute(sql, (*gaps, len(gaps)))]


def report(conn, start: str, end: str, gaps: list[str], typical: int,
           counts: dict[str, int]) -> None:
    print("")
    print("交易日覆盖率（只列缺失/偏少的，正常日省略）：")
    print("  窗口 %s ~ %s，同期正常水平 ≈ %d 只/日" % (start, end, typical))
    if not gaps:
        print("  [OK] 无缺口，数据连续")
        return
    for d in gaps:
        print("  [缺口] %s  入库 %d 只（应约 %d 只）" % (d, counts.get(d, 0), typical))
    print("  合计缺口 %d 个交易日：%s" % (len(gaps), ", ".join(gaps)))


def main() -> int:
    ap = argparse.ArgumentParser(description="修复库中缺失的交易日（逐股回补）")
    ap.add_argument("--days", type=int, default=60, help="回溯天数（默认 60）")
    ap.add_argument("--from", dest="d_from", default=None, help="窗口起点 ISO（覆盖 --days）")
    ap.add_argument("--to", dest="d_to", default=None,
                    help="扫描终点 ISO（默认今天；当天由整市场快照负责，不计入缺口）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只（联调用）")
    ap.add_argument("--codes", default=None, help="只处理指定代码，逗号分隔")
    ap.add_argument("--all-codes", action="store_true",
                    help="不做「只补仍缺数据」的过滤，强制对全部股票重拉")
    ap.add_argument("--sleep", type=float, default=None,
                    help="覆盖配置里的 fetch.sleep_s（越小越快，也越容易被限流）")
    ap.add_argument("--dry-run", action="store_true", help="只报告缺口，不抓取")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if cfg_get(cfg, "fetch.direct", True):
        sanitize_proxy_env()
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("fill_gap", cfg_get(cfg, "paths.log"))

    conn = S.open_db(db)
    db_max = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    today = dt.date.today().isoformat()
    # ★ 扫描终点必须是「今天」，不能用库里的最新日期：
    #   若库里最新只到 09-03，用库最新日期当终点就只会扫 09-03 之前那一大段（那通常是完整的），
    #   而把 09-04 ~ 今天 这段真正的缺口漏掉 —— 之前 repair 跑了 80 毫秒就"完成"就是这个原因。
    end = args.d_to or today
    # 当天由「整市场快照」负责（1 次请求补全市场），不算缺口；
    # 否则每个交易日都会把全市场 5000 多只重新拉一遍。
    scan_end = args.d_to or (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    start = args.d_from or (dt.date.fromisoformat(end) - dt.timedelta(days=args.days)).isoformat()

    logger.info("== 空洞扫描 %s ~ %s（库内最新 %s，今天 %s）==",
                start, scan_end, db_max or "无", today)
    gaps, typical, counts = find_gaps(conn, start, scan_end)
    report(conn, start, scan_end, gaps, typical, counts)
    if not gaps:
        conn.close()
        return 0
    if args.dry_run:
        print("\n[dry-run] 未抓取。去掉 --dry-run 即可开始回补。")
        conn.close()
        return 0

    # 从第一个缺口前 BUFFER_DAYS 天起重拉，保证形态窗口有上下文
    force_from = (dt.date.fromisoformat(gaps[0]) - dt.timedelta(days=BUFFER_DAYS)).isoformat()

    codes = None
    if args.codes:
        wanted = {c.strip() for c in args.codes.split(",") if c.strip()}
        codes = [c for c in S.list_codes(conn) if c in wanted]
        logger.info("裁剪到指定 %d 只", len(codes))
    elif not args.all_codes:
        codes = codes_missing(conn, gaps)
        logger.info("只补仍缺数据的股票：%d 只（其余已补齐，跳过）", len(codes))
        if not codes:
            logger.info("所有股票都已补齐，无需抓取")
            conn.close()
            return 0

    logger.info("开始逐股回补：%s ~ %s（本次约 %d 只，预计 30~180 分钟）",
                force_from, end, len(codes) if codes else len(S.list_stock_meta(conn)))

    stats = fetch_incremental_all(
        conn, cfg,
        codes=codes,
        limit=args.limit,
        date=end,
        sleep_s=args.sleep if args.sleep is not None else float(cfg_get(cfg, "fetch.sleep_s", 0.4)),
        force_from=force_from,
    )
    conn.commit()

    # 复检
    gaps2, typical2, counts2 = find_gaps(conn, start, scan_end)
    logger.info("回补结束：更新 %d 只 / %d 根，失败 %d%s",
                stats["codes"], stats["bars"], len(stats["failed"]),
                "（熔断提前停止）" if stats.get("stopped_early") else "")
    report(conn, start, scan_end, gaps2, typical2, counts2)
    conn.close()

    if stats.get("stopped_early"):
        logger.error("被行情源限流中断，请等 10~30 分钟后重跑本脚本（幂等，会自动续传）")
        return 2
    return 0 if not gaps2 else 3


if __name__ == "__main__":
    sys.exit(main())
