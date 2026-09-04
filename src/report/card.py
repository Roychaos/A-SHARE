"""图文卡片合成（Pillow，惰性导入）：K线图 + 中文标题/文案 拼成一张 PNG。"""
from __future__ import annotations

import logging
import os

from src.report.fonts import resolve_font, wrap_cjk

logger = logging.getLogger("screener.card")

CARD_W, CARD_H = 900, 1500
CHART_W, CHART_H = 860, 620


def compose_card(cfg: dict, s: dict, chart_path: str, out_path: str) -> str | None:
    """s 需含 code/name/rank/score/sim_score/narrative。返回卡片路径。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("缺少 Pillow，请先: pip install -r requirements.txt")

    if not os.path.exists(chart_path):
        return None
    font_path = resolve_font(cfg)

    canvas = Image.new("RGB", (CARD_W, CARD_H), "white")
    draw = ImageDraw.Draw(canvas)

    def font(sz, bold=False):
        if font_path:
            try:
                return ImageFont.truetype(font_path, sz, index=0)
            except Exception:  # noqa: BLE001
                pass
        return ImageFont.load_default()

    f_title = font(34, bold=True)
    f_body = font(24)

    # 标题区
    name = s.get("name") or ""
    score = s.get("score")
    draw.text((30, 24), f"#{s.get('rank')}  {s['code']}  {name}", fill="#111111", font=f_title)
    draw.text((30, 78), f"形态分 {s.get('sim_score')} · 总分 {score}",
              fill="#333333", font=font(26))

    # K线图
    try:
        chart = Image.open(chart_path).convert("RGB")
        chart = chart.resize((CHART_W, CHART_H))
        canvas.paste(chart, (20, 130))
    except Exception as exc:  # noqa: BLE001
        logger.warning("图表贴入失败: %s", exc)

    # 文案区
    y = 130 + CHART_H + 20
    for line in wrap_cjk(s.get("narrative") or "", 30):
        draw.text((30, y), line, fill="#111111", font=f_body)
        y += 36
    # 免责声明
    disc = cfg.get("push", {}).get("disclaimer", "仅供研究参考，不构成任何投资建议。")
    draw.text((30, CARD_H - 40), disc, fill="#999999", font=font(20))

    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    canvas.save(out_path)
    return out_path
