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
from src.utils import calendar as cal  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402
from src.utils.net import sanitize_proxy_env  # noqa: E402

BUFFER_DAYS = 5          # 缺口起点前留几天缓冲，保证形态窗口有上下文
MIN_TYPICAL = 200        # 参照值的绝对下限
RATIO = 0.80             # ★ 当日入库数 < 参照值 × RATIO 即判为缺口
COVERAGE_TAIL = 12       # 报告里额外打印最近 N 个交易日的每日入库只数（便于人眼核对）


def date_counts(conn, start: str, end: str) -> dict[str, int]:
    sql = ("SELECT date, COUNT(*) FROM daily_bar WHERE date >= ? AND date <= ? "
           "GROUP BY date")
    return {d: int(c) for d, c in conn.execute(sql, (start, end))}


def open_days(conn, start: str, end: str) -> list[str]:
    sql = "SELECT date FROM trade_cal WHERE date >= ? AND date <= ? ORDER BY date"
    return [r[0] for r in conn.execute(sql, (start, end))]


def find_gaps(conn, start: str, end: str) -> tuple[list[str], int, int, dict[str, int]]:
    """返回 (缺口日期列表, 参照值, 阈值, 每日入库数)。

    参照值 = max(窗口内每日入库数的中位数, 最大值, 股票池只数)。
    阈值   = 参照值 × RATIO(0.80)。

    为什么不用"中位数的 50%"（踩过的坑）：
        一次"补到一半就被中断"的运行会留下 旧日期 5200 只 / 新日期 3000 只 的库，
        中位数仍是 5200、50% 阈值 = 2600 → 3000 只的那些天被误判为完整，
        半成品库被当成好库采用，缺口就此永久保留。用股票池只数做参照最稳。
    """
    counts = date_counts(conn, start, end)
    days = open_days(conn, start, end)
    present = [counts[d] for d in days if counts.get(d)]
    median = int(statistics.median(present)) if present else 0
    universe = int(conn.execute("SELECT COUNT(*) FROM stock_meta").fetchone()[0] or 0)
    ref = max(median, max(present) if present else 0, universe)
    thr = max(MIN_TYPICAL, int(ref * RATIO))
    gaps = [d for d in days if counts.get(d, 0) < thr]
    return gaps, ref, thr, counts


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


def report(conn, start: str, end: str, gaps: list[str], ref: int, thr: int,
           counts: dict[str, int]) -> None:
    days = open_days(conn, start, end)
    print("")
    print("交易日覆盖率：")
    print("  窗口 %s ~ %s" % (start, end))
    print("  参照值 %d 只（股票池/中位数/最大值取大）→ 阈值 %d 只，低于此值判为缺口" % (ref, thr))
    if not gaps:
        print("  [OK] 无缺口，数据连续")
    else:
        for d in gaps:
            print("  [缺口] %s  入库 %d 只（应约 %d 只）" % (d, counts.get(d, 0), ref))
        print("  合计缺口 %d 个交易日：%s" % (len(gaps), ", ".join(gaps)))
    tail = days[-COVERAGE_TAIL:]
    if tail:
        print("  最近 %d 个交易日入库只数（人眼核对用）：" % len(tail))
        print("    " + "  ".join("%s:%d" % (d[5:], counts.get(d, 0)) for d in tail))


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

    # ★ 先确保交易日历可用（这是"能不能发现缺口"的前提）。
    #   踩过的坑：云端种子库 seed.zip 里的 trade_cal 是**空的**，
    #   于是 open_days() 返回空 → 缺口检测"什么都扫不到" → 静默地当成"无缺口"、
    #   跳过整个补洞流程（2026-09-17 的 repair 跑了 0.1 秒就是这个原因）。
    #   所以这里绝不能让自己在无日历时静默通过，宁可报错退出。
    try:
        cal.ensure_trade_calendar(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历刷新失败（将尝试使用库内已有日历）: %s: %s", type(exc).__name__, exc)
    n_cal = int(conn.execute("SELECT COUNT(*) FROM trade_cal").fetchone()[0] or 0)
    if not n_cal:
        logger.error("交易日历为空且刷新失败 —— 无法判断哪些交易日缺数据。"
                     "本次不做任何判断直接退出（避免把'扫不到'误当成'没有缺口'）。")
        conn.close()
        return 1
    logger.info("交易日历可用：%d 条，最新 %s",
                n_cal, conn.execute("SELECT MAX(date) FROM trade_cal").fetchone()[0])

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
    if not open_days(conn, start, scan_end):
        logger.error("交易日历在 %s ~ %s 区间内没有任何日期：日历异常，退出", start, scan_end)
        conn.close()
        return 1
    gaps, ref, thr, counts = find_gaps(conn, start, scan_end)
    report(conn, start, scan_end, gaps, ref, thr, counts)
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
    gaps2, ref2, thr2, counts2 = find_gaps(conn, start, scan_end)
    logger.info("回补结束：更新 %d 只 / %d 根，失败 %d%s",
                stats["codes"], stats["bars"], len(stats["failed"]),
                "（熔断提前停止）" if stats.get("stopped_early") else "")
    report(conn, start, scan_end, gaps2, ref2, thr2, counts2)
    conn.close()

    if stats.get("stopped_early"):
        logger.error("被行情源限流中断，请等 10~30 分钟后重跑本脚本（幂等，会自动续传）")
        return 2
    return 0 if not gaps2 else 3


if __name__ == "__main__":
    sys.exit(main())
