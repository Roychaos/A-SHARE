"""相似度检索：当前 W 根窗口 vs 赢家模板库（Phase 2 核心之一）。

度量（与主文档 §6.2/§6.3 一致）：
- 价格形态相似度 = Pearson(模板zscore窗口, 当前zscore窗口) 映射到 0~100；
- 量能形态相似度 = 1 - 平均绝对量比差/2（clip 0~1）-> 0~100；
- 综合 = w_price*价格 + w_vol*量能；取 Top-k 模板的综合均值作为 pattern_score。
纯 Python 实现可离线单测；全市场扫描可用 scorer 里的 numpy 批量路径。
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

logger = logging.getLogger("screener.similarity")


def pearson(a: list[float], b: list[float]) -> float | None:
    """皮尔逊相关系数；任一序列方差为 0 返回 None。"""
    n = len(a)
    if n != len(b) or n == 0:
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb) ** 0.5


def _parse_rows(rows: Iterable[dict]) -> list[dict]:
    out = []
    for r in rows:
        try:
            close_arr = [float(x) for x in json.loads(r.get("w_close") or "[]")]
            vol_arr = [float(x) for x in json.loads(r.get("w_vol") or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not close_arr:
            continue
        out.append(
            {
                "id": r.get("id"),
                "code": r.get("code"),
                "anchor_date": r.get("anchor_date"),
                "fwd_ret_10d": r.get("fwd_ret_10d"),
                "close_arr": close_arr,
                "vol_arr": vol_arr,
            }
        )
    return out


def _ensure_parsed(templates) -> list[dict]:
    """模板行既可能是原始DB行(w_close为JSON串)，也可能是已解析行(含close_arr)。"""
    if not templates:
        return []
    first = templates[0]
    if isinstance(first, dict) and "close_arr" in first:
        return list(templates)
    return _parse_rows(templates)


def _std_rows(a):
    """按行标准化（均值0/标准差1）；返回 (标准化矩阵, 有效行掩码)。

    方差为 0 的行（价格完全不动）在 Pearson 里是"无法比较"，掩码置 False，
    批量路径据此与逐只路径保持一致（返回 None 而不是 NaN）。
    """
    import numpy as np

    mu = a.mean(axis=1, keepdims=True)
    sd = a.std(axis=1, keepdims=True)
    ok = sd[:, 0] > 0
    out = np.zeros_like(a)
    if ok.any():
        out[ok] = (a[ok] - mu[ok]) / sd[ok]
    return out, ok


def template_sim_batch(w_close_list: list[list[float]], w_vol_list: list[list[float]],
                       templates, *, top_matches: int = 3,
                       w_price: float = 0.7, w_vol: float = 0.3,
                       chunk: int = 400) -> list[dict]:
    """template_sim 的 numpy 批量版：N 只股票 × M 个模板一次算完。

    公式与 template_sim **完全一致**：
        price_sim = (Pearson + 1) / 2 * 100
        vol_sim   = clip(1 - mean(|量比差|)/2, 0, 1) * 100   （模板无量能数据时 = 50）
        combo     = w_price*price_sim + w_vol*vol_sim
        pattern_score = Top-`top_matches` 个 combo 的均值（保留 2 位）
    为什么需要它：逐只调用时全市场 5200 只 × 约 2000 个模板 ≈ 1000 万次 Pearson，
    纯 Python 约 1 分钟/交易日 → 跑 250 日回测要 4 小时以上（撞 GitHub 6 小时上限）。
    批量矩阵乘后同样规模只需几秒。

    numpy 不可用时自动回退逐只调用（保证任何环境都能跑）。
    返回与 template_sim 同结构的 dict 列表，长度与输入一致。
    """
    n = len(w_close_list)
    empty = {"pattern_score": None, "best_tpl_id": None, "best_code": None,
             "best_anchor": None, "best_score": None}
    if n == 0:
        return []
    parsed = _ensure_parsed(templates)
    if not parsed:
        return [dict(empty) for _ in range(n)]

    try:
        import numpy as np
    except ImportError:  # pragma: no cover - 环境缺 numpy 时正确降级
        logger.info("未安装 numpy，回退逐只相似度计算（较慢）")
        return [template_sim(w_close_list[i], w_vol_list[i], parsed, top_matches=top_matches,
                             w_price=w_price, w_vol=w_vol) for i in range(n)]

    win = len(w_close_list[0])
    # 只保留与当前窗口等长的模板（与 pearson 的长度校验等价）
    kept = [t for t in parsed if len(t["close_arr"]) == win]
    if not kept:
        return [dict(empty) for _ in range(n)]

    Tc_raw = np.asarray([t["close_arr"] for t in kept], dtype=np.float64)
    # ★ 必须显式按行标准化后再做点积：Pearson 与标准化无关，但「点积/长度」只有在
    #   两侧都已经均值0、标准差1时才等于 Pearson。生产路径传的是 z 曲线（恰好成立），
    #   但那是巧合 —— 一旦有人传未标准化的窗口，结果会悄悄错掉（单测已抓到过一次）。
    Tc_z, t_ok = _std_rows(Tc_raw)
    if not t_ok.any():
        return [dict(empty) for _ in range(n)]
    kept = [t for t, ok in zip(kept, t_ok) if ok]
    Tc = Tc_z[t_ok]
    Tv = np.zeros((len(kept), win), dtype=np.float64)
    tvol_ok = np.zeros(len(kept), dtype=bool)
    for j, t in enumerate(kept):
        if len(t["vol_arr"]) == win:
            Tv[j] = t["vol_arr"]
            tvol_ok[j] = True

    Wc_raw = np.asarray(w_close_list, dtype=np.float64)
    Wc, w_ok = _std_rows(Wc_raw)
    Wv = np.zeros((n, win), dtype=np.float64)
    has_vol = np.zeros(n, dtype=bool)
    for i, v in enumerate(w_vol_list):
        if v and len(v) == win:
            Wv[i] = v
            has_vol[i] = True

    out: list[dict] = [dict(empty) for _ in range(n)]
    k = max(1, min(top_matches, len(kept)))
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        corr = (Wc[s:e] @ Tc.T) / win                    # 两侧已标准化 → 点积/长度 = Pearson
        price = (corr + 1.0) / 2.0 * 100.0
        diff = np.zeros_like(price)
        for d in range(win):                             # 逐维广播，避免 (k,m,win) 三维大数组
            diff += np.abs(Wv[s:e, d][:, None] - Tv[None, :, d])
        vol_sim = np.clip(1.0 - diff / win / 2.0, 0.0, 1.0) * 100.0
        vol_sim[:, ~tvol_ok] = 50.0                      # 模板无量能 → 与 template_sim 一致
        if not has_vol[s:e].all():                       # 当前窗口无量能 → 全部按 50
            vol_sim[~has_vol[s:e], :] = 50.0
        combo = w_price * price + w_vol * vol_sim
        combo = np.nan_to_num(combo, nan=-1e18)          # 理论上不会出现，保险起见
        kk = min(k, combo.shape[1])
        top_mean = np.partition(combo, -kk, axis=1)[:, -kk:].mean(axis=1)
        best_j = combo.argmax(axis=1)
        for row, (mean_v, bj) in enumerate(zip(top_mean, best_j)):
            if not bool(w_ok[s + row]):
                continue                                 # 该股窗口方差为 0 → 与逐只版一样返回 None
            t = kept[int(bj)]
            out[s + row] = {
                "pattern_score": round(float(mean_v), 2),
                "best_tpl_id": t.get("id"),
                "best_code": t.get("code"),
                "best_anchor": t.get("anchor_date"),
                "best_score": round(float(combo[row, int(bj)]), 2),
            }
    return out


def template_sim(w_close_now: list[float], w_vol_now: list[float],
                 templates, *, top_matches: int = 3,
                 w_price: float = 0.7, w_vol: float = 0.3) -> dict:
    """计算当前窗口与模板库的相似度。

    返回 {pattern_score, best_tpl_id, best_code, best_anchor, best_score}
    无可用模板或无可比窗口时 pattern_score=None。
    """
    parsed = _ensure_parsed(templates)
    scores: list[tuple[float, dict]] = []
    for t in parsed:
        ps = pearson(w_close_now, t["close_arr"])
        if ps is None:
            continue
        vol = w_vol_now if w_vol_now else []
        tv = t["vol_arr"]
        if vol and tv:
            d = sum(abs(a - b) for a, b in zip(vol, tv)) / max(len(vol), 1) / 2.0
            vol_sim = max(0.0, min(1.0, 1.0 - d)) * 100.0
        else:
            vol_sim = 50.0
        price_sim = (ps + 1.0) / 2.0 * 100.0
        combo = w_price * price_sim + w_vol * vol_sim
        scores.append((combo, t))
    if not scores:
        return {"pattern_score": None, "best_tpl_id": None,
                "best_code": None, "best_anchor": None, "best_score": None}
    scores.sort(key=lambda x: -x[0])
    top = scores[: max(1, top_matches)]
    best = top[0]
    return {
        "pattern_score": round(sum(s for s, _ in top) / len(top), 2),
        "best_tpl_id": best[1].get("id"),
        "best_code": best[1].get("code"),
        "best_anchor": best[1].get("anchor_date"),
        "best_score": round(best[0], 2),
    }
