"""Phase 2 验证器：样本外回放，评估选股信号是否真的优于「同日全市场」。

设计要点（2026-09-18 升级，之前几版的坑都写在这里）：
1) ★ 非重叠采样：默认每 step 个交易日取一个选股日（step 默认 = 持有期 10），
   避免"持有期重叠"造成的伪样本量（25 天看起来有 125 个持仓，实际只有 20+ 个独立观测）。
2) ★ 同日配对基准：同一天里「入选票平均收益」对比「全市场平均收益」，
   消掉大盘涨跌（beta）—— 用"随机抽单只票"当基准会把 beta 混进来。
3) ★ 现实口径：T+1 开盘买入 → 第 HOLD 个交易日收盘卖出，并扣双边成本（默认 0.15%）。
   同时输出"理想口径"（当日收盘买、HOLD 日后收盘卖、不扣成本）以便与历史数字对照。
4) ★ 稳健性：报告剔除最好 1/2/3 天后的超额 —— 直接暴露"超额是否只靠极少数行情"。

用法:
    python scripts/validate.py --days 250 --step 10      # 25 个独立观测（推荐）
    python scripts/validate.py --days 250 --step 1       # 每日都测（样本重叠，仅供参考）
    python scripts/validate.py --days 25                 # 快速跑通
    python scripts/validate.py --days 120 --sweep        # 对比 4 组权重
    python scripts/validate.py --days 120 --cost 0.003 --entry close
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.screen.scorer import compute_and_select  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402

SWEEP_SCENARIOS = [
    ("combined", None),
    ("pattern_only", {"pattern_sim": 1.0, "signal": 0.0, "trend": 0.0}),
    ("signal_only", {"pattern_sim": 0.0, "signal": 1.0, "trend": 0.0}),
    ("trend_only", {"pattern_sim": 0.0, "signal": 0.0, "trend": 1.0}),
]


# ---------------- 基础设施 ----------------

def _trade_dates(conn) -> list[str]:
    """可回放的交易日列表。

    ★ 必须按「库里真实有行情的最后一天」截断：
      trade_cal 是**整年**日历（含未来日期），直接 dates[-25:] 会取到还没发生的交易日，
      那些日子库里没有任何行情 → 候选恒为 0、回测静默产出 N/A。
    """
    dates = [r[0] for r in conn.execute("SELECT date FROM trade_cal ORDER BY date")]
    if not dates:
        dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date")]
    max_bar = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    if max_bar:
        future = [d for d in dates if d > max_bar]
        if future:
            logging.getLogger("validate").info(
                "交易日历含 %d 个晚于行情的日期（%s ~ %s），已截断到 %s",
                len(future), future[0], future[-1], max_bar)
        dates = [d for d in dates if d <= max_bar]
    return dates


def _pick_days(dates: list[str], days: int, step: int, offset: int, hold: int) -> list[str]:
    """挑出回放用的选股日：最近 days 个可用交易日中，每隔 step 个取一天（非重叠）。"""
    usable = dates[: len(dates) - hold]          # 最后 hold 天没有完整的前瞻窗口
    if offset > 0:
        usable = usable[:-offset] if offset < len(usable) else []
    seg = usable[-days:] if days < len(usable) else usable
    if not seg:
        return []
    return seg[::-1][::max(1, step)][::-1]


def _entry_price(conn, code: str, d1: str, d0: str):
    """现实口径的买入价：优先 d1 开盘，其次 d1 收盘，最后退回 d0 收盘（停牌等情形）。"""
    row = conn.execute("SELECT open, close FROM daily_bar WHERE code=? AND date=?",
                       (code, d1)).fetchone()
    if row:
        if row[0]:
            return row[0]
        if row[1]:
            return row[1]
    return S.close_on(conn, code, d0)


def _market_day(conn, d0: str, d1: str, d10: str, cost: float, price_min: float) -> dict | None:
    """当日全市场（非ST、价格≥下限、等权）的：理想收益 / 现实收益 / 胜率。"""
    st0 = "AND b0.code NOT IN (SELECT code FROM stock_meta WHERE is_st=1)"
    st2 = "AND b2.code NOT IN (SELECT code FROM stock_meta WHERE is_st=1)"
    row = conn.execute(
        f"SELECT AVG(b1.close / b0.close - 1.0), COUNT(*) FROM daily_bar b0 "
        f"JOIN daily_bar b1 ON b1.code = b0.code AND b1.date = ? "
        f"WHERE b0.date = ? AND b0.close >= ? {st0}", (d10, d0, price_min)).fetchone()
    if not row or not row[1]:
        return None
    ideal, n = float(row[0]), int(row[1])
    row = conn.execute(
        f"SELECT AVG(b1.close / b2.open - 1.0), "
        f"AVG(CASE WHEN b1.close > b2.open THEN 1.0 ELSE 0.0 END), COUNT(*) "
        f"FROM daily_bar b2 "
        f"JOIN daily_bar b1 ON b1.code = b2.code AND b1.date = ? "
        f"WHERE b2.date = ? AND b2.open >= ? {st2}", (d10, d1, price_min)).fetchone()
    if not row or not row[2]:
        return None
    return {"ideal": ideal, "real": float(row[0]) - cost, "hit": float(row[1]), "n_market": int(row[2])}


# ---------------- 回放 ----------------

def _run_replay(conn, cfg, dates: list[str], day_list: list[str], limit: int | None,
                hold: int, cost: float, entry: str) -> tuple[list[dict], list[dict]]:
    logger = setup_logger("validate")
    if not day_list:
        raise RuntimeError("没有可回放的选股日（数据长度不足）")
    idx = {d: i for i, d in enumerate(dates)}
    price_min = float(cfg_get(cfg, "universe.exclude_price_below", 2.0))
    logger.info("回放区间 %s ~ %s（%d 个选股日，持有 %d 个交易日）",
                day_list[0], day_list[-1], len(day_list), hold)

    picks: list[dict] = []
    per_day: list[dict] = []
    for i, d in enumerate(day_list, 1):
        j = idx[d]
        if j + hold >= len(dates):
            continue
        d1, d10 = dates[j + 1], dates[j + hold]
        mkt = _market_day(conn, d, d1, d10, cost, price_min)
        selected = compute_and_select(conn, cfg, d, limit=limit, persist=False)
        rows = []
        for s in selected:
            code = s["code"]
            c0 = S.close_on(conn, code, d)
            c10 = S.close_on_or_before(conn, code, d10)
            op1 = _entry_price(conn, code, d1, d) if entry == "open" else c0
            ideal = (c10 / c0 - 1.0) if (c0 and c10 and c0 > 0) else None
            if entry == "open":
                real = (c10 / op1 - 1.0 - cost) if (op1 and c10 and op1 > 0) else None
            else:
                real = (ideal - cost) if ideal is not None else None
            rows.append({"date": d, "code": code, "score": s.get("score"),
                         "ideal": ideal, "real": real})
        picks.extend(rows)
        rec = {"date": d, "n": len(rows)}
        for key in ("ideal", "real"):
            vals = [r[key] for r in rows if r[key] is not None]
            rec[f"picks_{key}"] = (sum(vals) / len(vals)) if vals else None
        rec["picks_hit"] = None
        rv = [r["real"] for r in rows if r["real"] is not None]
        if rv:
            rec["picks_hit"] = sum(1 for x in rv if x > 0) / len(rv)
        if mkt:
            rec["mkt_ideal"] = mkt["ideal"]
            rec["mkt_real"] = mkt["real"]
            rec["mkt_hit"] = mkt["hit"]
        per_day.append(rec)
        if i % 5 == 0 or i == len(day_list):
            logger.info("回放进度 %d/%d，累计入选 %d", i, len(day_list), len(picks))
    return picks, per_day


# ---------------- 统计 ----------------

def _tstat(xs: list[float]) -> tuple[float | None, float | None]:
    n = len(xs)
    if n < 3:
        return None, None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None, None
    t = m / math.sqrt(var / n)
    return t, math.erfc(abs(t) / math.sqrt(2))          # 双侧近似 p 值


def _mean(xs: list[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _summarize(per_day: list[dict], label: str) -> dict:
    """以"交易日"为观测单位做配对统计（非重叠采样后各观测才近似独立）。"""
    out = {"label": label}
    for key in ("real", "ideal"):
        ex = [d[f"picks_{key}"] - d[f"mkt_{key}"] for d in per_day
              if d.get(f"picks_{key}") is not None and d.get(f"mkt_{key}") is not None]
        out[key] = {
            "n_days": len(ex),
            "excess_mean": _mean(ex),
            "excess_median": (sorted(ex)[len(ex) // 2] if ex else None),
            "win_rate": (sum(1 for x in ex if x > 0) / len(ex)) if ex else None,
            "t": _tstat(ex)[0], "p": _tstat(ex)[1],
            "trim1": _mean(sorted(ex)[:-1]) if len(ex) > 3 else None,
            "trim2": _mean(sorted(ex)[:-2]) if len(ex) > 4 else None,
            "trim3": _mean(sorted(ex)[:-3]) if len(ex) > 5 else None,
        }
    pr = [d["picks_real"] for d in per_day if d.get("picks_real") is not None]
    mk = [d["mkt_real"] for d in per_day if d.get("mkt_real") is not None]
    ph = [d["picks_hit"] for d in per_day if d.get("picks_hit") is not None]
    mh = [d["mkt_hit"] for d in per_day if d.get("mkt_hit") is not None]
    out["picks_real_mean"] = _mean(pr)
    out["mkt_real_mean"] = _mean(mk)
    out["picks_hit_mean"] = _mean(ph)
    out["mkt_hit_mean"] = _mean(mh)
    out["n_picks"] = sum(d["n"] for d in per_day)
    return out


# ---------------- 报告 ----------------

def _pct(x, digits=1):
    return f"{x*100:+.{digits}f}%" if x is not None else "N/A"


def _pct0(x, digits=1):
    return f"{x*100:.{digits}f}%" if x is not None else "N/A"


def _main_report(results: list[dict], day_list: list[str], hold: int, cost: float,
                 entry: str, step: int) -> list[str]:
    L = ["# 样本外回放报告（同日配对基准）", "",
         f"- 回放区间: {day_list[0]} ~ {day_list[-1]}（{len(day_list)} 个选股日，抽样步长 {step}）",
         f"- 持有期: {hold} 个交易日；成交假设: **{('T+1 开盘买入' if entry == 'open' else '当日收盘买入')}"
         f"**，扣双边成本 {cost*100:.2f}%",
         f"- 基准: **同日全市场等权平均**（非ST、价格≥2元）", "",
         "| 场景 | 选股日 | 持仓 | 现实平均 | 市场平均 | **配对超额** | 超额胜率 | t 值 | p 值 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        rr = r["real"]
        t_s = f"{rr['t']:.2f}" if rr["t"] is not None else "N/A"
        p_s = f"{rr['p']:.3f}" if rr["p"] is not None else "N/A"
        L.append(f"| {r['label']} | {rr['n_days']} | {r['n_picks']} | {_pct(r.get('picks_real_mean'))} "
                 f"| {_pct(r.get('mkt_real_mean'))} | **{_pct(rr['excess_mean'])}** "
                 f"| {_pct0(rr['win_rate'], 0)} | {t_s} | {p_s} |")
    L += ["", "## 稳健性：剔除最好的几天之后还剩多少超额（现实口径）", "",
          "| 场景 | 全部 | 剔最好1天 | 剔最好2天 | 剔最好3天 |", "|---|---|---|---|---|"]
    for r in results:
        rr = r["real"]
        L.append(f"| {r['label']} | {_pct(rr['excess_mean'])} | {_pct(rr['trim1'])} "
                 f"| {_pct(rr['trim2'])} | {_pct(rr['trim3'])} |")
    L += ["", "## 理想口径对照（当日收盘买、不扣成本，仅供与历史数字比对）", "",
          "| 场景 | 配对超额 | 超额胜率 | t | p |", "|---|---|---|---|---|"]
    for r in results:
        ri = r["ideal"]
        t_s = f"{ri['t']:.2f}" if ri["t"] is not None else "N/A"
        p_s = f"{ri['p']:.3f}" if ri["p"] is not None else "N/A"
        L.append(f"| {r['label']} | {_pct(ri['excess_mean'])} | {_pct0(ri['win_rate'], 0)} "
                 f"| {t_s} | {p_s} |")
    for r in results:
        L += ["", f"## 逐日明细：{r['label']}", "",
              "| 日期 | 持仓 | 入选(现实) | 入选(理想) | 市场(现实) | 市场(理想) | 配对超额 | 入选胜率 | 市场胜率 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for d in r["per_day"]:
            if d.get("picks_real") is None:
                continue
            ex = (d["picks_real"] - d["mkt_real"]) if d.get("mkt_real") is not None else None
            L.append(f"| {d['date']} | {d['n']} | {_pct(d.get('picks_real'))} | {_pct(d.get('picks_ideal'))} "
                     f"| {_pct(d.get('mkt_real'))} | {_pct(d.get('mkt_ideal'))} | {_pct(ex)} "
                     f"| {_pct0(d.get('picks_hit'), 0)} | {_pct0(d.get('mkt_hit'), 0)} |")
    L += ["", "> 判读：配对超额为正、t 绝对值 > 2（约 p<0.05）、且剔除最好 2 天后仍为正，才算有可用信号。", ""]
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description="样本外回放验证器（同日配对基准 + 现实成交口径）")
    ap.add_argument("--days", type=int, default=250, help="回放最近 N 个交易日窗口")
    ap.add_argument("--step", type=int, default=None, help="每隔几个交易日取一个选股日（默认=持有期，即非重叠）")
    ap.add_argument("--offset", type=int, default=0, help="跳过最近 N 个交易日（做更早的样本）")
    ap.add_argument("--limit", type=int, default=None, help="只扫描前 N 只（联调加速）")
    ap.add_argument("--cost", type=float, default=0.0015, help="双边成本（佣金+印花税+滑点，默认0.15%%）")
    ap.add_argument("--entry", choices=["open", "close"], default="open",
                    help="成交口径：open=T+1开盘买入（现实，默认）；close=当日收盘买入（理想）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--weights", default=None, help="单场景权重覆盖 'pattern_sim=1,signal=0,trend=0'")
    ap.add_argument("--sweep", action="store_true", help="一键对比 4 组权重")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    setup_logger("validate", cfg_get(cfg, "paths.log"))
    hold = int(cfg_get(cfg, "learning.forward_days", 10))
    step = args.step if args.step else hold

    conn = S.open_db(db)
    dates = _trade_dates(conn)
    if len(dates) <= hold + 5:
        print(f"数据不足：可回放交易日仅 {len(dates)} 个")
        conn.close()
        return 2
    day_list = _pick_days(dates, args.days, step, args.offset, hold)
    if not day_list:
        print("没有可回放的选股日")
        conn.close()
        return 2

    if args.sweep:
        scenarios = SWEEP_SCENARIOS
    elif args.weights:
        w = {}
        for kv in args.weights.split(","):
            k, v = kv.split("=")
            w[k.strip()] = float(v.strip())
        scenarios = [("single", w)]
    else:
        scenarios = [("single", None)]

    results = []
    for label, w in scenarios:
        cfg2 = dict(cfg)
        if w is not None:
            cfg2["scoring"] = dict(cfg.get("scoring", {}))
            cfg2["scoring"]["weights"] = w
        picks, per_day = _run_replay(conn, cfg2, dates, day_list, args.limit, hold, args.cost, args.entry)
        s = _summarize(per_day, label)
        s["picks"], s["per_day"] = picks, per_day
        results.append(s)

    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    rp = os.path.join(out_dir, f"validate_{'sweep' if args.sweep else 'single'}_{stamp}.md")
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_main_report(results, day_list, hold, args.cost, args.entry, step)))

    print("\n=== 同日配对结果（现实口径：%s + 成本 %.2f%%）==="
          % ("T+1开盘" if args.entry == "open" else "当日收盘", args.cost * 100))
    print(f"{'场景':<14}{'选股日':>6}{'持仓':>6}{'入选':>9}{'市场':>9}{'配对超额':>10}{'胜率':>7}{'t':>7}{'p':>8}")
    for r in results:
        rr = r["real"]
        t_s = f"{rr['t']:.2f}" if rr["t"] is not None else "N/A"
        p_s = f"{rr['p']:.3f}" if rr["p"] is not None else "N/A"
        print(f"{r['label']:<14}{rr['n_days']:>6}{r['n_picks']:>6}"
              f"{_pct(r.get('picks_real_mean')):>9}{_pct(r.get('mkt_real_mean')):>9}"
              f"{_pct(rr['excess_mean']):>10}{_pct0(rr['win_rate'], 0):>7}"
              f"{t_s:>7}{p_s:>8}")
        print(f"{'  └ 剔除最好1/2/3天后的超额':<28}{_pct(rr['trim1'])} / {_pct(rr['trim2'])} / {_pct(rr['trim3'])}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()

    if all(r["n_picks"] == 0 for r in results):
        print("\n❌ 所有场景入选数都是 0：回测无效，请检查回放区间是否落在有行情的日期上")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
