"""图床上传（sm.ms / S.EE 兼容），用于 Server酱 markdown 内嵌图片。

sm.ms 已迁移至 S.EE，这里自动按顺序尝试两个端点：
  1) https://sm.ms/api/v2/upload   （旧，兼容期）
  2) https://s.ee/api/v2/upload    （新）
token 优先读环境变量 SMMS_TOKEN，其次 config.push.image_host.smms_token。
认证方式沿用 SM.MS v2 的 `Authorization: <token>` 请求头（兼容接口）。
"""
from __future__ import annotations

import logging
import os

from src.config import env_secret

logger = logging.getLogger("screener.imagehost")

DEFAULT_ENDPOINTS = [
    "https://sm.ms/api/v2/upload",
    "https://s.ee/api/v2/upload",
]


def _token(cfg: dict) -> str | None:
    return env_secret("SMMS_TOKEN") or cfg.get("push", {}).get("image_host", {}).get("smms_token")


def _endpoints(cfg: dict) -> list[str]:
    eps = cfg.get("push", {}).get("image_host", {}).get("endpoints") or DEFAULT_ENDPOINTS
    return list(eps)


def upload_smms(path: str, cfg: dict) -> str | None:
    """上传单张图片，依次尝试各端点，返回可访问 URL；失败返回 None。"""
    token = _token(cfg)
    if not token:
        logger.warning("未配置 SMMS_TOKEN，跳过图床上传")
        return None
    try:
        import requests
    except ImportError:
        logger.warning("未安装 requests")
        return None

    last_err = ""
    for ep in _endpoints(cfg):
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    ep,
                    headers={"Authorization": token},
                    files={"smfile": (os.path.basename(path), fh, "image/png")},
                    timeout=60,
                )
            d = r.json()
            if d.get("success") and d.get("data", {}).get("url"):
                return d["data"]["url"]
            if d.get("code") == "image_repeated" and d.get("images"):
                return d["images"]  # 重复图片直接复用已有 URL
            last_err = str(d)[:200]
        except Exception as exc:  # noqa: BLE001
            last_err = f"{ep}: {exc}"
        logger.warning("端点 %s 上传失败: %s", ep, last_err)
    logger.warning("sm.ms/S.EE 上传失败(最后): %s", last_err)
    return None


def upload_images(paths: list[str], cfg: dict) -> list[str]:
    """批量上传，返回成功的 URL 列表。"""
    urls = []
    for p in paths:
        u = upload_smms(p, cfg)
        if u:
            urls.append(u)
    return urls


def jsdelivr_urls(paths: list[str], cfg: dict, env=None) -> list[str]:
    """GitHub 图床：把仓库内文件转成 jsDelivr 直链（免费 CDN，国内可访问）。

    仓库需 public。CI 里自动读 GITHUB_REPOSITORY / GITHUB_REF_NAME；
    本地运行需在 config.github 里配 repo（如 "user/repo"）与 branch。
    """
    env = env or os.environ
    repo = env.get("GITHUB_REPOSITORY") or cfg.get("github", {}).get("repo")
    if not repo:
        logger.warning("未配置 GitHub 仓库（github.repo 或 GITHUB_REPOSITORY），无法生成图床直链")
        return []
    branch = env.get("GITHUB_REF_NAME") or cfg.get("github", {}).get("branch", "main")
    base = os.getcwd()
    urls = []
    for p in paths:
        rel = os.path.relpath(p, base).replace(os.sep, "/")
        urls.append(f"https://cdn.jsdelivr.net/gh/{repo}@{branch}/{rel}")
    return urls
