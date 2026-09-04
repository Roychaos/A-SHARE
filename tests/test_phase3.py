"""Phase 3 离线测试：文案兜底/汇总/推送payload/scan_result 重建（纯标准库）。"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data import store as S  # noqa: E402
from src.push import notifier  # noqa: E402
from src.push import wecom  # noqa: E402
from src.report import narrative as nar  # noqa: E402
from src.report import pipeline  # noqa: E402
from src.report.fonts import wrap_cjk  # noqa: E402

PASS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"[FAIL] {name} {detail}")
    PASS.append(name)
    print(f"  ok - {name}")


def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    S.init_db(conn)
    return conn


def test_wrap_and_payload():
    check("wrap_cjk", wrap_cjk("abcdefgh", 3) == ["abc", "def", "gh"])
    p = wecom.markdown_payload("hi")
    check("markdown payload", p["msgtype"] == "markdown")
    long = "x" * 3000
    t = wecom.text_payload(long)
    check("text truncated", len(t["text"]["content"]) == 2048)
    img = wecom.image_payload("media_id_1")
    check("image payload", img["image"]["media_id"] == "media_id_1")


def test_fallback_and_markdown():
    conn = mem_conn()
    S.upsert_stock_meta(conn, [{"code": "600519", "name": "贵州茅台", "board": "SH主板", "is_st": False}])
    S.replace_templates_since(conn, "2020-01-01", [{
        "code": "000858", "anchor_date": "2026-01-05", "fwd_ret_10d": 0.21,
        "w_close": "[1,2]", "w_vol": "[3,4]", "feat": "{}"}])
    s = {"code": "600519", "name": "贵州茅台", "rank": 1, "score": 88.0, "sim_score": 91.0,
         "hits": ["F4股价异动=90"], "best_tpl_code": "000858", "best_tpl_anchor": "2026-01-05"}
    text = nar.fallback_text(conn, s)
    check("fallback contains tpl", "000858" in text and "21.0%" in text)
    md = pipeline.build_markdown("2026-09-03", [dict(s, narrative="测试文案")])
    check("markdown builder", "600519" in md and "Top1" in md)
    conn.close()


def test_load_selected():
    conn = mem_conn()
    S.upsert_stock_meta(conn, [{"code": "600519", "name": "贵州茅台", "board": "SH主板", "is_st": False}])
    S.replace_scan_results(conn, "2026-09-03", [{
        "date": "2026-09-03", "code": "600519", "rank": 1, "score": 88.0,
        "sim_score": 91.0, "sig_score": 90.0, "trend_score": 80.0,
        "hits": ["F4=90"], "matched_tpl_id": 1, "best_tpl_code": "000858", "best_tpl_anchor": "2026-01-05"}])
    sel = pipeline._load_selected(conn, "2026-09-03")
    check("load_selected", len(sel) == 1 and sel[0]["code"] == "600519"
          and sel[0]["best_tpl_code"] == "000858" and sel[0]["best_tpl_anchor"] == "2026-01-05")
    conn.close()


def test_notifier_console():
    cfg = {"push": {"channels": ["console"], "image_count": 3}}
    res = notifier.notify(cfg, [], "# hi", "2026-09-03")
    check("console notify", res.get("console") is True)
    notifier.alert(cfg, "测试告警")  # 不抛异常即可


def main():
    print("== Phase 3 离线测试 ==")
    test_wrap_and_payload()
    test_fallback_and_markdown()
    test_load_selected()
    test_notifier_console()
    print(f"\n全部通过: {len(PASS)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
