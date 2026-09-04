"""量价信号（Phase 2）：每个信号对"截至最新一根K线"打分 0~100。

输入 ctx（由 scorer 构建，list 下标 i=len-1 为最新交易日）：
  date/close/high/low/volume/open?  (open 未用)
  ma5/ma10/ma20/vma20/high60/low?/obv  均为等长滚动数组（含当日）
信号函数签名：fn(ctx, p: dict) -> float（不满足判据返回 0）。
"""
from __future__ import annotations

from typing import Callable


def _vol_ratio(ctx, k: int = 1) -> float | None:
    """最新量 / 前k日起的20日均量（不含当日，避免自比）。"""
    i = ctx["i"]
    vma = ctx.get("vma20")
    if vma is None or vma[i - k] is None or ctx["volume"][i] is None:
        return None
    base = vma[i - k]
    return ctx["volume"][i] / base if base else None


def warm_volume_rise(ctx, p: dict) -> float:
    """温和放量上攻：量比∈[lo,hi] 且收阳且涨幅∈(0,max_pct]。"""
    i = ctx["i"]
    lo, hi = p.get("vol_ratio", [1.2, 4.0])
    pmin, pmax = p.get("pct_range", [0.0, 0.06])
    c, c1 = ctx["close"][i], ctx["close"][i - 1]
    if c1 <= 0:
        return 0.0
    ratio = _vol_ratio(ctx)
    if ratio is None or not (lo <= ratio <= hi):
        return 0.0
    chg = c / c1 - 1.0
    if not (pmin < chg <= pmax):
        return 0.0
    score = 60.0
    if ratio <= 3.0:            # 放量不过猛
        score += 15.0
    if c >= ctx["high"][i]:     # 收在最高附近
        score += 15.0
    if ctx["ma5"][i] and c > ctx["ma5"][i]:
        score += 10.0
    return min(100.0, score)


def platform_breakout(ctx, p: dict) -> float:
    """平台突破：收盘突破前 lookback 日平台高点且量比达标；接近突破给低分。"""
    i = ctx["i"]
    lb = int(p.get("lookback", 60))
    vol_min = float(p.get("vol_ratio_min", 1.5))
    plat = max(ctx["high"][max(0, i - lb): i]) if i - lb >= 0 else None
    if plat is None or plat <= 0:
        return 0.0
    c = ctx["close"][i]
    ratio = _vol_ratio(ctx)
    if c > plat:
        s = 70.0
        if ratio is not None and ratio >= vol_min:
            s += 25.0
        if ctx["ma5"][i] and c > ctx["ma5"][i]:
            s += 5.0
        return min(100.0, s)
    if c >= plat * 0.985 and ratio is not None and ratio >= 1.0:
        return 35.0  # 逼近突破，苗头
    return 0.0


def _golden_cross(ctx, fast: int, slow: int, within: int) -> bool:
    i = ctx["i"]
    mf, ms = ctx.get(f"ma{fast}"), ctx.get(f"ma{slow}")
    if mf is None or ms is None:
        return False
    for k in range(max(1, i - within + 1), i + 1):
        if mf[k - 1] is None or ms[k - 1] is None or mf[k] is None or ms[k] is None:
            continue
        if mf[k - 1] <= ms[k - 1] and mf[k] > ms[k]:
            return True
    return False


def ma_bullish_init(ctx, p: dict) -> float:
    """均线多头初成：近 within 日内出现金叉（5/10 或 10/20），MA20 转平向上加分。"""
    i = ctx["i"]
    within = int(p.get("cross_within_days", 3))
    g510 = _golden_cross(ctx, 5, 10, within)
    g1020 = _golden_cross(ctx, 10, 20, within)
    if not (g510 or g1020):
        return 0.0
    s = 55.0
    if g1020:
        s += 20.0
    ma20 = ctx.get("ma20")
    if ma20 and ma20[i] and ma20[i - 3] and ma20[i] > ma20[i - 3]:
        s += 15.0  # MA20 走平转上
    if ctx["ma5"][i] and ctx["close"][i] > ctx["ma5"][i]:
        s += 10.0
    return min(100.0, s)


def pullback_hold(ctx, p: dict) -> float:
    """上行趋势中缩量回踩 MA 企稳（洗盘结束信号）。"""
    i = ctx["i"]
    ma = int(p.get("ma", 20))
    ma_arr = ctx.get(f"ma{ma}")
    if not ma_arr or ma_arr[i] is None or ctx["close"][i - 5] is None or ctx["close"][i - 15] is None:
        return 0.0
    up_prior = ctx["close"][i - 5] > ctx["close"][i - 15]  # 之前处于上行
    if not up_prior:
        return 0.0
    low = ctx["low"][i]
    m = ma_arr[i]
    if not (m * 0.95 <= low <= m * 1.03):  # 回踩到均线附近
        return 0.0
    s = 50.0
    if ctx["close"][i] > ctx["open"][i]:
        s += 15.0
    ratio = _vol_ratio(ctx)
    if ratio is not None and ratio <= 1.0:
        s += 20.0  # 缩量
    if ctx["close"][i] > m:
        s += 15.0  # 收复均线
    return min(100.0, s)


def obv_high(ctx, p: dict) -> float:
    """OBV 创 lookback 日新高（资金持续），未放天量。"""
    i = ctx["i"]
    lb = int(p.get("lookback", 60))
    obv = ctx.get("obv")
    if not obv or i + 1 < lb:
        return 0.0
    window = obv[i - lb: i + 1]
    prev_max = max(window[:-1])
    if obv[i] < prev_max:
        return 0.0
    s = 55.0
    if obv[i] > prev_max:  # 真正的新高（> 前高）
        s += 20.0
    ratio = _vol_ratio(ctx)
    if ratio is not None and ratio >= 1.2:
        s += 10.0
    if ratio is None or ratio <= 4.0:
        s += 15.0  # 未放天量
    return min(100.0, s)


SIGNALS: dict[str, Callable[[dict, dict], float]] = {
    "warm_volume_rise": warm_volume_rise,
    "platform_breakout": platform_breakout,
    "ma_bullish_init": ma_bullish_init,
    "pullback_hold": pullback_hold,
    "obv_high": obv_high,
}
