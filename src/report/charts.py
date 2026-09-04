"""K线图生成（mplfinance，惰性导入）。图上用英文/数字标注，规避中文字体问题。"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("screener.charts")


def make_kline(conn, cfg: dict, code: str, date: str, out_path: str) -> str | None:
    """为 code 生成截至 date 的近 N 根日K PNG（含均线+成交量）。"""
    kline_bars = int(cfg.get("report", {}).get("charts", {}).get("kline_bars", 120))
    ma_windows = [int(x) for x in cfg.get("report", {}).get("charts", {}).get("ma_windows", [5, 10, 20, 60])]

    rows = [dict(zip(("date", "open", "high", "low", "close", "volume"), r))
            for r in conn.execute(
                "SELECT date,open,high,low,close,volume FROM daily_bar "
                "WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
                (code, date, kline_bars)).fetchall()]
    rows = rows[::-1]
    if len(rows) < 30:
        logger.warning("%s: K线数据不足30根，跳过图表", code)
        return None

    try:
        import pandas as pd
        import mplfinance as mpf
    except ImportError:
        raise RuntimeError("缺少 pandas/mplfinance，请先: pip install -r requirements.txt")

    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    mpf.plot(
        df, type="candle", mav=tuple(ma_windows), volume=True, style="yahoo",
        title=f"{code}  {date}", ylabel="", ylabel_lower="",
        savefig=dict(fname=out_path, dpi=110), figsize=(10, 7), tight_layout=True,
    )
    return out_path
