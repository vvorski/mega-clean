"""Read MEGA's node tree directly, for the content fingerprint rclone discards.

Every file node carries an encrypted attribute blob holding its name and a
fingerprint: a CRC over sampled content blocks plus the modification time.
MEGA's own clients write it at upload time, which means content identity is
already sitting in metadata that one API call returns for the whole account --
no file bytes need to be fetched to find exact duplicates.

The fingerprint is a *sparse* CRC, so it is a strong candidate filter and not
proof. Measured against this account it agreed with the bytes 79 times in 80.
Confirm anything you intend to act on; see `fingerprint_crc`.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import subprocess
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from Crypto.Cipher import AES

log = logging.getLogger(__name__)

API_ENDPOINT = "https://g.api.mega.co.nz/cs"
CRC_BYTES = 16
REQUEST_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class MegaFile:
    handle: str
    path: str
    size: int
    crc: bytes | None
    fingerprint: str | None
    file_attr: str | None      # handle for the stored thumbnail, when present
    key: bytes | None          # the node's 32-byte key, needed to decrypt thumbs


def b64decode(value: str) -> bytes:
    """MEGA uses URL-safe base64 with the padding stripped."""
    value = value.replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4))


def b64encode(data: bytes) -> str:
    return (base64.b64encode(data).decode()
            .replace("+", "-").replace("/", "_").rstrip("="))


def attribute_key(file_key: bytes) -> bytes:
    """A file's 32-byte key folds to the 16-byte key its attributes use."""
    return bytes(a ^ b for a, b in zip(file_key[:16], file_key[16:32]))


def fingerprint_crc(fingerprint: str | None) -> bytes | None:
    """The content half of a fingerprint, with the modification time dropped.

    Two copies of one file can carry different timestamps, so grouping on the
    whole fingerprint misses real duplicates -- on this account, 1,488 groups
    worth.
    """
    if not fingerprint:
        return None
    try:
        raw = b64decode(fingerprint)
    except Exception:
        return None
    return raw[:CRC_BYTES] if len(raw) >= CRC_BYTES else None


def _node_key(node: dict, master: AES.AesEcbCipher) -> bytes | None:
    blob = node.get("k", "")
    if ":" in blob:
        blob = blob.split(":", 1)[1]
    if not blob:
        return None
    try:
        return master.decrypt(b64decode(blob))
    except Exception:
        return None


def decrypt_attributes(node: dict, master: AES.AesEcbCipher) -> dict | None:
    """Decrypt a node's attribute blob, or None if it is not ours to read."""
    key_blob = _node_key(node, master)
    if not key_blob or not node.get("a"):
        return None
    key = (key_blob[:16] if len(key_blob) == 16
           else attribute_key(key_blob) if len(key_blob) >= 32 else None)
    if key is None:
        return None
    try:
        raw = AES.new(key, AES.MODE_CBC, b"\0" * 16).decrypt(b64decode(node["a"]))
    except Exception:
        return None
    if not raw.startswith(b"MEGA{"):
        return None                     # decrypted with the wrong key
    try:
        return json.loads(raw[4:].split(b"\0")[0].decode("utf-8", "replace"))
    except Exception:
        return None


def node_paths(nodes: Sequence[dict], master_key: bytes) -> list[MegaFile]:
    """Decode every file node we hold keys for into a path and a fingerprint."""
    master = AES.new(master_key, AES.MODE_ECB)
    by_handle = {n["h"]: n for n in nodes if "h" in n}
    attrs: dict[str, dict] = {}
    for node in nodes:
        decoded = decrypt_attributes(node, master)
        if decoded is not None:
            attrs[node["h"]] = decoded

    def full_path(handle: str) -> str | None:
        parts: list[str] = []
        seen = 0
        while handle in by_handle and seen < 64:
            name = attrs.get(handle, {}).get("n")
            if name is None:
                break                   # a parent we cannot read: path unknown
            parts.append(name)
            handle = by_handle[handle].get("p")
            seen += 1
        return "/".join(reversed(parts)) if parts else None

    files = []
    skipped = 0
    for node in nodes:
        if node.get("t") != 0:
            continue
        decoded = attrs.get(node["h"])
        if decoded is None:
            skipped += 1
            continue
        path = full_path(node["h"])
        if path is None:
            skipped += 1
            continue
        key_blob = _node_key(node, master)
        fingerprint = decoded.get("c")
        files.append(MegaFile(
            handle=node["h"], path=path, size=int(node.get("s", 0)),
            crc=fingerprint_crc(fingerprint), fingerprint=fingerprint,
            file_attr=node.get("fa"),
            key=key_blob if key_blob and len(key_blob) >= 32 else None,
        ))
    if skipped:
        log.info("skipped %d nodes we hold no key for (inbound shares)", skipped)
    return files


def _post(url: str, data: bytes, timeout: int):
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class MegaApi:
    def __init__(self, session_id: str, master_key: bytes, *,
                 endpoint: str = API_ENDPOINT, poster: Callable = _post) -> None:
        self.session_id = session_id
        self.master_key = master_key
        self.endpoint = endpoint
        self._post = poster

    def request(self, payload: list[dict]):
        url = f"{self.endpoint}?id={random.randint(0, 10 ** 9)}&sid={self.session_id}"
        result = self._post(url, json.dumps(payload).encode(),
                            REQUEST_TIMEOUT_SECONDS)
        # MEGA reports failure as a bare negative integer rather than an error.
        if isinstance(result, int):
            raise RuntimeError(f"MEGA API error {result}")
        if isinstance(result, list) and result and isinstance(result[0], int):
            raise RuntimeError(f"MEGA API error {result[0]}")
        return result

    def fetch_nodes(self) -> list[dict]:
        result = self.request([{"a": "f", "c": 1, "r": 1}])
        payload = result[0] if isinstance(result, list) else result
        return payload["f"]

    def files(self) -> list[MegaFile]:
        return node_paths(self.fetch_nodes(), self.master_key)


def session_from_rclone(remote: str, runner: Callable = subprocess.run,
                        binary: str = "rclone") -> tuple[str, bytes]:
    """Reuse the session rclone already holds, so no password is handled here."""
    result = runner([binary, "config", "show", remote], capture_output=True)
    text = (result.stdout or b"")
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    config = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    missing = [k for k in ("session_id", "master_key") if not config.get(k)]
    if missing:
        raise RuntimeError(
            f"rclone remote {remote!r} has no cached {' or '.join(missing)}; "
            f"run any rclone command against it once to establish a session")
    return config["session_id"], base64.b64decode(config["master_key"])
