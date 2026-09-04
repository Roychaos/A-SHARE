"""推送通道自检：发一条测试 markdown（有 Pillow 则再发一张测试图）。

用法:
    python scripts/test_push.py
"""
from __future__ import annotations

import argparse
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import cfg_get, load_config  # noqa: E402
from src.push import notifier  # noqa: E402
from src.utils.log import setup_logger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="推送通道自检")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logger("test_push", cfg_get(cfg, "paths.log"))

    md = "**选股系统通道自检**\n\n这是一条测试消息。若你能看到，说明推送链路已打通。\n\n> 仅供研究参考。"
    images: list[str] = []
    try:
        from PIL import Image
        os.makedirs("output", exist_ok=True)
        p = "output/_push_test.png"
        Image.new("RGB", (400, 200), "#2b6cb0").save(p)
        images.append(p)
    except Exception:  # noqa: BLE001
        pass

    res = notifier.notify(cfg, images, md, "测试")
    print("\n推送结果:", res)
    return 0 if any(res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
