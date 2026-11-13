"""组合净值回测：周度选股 + 入场确认 + 三档仓位择时，输出资金曲线与风险指标。

为什么需要这个：
    之前的"事件研究"（每个选股日 N 只票的 20 日超额）只回答"选股有没有 alpha"，
    回答不了"这个策略能不能赚钱、会亏多少"。实测：市场 -1.5%/20日时策略 -0.8%，
    相对赢、绝对亏 —— 所以必须看净值曲线与最大回撤。

本脚本做 2×2 对照（打分只算一遍，四种组合共用）：

    ┌─────────────┬──────────────┬──────────────┐
    │             │ 入场确认 开   │ 入场确认 关   │
    ├─────────────┼──────────────┼──────────────┤
    │ 仓位择时 开  │              │              │
    │ 仓位择时 关  │              │              │
    └─────────────┴──────────────┴──────────────┘

  入场确认（硬性条件，config: scoring.entry_confirm）：放量 / 站上 MA5 等，
      把"纯左侧"变成"左侧偏右"。
  仓位择时（本脚本参数）：全市场宽度 + 等权指数均线 → 100% / 50% / 0% 仓位。

用法:
    python scripts/portfolio_backtest.py --days 250 --step 5 --hold 20 --topn 20
    python scripts/portfolio_backtest.py --days 250 --breadth-strong 0.55 --breadth-weak 0.35
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.screen.scorer import score_universe, select_top  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


# ---------------- 市场状态（全市场宽度 + 等权指数） ----------------

def market_state(conn, dates: list[str], ma_short: int = 20, ma_long: int = 60) -> dict[str, dict]:
    """一次遍历算每日：全市场宽度（收盘>MA20 占比）与等权指数净值/均线。"""
    st = {r[0] for r in conn.execute("SELECT code FROM stock_meta WHERE is_st=1")}
    rows = conn.execute(
        "SELECT code, date, close FROM daily_bar WHERE date >= ? AND date <= ? "
        "ORDER BY code, date", (dates[0], dates[-1])).fetchall()

    hist: dict[str, list[float]] = defaultdict(list)
    above = defaultdict(int)
    counted = defaultdict(int)
    day_ret = defaultdict(list)
    prev_close: dict[str, float] = {}
    date_set = set(dates)

    for code, d, c in rows:
        if c is None or c <= 0 or code in st:
            continue
        h = hist[code]
        h.append(c)
        if d in date_set:
            counted[d] += 1
            if len(h) > ma_short:
                ma = sum(h[-ma_short - 1:-1]) / ma_short
                if ma > 0 and c > ma:
                    above[d] += 1
            p = prev_close.get(code)
            if p and p > 0:
                day_ret[d].append(c / p - 1.0)
        prev_close[code] = c

    out: dict[str, dict] = {}
    eq, eq_hist = 1.0, []
    for d in dates:
        rs = day_ret.get(d) or []
        r = (sum(rs) / len(rs)) if rs else 0.0
        eq *= (1.0 + r)
        eq_hist.append(eq)
        n = counted.get(d, 0)
        out[d] = {"breadth": (above.get(d, 0) / n) if n else None, "eq": eq, "ret": r}
    for i, d in enumerate(dates):
        for win, key in ((ma_short, "ma_short"), (ma_long, "ma_long")):
            seg = eq_hist[max(0, i - win + 1):i + 1]
            out[d][key] = sum(seg) / len(seg) if seg else None
    return out


def timing_weight(state: dict | None, strong: float, weak: float) -> float:
    if not state:
        return 0.0
    b, eq = state.get("breadth"), state.get("eq")
    ms, ml = state.get("ma_short"), state.get("ma_long")
    if b is None or ms is None or ml is None:
        return 0.5
    if eq > ms and b > strong:
        return 1.0
    if eq < ml or b < weak:
        return 0.0
    return 0.5


# ---------------- 选股（打分一次 → 确认开/关两份结果） ----------------

def select_days(conn, cfg, day_list: list[str], topn: int) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {"on": {}, "off": {}}
    scoring = dict(cfg.get("scoring", {}) or {})
    scoring["top_n"] = topn
    if topn >= 50:
        scoring["min_percentile"] = None
        scoring["min_score"] = None
    for d in day_list:
        scored = score_universe(conn, cfg, d, apply_confirm=False)
        if not scored:
            continue
        for tag in ("on", "off"):
            rows = scored if tag == "off" else [s for s in scored if s.get("confirm_ok")]
            sel = select_top(rows, {"scoring": scoring})
            if sel:
                out[tag][d] = [s["code"] for s in sel]
    return out


# ---------------- 批次与净值 ----------------

def build_batches(conn, dates: list[str], picks: dict[str, list[str]], hold: int, cost: float) -> list[dict]:
    idx = {d: i for i, d in enumerate(dates)}
    batches = []
    for d in sorted(picks):
        i = idx[d]
        if i + hold >= len(dates):
            continue
        codes = picks[d]
        win_dates = dates[i:i + hold + 1]
        ph = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT code, date, open, close FROM daily_bar WHERE code IN ({ph}) "
            f"AND date >= ? AND date <= ?", (*codes, win_dates[0], win_dates[-1])).fetchall()
        px: dict[str, dict[str, tuple]] = defaultdict(dict)
        for c, dd, o, cl in rows:
            px[c][dd] = (o, cl)
        entry_date, entry_px = None, {}
        for dd in win_dates[1:]:                       # T+1 开盘买入
            for c in codes:
                v = px.get(c, {}).get(dd)
                if v and v[0]:
                    entry_px[c] = v[0]
            if entry_px:
                entry_date = dd
                break
        if not entry_date:
            continue
        path: dict[str, float] = {}
        for dd in win_dates:
            if dd < entry_date:
                continue
            vals = [px[c][dd][1] / entry_px[c] - 1.0
                    for c in codes
                    if c in entry_px and dd in px.get(c, {}) and px[c][dd][1]]
            if vals:
                path[dd] = sum(vals) / len(vals) - cost      # 等权 + 双边成本
        batches.append({"open": entry_date, "close": win_dates[-1],
                        "select_i": i, "path": path, "n": len(codes)})
    return batches


def run_equity(dates: list[str], batches: list[dict], alloc: float, timing_on: bool,
               states: dict[str, dict], strong: float, weak: float) -> dict:
    if not batches:
        return {"curve": dates and [(dates[0], 1.0)] or [], "n_batches": 0, "total": None,
                "ann": None, "mdd": None, "sharpe": None, "calmar": None, "monthly_win": None}
    start_i = min(b["select_i"] for b in batches)
    window = dates[max(0, start_i - 1):]
    by_open = defaultdict(list)
    for b in batches:
        by_open[b["open"]].append(b)

    equity, cash, active = 1.0, 1.0, []
    curve: list[tuple[str, float]] = []
    for d in window:
        keep = []
        for b, cap in active:
            if d > b["close"]:
                cash += cap * (1.0 + b["path"].get(b["close"], 0.0))
            else:
                keep.append((b, cap))
        active = keep
        for b in by_open.get(d, []):
            w = timing_weight(states.get(dates[max(0, b["select_i"] - 1)]), strong, weak) if timing_on else 1.0
            cap = equity * alloc * w
            if cap > 0:
                cash -= cap
                active.append((b, cap))
        equity = cash + sum(cap * (1.0 + b["path"].get(d, 0.0)) for b, cap in active)
        curve.append((d, equity))

    eqs = [v for _d, v in curve]
    rets = [eqs[i] / eqs[i - 1] - 1.0 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
    n = len(rets)
    span = n / 244.0 if n else 0
    total = (eqs[-1] / eqs[0] - 1.0) if eqs and eqs[0] > 0 else None
    ann = ((eqs[-1] / eqs[0]) ** (1.0 / span) - 1.0) if (span > 0.05 and eqs[0] > 0) else None
    peak, mdd = -1e9, 0.0
    for v in eqs:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    mean_r = sum(rets) / n if n else 0.0
    sd = math.sqrt(sum((x - mean_r) ** 2 for x in rets) / n) if n > 1 else 0.0
    sharpe = (mean_r / sd * math.sqrt(244)) if sd > 0 else None
    calmar = (ann / abs(mdd)) if (ann is not None and mdd < 0) else None
    monthly: dict[str, list[float]] = defaultdict(list)
    for (d, v), (_dp, vp) in zip(curve[1:], curve[:-1]):
        if vp > 0:
            monthly[d[:7]].append(v / vp - 1.0)
    m_win = (sum(1 for rs in monthly.values() if sum(rs) > 0) / len(monthly)) if monthly else None
    return {"curve": curve, "n_batches": len(batches), "total": total, "ann": ann, "mdd": mdd,
            "sharpe": sharpe, "calmar": calmar, "monthly_win": m_win, "n_days": n,
            "avg_pos": (sum(b["n"] for b in batches) / len(batches))}


def _pct(x, d=1):
    return f"{x*100:+.{d}f}%" if x is not None else "N/A"


def main() -> int:
    ap = argparse.ArgumentParser(description="组合净值回测（2×2：入场确认 × 仓位择时）")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--step", type=int, default=5, help="每 N 个交易日建一批（默认5≈每周）")
    ap.add_argument("--hold", type=int, default=20, help="每批持有交易日")
    ap.add_argument("--topn", type=int, default=None, help="每批持股数（默认取配置 top_n）")
    ap.add_argument("--alloc", type=float, default=0.25, help="每批投入净值比例（默认0.25）")
    ap.add_argument("--cost", type=float, default=0.0015, help="双边成本")
    ap.add_argument("--breadth-strong", type=float, default=0.5)
    ap.add_argument("--breadth-weak", type=float, default=0.3)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    logger = setup_logger("portfolio_bt", cfg_get(cfg, "paths.log"))
    conn = S.open_db(cfg_get(cfg, "paths.db", "data/screener.db"))
    topn = args.topn or int(cfg_get(cfg, "scoring.top_n", 20))

    cal = [r[0] for r in conn.execute("SELECT date FROM trade_cal ORDER BY date")]
    max_bar = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    cal = [d for d in cal if d <= max_bar]
    if len(cal) < args.hold + 20:
        print("数据不足")
        return 2
    usable = cal[: len(cal) - args.hold - 2]
    day_list = usable[-args.days:][::-1][::args.step][::-1]
    ec = (cfg.get("scoring") or {}).get("entry_confirm") or {}
    logger.info("回测 %s ~ %s：%d 个选股日，持有 %d 日，每批 %d 只；入场确认=%s(%s)",
                day_list[0], day_list[-1], len(day_list), args.hold, topn,
                ec.get("enable"), ec.get("mode", "all"))

    states = market_state(conn, cal)
    picks = select_days(conn, cfg, day_list, topn)
    logger.info("选股：确认开 %d 天 / 确认关 %d 天", len(picks["on"]), len(picks["off"]))

    results = {}
    for tag in ("on", "off"):
        batches = build_batches(conn, cal, picks[tag], args.hold, args.cost)
        for timing in (True, False):
            key = f"{'确认开' if tag == 'on' else '确认关'}+{'择时开' if timing else '择时关'}"
            results[key] = run_equity(cal, batches, args.alloc, timing, states,
                                      args.breadth_strong, args.breadth_weak)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    rp = os.path.join(out_dir, f"portfolio_{stamp}.md")
    L = ["# 组合净值回测（2×2：入场确认 × 仓位择时）", "",
         f"- 区间 {day_list[0]} ~ {day_list[-1]}；每 {args.step} 交易日一批，持有 {args.hold} 日，"
         f"每批 {topn} 只等权，每批投入净值 {args.alloc:.0%}",
         f"- 收益口径：T+1 开盘买入 → 第 {args.hold} 日收盘卖出，扣双边成本 {args.cost*100:.2f}%",
         f"- 入场确认：enable={ec.get('enable')}, mode={ec.get('mode', 'all')}, "
         f"放量≥{ec.get('vol_ratio_min')}, 站上MA5={ec.get('above_ma5')}",
         f"- 仓位择时：宽度>{args.breadth_strong:.0%}且等权指数>MA20 → 满仓；"
         f"宽度<{args.breadth_weak:.0%}或等权指数<MA60 → 空仓；其余半仓", "",
         "| 方案 | 批次 | 期末净值 | 总收益 | 年化 | 最大回撤 | Sharpe | Calmar | 月度胜率 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for key, r in results.items():
        sh = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "N/A"
        cm = f"{r['calmar']:.2f}" if r["calmar"] is not None else "N/A"
        mw = f"{r['monthly_win']*100:.0f}%" if r["monthly_win"] is not None else "N/A"
        nav = r["curve"][-1][1] if r["curve"] else float("nan")
        L.append(f"| {key} | {r['n_batches']} | {nav:.4f} | {_pct(r['total'])} | {_pct(r['ann'])} "
                 f"| {_pct(r['mdd'])} | {sh} | {cm} | {mw} |")
    L += ["", "## 逐月净值", "",
          "| 月份 | " + " | ".join(results.keys()) + " |",
          "|---|" + "---|" * len(results)]
    mcurves = {}
    for key, r in results.items():
        m = {}
        for d, v in r["curve"]:
            m.setdefault(d[:7], v)
        mcurves[key] = m
    for mon in sorted(set().union(*[set(m) for m in mcurves.values()])):
        L.append("| " + mon + " | " + " | ".join(
            f"{mcurves[k].get(mon, float('nan')):.4f}" for k in results) + " |")
    L += ["", "> 判读：先看**入场确认**是否提升了 Sharpe / 降低了回撤（这是本次的主变量）；",
          "> 再看**仓位择时**在同一确认设置下是加分还是减分。", ""]
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    print("\n=== 组合净值回测 2×2（每批 %d 只，持有 %d 日，成本 %.2f%%）==="
          % (topn, args.hold, args.cost * 100))
    print(f"{'方案':<18}{'批次':>5}{'期末净值':>10}{'总收益':>9}{'年化':>9}{'最大回撤':>10}{'Sharpe':>8}{'Calmar':>8}")
    for key, r in results.items():
        sh = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "N/A"
        cm = f"{r['calmar']:.2f}" if r["calmar"] is not None else "N/A"
        nav = r["curve"][-1][1] if r["curve"] else float("nan")
        print(f"{key:<18}{r['n_batches']:>5}{nav:>10.4f}{_pct(r['total']):>9}{_pct(r['ann']):>9}"
              f"{_pct(r['mdd']):>10}{sh:>8}{cm:>8}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
