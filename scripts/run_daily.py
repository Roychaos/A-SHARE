"""Phase 0 每日入口：交易日判断 -> 增量更新当日行情 -> 汇总落库。

Phase 1-3 将在此处继续接入：模板学习/信号打分/图文/推送。
非交易日静默退出(exit 0)，便于 GitHub Actions cron 每天固定触发。

用法:
    python scripts/run_daily.py                      # 今天
    python scripts/run_daily.py --date 2026-01-05    # 指定回补某交易日
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
from src.utils import calendar as cal  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402
from src.utils.net import sanitize_proxy_env  # noqa: E402


def resolve_date(raw: str) -> dt.date:
    if raw in ("today", ""):
        return dt.date.today()
    return dt.date.fromisoformat(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description="每日量价选股流水线入口")
    ap.add_argument("--date", default="today", help="today 或 YYYY-MM-DD")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只（联调用）")
    ap.add_argument("--no-push", action="store_true", help="只生成图文不推送（CI 里先提交图片再推）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # 默认直连行情站：忽略系统代理（配置 fetch.direct: false 可关闭）
    if cfg_get(cfg, "fetch.direct", True):
        sanitize_proxy_env()
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("run_daily", cfg_get(cfg, "paths.log"))
    day = resolve_date(args.date).isoformat()

    logger.info("== 每日流水线 %s 开始 ==", day)
    conn = S.open_db(db)

    # ① 交易日判断（本地日历为空时经 akshare 拉取；网络失败会在此抛出并告警）
    cal.ensure_trade_calendar(conn, refresh=False)
    if not cal.is_trading_day(conn, day):
        msg = f"{day} 非交易日，跳过。"
        logger.info(msg)
        S.add_run_log(conn, day, "skip", msg)
        conn.close()
        return 0

    # ①.5 股票池为空时自动建（首次/全新环境）
    if not S.list_stock_meta(conn):
        boards = cfg_get(cfg, "universe.boards")
        ensure_universe(conn, boards=boards, quiet=False)

    # ② 增量更新当日行情
    stats = fetch_incremental_all(
        conn, cfg, date=day, limit=args.limit,
        sleep_s=float(cfg_get(cfg, "fetch.sleep_s", 0.5)),
    )

    # ②.5 用最新数据重建模板库（幂等；数据不足时跳过）
    try:
        from src.patterns.templates import update_templates

        if S.count_bars(conn) > 0:
            update_templates(conn, cfg)
    except Exception as exc:  # noqa: BLE001
        logger.error("模板更新失败: %s", exc)

    # ③ 选股打分并落库
    selected: list = []
    if S.count_templates(conn) == 0:
        logger.warning("模板库为空（历史数据不足），本次跳过选股")
    else:
        try:
            from src.screen.scorer import compute_and_select

            selected = compute_and_select(conn, cfg, day, limit=args.limit)
            logger.info("%s 入选 %d 只: %s", day, len(selected),
                        [f"{s['rank']}.{s['code']}" for s in selected])
        except Exception as exc:  # noqa: BLE001 选股失败不应中断已落库的行情更新
            logger.error("选股失败: %s", exc)

    # ④ 图文报告 + 推送（Phase 3）
    if selected:
        try:
            from src.report.pipeline import emit

            emit(conn, cfg, day, push=not args.no_push)
        except Exception as exc:  # noqa: BLE001
            logger.error("图文/推送失败: %s", exc)
            try:
                from src.push.notifier import alert
                alert(cfg, f"{day} 图文/推送失败: {exc}")
            except Exception:  # noqa: BLE001
                pass

    # ⑤ 汇总落库
    summary = (f"更新 {stats['codes']} 只 / {stats['bars']} 根, "
               f"失败 {len(stats['failed'])}, 入选 {len(selected)}")
    logger.info("== 完成: %s ==", summary)
    if stats["failed"]:
        for code, exc in stats["failed"][:5]:
            logger.warning("失败 %s: %s", code, exc)
    status = "ok"
    if stats["failed"]:
        status = "partial"
    if len(selected) == 0 and S.count_templates(conn) > 0:
        status = "no_pick"
    S.add_run_log(conn, day, status, summary)
    conn.close()
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
