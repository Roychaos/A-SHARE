"""股票池与板块分类：纯函数可离线单测。"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("screener.universe")

# 板块代码前缀 -> 板块名（与 config.universe.boards 对应）
_PREFIX_BOARD = {
    "600": "SH主板", "601": "SH主板", "603": "SH主板", "605": "SH主板",
    "000": "SZ主板", "001": "SZ主板", "002": "SZ主板", "003": "SZ主板",
    "300": "创业板", "301": "创业板",
    "688": "科创板", "689": "科创板",
    # 北交所 / B股 等未列入即返回 None，默认不进入股票池
}


def board_of(code: str) -> str | None:
    """按前 3 位代码返回板块名；不在上述范围（北交所/B股等）返回 None。"""
    code = code.strip()
    if len(code) < 3 or not code.isdigit():
        return None
    return _PREFIX_BOARD.get(code[:3])


def is_st_by_name(name: str) -> bool:
    """名称含 ST/*ST/S*ST 视为风险警示股。"""
    return "ST" in (name or "").upper()


def filter_by_board(meta: Iterable[dict], boards: list[str] | None) -> list[dict]:
    """boards=None 表示不限板块。"""
    wanted = set(boards or [])
    out = []
    for m in meta:
        if not wanted or m.get("board") in wanted:
            out.append(m)
    return out


def fetch_stock_list() -> list[dict]:
    """经 akshare 拉取 A股 代码-名称 列表（惰性导入）。

    返回 [{code,name}]，code 为 6 位数字字符串。
    """
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 akshare，请先执行: pip install -r requirements.txt") from exc

    df = ak.stock_info_a_code_name()
    rows = []
    for _, r in df.iterrows():
        code = str(r["code"]).zfill(6)
        name = str(r["name"])
        rows.append({"code": code, "name": name})
    return rows


def build_meta(universe_list: list[dict]) -> list[dict]:
    """列表 -> 元数据行：板块分类 + ST 标记（纯函数）。"""
    out = []
    for u in universe_list:
        board = board_of(u["code"])
        if board is None:
            continue  # 北交所/B股/非标代码不进池
        out.append(
            {
                "code": u["code"],
                "name": u["name"],
                "board": board,
                "is_st": is_st_by_name(u["name"]),
            }
        )
    return out
