"""打分与选股（Phase 2）：候选排雷 -> 因子/形态/趋势打分 -> TopN。

总分 = w1*pattern_sim + w2*signal + w3*trend（0~100，缺失分量权重自动再归一）
signal 分量 = factors.composite_score（高级7因子体系，可用项归一化）；
factors.enabled=false 时回退旧版5信号（score_components）。

硬过滤（排雷，命中即剔除候选）：
  避雷针(上影线≥振幅25%) / 爆量(>150%) / 暴热板块剔除 / 涨停无买点 / ST / 低价 / 无窗口历史
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from src.data import store as S
from src.patterns.similarity import pearson, template_sim
from src.patterns.templates import _rolling_max, _rolling_mean, _zscore
from src.screen import factors as F
from src.signals import rules as R

logger = logging.getLogger("screener.scorer")


# ---------------- 上下文 ----------------

def build_ctx(rows: list[dict]) -> dict | None:
    """rows 按日期升序。返回滚动指标上下文；数据不足(需>=60根)返回 None。"""
    if len(rows) < 60:
        return None
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    opens = [r.get("open") or r["close"] for r in rows]
    vols = [r.get("volume") or 0.0 for r in rows]

    obv = [0.0]
    for k in range(1, len(closes)):
        delta = vols[k] if closes[k] > closes[k - 1] else (-vols[k] if closes[k] < closes[k - 1] else 0.0)
        obv.append(obv[-1] + delta)

    return {
        "date": [r["date"] for r in rows],
        "close": closes, "high": highs, "low": lows, "open": opens, "volume": vols,
        "ma5": _rolling_mean(closes, 5), "ma10": _rolling_mean(closes, 10),
        "ma20": _rolling_mean(closes, 20), "vma20": _rolling_mean(vols, 20),
        "high60": _rolling_max(highs, 60), "obv": obv, "i": len(rows) - 1,
    }


def _trend_score(ctx: dict) -> float:
    i = ctx["i"]
    s = 20.0
    if ctx["ma5"][i] and ctx["ma10"][i] and ctx["ma5"][i] > ctx["ma10"][i]:
        s += 20.0
    if ctx["ma10"][i] and ctx["ma20"][i] and ctx["ma10"][i] > ctx["ma20"][i]:
        s += 20.0
    if ctx["ma20"][i] and ctx["ma20"][i - 3] and ctx["ma20"][i] > ctx["ma20"][i - 3]:
        s += 15.0
    if ctx["close"][i] > (ctx["ma5"][i] or 1e18):
        s += 15.0
    if ctx["obv"] and len(ctx["obv"]) > 4 and ctx["obv"][i] > ctx["obv"][i - 4]:
        s += 10.0
    return min(100.0, s)


# ---------------- 旧版5信号（factors.enabled=false 时使用，保留） ----------------

def _signal_params(cfg: dict) -> dict[str, dict]:
    out = {}
    for name in R.SIGNALS:
        sec = cfg.get("signals", {}).get(name, {})
        if sec.get("enable", True):
            out[name] = {k: v for k, v in sec.items() if k != "enable"}
    return out


def _active_signals(ctx: dict, params: dict[str, dict]) -> tuple[list[tuple[str, float]], float]:
    hits, mx = [], 0.0
    for name, p in params.items():
        try:
            sc = R.SIGNALS[name](ctx, p)
        except (IndexError, TypeError, ZeroDivisionError):
            sc = 0.0
        if sc > 0:
            hits.append((name, round(sc, 1)))
            mx = max(mx, sc)
    return hits, mx


def score_components(rows: list[dict], cfg: dict, sim: dict | None) -> dict | None:
    """旧版评分路径（信号=5信号最大值），供 factors.enabled=false 或测试使用。"""
    ctx = build_ctx(rows)
    if ctx is None:
        return None
    pat = sim or {}
    hits, sig_max = _active_signals(ctx, _signal_params(cfg))
    trend = _trend_score(ctx)
    return _combine(pat.get("pattern_score"), sig_max, trend, hits, pat, cfg)


def _combine(pat_score, sig_max, trend, hits, pat: dict, cfg: dict) -> dict | None:
    weights = cfg.get("scoring", {}).get("weights", {})
    parts = {}
    if pat_score is not None:
        parts["pattern"] = float(pat_score)
    if sig_max and sig_max > 0:
        parts["signal"] = float(sig_max)
    parts["trend"] = float(trend)
    wnames = {"pattern": "pattern_sim", "signal": "signal", "trend": "trend"}
    ws = {k: float(weights.get(wnames[k], 0.0)) for k in parts}
    wsum = sum(ws.values())
    total = round(sum(parts[k] * ws[k] for k in parts) / wsum, 2) if wsum > 0 else None
    return {
        "sim": pat_score, "best_tpl_id": pat.get("best_tpl_id"),
        "best_code": pat.get("best_code"), "best_anchor": pat.get("best_anchor"),
        "signal_max": round(sig_max, 1) if sig_max else 0.0,
        "hits": hits, "trend": round(trend, 1), "total": total,
    }


# ---------------- 行业数据（F7 板块联动） ----------------

def _build_industry_data(groups: dict[str, list[dict]], meta: dict) -> dict | None:
    """基于成分股自算行业等权日收益。

    返回 {ind_series: {industry: {date: mean_ret}}, rets_by_code: {code:{date:ret}},
          dates: [..], heat_top3: set}；行业覆盖不足返回 None。
    """
    with_ind = [c for c, m in meta.items() if (m or {}).get("industry")]
    if not with_ind:
        return None
    ind_rets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    rets_by_code: dict[str, dict[str, float]] = {}
    all_dates: set[str] = set()
    for code, rows in groups.items():
        ind = (meta.get(code) or {}).get("industry")
        if not ind:
            continue
        rmap: dict[str, float] = {}
        for k in range(1, len(rows)):
            d, c, p = rows[k]["date"], rows[k]["close"], rows[k - 1]["close"]
            if p and c:
                r = c / p - 1.0
                rmap[d] = r
                ind_rets[ind][d].append(r)
                all_dates.add(d)
        if rmap:
            rets_by_code[code] = rmap
    if len(ind_rets) < 3:
        return None
    ind_series = {ind: {d: _mean(v) for d, v in m.items()} for ind, m in ind_rets.items()}
    dates = sorted(all_dates)
    heat = _heat_top3(ind_series, dates)
    return {"ind_series": ind_series, "rets_by_code": rets_by_code,
            "dates": dates, "heat_top3": heat}


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _heat_top3(ind_series: dict, dates: list[str], k: int = 5) -> set[str]:
    """板块近5日累计涨幅前3的暴热行业集合（用于硬过滤）。"""
    window = dates[-k:] if len(dates) >= k else dates
    gains = {}
    for ind, m in ind_series.items():
        acc = 1.0
        n = 0
        for d in window:
            r = m.get(d)
            if r is not None:
                acc *= (1.0 + r)
                n += 1
        if n >= 3:
            gains[ind] = acc - 1.0
    ranked = sorted(gains, key=gains.get, reverse=True)
    return set(ranked[:3])


def _sector_data(code: str, ind: str, ind_data: dict, min_days: int = 15) -> dict | None:
    """该股与所属行业收益对齐 -> 相关系数与近10日超额。"""
    if not ind_data:
        return None
    ind_series = ind_data["ind_series"].get(ind)
    rmap = ind_data["rets_by_code"].get(code)
    if not ind_series or not rmap:
        return None
    common = sorted(d for d in rmap if d in ind_series)
    if len(common) < min_days:
        return None
    tail = common[-20:]
    x = [rmap[d] for d in tail]
    y = [ind_series[d] for d in tail]
    corr = pearson(x, y)
    tail10 = common[-10:]
    excess = sum(rmap[d] for d in tail10) - sum(ind_series[d] for d in tail10)
    return {"corr": corr, "excess": excess, "present": corr is not None}


# ---------------- 选股 ----------------

def select_top(scored: list[dict], cfg: dict) -> list[dict]:
    scoring = cfg.get("scoring", {})
    min_score = float(scoring.get("min_score", 60.0))
    top_n = int(scoring.get("top_n", 5))
    max_ind = int(scoring.get("max_per_industry", 2))

    def _val(s):
        return s.get("score") if "score" in s else s.get("total")

    cand = [s for s in scored if _val(s) is not None and _val(s) >= min_score]
    cand.sort(key=lambda s: (-_val(s), -(s.get("sig_score") or 0)))
    ind_count: dict[str, int] = defaultdict(int)
    out = []
    for s in cand:
        ind = s.get("industry")
        if ind and ind_count[ind] >= max_ind:
            continue
        if ind:
            ind_count[ind] += 1
        out.append(s)
        if len(out) >= top_n:
            break
    return out


def _limit_pct(code: str) -> float:
    return 0.195 if code.startswith(("300", "301", "688", "689")) else 0.095


def compute_and_select(conn, cfg: dict, date: str, *, limit: int | None = None,
                       persist: bool = True) -> list[dict]:
    w = int(cfg.get("learning", {}).get("window", 25))
    from_dt = dt.date.fromisoformat(date) - dt.timedelta(days=int((w + 120) * 1.45) + 30)
    from_iso = from_dt.isoformat()

    tpl_rows = conn.execute(
        "SELECT id,code,anchor_date,fwd_ret_10d,w_close,w_vol FROM template WHERE anchor_date < ?",
        (date,),
    ).fetchall()
    tpl_cols = ("id", "code", "anchor_date", "fwd_ret_10d", "w_close", "w_vol")
    templates = [dict(zip(tpl_cols, r)) for r in tpl_rows]

    st = {r[0] for r in conn.execute("SELECT code FROM stock_meta WHERE is_st=1")}
    meta = {m["code"]: m for m in S.list_stock_meta(conn)}
    price_min = float(cfg.get("universe", {}).get("exclude_price_below", 2.0))
    boards = set(cfg.get("universe", {}).get("boards", []))

    cur = conn.execute(
        "SELECT code,date,open,high,low,close,volume FROM daily_bar "
        "WHERE date>=? AND date<=? ORDER BY code,date", (from_iso, date),
    )
    groups: dict[str, list[dict]] = defaultdict(list)
    for code, d, o, h, lo, c, v in cur.fetchall():
        if c is None or c <= 0:
            continue
        groups[code].append({"date": d, "open": o or c, "high": h or c,
                             "low": lo or c, "close": c, "volume": v or 0.0})

    factors_cfg = cfg.get("factors", {})
    factors_on = bool(factors_cfg.get("enabled", True))
    ind_data = _build_industry_data(groups, meta) if factors_on else None
    available = F.active_factors(cfg)
    if ind_data is None:
        available = [k for k in available if k != "f7_sector"]  # 无行业数据 -> F7 失效

    weights = cfg.get("scoring", {}).get("weights", {})
    top_matches = int(cfg.get("learning", {}).get("top_matches", 3))
    sim_w = cfg.get("learning", {}).get("sim_weights", {})
    w_price = float(sim_w.get("price", 0.7))
    w_vol = float(sim_w.get("volume", 0.3))

    scored_all: list[dict] = []
    codes = sorted(groups)
    if limit:
        codes = codes[:limit]
    for code in codes:
        rows = groups[code]
        if rows[-1]["date"] != date:
            continue
        if len(rows) < 2:            # 窗口内只有1根K线（次新/长期停牌），无法算涨跌
            continue
        if code in st:
            continue
        m = meta.get(code)
        if m and m.get("board") and boards and m["board"] not in boards:
            continue
        close0 = rows[-1]["close"]
        if close0 < price_min:
            continue
        chg = close0 / rows[-2]["close"] - 1.0 if rows[-2]["close"] else 0.0
        if chg >= _limit_pct(code) - 0.001:
            continue

        ctx = build_ctx(rows)
        if ctx is None:
            continue

        # 排雷硬过滤（避雷针/爆量/暴热板块）
        ind = (meta.get(code) or {}).get("industry")
        hot = bool(ind_data and ind and ind in ind_data["heat_top3"])
        reasons = F.reject_reasons(ctx, cfg, f7_hot=hot)
        if reasons:
            continue

        sim = template_sim(
            _zscore([r["close"] for r in rows[-w:]]),
            _vol_window(rows, w), templates,
            top_matches=top_matches, w_price=w_price, w_vol=w_vol,
        )

        if factors_on:
            f7_data = _sector_data(code, ind, ind_data) if ind else None
            comp = F.composite_score(ctx, cfg, f7_data, available)
            sig = comp.get("signal_score")
            hits = [f"{f['name']}={f['score']:.0f}" for f in comp["factors"] if f["score"] > 0]
            combined = _combine(sim.get("pattern_score"), sig, _trend_score(ctx), hits, sim, cfg)
        else:
            combined = score_components(rows, cfg, sim)

        if combined is None or combined.get("total") is None:
            continue
        scored_all.append({
            "date": date, "code": code, "rank": 0,
            "industry": ind,
            "score": combined["total"], "sim_score": combined["sim"],
            "sig_score": combined["signal_max"], "trend_score": combined["trend"],
            "hits": combined["hits"], "matched_tpl_id": combined["best_tpl_id"],
            "best_tpl_code": combined["best_code"], "best_tpl_anchor": combined["best_anchor"],
        })

    selected = select_top(scored_all, cfg)
    for i, s in enumerate(selected, 1):
        s["rank"] = i
    if persist and selected:
        S.replace_scan_results(conn, date, selected)
    logger.info("%s: 候选 %d 只，入选 Top%d，因子层=%s", date, len(scored_all),
                len(selected), "高级7因子" if factors_on else "旧5信号")
    return selected


def _vol_window(rows: list[dict], w: int) -> list[float]:
    vols = [r["volume"] for r in rows]
    ma = _rolling_mean(vols, 20)
    return [round(v / (ma[k] or 0.0), 4) if ma[k] else 0.0
            for k, v in zip(range(len(vols) - w, len(vols)), vols[-w:])]
