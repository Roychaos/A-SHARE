"""Phase 1：赢家模板库（形态学习核心，纯标准库实现，可离线单测）。

思路（与主设计文档 §6.2 一致）：
1. 定义「起步型赢家」锚点 —— 未来 H 日收益达标、且启动当天才刚开始放量上攻的股票；
2. 提取该股**启动前** W 根日K 的归一化窗口（价格形态 + 量能形态）+ 启动日统计特征；
3. 落库到 template 表，供 Phase 2 每日全市场相似度检索使用。

无未来函数：锚点标签(fwd_ret)只用于离线筛选与统计；模板只保存「启动前」窗口，
不包含未来信息。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from src.data import store as S
import math
from collections import defaultdict

logger = logging.getLogger("screener.patterns")


# ---------------- 纯函数基础工具 ----------------

def _zscore(seq: list[float]) -> list[float]:
    """标准化为 0 均值 1 方差；标准差为 0 时返回全 0。"""
    n = len(seq)
    if n == 0:
        return []
    mean = sum(seq) / n
    var = sum((x - mean) ** 2 for x in seq) / n
    std = math.sqrt(var)
    if std == 0 or std != std:  # noqa: PLR2004  std==NaN 防御
        return [0.0] * n
    return [(x - mean) / std for x in seq]


def _rolling_mean(values: list[float], w: int) -> list[float | None]:
    """返回每点(含当日)的 w 日均值；不足 w 根为 None。O(n)。"""
    out: list[float | None] = []
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= w:
            acc -= values[i - w]
        out.append(acc / w if i >= w - 1 else None)
    return out


def _rolling_max(values: list[float], w: int) -> list[float | None]:
    """返回每点(含当日)向前 w 根的最大值；不足为 None。"""
    out: list[float | None] = []
    from collections import deque

    dq: deque = deque()
    for i, v in enumerate(values):
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        out.append(values[dq[0]] if i >= w - 1 else None)
    return out


def _slope_pct(y: list[float]) -> float | None:
    """对序列做线性回归，返回斜率相对均值价格的百分比（尺度无关）。"""
    n = len(y)
    if n < 2:
        return None
    mean_y = sum(y) / n
    if mean_y == 0:
        return None
    mean_x = (n - 1) / 2.0
    sxy = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(y))
    sxx = sum((i - mean_x) ** 2 for i in range(n))
    if sxx == 0:
        return None
    return round(sxy / sxx / mean_y * 100.0, 4)  # 每根K线斜率百分比


# ---------------- 数据装载 ----------------

def load_bars(conn, date_from: str) -> dict[str, list[dict]]:
    """从 daily_bar 按 code 装载 date>=date_from 的 (date,close,volume,high)，按日期升序。"""
    cur = conn.execute(
        "SELECT code,date,close,volume,high FROM daily_bar WHERE date >= ? ORDER BY code,date",
        (date_from,),
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for code, d, close, volume, high in cur.fetchall():
        if close is None or close <= 0:
            continue
        grouped[code].append(
            {"date": d, "close": float(close), "volume": float(volume or 0.0), "high": float(high or close)}
        )
    return dict(grouped)


def load_st_codes(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT code FROM stock_meta WHERE is_st=1")}


# ---------------- 锚点与模板提取 ----------------

def _defaults(cfg: dict) -> dict:
    lr = cfg.get("learning", {})
    anchor = lr.get("anchor", {})
    rng = anchor.get("prior60_range", [-0.05, 0.25])
    return {
        "lookback_days": int(lr.get("lookback_days", 250)),
        "forward_days": int(lr.get("forward_days", 10)),
        "min_fwd_return": float(lr.get("min_fwd_return", 0.09)),
        "day_gain_min": float(anchor.get("day_gain_min", 0.02)),
        "prior10_gain_max": float(anchor.get("prior10_gain_max", 0.03)),
        "prior60_min": float(rng[0]),
        "prior60_max": float(rng[1]),
        "window": int(lr.get("window", 25)),
        "max_per_day": int(lr.get("max_templates_per_day", 20)),
    }


def anchor_date_bounds(conn, params: dict) -> tuple[str, str, str]:
    """根据本地交易日历计算：
    (load_from, anchor_start, anchor_end)
    - anchor 只取最近 lookback_days 个可标注交易日（需未来 forward_days 根已存在）；
    - load_from 额外前推 (window+60+10) 根，保证窗口/均线/平台高点计算有历史。
    日历缺失时按 1.45 倍粗估工作日。"""
    rows = conn.execute("SELECT date FROM trade_cal ORDER BY date").fetchall()
    L = params["lookback_days"] + params["forward_days"] + 10
    if len(rows) >= L:
        dates = [r[0] for r in rows]
        anchor_start = dates[-params["lookback_days"] - params["forward_days"]]
        anchor_end = dates[-1 - params["forward_days"]]
        buf = params["window"] + 70
        load_from = dates[max(0, len(dates) - params["lookback_days"] - params["forward_days"] - buf)]
        return load_from, anchor_start, anchor_end
    # 无日历/过短：用日期粗估
    today = dt.date.today()
    days = int((params["lookback_days"] + params["forward_days"]) * 1.45 + 30)
    start = (today - dt.timedelta(days=days)).isoformat()
    buf_days = int((params["window"] + 70) * 1.45 + 10)
    load_from = (dt.date.fromisoformat(start) - dt.timedelta(days=buf_days)).isoformat()
    return load_from, start, today.isoformat()


def extract_templates(conn, cfg: dict, *, quiet: bool = False) -> dict:
    """主流程：加载数据 -> 逐股扫描锚点 -> 提取模板 -> 返回统计与模板行。

    返回 {stats:{...}, templates:[...]}（模板行供落库）。纯数据不写库。
    """
    params = _defaults(cfg)
    load_from, anchor_start, anchor_end = anchor_date_bounds(conn, params)
    st_codes = load_st_codes(conn)
    bars_by_code = load_bars(conn, load_from)
    logger.info(
        "学习参数: 窗口W=%d 前瞻H=%d 门槛>=%.1f%% 锚点区间 %s ~ %s（load_from=%s）",
        params["window"], params["forward_days"], params["min_fwd_return"] * 100,
        anchor_start, anchor_end, load_from,
    )
    if not bars_by_code:
        raise RuntimeError("daily_bar 无数据，请先执行 scripts/backfill.py")

    W, H = params["window"], params["forward_days"]
    out_templates: list[dict] = []
    scanned = 0

    for code, bars in bars_by_code.items():
        if code in st_codes:
            continue
        n = len(bars)
        if n < W + H + 10:
            continue
        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]
        highs = [b["high"] for b in bars]
        dates = [b["date"] for b in bars]

        ma5 = _rolling_mean(closes, 5)
        ma10 = _rolling_mean(closes, 10)
        ma20 = _rolling_mean(closes, 20)
        vma20 = _rolling_mean(volumes, 20)
        high60 = _rolling_max(highs, 60)

        accepted: list[dict] = []  # 本股已接受的锚点索引（保证"第一个满足日"）
        last_anchor_idx = -10**6
        for i in range(W + 60, n - H - 1):  # 需要前 60 根历史做平台/区间计算
            d = dates[i]
            if d < anchor_start or d > anchor_end:
                continue
            # —— 无未来函数检查：i+H 必须落在已加载数据内（数据到“今天”）——
            # —— 各判据 ——
            fwd = closes[i + H] / closes[i] - 1.0
            if fwd < params["min_fwd_return"]:
                continue
            day_gain = closes[i] / closes[i - 1] - 1.0
            if day_gain < params["day_gain_min"]:
                continue
            prior10 = closes[i - 1] / closes[i - 10] - 1.0
            if prior10 >= params["prior10_gain_max"]:
                continue
            prior60 = closes[i] / closes[i - 60] - 1.0
            if not (params["prior60_min"] <= prior60 <= params["prior60_max"]):
                continue
            if i - last_anchor_idx < H:  # 同一轮上涨只取第一个满足日
                continue

            # —— 通过判据：提取模板 ——
            w_close = _zscore(closes[i - W: i])
            vma = vma20[i - W: i]
            w_vol = [round(v / m, 4) if m else 0.0 for v, m in zip(volumes[i - W: i], vma)]

            feat = {
                "slope20": _slope_pct(closes[i - 19: i + 1]),
                "vol_trend": round(
                    sum(volumes[i - 4: i + 1]) / max(sum(volumes[i - 19: i + 1]), 1e-9), 4
                ),
                "dist_high": round(closes[i] / high60[i], 4) if high60[i] else None,
                "ma_aligned": int((ma5[i] > ma10[i]) + (ma10[i] > ma20[i])) if ma5[i] and ma10[i] and ma20[i] else 0,
            }
            accepted.append(
                {
                    "code": code,
                    "anchor_date": d,
                    "fwd_ret_10d": round(fwd, 4),
                    "w_close": json.dumps([round(x, 4) for x in w_close], separators=(",", ":")),
                    "w_vol": json.dumps(w_vol, separators=(",", ":")),
                    "feat": json.dumps({k: v for k, v in feat.items() if v is not None}, separators=(",", ":")),
                    "_idx": i,
                }
            )
            last_anchor_idx = i
        out_templates.extend(accepted)
        scanned += 1

    # 全局按日去重限流：每日按 fwd_ret 降序保留 max_per_day 条
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in out_templates:
        by_day[t["anchor_date"]].append(t)
    capped: list[dict] = []
    for day, rows in by_day.items():
        rows.sort(key=lambda r: -r["fwd_ret_10d"])
        capped.extend(rows[: params["max_per_day"]])
    capped.sort(key=lambda r: (r["anchor_date"], r["code"]))
    for t in capped:
        t.pop("_idx", None)

    stats = {
        "codes_scanned": scanned,
        "anchors_total": len(capped),
        "window": W,
        "anchor_start": anchor_start,
        "anchor_end": anchor_end,
        "days_covered": len(by_day),
    }
    if capped:
        fwds = [t["fwd_ret_10d"] for t in capped]
        stats.update(
            {
                "fwd_mean": round(sum(fwds) / len(fwds), 4),
                "fwd_median": round(sorted(fwds)[len(fwds) // 2], 4),
                "fwd_max": max(fwds),
            }
        )
    if not quiet:
        logger.info(
            "扫描 %d 只 → 起步型赢家锚点 %d 条（覆盖 %d 个交易日，未来10日平均收益 %s）",
            scanned, len(capped), len(by_day),
            f"{stats.get('fwd_mean', float('nan'))*100:.1f}%" if capped else "-",
        )
    return {"stats": stats, "templates": capped}


def update_templates(conn, cfg: dict, *, quiet: bool = False) -> dict:
    """离线学习入口（供 run_daily / update_templates.py 复用）：
    提取模板 -> 按锚点区间整体替换 -> 返回统计。"""
    result = extract_templates(conn, cfg, quiet=quiet)
    since = result["stats"].get("anchor_start", "")
    n = S.replace_templates_since(conn, since, result["templates"])
    logger.info("模板落库 %d 条（库中现有 %d 条）", n, S.count_templates(conn))
    result["stats"]["written"] = n
    return result

