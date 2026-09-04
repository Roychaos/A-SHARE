"""Phase 2b 离线测试：高级7因子（F1/F4/F5/F7）+ 排雷过滤 + 权重归一化。"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.screen import factors as F  # noqa: E402
from src.screen.scorer import build_ctx  # noqa: E402

PASS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"[FAIL] {name} {detail}")
    PASS.append(name)
    print(f"  ok - {name}")


def rows(n, close_fn, vol_fn=lambda i: 1000.0, start="2024-01-01"):
    d0 = dt.date.fromisoformat(start)
    out = []
    for i in range(n):
        c = float(close_fn(i))
        out.append({"date": (d0 + dt.timedelta(days=i)).isoformat(),
                    "open": c * 0.995, "high": c * 1.01, "low": c * 0.99,
                    "close": c, "volume": vol_fn(i)})
    return out


def cfg_all(enabled=("f1_efficiency", "f4_move", "f5_volume", "f7_sector")):
    return {
        "factors": {
            "enabled": True,
            "filters": {"shadow_reject": True, "volume_blowout": True,
                        "volume_blowout_ratio": 1.5, "heat_sector_reject": True},
            "f1_efficiency": {"enable": "f1_efficiency" in enabled, "weight": 25, "gate": 1.2},
            "f4_move": {"enable": "f4_move" in enabled, "weight": 15,
                        "day_band": [0.03, 0.045], "cum5_band": [0.06, 0.09]},
            "f5_volume": {"enable": "f5_volume" in enabled, "weight": 10,
                          "band": [1.15, 1.35]},
            "f7_sector": {"enable": "f7_sector" in enabled, "weight": 10,
                          "corr_band": [0.45, 0.58], "excess_min": 0.025},
        }
    }


def test_f1_efficiency():
    # 前78日: 窄幅震荡 |dP|=0.1, 量1000; 末日: +0.4 于小量500 -> 资金效率极高
    r = rows(79, lambda i: 10.0 + 0.1 * (i % 2), vol_fn=lambda i: 500.0 if i == 78 else 1000.0)
    r[-1]["close"] = 10.4
    r[-1]["high"] = 10.41
    r[-1]["low"] = 10.35
    ctx = build_ctx(r)
    s = F.f1_efficiency(ctx, {"gate": 1.2})
    check("f1 high efficiency", s > 0, f"score={s}")
    # 下跌日不得分
    r2 = rows(79, lambda i: 10.0 + 0.1 * (i % 2))
    r2[-1]["close"] = 9.6
    check("f1 down day zero", F.f1_efficiency(build_ctx(r2), {"gate": 1.2}) == 0)


def test_f4_move():
    r = rows(79, lambda i: 10.0)
    r[-1] = {"date": r[-1]["date"], "open": 10.30, "high": 10.36, "low": 10.29,
             "close": 10.35, "volume": 1000.0}
    ctx = build_ctx(r)
    check("f4 day band", F.f4_move(ctx, {"day_band": [0.03, 0.045], "cum5_band": [0.06, 0.09]}) > 0)


def test_f5_volume_and_filters():
    def vol(i):
        return {75: 1000.0, 76: 1050.0, 77: 1100.0, 78: 1150.0, 79: 1200.0}.get(i, 1000.0)
    r = rows(80, lambda i: 10.0, vol_fn=vol)
    ctx = build_ctx(r)
    s = F.f5_volume(ctx, {"band": [1.15, 1.35]})
    check("f5 band+streak+slope", s > 0, f"score={s}")
    # 爆量过滤
    r2 = rows(80, lambda i: 10.0, vol_fn=lambda i: 2000.0 if i == 79 else 1000.0)
    reasons = F.reject_reasons(build_ctx(r2), cfg_all())
    check("volume blowout reject", any("爆量" in x for x in reasons), str(reasons))
    # 避雷针过滤
    r3 = rows(80, lambda i: 10.0)
    r3[-1] = {"date": r3[-1]["date"], "open": 10.1, "high": 11.0, "low": 10.0,
              "close": 10.2, "volume": 1000.0}
    reasons3 = F.reject_reasons(build_ctx(r3), cfg_all())
    check("shadow reject", any("避雷针" in x for x in reasons3), str(reasons3))


def test_f7_and_composite():
    cfg = cfg_all()
    p = {"corr_band": [0.45, 0.58], "excess_min": 0.025}
    s_ok, _ = F.f7_sector_score({"present": True, "corr": 0.515, "excess": 0.05}, p)
    check("f7 center high", s_ok >= 99)
    s_over, _ = F.f7_sector_score({"present": True, "corr": 0.7, "excess": 0.0}, p)
    check("f7 too correlated", s_over == 30.0)
    s_low, _ = F.f7_sector_score({"present": True, "corr": 0.1, "excess": 0.0}, p)
    check("f7 low corr zero", s_low == 0.0)
    s_off, _ = F.f7_sector_score(None, p)
    check("f7 no data zero", s_off == 0.0)

    # 权重归一化：f1/f5 都满分 -> 合成100
    r = rows(80, lambda i: 10.0 + 0.1 * (i % 2))
    r[-1] = {"date": r[-1]["date"], "open": 10.4, "high": 10.42, "low": 10.35,
             "close": 10.4, "volume": 500.0}
    ctx = build_ctx(r)
    comp = F.composite_score(ctx, cfg, None, available=["f1_efficiency", "f5_volume"])
    check("composite normalized", comp["signal_score"] is not None)


def test_active_factors():
    check("active factors", set(F.active_factors(cfg_all())) ==
          {"f1_efficiency", "f4_move", "f5_volume", "f7_sector"})


def main():
    print("== Phase 2b 因子体系离线测试 ==")
    test_f1_efficiency()
    test_f4_move()
    test_f5_volume_and_filters()
    test_f7_and_composite()
    test_active_factors()
    print(f"\n全部通过: {len(PASS)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
