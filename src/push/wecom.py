"""企业微信群机器人推送：图片直发(无需图床) + markdown。

key 从环境变量 WECOM_WEBHOOK_KEY 读取（webhook 地址末尾的 key）。
"""
from __future__ import annotations

import logging
import os

from src.config import env_secret

logger = logging.getLogger("screener.wecom")

UPLOAD_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def _key() -> str:
    key = env_secret("WECOM_WEBHOOK_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 WECOM_WEBHOOK_KEY（企业微信群机器人 key）")
    return key


def markdown_payload(content: str) -> dict:
    return {"msgtype": "markdown", "markdown": {"content": content[:4096]}}


def text_payload(content: str) -> dict:
    return {"msgtype": "text", "text": {"content": content[:2048]}}


def image_payload(media_id: str) -> dict:
    return {"msgtype": "image", "image": {"media_id": media_id}}


def _send(payload: dict) -> bool:
    import requests

    key = _key()
    r = requests.post(SEND_URL, params={"key": key}, json=payload, timeout=20)
    ok = r.status_code == 200 and r.json().get("errcode") == 0
    if not ok:
        logger.warning("企业微信发送失败: %s", r.text[:300])
    return ok


def send_markdown(content: str) -> bool:
    return _send(markdown_payload(content))


def send_text(content: str) -> bool:
    return _send(text_payload(content))


def send_image(path: str) -> bool:
    """上传临时素材(3天有效, 图片<2MB)并发送图片消息。"""
    import requests

    key = _key()
    with open(path, "rb") as fh:
        r = requests.post(
            UPLOAD_URL, params={"key": key, "type": "image"},
            files={"media": (os.path.basename(path), fh, "image/png")}, timeout=30,
        )
    data = r.json()
    if r.status_code != 200 or data.get("errcode") != 0:
        logger.warning("企业微信图片上传失败: %s", r.text[:300])
        return False
    return _send(image_payload(data["media_id"]))


def push_report(images: list[str], md: str, image_count: int = 3) -> int:
    """发前 image_count 张图 + 一条 markdown 摘要；返回发送成功条数。"""
    n = 0
    for p in images[:image_count]:
        try:
            n += 1 if send_image(p) else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("图片发送异常 %s: %s", p, exc)
    try:
        if send_markdown(md):
            n += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("markdown 发送异常: %s", exc)
    return n
