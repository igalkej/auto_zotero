"""`@stage_item_handler` — uniform per-item error handling across S1 stages.

Wraps each per-item processing function so that a single failure persists the
error to `Item.last_error`, bumps the Run's failure counter, logs, and
**does not re-raise** — letting the rest of the stage continue.

The decorator also enforces the abort threshold: if the failure ratio in a
Run exceeds `abort_threshold` (default 30%), subsequent calls raise
`StageAbortedError` so the stage tears down cleanly.

Shape of a wrapped function:

    @stage_item_handler(stage=1)
    def inventory_one(item: Item, run: Run, *, ...) -> None:
        ...

The handler expects the first positional arg to be a `zotai.state.Item` and
the `run` keyword argument to be a `zotai.state.Run`. Both are mutated in
place; the caller is responsible for committing to the DB after the stage
finishes.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from zotai.state import Item, Run
from zotai.utils.logging import get_logger

log = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

_DEFAULT_ABORT_THRESHOLD = 0.30
_MIN_SAMPLES_BEFORE_ABORT = 10


class StageAbortedError(RuntimeError):
    """Raised once the Run's failure ratio crosses `abort_threshold`."""


def stage_item_handler(
    stage: int,
    *,
    abort_threshold: float = _DEFAULT_ABORT_THRESHOLD,
) -> Callable[[F], F]:
    """Decorator factory. See module docstring for semantics."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(item: Item, *args: Any, **kwargs: Any) -> Any:
            run: Run | None = kwargs.get("run")
            if run is None and args:
                run = args[0] if isinstance(args[0], Run) else None

            _maybe_abort(run, abort_threshold)

            try:
                result = func(item, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — deliberately broad at top-level
                item.last_error = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "stage_item_failed",
                    stage=stage,
                    item_id=item.id,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                if run is not None:
                    run.items_failed += 1
                return None
            else:
                item.last_error = None
                item.stage_completed = max(item.stage_completed, stage)
                if run is not None:
                    run.items_processed += 1
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _maybe_abort(run: Run | None, threshold: float) -> None:
    if run is None:
        return
    total = run.items_processed + run.items_failed
    if total < _MIN_SAMPLES_BEFORE_ABORT:
        return
    if run.items_failed / total > threshold:
        raise StageAbortedError(
            f"Run {run.id}: failure ratio "
            f"{run.items_failed}/{total} exceeds {threshold:.0%}"
        )


__all__ = ["StageAbortedError", "stage_item_handler"]
