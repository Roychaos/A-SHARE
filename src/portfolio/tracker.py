"""组合持仓追踪：周度建仓批次 + 到期卖出（持有 hold 个交易日）。

设计（2026-11-13 定稿）：
  · 决策：每周五收盘后，从"分数分位 2%~10% 带"里选 5 只（含入场确认）
  · 建仓：T+1 开盘买入
  · 持有：hold 个交易日（默认 20 ≈ 4 周）→ 到期卖出
  · 稳态：5 只 × 4 周 = 20 只持仓，每只 5%
  · 推送：只在"建仓 / 到期卖出"发生时推（不再每天推候选池）

本模块负责：
  record_batch()  记录本周建仓批次（含到期日）
  settle_due()    结算已到期的持仓（用真实价格算持有期收益）
  snapshot()      返回 {buys, sells, open_n}，供报告与推送使用
"""
from __future__ import annotations

import logging

from src.data import store as S

logger = logging.getLogger("screener.tracker")


def _next_trade_day(conn, day: str) -> str | None:
    row = conn.execute("SELECT date FROM trade_cal WHERE date > ? ORDER BY date LIMIT 1",
                       (day,)).fetchone()
    return row[0] if row else None


def _add_trade_days(conn, day: str, n: int) -> str | None:
    rows = conn.execute(
        "SELECT date FROM trade_cal WHERE date > ? ORDER BY date LIMIT ?", (day, n)).fetchall()
    return rows[-1][0] if len(rows) >= n else None


def _price(conn, code: str, day: str, field: str = "close") -> float | None:
    row = conn.execute(f"SELECT {field} FROM daily_bar WHERE code=? AND date=?", (code, day)).fetchone()
    if not row or row[0] is None:
        row2 = conn.execute(
            f"SELECT {field} FROM daily_bar WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (code, day)).fetchone()
        return float(row2[0]) if row2 and row2[0] else None
    return float(row[0])


def record_batch(conn, decision_date: str, selected: list[dict], hold: int) -> dict:
    """把本周选出的票记录为一批持仓（幂等：同一天重复调用会覆盖）。"""
    if not selected:
        return {"batch_date": decision_date, "records": 0}
    buy = _next_trade_day(conn, decision_date)
    due = _add_trade_days(conn, buy, hold - 1) if buy else None
    rows = [{"batch_date": decision_date, "code": s["code"], "buy_date": buy,
             "due_date": due, "score": s.get("score"), "status": "open"}
            for s in selected]
    n = S.upsert_positions(conn, rows)
    logger.info("%s: 记录批次 %d 只（买入 %s，到期 %s）", decision_date, n, buy, due)
    return {"batch_date": decision_date, "records": n, "buy_date": buy, "due_date": due}


def settle_due(conn, today: str) -> list[dict]:
    """结算到期的持仓：用真实价格算持有期收益（买入开盘 → 到期收盘）。"""
    due = S.due_positions(conn, today)
    if not due:
        return []
    by_batch: dict[str, list[dict]] = {}
    for r in due:
        by_batch.setdefault(r["batch_date"], []).append(r)
    out: list[dict] = []
    for batch, items in by_batch.items():
        rets: dict[str, float] = {}
        codes = [it["code"] for it in items]
        for it in items:
            entry = _price(conn, it["code"], it["buy_date"], "open") if it["buy_date"] else None
            exit_ = _price(conn, it["code"], today, "close")
            ret = ((exit_ / entry - 1.0) if (entry and exit_ and entry > 0) else None)
            rets[it["code"]] = ret
            out.append({"batch_date": batch, "code": it["code"],
                        "buy_date": it["buy_date"], "due_date": it["due_date"], "ret": ret})
        S.close_positions(conn, batch, codes, today, rets)
        ok = [r for r in rets.values() if r is not None]
        logger.info("%s: 批次 %s 到期结算 %d 只，平均收益 %s",
                    today, batch, len(items),
                    f"{sum(ok)/len(ok)*100:+.2f}%" if ok else "N/A")
    return out


def snapshot(conn, today: str, buys: list[dict] | None = None) -> dict:
    """组合动作快照：本周建仓 + 本周卖出 + 当前持仓数。"""
    buys = buys or []
    return {"buys": buys, "sells": [], "open_n": S.open_position_count(conn)}


def enrich_names(conn, items: list[dict]) -> list[dict]:
    """补上股票名称（报告里好读）。"""
    if not items:
        return items
    codes = {it["code"] for it in items}
    ph = ",".join("?" * len(codes))
    names = {r[0]: r[1] for r in conn.execute(
        f"SELECT code, name FROM stock_meta WHERE code IN ({ph})", tuple(codes))}
    for it in items:
        it["name"] = names.get(it["code"], "")
    return items
