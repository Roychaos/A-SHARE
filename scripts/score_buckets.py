"""分数分档收益分析：检验"分数越高，未来收益真的越高吗（单调吗）"。

为什么必须做这个：我们只测过"前10% vs 后10%"的价差（+3.43%），但不知道
**最顶尖的 0.5% 是否真的比前 5% 更好**。如果顶尖那一小撮是噪声最大的地方
（实测过 Top5 组合反而不如 Top20），那"每周精选 2~3 只"就等于把资金投到噪声角落。

输出（按当日形态分数把全市场分档，看每档的 20 日超额收益）：

    top 0.5% | 0.5~1% | 1~2% | 2~5% | 5~10% | 10~20% | 20~50% | 后 50%

同时给出三种采样的对照（用于同时回答"决策频率"问题）：
    step=1  每天（点估计最精确，但观测重叠 20 倍）
    step=5  每周（观测重叠 4 倍 —— 与其它分析口径一致）
    step=20 每 4 周（非重叠，最严格）

用法:
    python scripts/score_buckets.py --days 200 --hold 20
    python scripts/score_buckets.py --days 200 --hold 20 --no-confirm   # 不启用入场确认
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.screen.scorer import score_universe  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402

EDGES = [0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]


def _labels(edges: list[float]) -> list[str]:
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b >= 100:
            out.append(f"后{100 - a:.0f}%")
        elif a <= 0:
            out.append(f"top {b:g}%")
        else:
            out.append(f"{a:g}~{b:g}%")
    return out


def _tstat(xs: list[float]) -> tuple[float | None, float | None]:
    n = len(xs)
    if n < 3:
        return None, None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None, None
    t = m / math.sqrt(var / n)
    return t, math.erfc(abs(t) / math.sqrt(2))


def forward_returns(conn, d1: str, d_hold: str, cost: float) -> dict[str, float]:
    rows = conn.execute(
        "SELECT b1.code, b1.close / b2.open - 1.0 FROM daily_bar b2 "
        "JOIN daily_bar b1 ON b1.code = b2.code AND b1.date = ? "
        "WHERE b2.date = ? AND b2.open > 0 AND b1.close > 0", (d_hold, d1)).fetchall()
    return {c: float(r) - cost for c, r in rows}


def bucket_stats(scored: list[dict], fwd: dict[str, float], edges: list[float],
                 only_confirmed: bool) -> list[dict]:
    """当日按分数分档，返回每档的 (平均超额, 胜率, 只数)。"""
    items = []
    for s in scored:
        if only_confirmed and not s.get("confirm_ok"):
            continue
        r = fwd.get(s["code"])
        sc = s.get("score")
        if r is None or sc is None:
            continue
        items.append((float(sc), r))
    if len(items) < 100:
        return []
    market = sum(r for _s, r in items) / len(items)     # 同日全市场（同一池子）均值
    items.sort(key=lambda x: -x[0])
    n = len(items)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        lo = int(round(n * a / 100.0))
        hi = int(round(n * b / 100.0))
        seg = items[lo:hi]
        if not seg:
            out.append({"n": 0, "excess": None, "hit": None})
            continue
        vals = [r for _s, r in seg]
        out.append({"n": len(vals),
                    "excess": sum(vals) / len(vals) - market,
                    "hit": sum(1 for v in vals if v > 0) / len(vals)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="分数分档收益分析")
    ap.add_argument("--days", type=int, default=200, help="回看最近 N 个交易日（每日都打分，较慢）")
    ap.add_argument("--hold", type=int, default=20, help="持有期（交易日）")
    ap.add_argument("--cost", type=float, default=0.0015)
    ap.add_argument("--no-confirm", action="store_true", help="不启用入场确认（对比用）")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.no_confirm:
        ec = dict((cfg.get("scoring") or {}).get("entry_confirm") or {})
        ec["enable"] = False
        cfg.setdefault("scoring", {})["entry_confirm"] = ec
    logger = setup_logger("score_buckets", cfg_get(cfg, "paths.log"))
    conn = S.open_db(cfg_get(cfg, "paths.db", "data/screener.db"))

    cal = [r[0] for r in conn.execute("SELECT date FROM trade_cal ORDER BY date")]
    max_bar = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    cal = [d for d in cal if d <= max_bar]
    if len(cal) < args.hold + 10:
        print("数据不足")
        return 2
    usable = cal[: len(cal) - args.hold - 1]
    days = usable[-args.days:]
    idx = {d: i for i, d in enumerate(cal)}
    labels = _labels(EDGES)
    logger.info("分档分析：%s ~ %s（%d 个交易日，持有 %d 日，入场确认=%s）",
                days[0], days[-1], len(days), args.hold,
                (cfg.get("scoring", {}).get("entry_confirm") or {}).get("enable"))

    # 逐日聚合（每天只存各档的统计，内存轻）
    per_day: list[dict] = []
    for k, d in enumerate(days, 1):
        i = idx[d]
        fwd = forward_returns(conn, cal[i + 1], cal[i + args.hold], args.cost)
        if len(fwd) < 100:
            continue
        scored = score_universe(conn, cfg, d, apply_confirm=False)
        if not scored:
            continue
        rows_all = bucket_stats(scored, fwd, EDGES, only_confirmed=False)
        rows_cf = bucket_stats(scored, fwd, EDGES, only_confirmed=True)
        if rows_all:
            per_day.append({"date": d, "all": rows_all, "cf": rows_cf})
        if k % 10 == 0 or k == len(days):
            logger.info("进度 %d/%d", k, len(days))

    if not per_day:
        print("没有可用样本")
        return 3

    def aggregate(sub: list[dict], key: str) -> list[dict]:
        out = []
        for bi, lab in enumerate(labels):
            ex = [r[key][bi]["excess"] for r in sub
                  if r.get(key) and bi < len(r[key]) and r[key][bi]["excess"] is not None]
            hit = [r[key][bi]["hit"] for r in sub
                   if r.get(key) and bi < len(r[key]) and r[key][bi]["hit"] is not None]
            ns = [r[key][bi]["n"] for r in sub
                  if r.get(key) and bi < len(r[key]) and r[key][bi]["n"]]
            t, p = _tstat(ex)
            out.append({"label": lab, "n_days": len(ex),
                        "excess": (sum(ex) / len(ex)) if ex else None,
                        "hit": (sum(hit) / len(hit)) if hit else None,
                        "avg_n": (sum(ns) / len(ns)) if ns else None,
                        "t": t, "p": p, "series": ex})
        return out

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    rp = os.path.join(out_dir, f"score_buckets_{stamp}.md")
    L = ["# 分数分档收益分析（分数越高，收益真的越高吗？）", "",
         f"- 区间 {per_day[0]['date']} ~ {per_day[-1]['date']}（{len(per_day)} 个交易日）",
         f"- 持有 {args.hold} 个交易日；收益：次日开盘买 → 第 {args.hold} 日收盘卖，扣 {args.cost*100:.2f}%",
         f"- 超额 = 该档平均收益 − 同日同池平均值",
         f"- 入场确认 = {(cfg.get('scoring', {}).get('entry_confirm') or {}).get('enable')}", ""]

    # 三种采样频率
    for step, tag in ((1, "每天（观测重叠 20 倍，点估计最精确）"),
                      (5, "每周（重叠 4 倍）"),
                      (20, "每 4 周（非重叠，最严格）")):
        sub = per_day[::-1][::step][::-1]
        if len(sub) < 3:
            continue
        overlap = max(1, int(round(args.hold / step)))
        agg = aggregate(sub, "all")
        L += [f"## 采样：{tag} —— {len(sub)} 个观测", "",
              "| 档位 | 每日只数 | 平均超额 | 中位数 | 胜率 | t | t_adj |",
              "|---|---|---|---|---|---|---|"]
        for r in agg:
            ser = sorted(r["series"])
            med = ser[len(ser) // 2] if ser else None
            ta = (r["t"] / math.sqrt(overlap)) if (r["t"] is not None and overlap > 1) else r["t"]
            L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                r["label"],
                f"{r['avg_n']:.0f}" if r["avg_n"] else "N/A",
                f"{r['excess']*100:+.2f}%" if r["excess"] is not None else "N/A",
                f"{med*100:+.2f}%" if med is not None else "N/A",
                f"{r['hit']*100:.0f}%" if r["hit"] is not None else "N/A",
                f"{r['t']:.2f}" if r["t"] is not None else "N/A",
                f"{ta:.2f}" if ta is not None else "N/A"))
        L += [""]

    # 入场确认后的分档（同口径 step=5）
    sub5 = per_day[::-1][::5][::-1]
    agg_cf = aggregate(sub5, "cf")
    L += ["## 入场确认后（只统计通过确认的票，step=5）", "",
          "| 档位 | 每日只数 | 平均超额 | 胜率 |", "|---|---|---|---|"]
    for r in agg_cf:
        L.append("| %s | %s | %s | %s |" % (
            r["label"],
            f"{r['avg_n']:.0f}" if r["avg_n"] else "0",
            f"{r['excess']*100:+.2f}%" if r["excess"] is not None else "N/A",
            f"{r['hit']*100:.0f}%" if r["hit"] is not None else "N/A"))
    L += ["", "## 判读", "",
          "- **顶尖档（top 0.5%）是否明显高于 2~5% 档？** 高 → 支持\"每周精选 2~3 只\"；",
          "  不高 → 精选等于把资金投到噪声角落，应改为\"过线即买、不额外集中\"",
          "- **单调性**：若 1~2% 档 > top 0.5% 档，说明最好的票不在最顶端",
          "- **非重叠采样（step=20）的 t_adj** 才是可信的显著性", ""]
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    for step, tag in ((1, "每天"), (5, "每周"), (20, "每4周")):
        sub = per_day[::-1][::step][::-1]
        if len(sub) < 3:
            continue
        agg = aggregate(sub, "all")
        overlap = max(1, int(round(args.hold / step)))
        print(f"\n=== 采样 {tag}（{len(sub)} 观测，重叠 {overlap} 倍）===")
        print(f"{'档位':<12}{'只数':>7}{'平均超额':>10}{'胜率':>7}{'t':>7}{'t_adj':>8}")
        for r in agg:
            ta = (r["t"] / math.sqrt(overlap)) if (r["t"] is not None and overlap > 1) else r["t"]
            ex_s = f"{r['excess']*100:+.2f}%" if r["excess"] is not None else "N/A"
            hit_s = f"{r['hit']*100:.0f}%" if r["hit"] is not None else "N/A"
            n_s = f"{r['avg_n']:.0f}" if r["avg_n"] else "N/A"
            t_s = f"{r['t']:.2f}" if r["t"] is not None else "N/A"
            ta_s = f"{ta:.2f}" if ta is not None else "N/A"
            print(f"{r['label']:<12}{n_s:>7}{ex_s:>10}{hit_s:>7}{t_s:>7}{ta_s:>8}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
