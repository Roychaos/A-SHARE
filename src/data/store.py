"""SQLite 存储层：建表与所有读写操作（仅标准库 sqlite3，可离线测试）。

字段口径说明（与主设计文档 §5 一致）：
- daily_bar.volume   : 东财口径的成交量，单位「手」；
- daily_bar.amount   : 成交额，单位「元」；
- daily_bar.pct_chg  : 涨跌幅 %（NaN 已过滤，不落库）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Any, Iterable, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_meta(
  code TEXT PRIMARY KEY,
  name TEXT,
  board TEXT,
  is_st INTEGER DEFAULT 0,
  industry TEXT,
  list_date TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS trade_cal(date TEXT PRIMARY KEY);

CREATE TABLE IF NOT EXISTS daily_bar(
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, pct_chg REAL,
  PRIMARY KEY(code, date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bar_date ON daily_bar(date);

-- ★ 赢家模板库（Phase 1 使用；w_close/w_vol 为 JSON 数组文本）
CREATE TABLE IF NOT EXISTS template(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  anchor_date TEXT NOT NULL,
  fwd_ret_10d REAL,
  w_close TEXT,
  w_vol TEXT,
  feat TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tpl_anchor ON template(anchor_date);

-- 每日筛选结果（Phase 2 使用）
CREATE TABLE IF NOT EXISTS scan_result(
  date TEXT NOT NULL,
  code TEXT NOT NULL,
  rank INTEGER,
  score REAL,
  sim_score REAL,
  sig_score REAL,
  trend_score REAL,
  signals TEXT,
  matched_tpl_id INTEGER,
  reason TEXT,
  created_at TEXT,
  PRIMARY KEY(date, code)
);

CREATE TABLE IF NOT EXISTS run_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT,
  status TEXT,
  msg TEXT,
  ts TEXT
);

CREATE TABLE IF NOT EXISTS fetch_log(
  code TEXT PRIMARY KEY,
  last_ok_date TEXT,
  bars INTEGER DEFAULT 0,
  status TEXT,
  error TEXT,
  updated_at TEXT
);
"""


def open_db(path: str) -> sqlite3.Connection:
    """打开(必要时创建)数据库，返回连接。目录自动创建。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------- stock_meta ----------------

def upsert_stock_meta(conn, meta_rows: Iterable[dict]) -> int:
    """meta_rows: [{code,name,board,is_st}]，保留既有 industry/list_date。"""
    n = 0
    for m in meta_rows:
        conn.execute(
            """INSERT INTO stock_meta(code,name,board,is_st,updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, board=excluded.board, is_st=excluded.is_st,
                 updated_at=excluded.updated_at""",
            (m["code"], m.get("name"), m.get("board"), 1 if m.get("is_st") else 0, _now()),
        )
        n += 1
    conn.commit()
    return n


def _rows_as_dicts(conn, sql: str, args: tuple = ()) -> list[dict]:
    """按 cursor.description 将行映射为 dict（不依赖 row_factory 配置）。"""
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def list_stock_meta(conn, board: str | None = None) -> list[dict]:
    sql = "SELECT code,name,board,is_st,industry,list_date FROM stock_meta"
    args: tuple = ()
    if board:
        sql += " WHERE board = ?"
        args = (board,)
    return _rows_as_dicts(conn, sql, args)


# ---------------- trade_cal ----------------

def upsert_trade_cal_dates(conn, dates: Sequence[str]) -> int:
    conn.executemany("INSERT OR REPLACE INTO trade_cal(date) VALUES(?)", [(d,) for d in dates])
    conn.commit()
    return len(dates)


# ---------------- daily_bar ----------------

def upsert_daily_bars(conn, code: str, rows: Iterable[dict]) -> int:
    """rows: [{date,open,high,low,close,volume,amount,pct_chg}]；INSERT OR REPLACE 幂等。"""
    prepared = []
    for r in rows:
        prepared.append(
            (
                code,
                r["date"],
                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"), r.get("amount"), r.get("pct_chg"),
            )
        )
    if not prepared:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO daily_bar(code,date,open,high,low,close,volume,amount,pct_chg)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        prepared,
    )
    conn.commit()
    return len(prepared)


def latest_bar_date(conn, code: str) -> str | None:
    row = conn.execute("SELECT MAX(date) FROM daily_bar WHERE code=?", (code,)).fetchone()
    return row[0] if row and row[0] else None


def count_bars(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]


def list_codes(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT code FROM stock_meta ORDER BY code")]


def backfill_list_date(conn, code: str) -> str | None:
    """把该股最早一根K线的日期回写为 list_date 近似值（次新判定用，见主文档 §6.1）。"""
    row = conn.execute("SELECT MIN(date) FROM daily_bar WHERE code=?", (code,)).fetchone()
    if row and row[0]:
        conn.execute("UPDATE stock_meta SET list_date=? WHERE code=? AND list_date IS NULL", (row[0], code))
        conn.commit()
        return row[0]
    return None


# ---------------- template（Phase 1） ----------------

def replace_templates_since(conn, since_date: str, rows: Iterable[dict]) -> int:
    """删除 anchor_date>=since_date 的旧模板后整批插入（保证每日重跑幂等、无重复）。

    rows: [{code,anchor_date,fwd_ret_10d,w_close,w_vol,feat}]
    """
    conn.execute("DELETE FROM template WHERE anchor_date >= ?", (since_date,))
    prepared = [
        (r["code"], r["anchor_date"], r.get("fwd_ret_10d"),
         r.get("w_close"), r.get("w_vol"), r.get("feat"), _now())
        for r in rows
    ]
    if prepared:
        conn.executemany(
            """INSERT INTO template(code,anchor_date,fwd_ret_10d,w_close,w_vol,feat,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            prepared,
        )
    conn.commit()
    return len(prepared)


def count_templates(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM template").fetchone()[0]


# ---------------- scan_result / 收盘价查询 ----------------

def replace_scan_results(conn, day: str, rows: Iterable[dict]) -> int:
    """删除当日 scan_result 后整批写入（幂等）；返回写入条数。

    rows 各元素含: date/code/rank/score/sim_score/sig_score/trend_score/
                    hits(list)/matched_tpl_id/best_tpl_code/best_tpl_anchor
    """
    conn.execute("DELETE FROM scan_result WHERE date=?", (day,))
    prepared = []
    for r in rows:
        hits = r.get("hits")
        if isinstance(hits, list):
            hits = json.dumps(hits, ensure_ascii=False)
        ref = None
        if r.get("best_tpl_code"):
            ref = f"{r['best_tpl_code']}@{r.get('best_tpl_anchor')}"
        prepared.append(
            (r.get("date", day), r["code"], r.get("rank"), r.get("score"),
             r.get("sim_score"), r.get("sig_score"), r.get("trend_score"),
             hits, r.get("matched_tpl_id"), ref, _now())
        )
    if prepared:
        conn.executemany(
            """INSERT INTO scan_result(date,code,rank,score,sim_score,sig_score,trend_score,
                 signals,matched_tpl_id,reason,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            prepared,
        )
    conn.commit()
    return len(prepared)


def close_on_or_before(conn, code: str, day: str) -> float | None:
    """code 在 <=day 的最后一根K线收盘价（回测取未来收益用）。"""
    row = conn.execute(
        "SELECT close FROM daily_bar WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, day),
    ).fetchone()
    return row[0] if row else None


def close_on(conn, code: str, day: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM daily_bar WHERE code=? AND date=?", (code, day),
    ).fetchone()
    return row[0] if row else None


# ---------------- fetch_log / run_log ----------------

def set_fetch_log(conn, code: str, status: str, *, last_ok_date: str | None = None,
                  bars: int = 0, error: str | None = None) -> None:
    conn.execute(
        """INSERT INTO fetch_log(code,last_ok_date,bars,status,error,updated_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET
             last_ok_date=excluded.last_ok_date, bars=excluded.bars,
             status=excluded.status, error=excluded.error, updated_at=excluded.updated_at""",
        (code, last_ok_date, bars, status, error, _now()),
    )
    conn.commit()


def add_run_log(conn, day: str, status: str, msg: str) -> None:
    conn.execute("INSERT INTO run_log(date,status,msg,ts) VALUES(?,?,?,?)", (day, status, msg, _now()))
    conn.commit()


def load_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
