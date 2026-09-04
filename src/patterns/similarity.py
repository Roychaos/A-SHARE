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
