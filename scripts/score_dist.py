"""分数分布体检：全市场形态分的横截面分布，用来判断「Top5」到底有多少信息量。

为什么需要它：
    选股逻辑是「取形态分最高的 5 只」。但如果全市场有几百只票的分数几乎并列，
    那 Top5 就等同于从这批并列者里随机抽 5 只 —— 换一批样本就会换一批票。
    实测（2026-09-17）：数据修正前/后的两次选股，5 只票**零重合**，
    而两次的分数都挤在 94.9~95.5 这个极窄区间内。

本脚本用 numpy 批量算全市场 pattern_score（价格 z 曲线 Pearson + 量比 L1 距离，
权重 70/30，取 Top-3 模板均值，与 src/patterns/similarity.py 口径一致），
然后打印分布、并列规模、以及"第5名与第N名差多少分"。

用法:
    python scripts/score_dist.py                          # 库内最新交易日
    python scripts/score_dist.py --date 2026-09-08
    python scripts/score_dist.py --date 2026-09-08 --top 20
    python scripts/score_dist.py --date 2026-09-08 --gates 0.1 0.2 0.5 1.0 2.0
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def load_templates(conn, window: int):
    rows = conn.execute(
        "SELECT id, code, anchor_date, fwd_ret_10d, w_close, w_vol FROM template").fetchall()
    tcodes, tanchors, tfwd, tclose, tvol = [], [], [], [], []
    for _tid, code, anchor, fwd, wc, wv in rows:
        try:
            c = [float(x) for x in json.loads(wc or "[]")]
            v = [float(x) for x in json.loads(wv or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(c) != window:
            continue
        if len(v) != window:
            v = [float("nan")] * window
        tcodes.append(code)
        tanchors.append(anchor)
        tfwd.append(fwd)
        tclose.append(c)
        tvol.append(v)
    return (tcodes, tanchors, tfwd,
            np.asarray(tclose, dtype=np.float64) if tclose else np.zeros((0, window)),
            np.asarray(tvol, dtype=np.float64) if tvol else np.zeros((0, window)))


def load_windows(conn, date: str, window: int, codes: list[str]):
    """取每只股票截至 date 的最近 window 根 K 线（收盘 z 曲线 + 量比）。"""
    out_codes, closes, vols = [], [], []
    for code in codes:
        rows = conn.execute(
            "SELECT date, close, volume FROM daily_bar WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT ?", (code, date, window + 20)).fetchall()
        if len(rows) < window:
            continue
        rows = rows[:window][::-1]                     # 升序
        cl = [r[1] for r in rows]
        vo = [r[2] or 0.0 for r in rows]
        if any(c is None or c <= 0 for c in cl):
            continue
        mc = sum(cl) / len(cl)
        sd = (sum((x - mc) ** 2 for x in cl) / len(cl)) ** 0.5
        if sd <= 0:
            continue
        base_vol = sum(vo) / len(vo)
        ratio = [(v / base_vol) if base_vol else 1.0 for v in vo]
        out_codes.append(code)
        closes.append([(x - mc) / sd for x in cl])
        vols.append(ratio)
    return (out_codes,
            np.asarray(closes, dtype=np.float64) if closes else np.zeros((0, window)),
            np.asarray(vols, dtype=np.float64) if vols else np.zeros((0, window)))


def score_all(Wc, Wv, Tc, Tv, *, w_price=0.7, w_vol=0.3, top_matches=3, chunk=400):
    """返回每只股票的 pattern_score（与 similarity.template_sim 口径一致）。"""
    n, m = Wc.shape[0], Tc.shape[0]
    scores = np.empty(n, dtype=np.float64)
    best_idx = np.empty(n, dtype=np.int64)
    tvol_ok = ~np.isnan(Tv).any(axis=1)
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        wc = Wc[s:e]                                   # (k, 25)
        corr = (wc @ Tc.T) / Wc.shape[1]               # 都是 z 曲线 → 点积/n = Pearson
        price = (corr + 1.0) / 2.0 * 100.0
        # 量比 L1 距离：逐维广播，避免生成 (k, m, 25) 的三维大数组
        diff = np.zeros_like(price)
        for d in range(Wv.shape[1]):
            diff += np.abs(Wv[s:e, d][:, None] - Tv[None, :, d])
        vol_sim = np.clip(1.0 - diff / Wv.shape[1] / 2.0, 0.0, 1.0) * 100.0
        vol_sim[:, ~tvol_ok] = 50.0                    # 模板量能缺失时与 similarity.py 一致
        combo = w_price * price + w_vol * vol_sim
        k = min(top_matches, m)
        part = np.partition(combo, -k, axis=1)[:, -k:]
        scores[s:e] = part.mean(axis=1)
        best_idx[s:e] = combo.argmax(axis=1)
    return scores, best_idx


def main() -> int:
    ap = argparse.ArgumentParser(description="全市场形态分分布体检")
    ap.add_argument("--date", default=None, help="评估日（默认库内最新交易日）")
    ap.add_argument("--top", type=int, default=20, help="打印前 N 名")
    ap.add_argument("--gates", nargs="*", type=float, default=[0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
                    help="并列规模门槛（分）")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    window = int(cfg_get(cfg, "learning.window", 25))
    sim_w = cfg_get(cfg, "learning.sim_weights", {"price": 0.7, "volume": 0.3})
    top_matches = int(cfg_get(cfg, "learning.top_matches", 3))
    logger = setup_logger("score_dist", cfg_get(cfg, "paths.log"))

    conn = S.open_db(cfg_get(cfg, "paths.db", "data/screener.db"))
    date = args.date or conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    codes = [r[0] for r in conn.execute("SELECT code FROM stock_meta")]

    tcodes, tanchors, tfwd, Tc, Tv = load_templates(conn, window)
    wcodes, Wc, Wv = load_windows(conn, date, window, codes)
    logger.info("评估日 %s：模板 %d 个，可比股票 %d 只", date, len(tcodes), len(wcodes))
    if not len(wcodes) or not len(tcodes):
        print("数据不足：模板或窗口为空")
        return 1

    scores, best_idx = score_all(Wc, Wv, Tc, Tv, w_price=sim_w.get("price", 0.7),
                                 w_vol=sim_w.get("volume", 0.3), top_matches=top_matches)
    order = np.argsort(-scores)
    top = scores[order[0]]

    print("")
    print("=== 全市场形态分分布（%s，%d 只）===" % (date, len(scores)))
    print("  最高 %.2f | 中位数 %.2f | 最低 %.2f | 标准差 %.3f"
          % (top, float(np.median(scores)), float(scores.min()), float(scores.std())))
    for q in (99.9, 99.5, 99, 95, 90, 50):
        print("  第 %5.1f 百分位: %.2f" % (q, float(np.percentile(scores, q))))
    print("")
    print("  「并列规模」——距离最高分不到 X 分的有多少只：")
    for g in args.gates:
        print("     ≤ %.2f 分: %6d 只" % (g, int((scores >= top - g).sum())))
    print("")
    print("  前 %d 名：" % args.top)
    for rank, i in enumerate(order[:args.top], 1):
        bi = best_idx[i]
        fwd = tfwd[bi]
        fwd_s = ("%.1f%%" % (fwd * 100)) if isinstance(fwd, (int, float)) else "-"
        print("    #%-3d %-7s %.2f   最像 %s@%s（后10日 %s）"
              % (rank, wcodes[i], scores[i], tcodes[bi], tanchors[bi], fwd_s))
    print("")
    gaps = ["#1→#2", "#2→#5", "#5→#6", "#5→#10", "#5→#20", "#5→#50"]
    idxs = [(0, 1), (1, 4), (4, 5), (4, 9), (4, 19), (4, 49)]
    parts = []
    for label, (a, b) in zip(gaps, idxs):
        if b < len(order):
            parts.append("%s %.3f" % (label, scores[order[a]] - scores[order[b]]))
    print("  相邻名次分差：" + " | ".join(parts))
    print("  离最高分 %.2f 的距离分布：≤0.1 分 %d 只 | ≤0.5 分 %d 只 | ≤1 分 %d 只 | ≤2 分 %d 只"
          % (top, int((scores >= top - 0.1).sum()), int((scores >= top - 0.5).sum()),
             int((scores >= top - 1.0).sum()), int((scores >= top - 2.0).sum())))
    min_score = float(cfg_get(cfg, "scoring.min_score", 60.0))
    n_pass = int((scores >= min_score).sum())
    print("  配置 min_score=%.1f 的实际过滤效果：%d/%d 只（%.1f%%）通过 —— %s"
          % (min_score, n_pass, len(scores), 100.0 * n_pass / len(scores),
             "几乎等于不过滤，建议按分位数重设" if n_pass > 0.5 * len(scores) else "有效"))
    print("")
    print("  判读标准：若 #5→#10 的分差远小于名次间波动，说明 Top5 与 Top20 无实质差别；")
    print("            若离最高分 1 分内就有几百只，说明排序顶端不可区分。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
