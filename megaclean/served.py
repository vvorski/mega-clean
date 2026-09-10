"""A transport backed by one long-lived `rclone serve http` process.

rclone's mega backend loads the account's entire node tree every time the
process starts -- roughly 16 seconds for an 80k-file account -- so spawning one
`rclone cat` per file spends all its time re-reading the tree and none of it
transferring. A single server process pays that cost once and then answers HTTP
range requests in well under a second.
"""
from __future__ import annotations

import logging
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from .remote import RcloneRemote, RemoteError, RemoteFile

log = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 120


def range_header(offset: int, count: int) -> str:
    """An HTTP Range header for the same semantics `read_range` uses.

    A negative offset becomes a suffix range, which is HTTP's native way of
    saying "the last N bytes" -- exactly what the tail read needs.
    """
    if offset < 0:
        return f"bytes=-{count}"
    return f"bytes={offset}-{offset + count - 1}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServedRemote:
    """Serves reads over HTTP; delegates listing and upload to plain rclone."""

    def __init__(self, remote: str, *, binary: str = "rclone",
                 ready_timeout: float = READY_TIMEOUT_SECONDS) -> None:
        self.remote = remote.rstrip(":")
        self.binary = binary
        self.ready_timeout = ready_timeout
        self.base_url: str | None = None
        self._process: subprocess.Popen | None = None
        self._delegate = RcloneRemote(self.remote, binary=binary)

    @classmethod
    def attached(cls, base_url: str) -> "ServedRemote":
        """Point at an already-running server (used by tests and reattachment)."""
        remote = cls("unused")
        remote.base_url = base_url.rstrip("/")
        return remote

    def start(self) -> "ServedRemote":
        port = _free_port()
        argv = [self.binary, "serve", "http", f"{self.remote}:",
                "--addr", f"127.0.0.1:{port}", "--read-only"]
        log.info("starting %s", " ".join(argv))
        self._process = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RemoteError(
                    f"rclone serve exited with code {self._process.returncode} "
                    f"before becoming ready")
            try:
                urllib.request.urlopen(self.base_url + "/", timeout=5).read(1)
                log.info("server ready at %s", self.base_url)
                return self
            except Exception:
                time.sleep(1)
        self.stop()
        raise RemoteError(
            f"rclone serve http did not become ready within "
            f"{self.ready_timeout:.0f}s -- the account tree may be very large")

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def __enter__(self) -> "ServedRemote":
        return self.start() if self.base_url is None else self

    def __exit__(self, *exc) -> None:
        self.stop()

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        if self.base_url is None:
            raise RemoteError("server not started")
        url = self.base_url + "/" + urllib.parse.quote(path.lstrip("/"))
        request = urllib.request.Request(url)
        request.add_header("Range", range_header(offset, count))
        try:
            with urllib.request.urlopen(
                    request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                data = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raise RemoteError(f"{path}: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RemoteError(f"{path}: {exc}") from exc

        if status == 200:
            # The server ignored the Range header and sent the whole file.
            # Slicing here keeps the signature correct; without it we would
            # hash far more than the requested window and every signature
            # computed this way would disagree with a ranged one.
            log.debug("%s: server ignored Range, slicing locally", path)
            return data[-count:] if offset < 0 else data[offset:offset + count]
        return data

    def list_files(self, root: str) -> Iterator[RemoteFile]:
        """Listing runs once per command, so the plain transport is fine."""
        return self._delegate.list_files(root)

    def upload(self, local_path: str, dest_path: str) -> None:
        return self._delegate.upload(local_path, dest_path)
