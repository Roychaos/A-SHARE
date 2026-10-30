"""形态信号参数扫描：在不改生产代码的前提下，批量检验多种「形态度量 × 聚合方式」的 RankIC。

检验的变体（全部属于"形态相似度"这一族，不引入反转/低波动等外部因子）：

1) 价格相似度度量
     z    —— 25 根 K 线的 z-score 曲线做 Pearson（**当前生产口径**）
     ret  —— 先把窗口转成日收益率序列再算 Pearson（剥离绝对水平与末端尖峰的影响）
     rank —— 对 z 曲线做 Spearman（秩相关，抗极端值/尖峰）
2) 量能权重：0 / 0.3 / 0.5（当前固定 0.3，从未检验）
3) 聚合方式
     topk —— 最像的 k 个模板的**相似度均值**（当前口径，k=3）
     knn  —— 用相似度加权那些模板的**未来涨幅**（k=3/5/10，权重 = sim^p）
             即把"赢家模板库"当 k-NN 回归用：直接预测"这只票后面可能涨多少"。
             ★ 这一项是重点：模板库存的是赢家（未来 10 日涨幅 9%~80%），
               现在只用了"形状像不像"，把"赢家涨了多少"这个信息浪费了。

判读：与 ic_test.py 一致 —— 看平均 RankIC、IC_IR、t/t_adj、正 IC 占比、分位差。

用法:
    python scripts/pattern_scan.py --days 250 --step 5 --hold 20
    python scripts/pattern_scan.py --days 60 --step 10 --hold 20 --limit 800   # 联调
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.patterns.similarity import _std_rows  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402
from ic_test import _tstat, forward_returns, spearman  # noqa: E402

METRICS = ["z", "ret", "rank"]
VOL_WEIGHTS = [0.0, 0.3, 0.5]
KNN_KS = [3, 5, 10]


# ---------------- 数据准备 ----------------

def load_templates(conn, window: int):
    rows = conn.execute(
        "SELECT id, code, anchor_date, fwd_ret_10d, w_close, w_vol FROM template").fetchall()
    close, vol, fwd, ok = [], [], [], []
    for _tid, _code, _anchor, f, wc, wv in rows:
        try:
            c = [float(x) for x in json.loads(wc or "[]")]
            v = [float(x) for x in json.loads(wv or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(c) != window:
            continue
        close.append(c)
        vol.append(v if len(v) == window else [float("nan")] * window)
        fwd.append(float(f) if isinstance(f, (int, float)) else float("nan"))
        ok.append(True)
    return (np.asarray(close, dtype=np.float64) if close else np.zeros((0, window)),
            np.asarray(vol, dtype=np.float64) if vol else np.zeros((0, window)),
            np.asarray(fwd, dtype=np.float64) if fwd else np.zeros(0))


def stock_windows(conn, date: str, window: int, codes: list[str] | None = None):
    """取每只股票截至 date 的窗口：z 曲线 + 量比曲线（口径与生产 _vol_window 一致）。"""
    from_dt = (dt.date.fromisoformat(date) - dt.timedelta(days=int((window + 130) * 1.45))).isoformat()
    groups: dict[str, list[dict]] = {}
    for code, dd, o, h, lo, c, v in conn.execute(
            "SELECT code,date,open,high,low,close,volume FROM daily_bar "
            "WHERE date>=? AND date<=? ORDER BY code,date", (from_dt, date)):
        if c is None or c <= 0 or (codes is not None and code not in codes):
            continue
        groups.setdefault(code, []).append({"date": dd, "close": c, "volume": v or 0.0})

    out_codes, zc, vv = [], [], []
    for code, rows in groups.items():
        if rows[-1]["date"] != date or len(rows) < window + 20:
            continue
        cl = [r["close"] for r in rows[-window:]]
        m = sum(cl) / window
        sd = math.sqrt(sum((x - m) ** 2 for x in cl) / window)
        if sd <= 0:
            continue
        vols = [r["volume"] for r in rows]
        ma = [sum(vols[max(0, i - 19):i + 1]) / min(i + 1, 20) for i in range(len(vols))]
        vwin = [(vols[i] / ma[i]) if ma[i] else 0.0 for i in range(len(vols) - window, len(vols))]
        out_codes.append(code)
        zc.append([(x - m) / sd for x in cl])
        vv.append(vwin)
    return out_codes, np.asarray(zc, dtype=np.float64), np.asarray(vv, dtype=np.float64)


# ---------------- 变体计算 ----------------

def _metric_matrix(W: np.ndarray, metric: str) -> np.ndarray:
    """把窗口矩阵转换成"用于做点积相关"的形式（行已标准化）。"""
    if metric == "ret":
        R = np.diff(W, axis=1)                       # 日收益率序列
        return _std_rows(R)[0]
    if metric == "rank":
        # 行内秩标准化（Spearman = 秩的 Pearson）
        order = np.argsort(np.argsort(W, axis=1), axis=1).astype(np.float64)
        return _std_rows(order)[0]
    return _std_rows(W)[0]                           # "z"


def variants_for_day(conn, date: str, Tc, Tv, Tfwd, window: int, limit: int | None,
                     targets: dict[str, float]) -> dict[str, list[tuple[float, float]]]:
    """算当天所有变体的 (特征值, 未来收益) 对。"""
    codes, Wz, Wv = stock_windows(conn, date, window)
    if limit:
        codes, Wz, Wv = codes[:limit], Wz[:limit], Wv[:limit]
    if not len(codes) or not len(Tc):
        return {}

    tvol_ok = ~np.isnan(Tv).any(axis=1)
    Tf = np.nan_to_num(Tfwd, nan=0.0)
    have_fwd = ~np.isnan(Tfwd)

    feats: dict[str, list[tuple[float, float]]] = {}
    rets = np.array([targets.get(c, float("nan")) for c in codes], dtype=np.float64)
    keep = ~np.isnan(rets)
    if not keep.any():
        return {}
    Wz, Wv, codes, rets = Wz[keep], Wv[keep], [c for c, k in zip(codes, keep) if k], rets[keep]
    n = len(codes)

    # ★ 量能相似度与价格度量无关 → 只算一次，3 个度量复用（原来重复算了 3 遍）
    diff = np.zeros((n, len(Tc)), dtype=np.float64)
    for d in range(window):
        diff += np.abs(Wv[:, d][:, None] - Tv[None, :, d])
    vol_sim = np.clip(1.0 - diff / window / 2.0, 0.0, 1.0) * 100.0
    vol_sim[:, ~tvol_ok] = 50.0
    del diff

    for metric in METRICS:
        Wc = _metric_matrix(Wz, metric)
        Tc_std = _std_rows(Tc)[0] if metric == "z" else _metric_matrix(Tc, metric)
        corr = (Wc @ Tc_std.T) / Wc.shape[1]         # (n, m) Pearson
        price = (corr + 1.0) / 2.0 * 100.0
        for vw in VOL_WEIGHTS:
            combo = price if vw <= 0 else (1.0 - vw) * price + vw * vol_sim
            tag = f"{metric}_vol{int(vw*100)}"
            # 聚合 1：Top3 相似度均值（生产口径）
            k = min(3, combo.shape[1])
            top3 = np.partition(combo, -k, axis=1)[:, -k:].mean(axis=1)
            feats.setdefault(f"{tag}_top3", []).extend(zip(top3.tolist(), rets.tolist()))
            # 聚合 2：k-NN 加权赢家未来涨幅
            for kk in KNN_KS:
                kk2 = min(kk, combo.shape[1])
                idx = np.argpartition(combo, -kk2, axis=1)[:, -kk2:]      # (n, kk2)
                sims = np.take_along_axis(combo, idx, axis=1)
                fwds = Tf[idx]
                w = np.clip(sims, 0, None) ** 2                             # sim^2 加权
                valid = np.take_along_axis(have_fwd[None, :].repeat(n, 0), idx, axis=1)
                w = w * valid
                denom = w.sum(axis=1)
                with np.errstate(invalid="ignore", divide="ignore"):
                    pred = (w * fwds).sum(axis=1) / denom
                pred = np.where(denom > 0, pred, np.nan)
                m2 = ~np.isnan(pred)
                if m2.any():
                    feats.setdefault(f"{tag}_knn{kk}", []).extend(
                        zip(pred[m2].tolist(), rets[m2].tolist()))
    return feats


def main() -> int:
    ap = argparse.ArgumentParser(description="形态信号参数扫描（RankIC）")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--cost", type=float, default=0.0015)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    logger = setup_logger("pattern_scan", cfg_get(cfg, "paths.log"))
    window = int(cfg_get(cfg, "learning.window", 25))
    conn = S.open_db(cfg_get(cfg, "paths.db", "data/screener.db"))

    Tc, Tv, Tfwd = load_templates(conn, window)
    if not len(Tc):
        print("模板库为空（或长度与 window 不匹配）")
        return 2
    logger.info("模板 %d 个（赢家未来涨幅 中位 %.1f%%）",
                len(Tc), float(np.nanmedian(Tfwd)) * 100)

    cal = [r[0] for r in conn.execute("SELECT date FROM trade_cal ORDER BY date")]
    max_bar = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    cal = [d for d in cal if d <= max_bar]
    usable = cal[: len(cal) - args.hold - 1]
    sample = usable[-args.days:][::-1][::args.step][::-1]
    idx = {d: i for i, d in enumerate(cal)}

    ic_series: dict[str, list[float]] = {}
    q_pairs: dict[str, list[tuple[float, float]]] = {}
    for k, d in enumerate(sample, 1):
        i = idx[d]
        fwd = forward_returns(conn, cal[i + 1], cal[i + args.hold], args.cost)
        if len(fwd) < 100:
            continue
        feats = variants_for_day(conn, d, Tc, Tv, Tfwd, window, args.limit, fwd)
        for name, pairs in feats.items():
            if len(pairs) < 100:
                continue
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if ic is not None:
                ic_series.setdefault(name, []).append(ic)
            pairs.sort(key=lambda x: -x[0])
            k10 = max(5, len(pairs) // 10)
            top = [p[1] for p in pairs[:k10]]
            bot = [p[1] for p in pairs[-k10:]]
            q_pairs.setdefault(name, []).append((sum(top) / len(top), sum(bot) / len(bot)))
        if k % 5 == 0 or k == len(sample):
            logger.info("进度 %d/%d（%d 个变体）", k, len(sample), len(ic_series))

    if not ic_series:
        print("没有算出任何 IC")
        return 3

    overlap = max(1, int(round(args.hold / max(1, args.step))))
    rows = []
    for name, ics in ic_series.items():
        n = len(ics)
        mean = sum(ics) / n
        std = math.sqrt(sum((x - mean) ** 2 for x in ics) / (n - 1)) if n > 1 else 0.0
        t, p = _tstat(ics)
        spreads = [a - b for a, b in q_pairs.get(name, [])]
        rows.append({
            "name": name, "n": n, "ic": mean,
            "ir": (mean / std) if std > 0 else None,
            "t": t, "t_adj": (t / math.sqrt(overlap)) if (t is not None and overlap > 1) else t,
            "p": p, "pos": sum(1 for x in ics if x > 0) / n,
            "spread": (sum(spreads) / len(spreads)) if spreads else None,
        })
    rows.sort(key=lambda r: -(abs(r["t_adj"]) if r["t_adj"] is not None else 0))

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    rp = os.path.join(out_dir, f"pattern_scan_{stamp}.md")
    L = ["# 形态信号参数扫描（RankIC）", "",
         f"- 采样 {len(sample)} 个交易日（{sample[0]} ~ {sample[-1]}，每 {args.step} 日取 1 日）",
         f"- 持有 {args.hold} 个交易日；收益：次日开盘买 → 第 {args.hold} 日收盘卖，扣 {args.cost*100:.2f}%",
         f"- 模板 {len(Tc)} 个（赢家，未来10日涨幅中位 {float(np.nanmedian(Tfwd))*100:.1f}%）",
         f"- 观测重叠倍数 ≈ {overlap}（t_adj = t/√{overlap} 为保守值）", "",
         "## 各变体按 |t_adj| 排序", "",
         "| 变体 | 样本 | 平均RankIC | IC_IR | t | t_adj | 正IC | 前10%−后10% |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %d | %+.4f | %s | %s | %s | %.0f%% | %s |" % (
            r["name"], r["n"], r["ic"],
            f"{r['ir']:+.3f}" if r["ir"] is not None else "N/A",
            f"{r['t']:.2f}" if r["t"] is not None else "N/A",
            f"{r['t_adj']:.2f}" if r["t_adj"] is not None else "N/A",
            r["pos"] * 100,
            f"{r['spread']*100:+.2f}%" if r["spread"] is not None else "N/A"))
    L += ["", "## 命名说明", "",
          "- `z_vol30_top3`：**当前生产口径**（z 曲线 Pearson + 量能权重 0.3 + 取最像 3 个模板的相似度均值）",
          "- `*_ret_*`：用日收益率序列做相关", "- `*_rank_*`：用 Spearman 秩相关",
          "- `*_vol0/30/50`：量能权重 0%/30%/50%",
          "- `*_knnK`：用最像的 K 个模板的**未来涨幅**相似度加权 → 预测未来涨幅（k-NN 回归）", ""]
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    print("\n=== 形态变体扫描（按 |t_adj| 排序）===")
    print(f"{'变体':<22}{'样本':>5}{'平均IC':>9}{'IC_IR':>8}{'t':>7}{'t_adj':>7}{'正IC':>6}{'分位差':>9}")
    for r in rows[:24]:
        ir_s = f"{r['ir']:+.3f}" if r["ir"] is not None else "N/A"
        t_s = f"{r['t']:.2f}" if r["t"] is not None else "N/A"
        ta_s = f"{r['t_adj']:.2f}" if r["t_adj"] is not None else "N/A"
        sp_s = f"{r['spread']*100:+.2f}%" if r["spread"] is not None else "N/A"
        print(f"{r['name']:<22}{r['n']:>5}{r['ic']:>+9.4f}{ir_s:>8}{t_s:>7}{ta_s:>7}"
              f"{r['pos']*100:>5.0f}%{sp_s:>9}")
    print(f"\n完整报告已写入: {rp}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
