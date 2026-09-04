"""生成并推送某日选股图文报告（基于 scan_result，不重新选股、不联网拉数）。

用法:
    python scripts/emit_report.py                     # 最新一次选股结果
    python scripts/emit_report.py --date 2026-09-03
    python scripts/emit_report.py --no-push           # 只出图/摘要，不推送
"""
from __future__ import annotations

import argparse
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.report import pipeline  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/推送选股图文报告")
    ap.add_argument("--date", default=None, help="选股结果日期（默认最新）")
    ap.add_argument("--no-push", action="store_true", help="只生成不推送")
    ap.add_argument("--push-only", action="store_true", help="不重新生成，仅读取已有产物并按 GitHub图床 推送")
    ap.add_argument("--image-count", type=int, default=None, help="发图只数")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    setup_logger("emit_report", cfg_get(cfg, "paths.log"))

    if args.push_only:
        from glob import glob
        from src.push import imagehost, notifier

        if args.date:
            date = args.date
        else:
            conn = S.open_db(db)
            row = conn.execute("SELECT MAX(date) FROM scan_result").fetchone()
            date = row[0] if row and row[0] else None
            conn.close()
        if not date:
            print("scan_result 为空，请先运行 pick_top / run_daily")
            return 1
        out_dir = cfg_get(cfg, "paths.output", "output")
        d = os.path.join(out_dir, date)
        md_path = os.path.join(d, "summary.md")
        if not os.path.exists(md_path):
            print(f"未找到 {md_path}，请先执行 emit_report --no-push")
            return 1
        cards = sorted(p for p in glob(os.path.join(d, "*.png")) if not p.endswith("_kline.png"))
        md = open(md_path, encoding="utf-8").read()
        urls = imagehost.jsdelivr_urls(cards, cfg)
        notifier.notify(cfg, cards, md, date, image_urls=urls)
        print(f"\npush-only 完成：图文 {len(cards)} 张，直链 {len(urls)} 条")
        return 0

    conn = S.open_db(db)
    if args.date:
        date = args.date
    else:
        row = conn.execute("SELECT MAX(date) FROM scan_result").fetchone()
        date = row[0] if row and row[0] else None
        if not date:
            print("scan_result 为空，请先运行 python scripts/pick_top.py")
            conn.close()
            return 1

    res = pipeline.emit(conn, cfg, date, push=not args.no_push,
                        image_count=args.image_count)
    print(f"\n完成：{date} 选股 {len(res['selected'])} 只，图文 {len(res['cards'])} 张")
    if res["md_path"]:
        print(f"摘要: {res['md_path']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
