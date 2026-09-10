"""All network access, behind a protocol with an in-memory fake.

rclone is the transport for every operation. MEGAcmd cannot serve partial
reads, so fingerprinting requires rclone regardless of how the account was
authenticated.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class RemoteError(Exception):
    """An rclone invocation failed."""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    mtime: str


def join_remote_path(root: str, rel: str) -> str:
    """Join a remote root and a relative path into a full account path."""
    root = root.strip("/")
    return f"{root}/{rel}" if root else rel


class Remote(Protocol):
    def list_files(self, root: str) -> Iterator[RemoteFile]: ...
    def read_range(self, path: str, offset: int, count: int) -> bytes: ...
    def upload(self, local_path: str, dest_path: str) -> None: ...


class RcloneRemote:
    def __init__(self, remote: str, runner: Callable = subprocess.run,
                 binary: str = "rclone") -> None:
        self.remote = remote.rstrip(":")
        self.runner = runner
        self.binary = binary

    def _target(self, path: str) -> str:
        return f"{self.remote}:{path}"

    def _run(self, argv: list[str]) -> bytes:
        result = self.runner(argv, capture_output=True)
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise RemoteError(f"{' '.join(argv)} failed: {stderr}")
        return result.stdout or b""

    def list_files(self, root: str) -> Iterator[RemoteFile]:
        out = self._run([
            self.binary, "lsjson", "--recursive", "--files-only",
            self._target(root),
        ])
        for entry in json.loads(out or b"[]"):
            if entry.get("IsDir"):
                continue
            yield RemoteFile(entry["Path"], int(entry["Size"]),
                             entry.get("ModTime", ""))

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        """Read `count` bytes at `offset`; a negative offset counts from the end."""
        return self._run([
            self.binary, "cat", "--offset", str(offset), "--count", str(count),
            self._target(path),
        ])

    def upload(self, local_path: str, dest_path: str) -> None:
        self._run([self.binary, "copyto", local_path, self._target(dest_path)])


@dataclass
class FakeRemote:
    """In-memory Remote so every flow is testable with no network."""

    files: dict[str, bytes]
    uploads: list[tuple[str, str]] = field(default_factory=list)

    def list_files(self, root: str) -> Iterator[RemoteFile]:
        prefix = root.strip("/") + "/" if root.strip("/") else ""
        for path, data in sorted(self.files.items()):
            if path.startswith(prefix):
                yield RemoteFile(path[len(prefix):], len(data),
                                 "2024-01-01T00:00:00Z")

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        if path not in self.files:
            raise RemoteError(f"no such remote file: {path}")
        data = self.files[path]
        if offset < 0:
            return data[offset:][:count]
        return data[offset:offset + count]

    def upload(self, local_path: str, dest_path: str) -> None:
        self.uploads.append((local_path, dest_path))
        self.files[dest_path] = Path(local_path).read_bytes()
