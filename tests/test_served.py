"""The served transport is tested against a plain local HTTP server: the range
semantics are what matter, and they are identical whoever is serving."""
import functools
import http.server
import threading

import pytest

from megaclean.served import ServedRemote, range_header
from megaclean.remote import RemoteError


@pytest.fixture
def http_root(tmp_path):
    (tmp_path / "a.bin").write_bytes(bytes(range(256)) * 8)     # 2048 bytes
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_range_header_for_a_head_read():
    assert range_header(0, 65536) == "bytes=0-65535"


def test_range_header_for_an_interior_read():
    assert range_header(100, 50) == "bytes=100-149"


def test_negative_offset_becomes_a_suffix_range():
    """HTTP suffix ranges express "the last N bytes" directly, which is exactly
    what the tail read wants."""
    assert range_header(-65536, 65536) == "bytes=-65536"


def test_reads_a_head_range(http_root):
    root, base = http_root
    remote = ServedRemote.attached(base)
    assert remote.read_range("a.bin", 0, 10) == (root / "a.bin").read_bytes()[:10]


def test_reads_a_tail_range(http_root):
    root, base = http_root
    remote = ServedRemote.attached(base)
    assert remote.read_range("a.bin", -10, 10) == (root / "a.bin").read_bytes()[-10:]


def test_reads_paths_needing_url_quoting(http_root):
    root, base = http_root
    (root / "Camera uploads").mkdir()
    (root / "Camera uploads" / "a b&c.bin").write_bytes(b"spaces and ampersands")
    remote = ServedRemote.attached(base)
    assert remote.read_range("Camera uploads/a b&c.bin", 0, 6) == b"spaces"


def test_missing_file_raises_remote_error(http_root):
    _, base = http_root
    with pytest.raises(RemoteError, match="404"):
        ServedRemote.attached(base).read_range("nope.bin", 0, 10)


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """A server that honours Range, the way rclone's does."""

    def do_GET(self):
        import os
        import urllib.parse as _up
        path = os.path.join(self.directory,
                            _up.unquote(self.path.lstrip("/")))
        if not os.path.isfile(path):
            self.send_error(404)
            return
        data = open(path, "rb").read()
        header = self.headers.get("Range")
        if not header:
            self.send_response(200)
            body = data
        else:
            spec = header.split("=", 1)[1]
            if spec.startswith("-"):
                body = data[-int(spec[1:]):]
                start = len(data) - len(body)
            else:
                first, last = spec.split("-")
                start = int(first)
                body = data[start:int(last) + 1]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{start + len(body) - 1}/{len(data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def range_server(tmp_path):
    (tmp_path / "a.bin").write_bytes(bytes(range(256)) * 8)
    handler = functools.partial(_RangeHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_real_range_server_returns_partial_content(range_server):
    root, base = range_server
    data = (root / "a.bin").read_bytes()
    remote = ServedRemote.attached(base)
    assert remote.read_range("a.bin", 0, 10) == data[:10]
    assert remote.read_range("a.bin", -10, 10) == data[-10:]
    assert remote.read_range("a.bin", 100, 20) == data[100:120]


def test_signature_agrees_whether_or_not_the_server_honours_range(
        range_server, http_root):
    """A server that ignores Range must not silently produce a different
    signature from one that honours it."""
    from megaclean.fingerprint import fetch_fingerprint

    honouring_root, honouring = range_server
    ignoring_root, ignoring = http_root
    size = len((honouring_root / "a.bin").read_bytes())
    a = fetch_fingerprint(ServedRemote.attached(honouring), "a.bin", size)
    b = fetch_fingerprint(ServedRemote.attached(ignoring), "a.bin", size)
    assert a.sig == b.sig
