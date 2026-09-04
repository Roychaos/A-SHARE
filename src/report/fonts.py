"""中文字体定位：按配置顺序返回第一个存在的字体文件路径。"""
from __future__ import annotations

import os


def resolve_font(cfg: dict) -> str | None:
    """依次尝试 report.fonts.windows / report.fonts.linux，返回存在的路径或 None。"""
    fonts_cfg = cfg.get("report", {}).get("fonts", {})
    for p in list(fonts_cfg.get("windows", [])) + list(fonts_cfg.get("linux", [])):
        if p and os.path.exists(p):
            return p
    return None


def wrap_cjk(text: str, width: int) -> list[str]:
    """按字符数硬换行（中文无空格），纯函数可离线测试。"""
    text = (text or "").replace("\n", " ")
    out, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out or [""]
