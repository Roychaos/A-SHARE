"""通道分发：按 config.push.channels 分发；失败告警走 text/markdown。"""
from __future__ import annotations

import logging

logger = logging.getLogger("screener.notifier")


def notify(cfg: dict, images: list[str], md: str, date: str, image_urls: list[str] | None = None) -> dict:
    """发送图文报告。channels: wecom / serverchan / console（可多开）。

    image_urls 传入时（如 GitHub/jsDelivr 直链），serverchan 直接内嵌，
    不再走 sm.ms 上传。
    """
    channels = cfg.get("push", {}).get("channels", ["console"])
    image_count = int(cfg.get("push", {}).get("image_count", 3))
    result = {}
    for ch in channels:
        try:
            if ch == "wecom":
                from src.push import wecom
                result["wecom"] = wecom.push_report(images, md, image_count)
            elif ch == "serverchan":
                from src.push import imagehost
                from src.push import serverchan
                if image_urls is not None:
                    urls = image_urls
                else:
                    urls = imagehost.upload_images(images, cfg)
                result["serverchan"] = serverchan.send(f"A股选股 {date}", md, urls)
            elif ch == "console":
                print(f"\n===== 推送内容（console 调试）=====\n{md}")
                result["console"] = True
        except Exception as exc:  # noqa: BLE001
            logger.error("通道 %s 失败: %s", ch, exc)
            result[ch] = False
    return result


def alert(cfg: dict, msg: str) -> None:
    """主流程异常告警（尽量用 text 送达）。"""
    channels = cfg.get("push", {}).get("channels", ["console"])
    for ch in channels:
        try:
            if ch == "wecom":
                from src.push import wecom
                wecom.send_text(f"[选股系统告警] {msg}")
            elif ch == "serverchan":
                from src.push import serverchan
                serverchan.send("选股系统告警", msg)
            elif ch == "console":
                print(f"[告警] {msg}")
        except Exception:  # noqa: BLE001
            pass
