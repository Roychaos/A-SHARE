"""Server酱通道（推到自己微信）。key 从 SERVERCHAN_SENDKEY 读取。

markdown 内嵌图片需要公网 URL：调用方传入 images(URL列表) 即可附加图片。

★ 免费额度陷阱（2026-09-17 实测）：
  Server酱 Turbo 免费版每天只有 5 条，而**长正文会被自动分条，每一条都算一次额度**。
  我们原来那份 ~1.8KB 的报告（5只票 + 3张图链接）被拆成了 5 条，
  用户收到的标题就是「【1/5】A股选股 2026-09-17」—— 一次推送就把当天额度吃光，
  之后同一天的其它推送全部因超额失败。
  因此默认改用 compact_markdown()：只发「日期 + Top5 代码/名称/形态分 + 完整图文链接」，
  正文控制在 ~300 字符内，稳定只占 1 条额度。
"""
from __future__ import annotations

import logging
import os
import re

from src.config import env_secret

logger = logging.getLogger("screener.serverchan")

# 行形如：  ## #1 600436 片仔癀
_HEAD_RE = re.compile(r"^#{2,3}\s*#?(\d+)\s+([0-9A-Za-z]+)\s*(.*)$")
_SCORE_RE = re.compile(r"形态分\s*([0-9.]+)")


def compact_markdown(date: str, md: str, cfg: dict, top_n: int = 5) -> str:
    """把完整报告压成"一条就能发完"的摘要（避免 Server酱 分条吃光额度）。"""
    items: list[dict] = []
    for raw in (md or "").splitlines():
        line = raw.strip()
        m = _HEAD_RE.match(line)
        if m:
            items.append({"rank": m.group(1), "code": m.group(2),
                          "name": (m.group(3) or "").strip(), "score": ""})
            continue
        if items and (not items[-1]["score"]):
            sm = _SCORE_RE.search(line)
            if sm:
                items[-1]["score"] = sm.group(1)

    repo = (cfg.get("github", {}) or {}).get("repo") or os.environ.get("GITHUB_REPOSITORY", "")
    branch = (cfg.get("github", {}) or {}).get("branch") or "main"
    out = [f"共 {len(items)} 只入选（纯形态相似度排序）："]
    for it in items[:top_n]:
        name = f" {it['name']}" if it["name"] else ""
        score = f"  {it['score']}" if it["score"] else ""
        out.append(f"{it['rank']}. {it['code']}{name}{score}")
    if len(items) > top_n:
        out.append(f"…等共 {len(items)} 只")
    if repo:
        out.append(f"完整图文: https://github.com/{repo}/tree/{branch}/output/{date}")
    out.append("仅供研究参考，不构成投资建议")
    return "\n".join(out)


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
            logger.warning("Server酱业务错误 code=%s（0=成功；额度用尽通常是 40001/超过当日限额）"
                           "，请检查 SendKey/额度", code)
            return False
    except Exception:  # noqa: BLE001
        pass
    return True
