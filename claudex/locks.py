"""Cross-process locks: the Chrome key (one agent in your signed-in browser at a time) and one job per project."""
import asyncio
import fcntl
import os
import pathlib

CHROME_LOCK = pathlib.Path.home() / ".cache/claudex/chrome.lock"


class Lock:
    """An exclusive flock, acquired by polling so waiting can be cancelled and reported."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.fd = None

    def try_acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return True

    async def acquire(self, on_wait=None, poll=2.0):
        if self.try_acquire():
            return
        if on_wait:
            await on_wait()
        while not self.try_acquire():
            await asyncio.sleep(poll)

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
