"""Cross-process exclusive lock for all operations that read or mutate dataset files."""
from contextlib import contextmanager
import os
import time

@contextmanager
def dataset_lock(lock_dir=None):
    """Serialize dataset reads/writes across API workers on Windows and POSIX."""
    if lock_dir is None:
        from app.config import DATA_DIR
        lock_dir = DATA_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / ".generation.lock"
    handle = open(path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while not locked:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
