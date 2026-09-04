"""文字版形态对比（无 matplotlib，纯标准库）：把"当前窗口 vs 最像模板"画成 ASCII。

用法:
    python scripts/ascii_compare.py --rank 1      # 取当日 Top1
    python scripts/ascii_compare.py --code 601021
输出: 两条价格形态曲线(ASCII) + 两条量能曲线 + 数值指标
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sqlite3  # noqa: E402


def _zscore(seq):
    n = len(seq)
    if n == 0:
        return []
    m = sum(seq) / n
    var = sum((x - m) ** 2 for x in seq) / n
    std = var ** 0.5
    return [0.0] * n if std == 0 else [(x - m) / std for x in seq]


def _rolling_mean(vals, w):
    out, acc = [], 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= w:
            acc -= vals[i - w]
        out.append(acc / w if i >= w - 1 else None)
    return out


def _pearson(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return None if va == 0 or vb == 0 else cov / (va * vb) ** 0.5


def spark(vals, width=50):
    """把序列映射到 0..1，渲染为 ASCII 高度条。"""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    levels = " .:-=+*#%@"
    out = []
    for v in vals:
        idx = int((v - lo) / rng * (len(levels) - 1))
        out.append(levels[min(idx, len(levels) - 1)])
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="文字版形态对比")
    ap.add_argument("--code", default=None, help="股票代码（缺省取当日 Top1）")
    ap.add_argument("--rank", type=int, default=None, help="取当日第 rank 名")
    ap.add_argument("--db", default="data/screener.db", help="数据库路径")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    date = conn.execute("SELECT MAX(date) FROM scan_result").fetchone()
    if not date:
        print("scan_result 为空，请先运行: python scripts/pick_top.py")
        conn.close()
        return 1
    date = date[0]

    if args.rank:
        row = conn.execute("SELECT * FROM scan_result WHERE date=? AND rank=?", (date, args.rank)).fetchone()
    elif args.code:
        row = conn.execute("SELECT * FROM scan_result WHERE date=? AND code=?", (date, args.code)).fetchone()
    else:
        row = conn.execute("SELECT * FROM scan_result WHERE date=? ORDER BY rank LIMIT 1", (date,)).fetchone()
    if not row:
        print(f"{date} 无该股票/排名")
        conn.close()
        return 1

    code = row["code"]
    ref = row["reason"] or ""
    tpl_code, anchor = ref.split("@") if "@" in ref else (None, None)
    if not tpl_code and row["matched_tpl_id"]:
        t = conn.execute("SELECT code,anchor_date FROM template WHERE id=?", (row["matched_tpl_id"],)).fetchone()
        tpl_code, anchor = t["code"], t["anchor_date"]

    name = conn.execute("SELECT name FROM stock_meta WHERE code=?", (code,)).fetchone()
    name = name["name"] if name else ""

    w = 25
    tpl = None
    if tpl_code:
        tpl = conn.execute("SELECT w_close,w_vol,fwd_ret_10d FROM template WHERE code=? AND anchor_date=?",
                           (tpl_code, anchor)).fetchone()
    if not tpl:
        print("未找到模板记录")
        conn.close()
        return 1
    tpl_close = json.loads(tpl["w_close"])
    tpl_vol = json.loads(tpl["w_vol"]) if tpl["w_vol"] else []

    rows = conn.execute(
        "SELECT date,close,volume FROM daily_bar WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
        (code, date, w + 30)).fetchall()
    rows = rows[::-1]
    if len(rows) < w:
        print(f"{code} 数据不足 {w} 根")
        conn.close()
        return 1
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    ma = _rolling_mean(vols, 20)
    cur_close = _zscore(closes[-w:])
    cur_vol = [round(v / (ma[len(rows) - w + k] or 0.0), 4) if ma[len(rows) - w + k] else 0.0
               for k, v in enumerate(vols[-w:])]

    pr = _pearson(cur_close, tpl_close)
    vol_diff = sum(abs(a - b) for a, b in zip(cur_vol, tpl_vol)) / max(len(tpl_vol), 1) if tpl_vol else None

    print(f"\n===== {date}  {code} {name}  最像模板 {tpl_code}@{anchor} =====")
    print(f"模板启动后10日收益: {tpl['fwd_ret_10d']*100 if tpl['fwd_ret_10d'] else 0:.1f}%")
    print(f"价格相关(Pearson): {pr:.3f}   |   量能平均偏差: {vol_diff:.3f}" if vol_diff is not None else f"价格相关: {pr:.3f}")
    print(f"\n价格形态 (zscore 曲线，最旧→最新 各{len(cur_close)}根):")
    print(f"  当前 {code:<8} {spark(cur_close)}")
    print(f"  模板 {tpl_code:<8} {spark(tpl_close)}")
    if tpl_vol:
        print(f"\n量能形态 (量/20日均量):")
        print(f"  当前 {code:<8} {spark(cur_vol)}")
        print(f"  模板 {tpl_code:<8} {spark(tpl_vol)}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
