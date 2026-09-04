"""Phase 2 验证器：样本外回放，评估选股信号是否显著优于随机基线。

用法:
    # 单场景（指定权重）
    python scripts/validate.py --days 20
    python scripts/validate.py --days 20 --weights "pattern_sim=1,signal=0,trend=0"
    # 一键对比 4 组：combined / 纯形态 / 纯信号 / 纯趋势，输出一份对比报告
    python scripts/validate.py --days 20 --sweep
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import random
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


def _trade_dates(conn) -> list[str]:
    rows = conn.execute("SELECT date FROM trade_cal ORDER BY date").fetchall()
    if rows:
        return [r[0] for r in rows]
    rows = conn.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date").fetchall()
    return [r[0] for r in rows]


def _run_replay(conn, cfg, dates: list[str], days: int, limit: int | None, offset: int = 0) -> list[dict]:
    usable = dates[: len(dates) - 10]
    if offset > 0:
        replay = usable[-days - offset: -offset] if days + offset <= len(usable) else usable[:-offset]
    else:
        replay = usable[-days:]
    logger = setup_logger("validate")
    logger.info("回放区间: %s ~ %s（%d 个交易日）", replay[0], replay[-1], len(replay))

    picks: list[dict] = []
    for i, d in enumerate(replay, 1):
        selected = compute_and_select(conn, cfg, d, limit=limit, persist=False)
        fwd_idx = dates.index(d) + 10
        target = dates[fwd_idx]
        for s in selected:
            c0 = S.close_on(conn, s["code"], d)
            c10 = S.close_on_or_before(conn, s["code"], target)
            fwd = (c10 / c0 - 1.0) if (c0 and c10 and c0 > 0) else None
            picks.append({"date": d, "code": s["code"], "score": s.get("score"),
                          "fwd10": fwd, "sig": s.get("sig_score")})
        if i % 5 == 0 or i == len(replay):
            logger.info("回放进度 %d/%d，累计入选 %d", i, len(replay), len(picks))
    return picks, replay


def _random_baseline(conn, dates: list[str], replay: list[str], n: int, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    pool_codes = [r[0] for r in conn.execute("SELECT code FROM stock_meta WHERE is_st=0").fetchall()]
    out: list[dict] = []
    for _ in range(n):
        d = rng.choice(replay)
        target = dates[dates.index(d) + 10]
        code = rng.choice(pool_codes)
        c0 = S.close_on(conn, code, d)
        c10 = S.close_on_or_before(conn, code, target)
        if c0 and c10 and c0 > 0:
            out.append({"fwd10": c10 / c0 - 1.0})
    return out


def _stats(fwds: list[float]) -> dict:
    if not fwds:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    hit = sum(1 for x in fwds if x > 0) / len(fwds)
    return {"n": len(fwds), "hit": round(hit, 4),
            "mean": round(sum(fwds) / len(fwds), 4),
            "median": round(sorted(fwds)[len(fwds) // 2], 4)}


def _per_day(picks: list[dict]) -> dict[str, list[float]]:
    per_day: dict[str, list[float]] = {}
    for p in picks:
        if p["fwd10"] is not None:
            per_day.setdefault(p["date"], []).append(p["fwd10"])
    return per_day


def _run_scenario(conn, cfg, dates, days, limit, label, weights, offset=0):
    cfg2 = dict(cfg)
    if weights is not None:
        cfg2["scoring"] = dict(cfg.get("scoring", {}))
        cfg2["scoring"]["weights"] = weights
    picks, replay = _run_replay(conn, cfg2, dates, days, limit, offset)
    fwds = [p["fwd10"] for p in picks if p["fwd10"] is not None]
    sel = _stats(fwds)
    base = _random_baseline(conn, dates, replay, sel["n"])
    base_stats = _stats([b["fwd10"] for b in base])
    return {
        "label": label, "weights": weights, "picks": picks, "replay": replay,
        "sel": sel, "base": base_stats,
        "excess_hit": (sel["hit"] - base_stats["hit"]) if sel["hit"] is not None and base_stats["hit"] is not None else None,
        "excess_mean": (sel["mean"] - base_stats["mean"]) if sel["mean"] is not None and base_stats["mean"] is not None else None,
    }


def _pct(x):
    return f"{x*100:.1f}%" if x is not None else "N/A"


def main() -> int:
    ap = argparse.ArgumentParser(description="样本外回放验证器（支持一键对比）")
    ap.add_argument("--days", type=int, default=40, help="回放最近 N 个交易日")
    ap.add_argument("--offset", type=int, default=0, help="回放更早一段：跳过最近 offset 个交易日")
    ap.add_argument("--limit", type=int, default=None, help="只扫描前 N 只（联调加速）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--weights", default=None, help="单场景权重覆盖 'pattern_sim=1,signal=0,trend=0'")
    ap.add_argument("--sweep", action="store_true", help="一键对比 combined/纯形态/纯信号/纯趋势")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("validate", cfg_get(cfg, "paths.log"))
    conn = S.open_db(db)

    dates = _trade_dates(conn)
    usable = dates[: len(dates) - 10]
    if len(usable) < args.days:
        logger.warning("可回放交易日仅 %d 个(<%d)，按实际执行", len(usable), args.days)
        args.days = len(usable)
    if args.days <= 0:
        logger.error("数据不足以回放（需未来10日已存在）")
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

    results = [_run_scenario(conn, cfg, dates, args.days, args.limit, label, w, args.offset)
               for label, w in scenarios]

    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    rp = os.path.join(out_dir, f"validate_{'sweep' if args.sweep else 'single'}_{stamp}.md")

    lines = ["# 回放验证对比报告", "",
             f"- 回放区间: {results[0]['replay'][0]} ~ {results[0]['replay'][-1]}（{args.days} 个交易日）", "",
             "| 场景 | 入选数 | 命中率 | 平均收益 | 基线命中 | 基线收益 | 超额命中 | 超额收益 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        s, b = r["sel"], r["base"]
        lines.append(f"| {r['label']} | {s['n']} | {_pct(s['hit'])} | {_pct(s['mean'])} "
                     f"| {_pct(b['hit'])} | {_pct(b['mean'])} "
                     f"| {_pct(r['excess_hit'])} | {_pct(r['excess_mean'])} |")
    lines += ["", "> 验收基线：命中率/平均收益显著高于随机基线（命中率高 ≥5pct）且稳定，才进入 Phase 3。", ""]

    for r in results:
        lines.append(f"## {r['label']}")
        per_day = _per_day(r["picks"])
        for d in sorted(per_day):
            v = per_day[d]
            lines.append(f"- {d}: n={len(v)} 命中{sum(x > 0 for x in v)/len(v)*100:.0f}% 平均{sum(v)/len(v)*100:+.1f}%")
        lines.append("")

    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    # 控制台打印对比表
    print("\n=== 对比结果 ===")
    print(f"{'场景':<14}{'入选':>5}{'命中率':>9}{'平均':>9}{'基线命中':>9}{'超额命中':>10}{'超额收益':>10}")
    for r in results:
        s = r["sel"]
        print(f"{r['label']:<14}{s['n']:>5}{_pct(s['hit']):>9}{_pct(s['mean']):>9}"
              f"{_pct(r['base']['hit']):>9}{_pct(r['excess_hit']):>10}{_pct(r['excess_mean']):>10}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
