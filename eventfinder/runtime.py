"""Process ownership guard for a single local EventFinder service instance."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from eventfinder.config import PROJECT_ROOT

_HELD_PATHS: set[Path] = set()


@contextmanager
def single_instance_lock(path: Path | None = None) -> Iterator[None]:
    lock_path = path or PROJECT_ROOT / "data" / "eventfinder.lock"
    lock_path = lock_path.resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path in _HELD_PATHS:
        raise RuntimeError("EventFinder is already running; use `make status` or `make restart`.")
    with lock_path.open("w", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("EventFinder is already running; use `make status` or `make restart`.") from error
        _HELD_PATHS.add(lock_path)
        stream.write(str(os.getpid()))
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            _HELD_PATHS.remove(lock_path)
