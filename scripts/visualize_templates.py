"""模板可视化：抽样历史赢家的"启动前形态"，标注其未来10日收益。

用法（需已安装 matplotlib，且已运行过 update_templates 生成模板库）:
    python scripts/visualize_templates.py --n 3            # 未来收益最高的3个赢家
    python scripts/visualize_templates.py --n 5 --random   # 随机抽5个
输出: output/template_sample_{code}_{anchor}.png
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.patterns.templates import _zscore  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def _rolling_mean(vals, w):
    out = []
    acc = 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= w:
            acc -= vals[i - w]
        out.append(acc / w if i >= w - 1 else None)
    return out


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


def main() -> int:
    ap = argparse.ArgumentParser(description="模板可视化")
    ap.add_argument("--n", type=int, default=3, help="抽样数量")
    ap.add_argument("--random", action="store_true", help="随机抽样（默认取未来收益最高）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("缺少 matplotlib，请先: pip install -r requirements.txt")
        return 2

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("visualize", cfg_get(cfg, "paths.log"))
    conn = S.open_db(db)

    rows = [dict(zip(("id", "code", "anchor_date", "fwd_ret_10d", "w_close", "w_vol"), r))
            for r in conn.execute(
                "SELECT id,code,anchor_date,fwd_ret_10d,w_close,w_vol FROM template ORDER BY fwd_ret_10d DESC")]
    if not rows:
        logger.warning("模板库为空：请先运行 python scripts/update_templates.py")
        conn.close()
        return 1

    if args.random:
        samples = random.sample(rows, min(args.n, len(rows)))
    else:
        samples = rows[: args.n]

    _setup_font(cfg)
    out_dir = cfg_get(cfg, "paths.output", "output")
    os.makedirs(out_dir, exist_ok=True)
    W = int(cfg_get(cfg, "learning.window", 25))
    before, after = 40, 10

    for s in samples:
        code, anchor = s["code"], s["anchor_date"]
        bars = [dict(zip(("date", "open", "high", "low", "close", "volume"), r))
                for r in conn.execute(
                    "SELECT date,open,high,low,close,volume FROM daily_bar WHERE code=? ORDER BY date", (code,))]
        idx = next((i for i, b in enumerate(bars) if b["date"] == anchor), None)
        if idx is None:
            continue
        lo, hi = max(0, idx - before), min(len(bars), idx + after + 1)
        win = bars[lo:hi]
        a = idx - lo  # 锚点在窗口中的位置
        closes = [b["close"] for b in win]
        vols = [b["volume"] for b in win]
        dates = [b["date"][5:] for b in win]  # MM-DD
        ma5 = _rolling_mean(closes, 5)
        ma20 = _rolling_mean(closes, 20)

        # 形态子图数据：锚点前 W 根的归一化价格 + 量比
        pre = bars[max(0, idx - W): idx]
        pre_close = [b["close"] for b in pre]
        pre_vol = [b["volume"] for b in pre]
        zc = _zscore(pre_close) if len(pre_close) == W else [0.0] * len(pre_close)
        vma = _rolling_mean(pre_vol, 20)
        vr = [v / (vma[k] or 0.0) if vma[k] else 0.0 for k, v in enumerate(pre_vol)]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.6))
        fig.suptitle(f"{code}  锚点 {anchor}   未来10日收益 {s['fwd_ret_10d']*100:.1f}%",
                     fontsize=13, fontweight="bold")

        ax1.plot(range(len(closes)), closes, color="#1f77b4", lw=1.4, label="close")
        ax1.plot(range(len(closes)), ma5, color="#ff7f0e", lw=1.0, label="MA5")
        ax1.plot(range(len(closes)), ma20, color="#2ca02c", lw=1.0, label="MA20")
        ax1.axvline(a, color="red", linestyle="--", lw=1.2)
        ax1.text(a, closes[a], " 锚点(启动日)", color="red", fontsize=9)
        ax1.set_title(f"启动前后走势（{-before}~+{after}日）")
        step = max(1, len(dates) // 6)
        ax1.set_xticks(range(0, len(dates), step))
        ax1.set_xticklabels([dates[i] for i in range(0, len(dates), step)], rotation=0, fontsize=8)
        ax1.legend(fontsize=8)
        ax1.grid(alpha=0.3)

        ax2.plot(range(len(zc)), zc, color="#9467bd", lw=1.6, label="价格(zscore)")
        ax2.bar(range(len(vr)), vr, color="#c5b0d5", alpha=0.7, label="量比(v/ma20)")
        ax2.axhline(0, color="gray", lw=0.8)
        ax2.set_title(f"启动前 {W} 根形态（学习到的模板）")
        ax2.set_xlabel("交易日(距锚点前)")
        ax2.legend(fontsize=8)
        ax2.grid(alpha=0.3)

        fig.tight_layout()
        out = os.path.join(out_dir, f"template_sample_{code}_{anchor}.png")
        fig.savefig(out, dpi=110)
        plt.close(fig)
        logger.info("已生成 %s", out)

    conn.close()
    print(f"完成：抽样 {len(samples)} 只，图片输出到 {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
