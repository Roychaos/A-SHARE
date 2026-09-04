"""交易日历：优先用本地缓存，缺失时经 akshare 拉取并落库。"""
from __future__ import annotations

import datetime as dt
import logging

from src.data.store import upsert_trade_cal_dates

logger = logging.getLogger("screener.calendar")


def fetch_trade_dates() -> list[str]:
    """经 akshare 拉取交易日历（惰性导入，避免离线环境 import 失败）。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - 依赖未装时给出明确提示
        raise RuntimeError("缺少 akshare，请先执行: pip install -r requirements.txt") from exc

    df = ak.tool_trade_date_hist_sina()  # 列: trade_date (datetime.date)
    return [str(d.date()) if hasattr(d, "date") else str(d) for d in df["trade_date"].tolist()]


def ensure_trade_calendar(conn, refresh: bool = False) -> list[str] | None:
    """确保本地有交易日历。返回日历列表；refresh=True 时强制重拉。"""
    cur = conn.execute("SELECT COUNT(*), COALESCE(MAX(date),'') FROM trade_cal")
    count, latest = cur.fetchone()
    today = dt.date.today().isoformat()
    if count > 0 and latest >= today and not refresh:
        return _load(conn)
    logger.info("刷新交易日历（本地 %d 条，最新 %s）", count or 0, latest or "-")
    dates = fetch_trade_dates()
    upsert_trade_cal_dates(conn, dates)
    conn.commit()
    logger.info("交易日历已更新：%d 条", len(dates))
    return dates


def _load(conn) -> list[str]:
    rows = conn.execute("SELECT date FROM trade_cal ORDER BY date").fetchall()
    return [r[0] for r in rows]


def is_trading_day(conn, day: str | dt.date) -> bool:
    """给定日期(ISO 或 date)是否为交易日（须已存在本地日历）。"""
    d = day if isinstance(day, str) else day.isoformat()
    row = conn.execute("SELECT 1 FROM trade_cal WHERE date = ?", (d,)).fetchone()
    return row is not None
