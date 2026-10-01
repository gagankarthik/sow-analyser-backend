"""Bounded parallelism for I/O-bound calls (OpenAI, DynamoDB).

``bounded_map`` runs a function over items on a small thread pool and returns
one outcome per item, in order, as ``(result, error)`` — a failing item never
cancels or hides the others, which is what makes per-batch failure isolation
possible. The pool size is the hard ceiling on simultaneous requests.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def _trace_entity() -> Any:
    """The current X-Ray entity, so calls made on worker threads still attach to
    the invocation's trace (otherwise the SDK logs a 'segment not found' error
    for every request). Best-effort: tracing must never break the work."""
    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return None                     # not in Lambda: there is no trace to join
    try:
        from aws_xray_sdk.core import xray_recorder

        return xray_recorder.get_trace_entity()
    except Exception:
        return None


def bounded_map(
    fn: Callable[[T], R],
    items: Iterable[T],
    max_workers: int,
) -> list[tuple[R | None, BaseException | None]]:
    """Apply ``fn`` to every item with at most ``max_workers`` running at once.

    Returns ``[(result, None) | (None, exception), ...]`` in the order of
    ``items``. Runs inline (no threads) for a single item or a pool of one.
    """
    work = list(items)
    if not work:
        return []

    def guarded(item: T) -> tuple[R | None, BaseException | None]:
        try:
            return fn(item), None
        except BaseException as exc:  # noqa: BLE001 — reported to the caller per item
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return None, exc

    workers = max(1, min(int(max_workers or 1), len(work)))
    if workers == 1:
        return [guarded(item) for item in work]

    entity = _trace_entity()

    def in_thread(item: T) -> tuple[R | None, BaseException | None]:
        if entity is not None:
            try:
                from aws_xray_sdk.core import xray_recorder

                xray_recorder.set_trace_entity(entity)
            except Exception:
                pass
        return guarded(item)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="blueiq") as pool:
        return list(pool.map(in_thread, work))
