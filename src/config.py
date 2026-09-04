"""配置加载：读取 config.yaml（未提供时回退 config.example.yaml 作只读默认值）。

密钥一律不进配置文件，运行时从环境变量读取（见 env_secret）。
"""
from __future__ import annotations

import os
from typing import Any


def _find_default_config() -> str | None:
    for name in ("config.yaml", "config.example.yaml"):
        if os.path.exists(name):
            return name
    return None


def load_config(path: str | None = None) -> dict[str, Any]:
    """加载 YAML 配置为 dict。

    path 为空时按 config.yaml -> config.example.yaml 顺序查找；
    显式传入 path 且文件不存在会报错。
    """
    if path is None:
        path = _find_default_config()
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"未找到配置文件: {path or 'config.yaml'}")

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 PyYAML，请先执行: pip install -r requirements.txt") from exc

    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"配置文件结构错误（应为 YAML 映射）: {path}")
    return cfg


def cfg_get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """按点分路径取配置，如 cfg_get(cfg, 'scoring.top_n', 5)。"""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def env_secret(name: str, default: str | None = None) -> str | None:
    """读取环境变量密钥（密钥只允许来自环境变量）。"""
    return os.environ.get(name, default)
