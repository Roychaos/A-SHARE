"""汇总流水线：scan_result -> 文案 -> 图表/卡片 -> markdown -> 推送。"""
from __future__ import annotations

import json
import logging
import os

from src.report import card as card_mod
from src.report import charts as charts_mod
from src.report import narrative as nar
from src.report.fonts import wrap_cjk  # noqa: F401  (预留)

logger = logging.getLogger("screener.pipeline")


def _load_selected(conn, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT date,code,rank,score,sim_score,sig_score,trend_score,signals,matched_tpl_id,reason "
        "FROM scan_result WHERE date=? ORDER BY rank", (date,)).fetchall()
    names = {r[0]: r[1] for r in conn.execute("SELECT code,name FROM stock_meta")}
    out = []
    for r in rows:
        d = dict(zip(("date", "code", "rank", "score", "sim_score", "sig_score",
                      "trend_score", "signals", "matched_tpl_id", "reason"), r))
        hits = json.loads(d["signals"]) if d["signals"] else []
        tpl_code = tpl_anchor = None
        if d["reason"] and "@" in d["reason"]:
            tpl_code, tpl_anchor = d["reason"].split("@", 1)
        out.append({
            "code": d["code"], "name": names.get(d["code"], ""),
            "rank": d["rank"], "score": d["score"], "sim_score": d["sim_score"],
            "sig_score": d["sig_score"], "trend_score": d["trend_score"],
            "hits": hits, "matched_tpl_id": d["matched_tpl_id"],
            "best_tpl_code": tpl_code, "best_tpl_anchor": tpl_anchor,
        })
    return out


def build_markdown(date: str, selected: list[dict]) -> str:
    disc = "仅供研究参考，不构成任何投资建议。"
    lines = [f"# A股量价选股 · {date}  Top{len(selected)}", ""]
    for s in selected:
        tpl = f"{s['best_tpl_code']}@{s['best_tpl_anchor']}" if s.get("best_tpl_code") else "—"
        lines.append(f"## #{s['rank']} {s['code']} {s['name']}")
        lines.append(f"- 形态分 {s['sim_score']} · 总分 {s['score']} · 最像模板 {tpl}")
        lines.append(f"- {s.get('narrative', '')}")
        lines.append("")
    lines.append(f"> {disc}")
    return "\n".join(lines)


def emit(conn, cfg: dict, date: str, *, push: bool = True, image_count: int | None = None) -> dict:
    """生成并推送某日选股报告。返回 {selected, cards, md, md_path}。"""
    selected = _load_selected(conn, date)
    if not selected:
        logger.warning("%s: scan_result 为空，请先运行选股(pick_top/run_daily)", date)
        return {"selected": [], "cards": [], "md": "", "md_path": None}

    selected = nar.build_narratives(conn, cfg, selected)

    out_dir = os.path.join(cfg.get("paths", {}).get("output", "output"), date)
    os.makedirs(out_dir, exist_ok=True)
    img_n = image_count if image_count is not None else int(cfg.get("push", {}).get("image_count", 3))
    img_n = min(img_n, len(selected))

    cards: list[str] = []
    for s in selected[:img_n]:
        chart_path = os.path.join(out_dir, f"{s['rank']}_{s['code']}_kline.png")
        card_path = os.path.join(out_dir, f"{s['rank']}_{s['code']}.png")
        try:
            charts_mod.make_kline(conn, cfg, s["code"], date, chart_path)
            card_mod.compose_card(cfg, s, chart_path, card_path)
            cards.append(card_path)
        except Exception as exc:  # noqa: BLE001 图形失败不阻断推送其余
            logger.warning("%s 图文生成失败: %s", s["code"], exc)

    md = build_markdown(date, selected)
    md_path = os.path.join(out_dir, "summary.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    if push and (cards or md):
        try:
            from src.push import notifier
            notifier.notify(cfg, cards, md, date)
        except Exception as exc:  # noqa: BLE001
            logger.error("推送失败: %s", exc)

    logger.info("%s: 报告完成，选股 %d 只，图文 %d 张，摘要 %s", date, len(selected), len(cards), md_path)
    return {"selected": selected, "cards": cards, "md": md, "md_path": md_path}
