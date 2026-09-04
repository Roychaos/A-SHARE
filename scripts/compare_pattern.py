"""形态对比图：当前股票近25根形态 vs 它命中的"最像模板"，直观验证选股逻辑。

用法:
    python scripts/compare_pattern.py --code 300xxx              # 指定股票
    python scripts/compare_pattern.py --rank 1                   # 取当日Top1
    python scripts/compare_pattern.py --code 300xxx --date 2026-09-03
输出: output/compare_{code}_{date}.png（左: 价格zscore曲线对比; 右: 量比对比）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.patterns.similarity import pearson, template_sim  # noqa: E402
from src.patterns.templates import _rolling_mean, _zscore  # noqa: E402
from src.screen.scorer import compute_and_select  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def _setup_font(cfg):
    import matplotlib as mpl
    try:
        for p in cfg_get(cfg, "report.fonts.windows", []) + cfg_get(cfg, "report.fonts.linux", []):
            if os.path.exists(p):
                mpl.font_manager.fontManager.addfont(p)
    except Exception:  # noqa: BLE001
        pass
    mpl.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
    mpl.rcParams["axes.unicode_minus"] = False


def _current_window(conn, code, date, w):
    rows = [dict(zip(("date", "close", "volume"), r)) for r in conn.execute(
        "SELECT date,close,volume FROM daily_bar WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
        (code, date, w + 30))]
    rows = rows[::-1]  # 升序
    if len(rows) < w:
        return None
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    ma = _rolling_mean(vols, 20)
    zc = _zscore(closes[-w:])
    vr = [round(v / (ma[len(rows) - w + k] or 0.0), 4) if ma[len(rows) - w + k] else 0.0
          for k, v in enumerate(vols[-w:])]
    return {"zc": zc, "vr": vr}


def main() -> int:
    ap = argparse.ArgumentParser(description="形态对比图")
    ap.add_argument("--code", default=None, help="股票代码（缺省取当日 Top1）")
    ap.add_argument("--rank", type=int, default=None, help="取当日第 rank 名")
    ap.add_argument("--date", default=None, help="基准交易日，默认库内最后交易日")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("缺少 matplotlib，请先: pip install -r requirements.txt")
        return 2

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    setup_logger("compare", cfg_get(cfg, "paths.log"))
    conn = S.open_db(db)

    if args.date:
        date = args.date
    else:
        date = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]

    w = int(cfg_get(cfg, "learning.window", 25))
    sim_w = cfg.get("learning", {}).get("sim_weights", {})
    w_price = float(sim_w.get("price", 0.7))
    w_vol = float(sim_w.get("volume", 0.3))
    top_matches = int(cfg_get(cfg, "learning.top_matches", 3))

    # 定位目标股票与它的最像模板
    selected = compute_and_select(conn, cfg, date, persist=False)
    if args.rank:
        tgt = next((s for s in selected if s["rank"] == args.rank), None)
    elif args.code:
        tgt = next((s for s in selected if s["code"] == args.code), None)
    else:
        tgt = selected[0] if selected else None
    if tgt is None:
        print(f"{date} 当日选股结果中无该股票/排名，请确认它在 Top 榜内")
        conn.close()
        return 1

    code = tgt["code"]
    tpl_code, anchor = tgt.get("best_tpl_code"), tgt.get("best_tpl_anchor")
    if not tpl_code:
        print(f"{code} 无最像模板（形态分可能来自多条模板平均）")
        conn.close()
        return 1

    tpl = conn.execute(
        "SELECT w_close,w_vol,fwd_ret_10d FROM template WHERE code=? AND anchor_date=?",
        (tpl_code, anchor)).fetchone()
    if not tpl:
        print("模板记录未找到")
        conn.close()
        return 1
    tpl_close = json.loads(tpl[0])
    tpl_vol = json.loads(tpl[1]) if tpl[1] else []
    fwd = tpl[2]

    cur = _current_window(conn, code, date, w)
    if cur is None:
        print(f"{code} 历史数据不足 {w} 根")
        conn.close()
        return 1

    pr = pearson(cur["zc"], tpl_close)
    name = conn.execute("SELECT name FROM stock_meta WHERE code=?", (code,)).fetchone()

    _setup_font(cfg)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.6))
    fig.suptitle(
        f"{code} {name[0] if name else ''}  vs 最像模板 {tpl_code}@{anchor}"
        f"（形态分 {tgt.get('sim_score')} / 价格相关 {pr:.2f}）",
        fontsize=12, fontweight="bold")
    x = range(w)
    ax1.plot(x, cur["zc"], "o-", color="#1f77b4", lw=1.8, label=f"当前 {code}")
    ax1.plot(x, tpl_close, "s--", color="#d62728", lw=1.8, label=f"模板 {tpl_code}(启动前)")
    ax1.axhline(0, color="gray", lw=0.8)
    ax1.set_title("价格形态（zscore 归一化）")
    ax1.set_xlabel("交易日（0=最旧, %d=最新）" % (w - 1))
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)

    if tpl_vol:
        width = 0.38
        ax2.bar([i - width / 2 for i in x], cur["vr"], width, color="#1f77b4", alpha=0.85, label=f"当前 {code}")
        ax2.bar([i + width / 2 for i in x], tpl_vol, width, color="#d62728", alpha=0.85, label=f"模板 {tpl_code}")
    else:
        ax2.plot(x, cur["vr"], "o-", label=f"当前 {code}")
    ax2.axhline(1.0, color="gray", lw=0.8)
    ax2.set_title("量能形态（量/20日均量）")
    ax2.set_xlabel("交易日")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)
    if fwd is not None:
        ax2.text(0.02, 0.97, f"该模板启动后10日收益 {fwd*100:.1f}%", transform=ax2.transAxes,
                 fontsize=9, va="top", color="green")

    fig.tight_layout()
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"compare_{code}_{date}.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(f"已生成对比图: {out}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
