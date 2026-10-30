"""逐因子 RankIC 检验：用全市场横截面判断「哪个特征真的能预测未来收益」。

为什么先做这个：现在的打分是"25根K线形状相似度"拍脑袋加权，从未验证过任何
单一特征本身有没有预测力。IC 检验是量化研究里筛特征的标准第一步 ——
它用**全市场每一天的全部股票**（而不是只盯选出来的 5 只），统计功效高得多。

方法：
    对每个采样日 d：
      1) 全市场打分 → 每只票的特征值（形态相似度 + 各因子分 + 若干原始技术特征）
      2) 每只票的未来收益：d+1 开盘买 → 第 hold 个交易日收盘卖（可扣成本）
      3) 对「特征值 vs 未来收益」算 Spearman 秩相关 = RankIC
    再对 IC 序列统计：均值、标准差、IC_IR = 均值/标准差、t = IC_IR·√n、正 IC 占比。
    另做「分位组合」：按特征排序取前 10% / 后 10%，看收益差（多空价差）。

判读经验值：
    |IC_IR| > 0.3 且 t > 2  → 该特征有稳定预测力
    |IC_IR| < 0.1           → 基本是噪声，应从打分体系里删掉
    注意：若 step < hold，观测窗口重叠 → t 值会偏大，报告里给出重叠调整后的 t_adj。

用法:
    python scripts/ic_test.py --days 250 --step 5 --hold 20
    python scripts/ic_test.py --days 250 --step 5 --hold 20 --cost 0.0015 --quantiles
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


# ---------------- 统计工具（纯标准库，无需 scipy） ----------------

def _ranks(xs: list[float]) -> list[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


def spearman(a: list[float], b: list[float]) -> float | None:
    """Spearman 秩相关 = 秩的 Pearson 相关。"""
    if len(a) != len(b) or len(a) < 3:
        return None
    return _pearson(_ranks(a), _ranks(b))


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


# ---------------- 原始技术特征（直接从日线算，成本极低） ----------------

def raw_features(rows: list[dict], date: str) -> dict[str, float]:
    """从最近若干根K线算一组原始特征（与 7 因子规格中的量价概念对应）。"""
    if len(rows) < 25:
        return {}
    cl = [r["close"] for r in rows]
    vo = [r["volume"] for r in rows]
    hi = [r["high"] for r in rows]
    lo = [r["low"] for r in rows]
    op = [r["open"] for r in rows]
    out: dict[str, float] = {}
    for n, key in ((5, "mom5"), (20, "mom20"), (60, "mom60")):
        if len(cl) > n:
            out[key] = cl[-1] / cl[-1 - n] - 1.0
    ma20 = sum(cl[-20:]) / 20
    if ma20:
        out["dist_ma20"] = cl[-1] / ma20 - 1.0
    vma20 = sum(vo[-20:]) / 20
    if vma20:
        out["vol_ratio"] = vo[-1] / vma20
    out["amplitude"] = (hi[-1] - lo[-1]) / cl[-1] if cl[-1] else 0.0
    out["ma5_over_ma20"] = (sum(cl[-5:]) / 5) / ma20 - 1.0 if ma20 else 0.0
    rets = [cl[i] / cl[i - 1] - 1.0 for i in range(len(cl) - 20, len(cl)) if cl[i - 1]]
    if len(rets) > 2:
        m = sum(rets) / len(rets)
        out["vol20"] = math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets))
    if len(op) and cl[-2]:
        out["gap"] = op[-1] / cl[-2] - 1.0
    if len(cl) >= 61:
        win = cl[-60:]
        out["pos_in_60d"] = (cl[-1] - min(win)) / (max(win) - min(win)) if max(win) > min(win) else 0.5
    return out


# ---------------- 主流程 ----------------

def forward_returns(conn, d1: str, d_hold: str, cost: float) -> dict[str, float]:
    """d1 开盘买入 → d_hold 收盘卖出，扣双边成本。"""
    rows = conn.execute(
        "SELECT b1.code, b1.close / b2.open - 1.0 FROM daily_bar b2 "
        "JOIN daily_bar b1 ON b1.code = b2.code AND b1.date = ? "
        "WHERE b2.date = ? AND b2.open > 0 AND b1.close > 0", (d_hold, d1)).fetchall()
    return {c: float(r) - cost for c, r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="逐因子 RankIC 检验（全市场横截面）")
    ap.add_argument("--days", type=int, default=250, help="回看最近 N 个交易日")
    ap.add_argument("--step", type=int, default=5, help="每隔几个交易日取一个采样日（5≈每周）")
    ap.add_argument("--hold", type=int, default=20, help="持有期（交易日）")
    ap.add_argument("--cost", type=float, default=0.0015, help="双边成本（默认0.15%%）")
    ap.add_argument("--limit", type=int, default=None, help="只扫描前 N 只（联调）")
    ap.add_argument("--quantiles", action="store_true", help="额外输出前10%%/后10%%分位收益差")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    logger = setup_logger("ic_test", cfg_get(cfg, "paths.log"))
    conn = S.open_db(cfg_get(cfg, "paths.db", "data/screener.db"))

    cal = [r[0] for r in conn.execute("SELECT date FROM trade_cal ORDER BY date")]
    max_bar = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    cal = [d for d in cal if d <= max_bar]
    if len(cal) < args.hold + 40:
        print("数据不足")
        return 2
    usable = cal[: len(cal) - args.hold - 1]
    sample = usable[-args.days:][::-1][::args.step][::-1]
    logger.info("采样 %d 个交易日（%s ~ %s），持有 %d 日，成本 %.2f%%",
                len(sample), sample[0], sample[-1], args.hold, args.cost * 100)

    idx = {d: i for i, d in enumerate(cal)}
    bars_cache: dict[str, list[dict]] = {}
    ic_series: dict[str, list[float]] = {}
    q_records: dict[str, list[tuple[float, float]]] = {}
    per_day_n: list[int] = []

    for k, d in enumerate(sample, 1):
        i = idx[d]
        d1, dh = cal[i + 1], cal[i + args.hold]
        scored = score_universe(conn, cfg, d, limit=args.limit)
        if not scored:
            continue
        fwd = forward_returns(conn, d1, dh, args.cost)
        if len(fwd) < 100:            # 前瞻窗口还没数据（库尾），跳过，避免白算
            logger.info("跳过 %s：未来收益不足（%d 只）", d, len(fwd))
            continue

        # 原始特征：取每只票的最近 120 根K线
        from_dt = (dt.date.fromisoformat(d) - dt.timedelta(days=260)).isoformat()
        groups: dict[str, list[dict]] = {}
        for code, dd, o, h, lo, c, v in conn.execute(
                "SELECT code,date,open,high,low,close,volume FROM daily_bar "
                "WHERE date>=? AND date<=? ORDER BY code,date", (from_dt, d)):
            groups.setdefault(code, []).append({"date": dd, "open": o, "high": h, "low": lo,
                                                "close": c, "volume": v})

        feats: dict[str, list[tuple[float, float]]] = {}
        for s in scored:
            code = s["code"]
            r = fwd.get(code)
            if r is None:
                continue
            vals: dict[str, float] = {"composite": s["score"]}
            if s.get("sim_score") is not None:
                vals["pattern_sim"] = s["sim_score"]
            for key, v in (s.get("factors") or {}).items():
                if v is not None:
                    vals[f"factor_{key}"] = float(v)
            rowsl = groups.get(code) or []
            if rowsl and rowsl[-1]["date"] == d:
                vals.update(raw_features(rowsl, d))
            for name, v in vals.items():
                try:
                    if v is None or v != v:      # 跳过 None / NaN
                        continue
                except TypeError:
                    continue
                feats.setdefault(name, []).append((float(v), r))

        for name, pairs in feats.items():
            if len(pairs) < 100:
                continue
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if ic is not None:
                ic_series.setdefault(name, []).append(ic)
            if args.quantiles:
                pairs.sort(key=lambda x: -x[0])
                k10 = max(5, len(pairs) // 10)
                top = [p[1] for p in pairs[:k10]]
                bot = [p[1] for p in pairs[-k10:]]
                q_records.setdefault(name, []).append(
                    (sum(top) / len(top), sum(bot) / len(bot)))
        per_day_n.append(len(fwd))
        if k % 5 == 0 or k == len(sample):
            logger.info("进度 %d/%d（本日 %d 只有未来收益）", k, len(sample), len(fwd))

    if not ic_series:
        print("没有算出任何 IC，检查数据")
        conn.close()
        return 3

    overlap = max(1, int(round(args.hold / max(1, args.step))))
    rows_out = []
    for name, ics in ic_series.items():
        n = len(ics)
        mean = sum(ics) / n
        std = math.sqrt(sum((x - mean) ** 2 for x in ics) / (n - 1)) if n > 1 else 0.0
        ir = (mean / std) if std > 0 else None
        t, p = _tstat(ics)
        t_adj = (t / math.sqrt(overlap)) if (t is not None and overlap > 1) else t
        pos = sum(1 for x in ics if x > 0) / n
        spread = None
        if name in q_records:
            spreads = [a - b for a, b in q_records[name]]
            spread = sum(spreads) / len(spreads)
        rows_out.append({"name": name, "n": n, "ic": mean, "ir": ir, "t": t, "t_adj": t_adj,
                         "p": p, "pos": pos, "spread": spread})

    rows_out.sort(key=lambda r: -(abs(r["t"]) if r["t"] is not None else 0))

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    rp = os.path.join(out_dir, f"ic_test_{stamp}.md")
    L = ["# 逐因子 RankIC 检验", "",
         f"- 采样 {len(sample)} 个交易日（{sample[0]} ~ {sample[-1]}，每 {args.step} 日取 1 日）",
         f"- 持有期 {args.hold} 个交易日；收益口径：次日开盘买入 → 第 {args.hold} 日收盘卖出，"
         f"扣双边成本 {args.cost*100:.2f}%",
         f"- 每日平均参与股票数 ≈ {sum(per_day_n)//max(1,len(per_day_n))}",
         f"- 观测重叠倍数 ≈ {overlap}（因此给出 t_adj = t / √{overlap} 作为保守值）", "",
         "| 特征 | 样本数 | 平均RankIC | IC_IR | t | t_adj | 正IC占比 | 前10%−后10%收益差 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rows_out:
        L.append("| %s | %d | %s | %s | %s | %s | %s | %s |" % (
            r["name"], r["n"],
            f"{r['ic']:+.4f}", f"{r['ir']:+.3f}" if r["ir"] is not None else "N/A",
            f"{r['t']:.2f}" if r["t"] is not None else "N/A",
            f"{r['t_adj']:.2f}" if r["t_adj"] is not None else "N/A",
            f"{r['pos']*100:.0f}%",
            f"{r['spread']*100:+.2f}%" if r["spread"] is not None else "N/A"))
    L += ["", "## 判读标准", "",
          "- **|IC_IR| > 0.3 且 \\|t_adj\\| > 2** → 有稳定预测力，值得纳入打分",
          "- **|IC_IR| < 0.1** → 基本是噪声，应从打分体系里删除",
          "- 前10%−后10% 收益差 > 0 说明方向正确（特征大的一方未来收益高）", ""]
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    print("\n=== RankIC 检验结果（按 |t| 排序）===")
    print(f"{'特征':<24}{'样本':>5}{'平均IC':>9}{'IC_IR':>8}{'t':>7}{'t_adj':>7}{'正IC':>6}{'分位差':>9}")
    for r in rows_out:
        ir_s = f"{r['ir']:+.3f}" if r["ir"] is not None else "N/A"
        t_s = f"{r['t']:.2f}" if r["t"] is not None else "N/A"
        ta_s = f"{r['t_adj']:.2f}" if r["t_adj"] is not None else "N/A"
        sp_s = f"{r['spread']*100:+.2f}%" if r["spread"] is not None else "N/A"
        print(f"{r['name']:<24}{r['n']:>5}{r['ic']:>+9.4f}{ir_s:>8}{t_s:>7}{ta_s:>7}"
              f"{r['pos']*100:>5.0f}%{sp_s:>9}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
