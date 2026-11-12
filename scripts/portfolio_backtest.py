"""组合净值回测：周度选股 + 三档仓位择时，输出真正的资金曲线与风险指标。

为什么需要这个：
    之前所有评估都是"事件研究"（每个选股日 5/20 只票的 20 日超额），
    它回答"选股有没有 alpha"，但回答不了"这个策略能不能赚钱、会亏多少"。
    实测发现：市场 -1.5%/20日时策略 -0.8% —— 相对赢，绝对亏。
    纯多头在下跌市必亏，所以必须加一层仓位管理，并用净值曲线评估。

设计（按用户决定的方案）：
  1) 市场状态 = 全市场宽度（收盘>MA20 的股票占比）+ 等权指数均线
        强势：等权指数 > MA20 且 宽度 > strong  → 仓位 100%
        弱势：等权指数 < MA60 或 宽度 < weak     → 仓位 0%（持币）
        其余                                    → 仓位 50%
  2) 每周五选 Top N 只，资金分批：每次投入「当前净值 × alloc × 仓位」建一个新批次，
     持有 hold 个交易日（T+1 开盘买入 → 第 hold 日收盘卖出），4 批错开滚动。
  3) 输出：年化收益、最大回撤、Sharpe、Calmar、月度胜率、以及"无择时"对照。

用法:
    python scripts/portfolio_backtest.py --days 250 --step 5 --hold 20 --topn 20
    python scripts/portfolio_backtest.py --days 250 --timing off        # 无择时对照
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
    """一次遍历算每日：全市场宽度（收盘>MA20 占比）与等权指数净值/均线。

    等权指数 = 每日全市场（非ST、价格≥2）等权收益的累积 —— 比沪深300更贴近我们
    实际选股的池子，且不受指数成分调整影响。
    """
    st = {r[0] for r in conn.execute("SELECT code FROM stock_meta WHERE is_st=1")}
    start = dates[0]
    rows = conn.execute(
        "SELECT code, date, close FROM daily_bar WHERE date >= ? AND date <= ? "
        "ORDER BY code, date", (start, dates[-1])).fetchall()

    close_hist: dict[str, list[float]] = defaultdict(list)
    above = defaultdict(int)          # date -> 站上MA20的只数
    counted = defaultdict(int)        # date -> 参与统计的只数
    day_ret = defaultdict(list)       # date -> 全市场当日收益样本
    prev_close: dict[str, float] = {}

    date_set = set(dates)
    for code, d, c in rows:
        if c is None or c <= 0 or code in st:
            continue
        hist = close_hist[code]
        hist.append(c)
        if d in date_set:
            counted[d] += 1
            if len(hist) > ma_short:
                ma = sum(hist[-ma_short - 1:-1]) / ma_short
                if ma > 0 and c > ma:
                    above[d] += 1
            p = prev_close.get(code)
            if p and p > 0 and d in date_set:
                day_ret[d].append(c / p - 1.0)
        prev_close[code] = c

    out: dict[str, dict] = {}
    eq = 1.0
    eq_hist: list[float] = []
    for d in dates:
        rs = day_ret.get(d) or []
        r = (sum(rs) / len(rs)) if rs else 0.0
        eq *= (1.0 + r)
        eq_hist.append(eq)
        n = counted.get(d, 0)
        out[d] = {
            "breadth": (above.get(d, 0) / n) if n else None,
            "eq": eq,
            "ret": r,
        }
    # 均线（不足时用已有样本均值）
    for i, d in enumerate(dates):
        for win, key in ((ma_short, "ma_short"), (ma_long, "ma_long")):
            seg = eq_hist[max(0, i - win + 1):i + 1]
            out[d][key] = sum(seg) / len(seg) if seg else None
    return out


def timing_weight(state: dict | None, strong: float, weak: float) -> float:
    if not state:
        return 0.0
    b, eq, ms, ml = state.get("breadth"), state.get("eq"), state.get("ma_short"), state.get("ma_long")
    if b is None or ms is None or ml is None:
        return 0.5
    if eq > ms and b > strong:
        return 1.0
    if eq < ml or b < weak:
        return 0.0
    return 0.5


# ---------------- 组合净值 ----------------

def run_portfolio(conn, cfg, dates: list[str], day_list: list[str], hold: int, topn: int,
                  alloc: float, cost: float, timing_on: bool, strong: float, weak: float,
                  states: dict[str, dict]) -> dict:
    """dates 用完整日历（供市场状态查表），净值曲线只从首个批次开始算 ——
    否则会把库里 30 年的空仓期一起算进年化，年化被稀释成 -0.3%。"""
    logger = setup_logger("portfolio")
    idx = {d: i for i, d in enumerate(dates)}
    window = [d for d in dates if day_list and d >= dates[max(0, idx[day_list[0]] - hold)]]
    scoring = dict(cfg.get("scoring", {}) or {})
    scoring["top_n"] = topn
    if topn >= 50:
        scoring["min_percentile"] = None
        scoring["min_score"] = None

    # 1) 建批次：每个选股日一批，记录持仓与各日累计收益路径
    batches = []
    for d in day_list:
        i = idx[d]
        if i + hold >= len(dates):
            continue
        sel = select_top(score_universe(conn, cfg, d), {"scoring": scoring})
        if not sel:
            continue
        codes = [s["code"] for s in sel]
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
            vals = []
            for c in codes:
                e = entry_px.get(c)
                v = px.get(c, {}).get(dd)
                if e and v and v[1]:
                    vals.append(v[1] / e - 1.0)
            if vals:
                path[dd] = sum(vals) / len(vals) - cost     # 等权、扣双边成本
        batches.append({"open": entry_date, "close": win_dates[-1],
                        "timing_i": i, "path": path, "n": len(codes)})

    if not batches:
        raise RuntimeError("没有可用的批次（选股或行情为空）")

    # 2) 顺序推进净值：每周投入 alloc×仓位 建新批次，到期回笼现金
    equity = 1.0
    cash = 1.0
    active = []                     # [(batch, capital)]
    curve: list[tuple[str, float]] = []
    by_open = defaultdict(list)
    for b in batches:
        by_open[b["open"]].append(b)

    for k, d in enumerate(window):
        # 到期的批次回笼
        keep = []
        for b, cap in active:
            if d > b["close"]:
                cash += cap * (1.0 + b["path"].get(b["close"], 0.0))
            else:
                keep.append((b, cap))
        active = keep
        # 本日新开的批次
        for b in by_open.get(d, []):
            w = timing_weight(states.get(dates[max(0, b["timing_i"] - 1)]), strong, weak) if timing_on else 1.0
            cap = equity * alloc * w
            if cap > 0:
                cash -= cap
                active.append((b, cap))
        # 用今日收盘标记
        total = cash + sum(cap * (1.0 + b["path"].get(d, 0.0)) for b, cap in active)
        equity = total
        curve.append((d, equity))

    # 3) 风险指标
    eqs = [v for _d, v in curve]
    rets = [eqs[i] / eqs[i - 1] - 1.0 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
    n = len(rets)
    span_years = n / 244.0 if n else 0
    total_ret = (eqs[-1] / eqs[0] - 1.0) if eqs and eqs[0] > 0 else None
    ann = ((eqs[-1] / eqs[0]) ** (1.0 / span_years) - 1.0) if (span_years > 0.1 and eqs[0] > 0) else None
    peak, mdd = -1e9, 0.0
    for v in eqs:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    mean_r = sum(rets) / n if n else 0.0
    sd = math.sqrt(sum((x - mean_r) ** 2 for x in rets) / n) if n > 1 else 0.0
    sharpe = (mean_r / sd * math.sqrt(244)) if sd > 0 else None
    calmar = (ann / abs(mdd)) if (ann is not None and mdd < 0) else None
    # 月度胜率
    monthly: dict[str, list[float]] = defaultdict(list)
    for (d, v), (d_prev, v_prev) in zip(curve[1:], curve[:-1]):
        if v_prev > 0:
            monthly[d[:7]].append(v / v_prev - 1.0)
    m_win = None
    if monthly:
        wins = sum(1 for m, rs in monthly.items() if sum(rs) > 0)
        m_win = wins / len(monthly)

    logger.info("组合回测完成：批次 %d，交易日 %d，期末净值 %.4f", len(batches), len(curve), eqs[-1])
    return {"curve": curve, "n_batches": len(batches), "total": total_ret, "ann": ann,
            "mdd": mdd, "sharpe": sharpe, "calmar": calmar, "monthly_win": m_win,
            "n_days": n, "avg_positions": (sum(b["n"] for b in batches) / len(batches))}


def _pct(x, d=1):
    return f"{x*100:+.{d}f}%" if x is not None else "N/A"


def main() -> int:
    ap = argparse.ArgumentParser(description="组合净值回测（周度选股 + 三档仓位择时）")
    ap.add_argument("--days", type=int, default=250, help="回看最近 N 个交易日")
    ap.add_argument("--step", type=int, default=5, help="每 N 个交易日建一批（默认5≈每周）")
    ap.add_argument("--hold", type=int, default=20, help="每批持有交易日")
    ap.add_argument("--topn", type=int, default=None, help="每批持股数（默认取配置 top_n）")
    ap.add_argument("--alloc", type=float, default=0.25, help="每批投入净值比例（默认0.25=4批错开）")
    ap.add_argument("--cost", type=float, default=0.0015, help="双边成本")
    ap.add_argument("--timing", choices=["on", "off"], default="on", help="是否启用仓位择时")
    ap.add_argument("--breadth-strong", type=float, default=0.5, help="宽度强势阈值（> 则满仓）")
    ap.add_argument("--breadth-weak", type=float, default=0.3, help="宽度弱势阈值（< 则空仓）")
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
    logger.info("回测 %s ~ %s：%d 个选股日，每 %d 日一批，持有 %d 日，每批 %d 只，仓位择时=%s",
                day_list[0], day_list[-1], len(day_list), args.step, args.hold, topn, args.timing)

    states = market_state(conn, cal)
    res_on = run_portfolio(conn, cfg, cal, day_list, args.hold, topn, args.alloc,
                           args.cost, args.timing == "on", args.breadth_strong,
                           args.breadth_weak, states)
    res_off = run_portfolio(conn, cfg, cal, day_list, args.hold, topn, args.alloc,
                            args.cost, False, args.breadth_strong, args.breadth_weak, states)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    rp = os.path.join(out_dir, f"portfolio_{stamp}.md")
    L = ["# 组合净值回测（周度选股 + 三档仓位择时）", "",
         f"- 区间 {day_list[0]} ~ {day_list[-1]}；每 {args.step} 交易日一批，持有 {args.hold} 日，"
         f"每批 {topn} 只等权，每批投入净值 {args.alloc:.0%}",
         f"- 收益口径：T+1 开盘买入 → 第 {args.hold} 日收盘卖出，扣双边成本 {args.cost*100:.2f}%",
         f"- 择时：宽度强势>{args.breadth_strong:.0%}且等权指数>MA20 → 满仓；"
         f"宽度<{args.breadth_weak:.0%}或等权指数<MA60 → 空仓；其余半仓", "",
         "| 方案 | 批次 | 期末净值 | 总收益 | 年化 | 最大回撤 | Sharpe | Calmar | 月度胜率 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for label, r in (("**有择时**", res_on), ("无择时（满仓）", res_off)):
        sh = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "N/A"
        cm = f"{r['calmar']:.2f}" if r["calmar"] is not None else "N/A"
        mw = f"{r['monthly_win']*100:.0f}%" if r["monthly_win"] is not None else "N/A"
        L.append(f"| {label} | {r['n_batches']} | {r['curve'][-1][1]:.4f} | {_pct(r['total'])} "
                 f"| {_pct(r['ann'])} | {_pct(r['mdd'])} | {sh} | {cm} | {mw} |")
    L += ["", "## 逐月净值（有择时 / 无择时）", "",
          "| 月份 | 有择时 | 无择时 |", "|---|---|---|"]
    def monthly_curve(curve):
        out = {}
        for d, v in curve:
            out.setdefault(d[:7], v)
        return out
    m_on, m_off = monthly_curve(res_on["curve"]), monthly_curve(res_off["curve"])
    for m in sorted(set(m_on) | set(m_off)):
        L.append(f"| {m} | {m_on.get(m, float('nan')):.4f} | {m_off.get(m, float('nan')):.4f} |")
    L += ["", "> 判读：目标是**有择时版本的最大回撤显著小于无择时、且年化不更差**。",
          "> 单看年化容易被一两段行情主导；回撤与 Calmar 更能反映这套东西能不能拿得住。", ""]
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    print("\n=== 组合净值回测（每批 %d 只，持有 %d 日，成本 %.2f%%）==="
          % (topn, args.hold, args.cost * 100))
    print(f"{'方案':<14}{'批次':>5}{'期末净值':>9}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'Sharpe':>8}{'Calmar':>8}")
    for label, r in (("有择时", res_on), ("无择时", res_off)):
        sh = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "N/A"
        cm = f"{r['calmar']:.2f}" if r["calmar"] is not None else "N/A"
        print(f"{label:<14}{r['n_batches']:>5}{r['curve'][-1][1]:>9.4f}{_pct(r['total']):>9}"
              f"{_pct(r['ann']):>8}{_pct(r['mdd']):>9}{sh:>8}{cm:>8}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
