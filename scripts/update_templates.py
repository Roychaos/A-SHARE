"""Phase 1：更新赢家模板库（形态学习离线管线）。

扫描最近 lookback_days(默认250) 个交易日 → 找「起步型赢家」锚点
→ 提取启动前窗口模板 → 替换 template 表（幂等）→ 输出统计报告。

用法:
    python scripts/update_templates.py                # 按配置默认
    python scripts/update_templates.py --days 120     # 只看近120个交易日
    python scripts/update_templates.py --no-report    # 不写报告文件
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.data import store as S  # noqa: E402
from src.patterns.templates import _defaults, extract_templates  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def render_report(stats: dict, params: dict) -> str:
    lines = [
        "# 赢家模板库统计报告",
        "",
        f"- 生成时间: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- 学习窗口: {params['lookback_days']} 个交易日（锚点区间 {stats.get('anchor_start')} ~ {stats.get('anchor_end')}）",
        f"- 窗口 W={params['window']} 根 · 前瞻 H={params['forward_days']} 日 · 收益门槛 ≥ {params['min_fwd_return']*100:.0f}%",
        f"- 锚点定义: 当日涨幅≥{params['day_gain_min']*100:.0f}% · 前10日涨幅<{params['prior10_gain_max']*100:.0f}% · 前60日区间[{params['prior60_min']*100:.0f}%,{params['prior60_max']*100:.0f}%]",
        "",
        "## 结果",
        "",
        f"- 扫描股票数: {stats.get('codes_scanned', 0)}",
        f"- **起步型赢家锚点（模板）数: {stats.get('anchors_total', 0)}**",
        f"- 覆盖交易日: {stats.get('days_covered', 0)}",
        f"- 未来10日平均收益: {stats.get('fwd_mean', float('nan'))*100 if stats.get('fwd_mean') is not None else float('nan'):.1f}%",
        f"- 中位数: {stats.get('fwd_median', float('nan'))*100 if stats.get('fwd_median') is not None else float('nan'):.1f}%  · 最大: {stats.get('fwd_max', 0)*100:.1f}%",
        "",
        "> 说明: fwd_ret 仅用于离线统计与 Phase2 验证器，线上打分不使用未来数据。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="更新赢家模板库（形态学习）")
    ap.add_argument("--days", type=int, default=None, help="覆盖最近 N 个交易日（默认取配置）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--no-report", action="store_true", help="不写 markdown 报告文件")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = cfg_get(cfg, "paths.db", "data/screener.db")
    logger = setup_logger("update_templates", cfg_get(cfg, "paths.log"))
    logger.info("== 赢家模板学习开始 ==")

    conn = S.open_db(db)
    if args.days:
        lr = dict(cfg.get("learning", {}))
        lr["lookback_days"] = args.days
        cfg["learning"] = lr

    params = _defaults(cfg)
    result = extract_templates(conn, cfg)

    # 落库（以锚点区间起点为替换边界，幂等）
    since = result["stats"].get("anchor_start", "")
    n = S.replace_templates_since(conn, since, result["templates"])
    total = S.count_templates(conn)
    logger.info("落库 %d 条（模板表现有 %d 条）", n, total)

    if not args.no_report:
        out_dir = cfg_get(cfg, "paths.output", "output")
        os.makedirs(out_dir, exist_ok=True)
        report_path = os.path.join(out_dir, f"templates_report_{dt.date.today().isoformat()}.md")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(render_report(result["stats"], params))
        logger.info("报告已写入: %s", report_path)

    conn.close()
    if result["stats"]["anchors_total"] == 0:
        logger.warning("未提取到任何模板：请检查数据深度（建议 backfill 2 年）与锚点参数")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
