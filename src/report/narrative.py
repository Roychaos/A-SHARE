"""形态文案：LLM(DeepSeek, JSON模式) 生成 + 纯模板兜底。

- 兜底文案不依赖网络/密钥，保证每日推送不因 LLM 中断；
- LLM 只做解释，不参与选股打分（与 dualalpha-lite 同设计）。
"""
from __future__ import annotations

import json
import logging

from src.config import env_secret

logger = logging.getLogger("screener.narrative")

_DISCLAIMER = "仅供研究参考，不构成任何投资建议。"


def _tpl_fwd(conn, code, anchor) -> float | None:
    if not code or not anchor:
        return None
    row = conn.execute(
        "SELECT fwd_ret_10d FROM template WHERE code=? AND anchor_date=?",
        (code, anchor)).fetchone()
    return row[0] if row else None


def fallback_text(conn, s: dict) -> str:
    """无 LLM 时的模板文案。"""
    fwd = _tpl_fwd(conn, s.get("best_tpl_code"), s.get("best_tpl_anchor"))
    tpl_desc = f"{s.get('best_tpl_code')}@{s.get('best_tpl_anchor')}" if s.get("best_tpl_code") else "历史赢家形态"
    fwd_desc = f"该形态启动后10日上涨 {fwd*100:.1f}%" if fwd is not None else "历史上同类形态多出现在上涨启动前"
    hits = "、".join(s.get("hits") or []) or "形态高度匹配"
    return (
        f"形态：近25根K线与 {tpl_desc} 启动前形态高度相似（形态分 {s.get('sim_score')}），{fwd_desc}。"
        f"信号：{hits}。"
    )


def llm_texts(conn, cfg, selected: list[dict]) -> dict[str, str]:
    """一次 LLM 调用为整批股票产出文案；失败返回 {}（调用方回退兜底）。"""
    llm_cfg = cfg.get("report", {}).get("llm", {})
    api_key = env_secret("DEEPSEEK_API_KEY")
    if not api_key or not llm_cfg.get("enable", True):
        return {}
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("未安装 openai，跳过 LLM 文案")
        return {}

    items = []
    for s in selected:
        fwd = _tpl_fwd(conn, s.get("best_tpl_code"), s.get("best_tpl_anchor"))
        items.append({
            "code": s["code"], "name": s.get("name", ""),
            "score": s.get("score"), "sim": s.get("sim_score"),
            "tpl": f"{s.get('best_tpl_code')}@{s.get('best_tpl_anchor')}" if s.get("best_tpl_code") else "",
            "tpl_fwd10": f"{fwd*100:.1f}%" if fwd is not None else "-",
            "hits": "、".join(s.get("hits") or []),
        })
    prompt = (
        "你是A股技术形态分析助手。根据给定的量价选股结果，为每只股票写一段不超过60字的中文形态分析，"
        "说明'为什么它像上涨启动前的形态'，给出关注点与风险点，语气客观、不承诺收益。"
        "只输出JSON，结构：{\"items\":[{\"code\":\"...\",\"summary\":\"...\"}]}。\n"
        f"选股结果：{json.dumps(items, ensure_ascii=False)}\n"
        f"结尾统一加上：{_DISCLAIMER}"
    )
    try:
        client = OpenAI(base_url=llm_cfg.get("base_url", "https://api.deepseek.com"), api_key=api_key)
        resp = client.chat.completions.create(
            model=llm_cfg.get("model", "deepseek-chat"),
            messages=[{"role": "user", "content": prompt}],
            temperature=float(llm_cfg.get("temperature", 0.3)),
            max_tokens=int(llm_cfg.get("max_tokens", 800)),
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return {it["code"]: it["summary"] for it in data.get("items", []) if it.get("code")}
    except Exception as exc:  # noqa: BLE001 任何失败都回退兜底
        logger.warning("LLM 文案失败，回退兜底: %s", exc)
        return {}


def build_narratives(conn, cfg, selected: list[dict]) -> list[dict]:
    """为 selected 逐条附加 narrative 文本（LLM 优先，失败/无key 回退兜底）。"""
    out = [dict(s) for s in selected]
    llm_map = llm_texts(conn, cfg, out)
    for s in out:
        s["narrative"] = llm_map.get(s["code"]) or fallback_text(conn, s)
    return out
