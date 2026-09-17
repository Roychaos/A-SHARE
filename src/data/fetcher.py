"""行情抓取：多源降级 + 限速 + 重试 + 连败熔断。

主源     : akshare 东财日线 (stock_zh_a_hist, 前复权)
降级源   : akshare 新浪日线 (stock_zh_a_daily, 前复权)
第三方依赖(akshare)在函数内惰性导入：离线环境 import 本模块不报错。

对单只股票的重试策略（配置 fetch.*）：
  retry_times     每(源,复权)组合的尝试次数
  base_delay      首次重试等待秒数（指数退避）
  sources         源顺序，如 [eastmoney, sina]
  early_stop_after 连续失败达到该数即熔断退出（多半是被源限流/封禁）
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import time
from typing import Callable, Iterable

from src.data import store as S
from src.utils.retry import retry_call

logger = logging.getLogger("screener.fetcher")

# 东财 stock_zh_a_hist 中文列 -> 内部英文列
_EM_COL_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_chg",
}


def _to_float(v):
    try:
        f = float(v)
        return None if f != f else f  # NaN -> None
    except (TypeError, ValueError):
        return None


def _rows_from_df(df, col_map: dict, date_col: str = "date") -> list[dict]:
    """通用 DataFrame -> 内部行列表（含 NaN 清洗、日期归一为 ISO 字符串）。"""
    rows: list[dict] = []
    if df is None or df.empty:
        return rows
    for _, r in df.iterrows():
        item: dict = {}
        for src, dst in col_map.items():
            if src in r.index:
                val = r[src]
                item[dst] = str(val)[:10] if dst == "date" else _to_float(val)
        if item.get(date_col):
            item["date"] = str(item[date_col])[:10]
            rows.append(item)
    return rows


# ---------------- 各数据源实现 ----------------

def fetch_history_em(code: str, start_date: str, end_date: str, adjust: str = "qfq") -> list[dict]:
    """东财日线。volume 单位：手；amount 单位：元。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 akshare，请先执行: pip install -r requirements.txt") from exc

    fmt = lambda d: d.replace("-", "")
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=fmt(start_date), end_date=fmt(end_date), adjust=adjust,
    )
    return _rows_from_df(df, _EM_COL_MAP)


def _sina_symbol(code: str) -> str:
    c = code[0]
    prefix = "sh" if c == "6" else ("bj" if c in ("4", "8", "9") else "sz")
    return f"{prefix}{code}"


def fetch_history_sina(code: str, start_date: str, end_date: str, adjust: str = "qfq") -> list[dict]:
    """新浪日线降级源。volume 单位：股（量比特征尺度无关，可混用）。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 akshare，请先执行: pip install -r requirements.txt") from exc

    fmt = lambda d: d.replace("-", "")
    df = ak.stock_zh_a_daily(
        symbol=_sina_symbol(code),
        start_date=fmt(start_date), end_date=fmt(end_date), adjust=adjust,
    )
    col_map = {k: k for k in ("date", "open", "high", "low", "close", "volume", "amount")}
    rows = _rows_from_df(df, col_map)
    for r in rows:  # 新浪无涨跌幅列，置空
        r.setdefault("pct_chg", None)
    return rows


_SOURCES: dict[str, Callable] = {
    "eastmoney": fetch_history_em,
    "sina": fetch_history_sina,
}


def fetch_history_safe(code: str, start_date: str, end_date: str,
                       times: int = 5, base_delay: float = 2.0,
                       sources: Iterable[str] = ("eastmoney", "sina"),
                       adjust_fallback: bool = True) -> list[dict]:
    """按源顺序+复权顺序带重试抓取；全部失败抛最后一次异常。

    adjust_fallback=True 时，每个源会尝试 (qfq, 不复权) 两个组合；
    这也意味着「单只股票彻底失败」的代价 = len(sources) * times * 2 次请求
    + 双份指数退避睡眠（times=5/base=2 时纯等待就有 60 秒）。
    只用一个源（如仅新浪）时建议置 false：连接被重置时再试"不复权"毫无意义，
    只会让失败成本翻倍、把整体运行从 1 小时拖成十几小时。
    """
    last_err: Exception | None = None
    em_tried = False  # 本只股票是否曾尝试过东财源（用于区分"主源即新浪"与"降级到新浪"）
    adjs = ("qfq", "") if adjust_fallback else ("qfq",)
    for src in sources:
        fn = _SOURCES.get(src)
        if fn is None:
            continue
        if src == "eastmoney":
            em_tried = True
        for adj in adjs:
            try:
                rows = retry_call(fn, code, start_date, end_date, adjust=adj,
                                  times=times, base_delay=base_delay, backoff=2.0)
                if rows:
                    if src == "sina" and em_tried:
                        logger.warning("%s: 东财源不可用，已用新浪源(adjust=%r)", code, adj)
                    elif src == "sina":
                        logger.debug("%s: 新浪源(adjust=%r)", code, adj)  # 新浪即主源时不刷屏
                    elif adj == "":
                        logger.warning("%s: 复权数据失败，已退回不复权", code)
                    return rows
            except Exception as exc:  # noqa: BLE001 逐源逐复权记录，全部失败后抛出
                last_err = exc
                logger.debug("%s: %s/%r 失败: %s", code, src, adj, exc)
    raise last_err  # type: ignore[misc]


# ---------------- 整市场快照（每日增量快路径） ----------------

def _spot_col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None


def fetch_bulk_snapshot(conn, date: str) -> int:
    """新浪全市场快照 1 次请求 → 把 date 当日 OHLCV 全部入库。

    返回写入的股票数；接口/列名不匹配等失败返回 0（调用方回退逐股拉取）。
    """
    try:
        import akshare as ak
    except ImportError:  # pragma: no cover
        logger.warning("快照: 缺少 akshare")
        return 0
    try:
        df = ak.stock_zh_a_spot()
    except Exception as exc:  # noqa: BLE001 便于诊断（云端/本地都可见）
        logger.warning("快照接口调用失败: %s: %s", type(exc).__name__, exc)
        return 0
    if df is None or getattr(df, "empty", True):
        logger.warning("快照接口返回空数据（新浪全市场接口不可用/被限流）")
        return 0
    logger.warning("快照返回 %d 行, 列名: %s", len(df), list(df.columns)[:12])
    c_code = _spot_col(df, "代码")
    c_open, c_high, c_low, c_close = _spot_col(df, "今开", "开盘"), \
        _spot_col(df, "最高"), _spot_col(df, "最低"), _spot_col(df, "最新价", "收盘")
    c_vol = _spot_col(df, "成交量", "成交数量")
    c_amt = _spot_col(df, "成交额")
    c_chg = _spot_col(df, "涨跌幅")
    if not all([c_code, c_open, c_high, c_low, c_close, c_vol]):
        logger.warning("快照列名不匹配(代码=%s 开=%s 高=%s 低=%s 收=%s 量=%s)，放弃快照",
                       c_code, c_open, c_high, c_low, c_close, c_vol)
        return 0

    filled = 0
    skipped = 0
    for _, r in df.iterrows():
        try:
            # 新浪快照的代码带交易所前缀（sh600600 / sz000001 / bj920000）→ 只保留6位数字
            digits = "".join(ch for ch in str(r[c_code]) if ch.isdigit())
            code = digits.zfill(6) if len(digits) <= 6 else digits[-6:]
            close = _to_float(r[c_close])
            if len(code) != 6 or not code.isdigit() or not close or close <= 0:
                skipped += 1
                continue
            row = {"date": date, "open": _to_float(r[c_open]), "high": _to_float(r[c_high]),
                   "low": _to_float(r[c_low]), "close": close,
                   "volume": _to_float(r[c_vol]), "amount": _to_float(r[c_amt]) if c_amt else None,
                   "pct_chg": _to_float(r[c_chg]) if c_chg else None}
            if row["open"] is None or row["high"] is None or row["low"] is None:
                skipped += 1
                continue
            S.upsert_daily_bars(conn, code, [row])
            filled += 1
        except Exception:  # noqa: BLE001 单行失败忽略
            skipped += 1
            continue
    if filled == 0:
        logger.warning("快照解析后入库 0 只（跳过 %d 行），例如首行代码=%r",
                       skipped, str(df.iloc[0][c_code]) if len(df) else "?")
    return filled


# ---------------- 股票池 ----------------

def ensure_universe(conn, boards: list[str] | None = None, quiet: bool = False) -> list[dict]:
    """拉股票列表 -> 分类建 meta（不覆盖 industry/list_date）。返回入库的 meta 列表。"""
    from src.data import universe as U

    raw = retry_call(U.fetch_stock_list, times=3, base_delay=2.0, backoff=2.0)
    meta = U.build_meta(raw)
    if boards:
        meta = U.filter_by_board(meta, boards)
    n = S.upsert_stock_meta(conn, meta)
    if not quiet:
        logger.info("股票池入库 %d 只（范围 %s）", n, boards or "全部")
    return meta


# ---------------- 批量增量更新 ----------------

def fetch_incremental_all(conn, cfg: dict, *, date: str | None = None,
                          codes: list[str] | None = None,
                          limit: int | None = None, sleep_s: float = 0.5,
                          fallback_start: str | None = None,
                          force_from: str | None = None,
                          quiet: bool = False) -> dict:
    """对目标代码逐只增量更新日线（从各自最新已存日期之后开始）。

    参数：
      date          目标交易日（ISO）；None=今天
      codes         目标代码；None=取 stock_meta 全部
      limit         只处理前 N 只（调试用）
      sleep_s       每只请求间隔（秒），带 ±50% 随机抖动防规律限流
      fallback_start 该股库中无任何数据时回拉的起点(ISO)；None=默认近370天
      force_from    强制所有股票都从该日期起重拉（ISO）。用于修复历史空洞
                    （scripts/fill_gap.py）：它会绕过「latest >= end 就跳过」的短路，
                    这样中间缺失的交易日才会被真正补上。
    其余节流参数读配置 fetch.*（retry_times/base_delay/sources/early_stop_after）。

    返回 {codes, bars, failed, stopped_early}
    """
    fetch_cfg = cfg.get("fetch", {})
    retry_times = int(fetch_cfg.get("retry_times", 5))
    base_delay = float(fetch_cfg.get("base_delay", 2.0))
    sources = tuple(fetch_cfg.get("sources", ["eastmoney", "sina"]))
    early_stop = int(fetch_cfg.get("early_stop_after", 30))
    adjust_fallback = bool(fetch_cfg.get("adjust_fallback", True))

    if codes is None:
        meta = S.list_stock_meta(conn)
        if not meta:
            raise RuntimeError("股票池为空，请先执行 scripts/backfill.py 或 ensure_universe")
        codes = [m["code"] for m in meta]
    if limit:
        codes = codes[:limit]

    start_default = fallback_start or (dt.date.today() - dt.timedelta(days=370)).isoformat()
    end = date or dt.date.today().isoformat()

    # ★快路径：整市场快照一次入库（把每日 5000 次请求 → 1 次），失败自动回退逐股
    bulk_filled = False
    after_close = dt.datetime.now().hour >= 15
    allow_intraday = bool(fetch_cfg.get("bulk_allow_intraday", False))
    if (not limit and codes and bool(fetch_cfg.get("bulk_snapshot", True))
            and end == dt.date.today().isoformat() and (after_close or allow_intraday)):
        try:
            filled = fetch_bulk_snapshot(conn, end)
            if filled:
                bulk_filled = True
                logger.info("整市场快照入库 %d 只/%d 根（跳过逐股拉取）", filled, filled)
        except Exception as exc:  # noqa: BLE001 快照失败不影响主流程
            logger.warning("整市场快照失败，回退逐股: %s", exc)

    stats = {"codes": 0, "bars": 0, "failed": [], "stopped_early": False}
    consecutive_fail = 0
    for i, code in enumerate(codes, 1):
        if consecutive_fail >= early_stop:
            stats["stopped_early"] = True
            logger.error(
                "连续 %d 只抓取失败，疑似被行情源限流/封禁。建议等待 10~30 分钟后重跑（自动续传）。",
                consecutive_fail,
            )
            break
        try:
            latest = S.latest_bar_date(conn, code)
            if latest and latest >= end and not force_from:
                continue  # 已覆盖，跳过
            start = force_from or (latest if latest else start_default)
            rows = fetch_history_safe(code, start, end,
                                      times=retry_times, base_delay=base_delay,
                                      sources=sources, adjust_fallback=adjust_fallback)
            n = S.upsert_daily_bars(conn, code, rows)
            S.set_fetch_log(conn, code, "ok", last_ok_date=end, bars=n)
            S.backfill_list_date(conn, code)
            stats["codes"] += 1
            stats["bars"] += n
            consecutive_fail = 0
            if not quiet and (i % 100 == 0 or i == len(codes)):
                logger.info("进度 %d/%d：已更新 %d 只/%d 根，失败 %d",
                            i, len(codes), stats["codes"], stats["bars"], len(stats["failed"]))
        except Exception as exc:  # noqa: BLE001 单只失败不中断，计数用于熔断
            S.set_fetch_log(conn, code, "error", error=str(exc)[:500])
            stats["failed"].append((code, exc))
            consecutive_fail += 1
            if not quiet:
                logger.warning("失败 %s(%d连败): %s", code, consecutive_fail, exc)
        # 限速：基础间隔 ± 50% 抖动；失败后再多等 2s
        pause = sleep_s * random.uniform(0.5, 1.5)
        if consecutive_fail:
            pause += 2.0
        time.sleep(pause)

    if not quiet:
        logger.info("增量更新完成：%d 只 / %d 根，失败 %d%s",
                    stats["codes"], stats["bars"], len(stats["failed"]),
                    "（熔断提前停止）" if stats["stopped_early"] else "")
    return stats
