"""重试工具：指数退避重试，供网络抓取等不稳定调用使用。"""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, Tuple, Type

logger = logging.getLogger("screener.retry")


def retry_call(
    fn: Callable,
    *args,
    times: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
):
    """执行 fn(*args, **kwargs)，失败按指数退避重试。

    times   : 总尝试次数（含首次）
    base_delay / backoff : 第 n 次失败后等待 base_delay * backoff^(n-1) 秒
    exceptions: 需要重试的异常类型集合
    sleep   : 可注入（测试用 0 延时）
    """
    last_exc: BaseException | None = None
    for attempt in range(1, times + 1):
        try:
            return fn(*args, **kwargs)
        except exceptions as exc:  # noqa: PERF203
            last_exc = exc
            if attempt >= times:
                break
            delay = base_delay * (backoff ** (attempt - 1))
            logger.warning("第 %d/%d 次尝试失败: %s；%0.1fs 后重试", attempt, times, exc, delay)
            sleep(delay)
    raise last_exc  # type: ignore[misc]


def iter_until_ok(items: Iterable, worker: Callable, *, on_error=None) -> list:
    """对 items 逐个执行 worker；worker 抛错时记录并继续，返回 (成功数, 失败列表)。"""
    ok, failed = 0, []
    for it in items:
        try:
            worker(it)
            ok += 1
        except Exception as exc:  # noqa: BLE001 单条失败不应中断整体
            failed.append((it, exc))
            if on_error:
                on_error(it, exc)
    return [ok, failed]
