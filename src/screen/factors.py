"""高级因子体系（用户规格表落地）：硬过滤 + 加权打分。

可用因子（免费日线可实现；不可得项按决策跳过、可用项权重自动归一化）：
  F1 资金效率   核心前置门槛 + 打分（日线 ΔP/V 相对自身20日均值）
  F4 股价异动   单日/5日涨幅带 + 避雷针(上影线)硬过滤
  F5 成交量异动 115%~135% 放量带 + 爆量>150% 硬过滤 + 持续性/斜率
  F7 板块联动   与所属行业20日收益相关(带区间) + 超额领跑 + 暴热板块硬过滤
暂缺(数据不可得, 保留占位与原始权重注释, 见配置 factors.*):
  F2 流动性空洞(需历史换手率) / F3 订单流OFI(需Level2) / F6 资金净流(需分钟级资金流)

输入 ctx 由 scorer.build_ctx 提供(含 close/high/low/volume/ma*/vma20/obv, i=最新)。
F7 需要调用方传入 f7_data: {corr, excess, present}（present=False 表示该股无行业数据）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("screener.factors")

# 配置键 -> 显示名（用于说明与持久化）
FACTOR_META = {
    "f1_efficiency": ("F1资金效率", 25),
    "f2_lvf": ("F2流动性空洞", 15),
    "f3_ofi": ("F3订单流", 15),
    "f4_move": ("F4股价异动", 15),
    "f5_volume": ("F5成交量异动", 10),
    "f6_fundflow": ("F6资金净流", 10),
    "f7_sector": ("F7板块联动", 10),
}
# 已实现(可打分)的配置键
AVAILABLE = {"f1_efficiency", "f4_move", "f5_volume", "f7_sector"}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ---------------- 硬过滤（排雷） ----------------

def reject_reasons(ctx: dict, cfg: dict, f7_hot: bool = False) -> list[str]:
    """返回触发的排雷规则列表（空=通过）。命中任一规则的候选应被剔除。"""
    reasons: list[str] = []
    filters = cfg.get("factors", {}).get("filters", {})
    i = ctx["i"]
    hi, lo, c = ctx["high"][i], ctx["low"][i], ctx["close"][i]
    # F4 避雷针：上影线 >= 振幅25%（收盘离高点太远）
    if filters.get("shadow_reject", True):
        amp = hi - lo
        if amp > 0 and (hi - c) / amp >= 0.25:
            reasons.append("避雷针:上影线过长")
    # F5 绝对禁止爆量 >150%
    if filters.get("volume_blowout", True):
        vr = _vol_ratio_prev(ctx)
        if vr is not None and vr > float(filters.get("volume_blowout_ratio", 1.5)):
            reasons.append(f"爆量:{vr:.2f}倍")
    # F7 暴热板块剔除（板块近5日涨幅全市场前3）
    if f7_hot:
        reasons.append("暴热板块剔除")
    return reasons


def _vol_ratio_prev(ctx: dict) -> float | None:
    """最新量 / 过去20日(不含当日)均量。"""
    i = ctx["i"]
    if i < 21:
        return None
    base = _mean(ctx["volume"][i - 20: i])
    return ctx["volume"][i] / base if base else None


# ---------------- 因子打分 ----------------

def f1_efficiency(ctx: dict, p: dict) -> float:
    """资金效率：[(ΔP_t/V_t) / (MA20(|ΔP|)/MA20(V))]；上涨日且比值>gate 才得分。"""
    i = ctx["i"]
    gate = float(p.get("gate", 1.2))
    if i < 21:
        return 0.0
    dp_today = ctx["close"][i] - ctx["close"][i - 1]
    v_today = ctx["volume"][i]
    if dp_today <= 0 or v_today <= 0:
        return 0.0
    dPs = [abs(ctx["close"][k] - ctx["close"][k - 1]) for k in range(i - 20, i)]
    Vs = ctx["volume"][i - 20: i]
    denom = (_mean(dPs) / _mean(Vs)) if (_mean(Vs) > 0 and _mean(dPs) > 0) else None
    ratio = (dp_today / v_today) / denom if denom else None
    if ratio is None:  # 此前20日几乎无波动/无量：首动视为高效
        ratio = 99.0
    if ratio <= gate:
        return 0.0
    return min(100.0, 60.0 + max(0.0, (ratio - gate) / 0.8) * 40.0)


def f4_move(ctx: dict, p: dict) -> float:
    """股价异动：单日 [3%,4.5%] 或 5日累计 [6%,9%]；近高收盘加分。"""
    i = ctx["i"]
    lo_d, hi_d = p.get("day_band", [0.03, 0.045])
    lo_c, hi_c = p.get("cum5_band", [0.06, 0.09])
    if ctx["close"][i - 1] <= 0 or i < 5:
        return 0.0
    day = ctx["close"][i] / ctx["close"][i - 1] - 1.0
    cum5 = ctx["close"][i] / ctx["close"][i - 5] - 1.0
    s = 0.0
    if lo_d <= day <= hi_d:
        s = 75.0
    elif lo_c <= cum5 <= hi_c and day > 0:
        s = 60.0
    if s == 0:
        return 0.0
    amp = ctx["high"][i] - ctx["low"][i]
    if amp > 0 and (ctx["high"][i] - ctx["close"][i]) / amp <= 0.10:
        s += 15.0  # 收盘贴近日内高点
    return min(100.0, s)


def f5_volume(ctx: dict, p: dict) -> float:
    """成交量异动：量比 ∈[1.15,1.35]；连3日>MA20 + 5日量能斜率为正 加分。"""
    i = ctx["i"]
    lo_v, hi_v = p.get("band", [1.15, 1.35])
    vr = _vol_ratio_prev(ctx)
    if vr is None:
        return 0.0
    if lo_v <= vr <= hi_v:
        s = 70.0
    elif 1.0 < vr < lo_v:
        s = 30.0  # 温和放量苗头
    else:
        return 0.0
    # 连3日量 > 前20日均量
    streak = True
    for k in range(i - 2, i + 1):
        if k < 21:
            streak = False
            break
        if ctx["volume"][k] <= _mean(ctx["volume"][k - 20: k]):
            streak = False
            break
    if streak:
        s += 15.0
    # 近5日量能斜率 > 0
    v5 = ctx["volume"][i - 4: i + 1]
    if len(v5) == 5 and v5[4] > v5[0]:
        s += 15.0
    return min(100.0, s)


def f7_sector_score(f7: dict | None, p: dict) -> tuple[float, dict]:
    """板块联动打分。f7=None 表示无行业数据(该股/全局)，返回 0 与空信息。

    需要调用方提供 {corr, excess}；corr 与所属行业20日收益的相关，excess 为近10日超额。
    """
    if not f7 or not f7.get("present"):
        return 0.0, {}
    lo, hi = p.get("corr_band", [0.45, 0.58])
    corr = f7.get("corr")
    excess = f7.get("excess", 0.0)
    info = {"corr": round(corr, 3), "excess": round(excess, 4)}
    if corr is None:
        return 0.0, info
    if lo <= corr <= hi:
        mid = (lo + hi) / 2.0
        s = 60.0 + 40.0 * (1.0 - abs(corr - mid) / ((hi - lo) / 2.0))
    elif corr > hi:
        s = 30.0  # 相关性过高：警惕板块拥挤
    elif corr >= 0.35:
        s = 40.0
    else:
        return 0.0, info
    if excess > float(p.get("excess_min", 0.025)):
        s += 15.0  # 超额领跑（强于板块的龙头阿尔法）
    return min(100.0, s), info


def active_factors(cfg: dict) -> list[str]:
    """配置中启用且已实现的因子键列表（按配置顺序）。"""
    sec = cfg.get("factors", {})
    return [k for k in ("f1_efficiency", "f4_move", "f5_volume", "f7_sector")
            if sec.get(k, {}).get("enable", True)]


def composite_score(ctx: dict, cfg: dict,
                    f7_data: dict | None = None,
                    available: list[str] | None = None) -> dict:
    """把可用因子加权合成 signal 分量（0~100，权重按已启用项归一化）。

    返回 {signal_score, factors:[(key,score,detail)], note}
    """
    keys = available if available is not None else active_factors(cfg)
    sec = cfg.get("factors", {})
    scored: list[tuple[str, float, dict]] = []
    raw = 0.0
    for k in keys:
        p = sec.get(k, {})
        if k == "f1_efficiency":
            v = f1_efficiency(ctx, p)
            scored.append((k, v, {"ratio_hint": round(v, 1)}))
        elif k == "f4_move":
            v = f4_move(ctx, p)
            scored.append((k, v, {}))
        elif k == "f5_volume":
            v = f5_volume(ctx, p)
            scored.append((k, v, {}))
        elif k == "f7_sector":
            v, info = f7_sector_score(f7_data, p)
            scored.append((k, v, info))
        raw += v * float(p.get("weight", FACTOR_META[k][1]))
    total_w = sum(float(sec.get(k, {}).get("weight", FACTOR_META[k][1])) for k in keys)
    if total_w <= 0:
        return {"signal_score": None, "factors": [], "note": "无可启用因子"}
    return {
        "signal_score": round(raw / total_w, 2),
        "factors": [{"key": k, "name": FACTOR_META[k][0], "score": round(v, 1), **d}
                    for k, v, d in scored],
        "note": "",
    }
