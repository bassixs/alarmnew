"""Serialize the bot and scheduler's publications on a single deployment host."""

from __future__ import annotations

import os
import time
from functools import wraps
from pathlib import Path
from threading import Lock

_thread_lock = Lock()


def serialized_publication(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _thread_lock:
            path = Path("data/publish.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    deadline = time.monotonic() + 180
                    while True:
                        handle.seek(0)
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Публикация занята другим процессом") from None
                            time.sleep(0.1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    return function(*args, **kwargs)
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return wrapped
