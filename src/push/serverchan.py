"""Server酱通道（推到自己微信）。key 从 SERVERCHAN_SENDKEY 读取。

markdown 内嵌图片需要公网 URL：调用方传入 images(URL列表) 即可附加图片。
"""
from __future__ import annotations

import logging

from src.config import env_secret

logger = logging.getLogger("screener.serverchan")


def send(title: str, desp: str, images: list[str] | None = None) -> bool:
    import requests

    key = env_secret("SERVERCHAN_SENDKEY")
    if not key:
        logger.warning("未配置 SERVERCHAN_SENDKEY，跳过 Server酱")
        return False
    content = desp
    for url in images or []:
        content += f"\n\n![图]({url})"
    logger.warning("Server酱发送中: key=%s..., 标题=%s, 正文字数=%d, 图片=%d张",
                   key[:8], title, len(content), len(images or []))
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": content}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Server酱发送异常: %s: %s", type(exc).__name__, exc)
        return False
    if r.status_code != 200:
        logger.warning("Server酱返回 HTTP %s: %s", r.status_code, r.text[:300])
        return False
    body = r.text[:300]
    logger.warning("Server酱返回内容: %s", body)
    try:
        code = r.json().get("code")
        if code not in (0, None):
            logger.warning("Server酱业务错误 code=%s，请检查 SendKey/额度", code)
            return False
    except Exception:  # noqa: BLE001
        pass
    return True
