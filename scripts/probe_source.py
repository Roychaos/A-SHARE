"""源连通性探测：单只股票分别测 东财/新浪 两个源（各 2 次尝试），定位问题。

用法:  python scripts/probe_source.py [--codes 000025,600519]
（只发极少量请求，不会触发限流；请在单窗口执行）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data import fetcher as F  # noqa: E402
from src.utils.net import sanitize_proxy_env  # noqa: E402


def probe_one(code: str) -> None:
    print(f"\n=== 探测 {code} ===")
    for src, fn in (("eastmoney(东财)", F.fetch_history_em), ("sina(新浪)", F.fetch_history_sina)):
        for adj in ("qfq", ""):
            t0 = time.time()
            try:
                rows = F.retry_call(fn, code, "2025-08-01", "2026-09-03", adjust=adj,
                                    times=2, base_delay=1.0, backoff=2.0)
                dt_ = time.time() - t0
                if rows:
                    print(f"  [OK ] {src} adjust={adj!r:5} 行数={len(rows):5} "
                          f"范围={rows[0]['date']}~{rows[-1]['date']}  用时{dt_:.1f}s")
                else:
                    print(f"  [EMPTY] {src} adjust={adj!r} 返回空（可能该区间无数据）")
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] {src} adjust={adj!r}: {type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="数据源连通性探测")
    ap.add_argument("--codes", default="000025", help="逗号分隔的股票代码")
    args = ap.parse_args()
    sanitize_proxy_env()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    for code in codes:
        probe_one(code)
    print("\n探测结束。把以上输出完整贴回来即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
