import json

import pytest

from megaclean.remote import (
    FakeRemote, RcloneRemote, RemoteError, RemoteFile, join_remote_path,
)


class Recorder:
    """Stands in for subprocess.run and records the argv it was handed."""

    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)

        class R:
            pass

        r = R()
        r.stdout, r.stderr, r.returncode = self.stdout, self.stderr, self.returncode
        return r


def test_list_files_parses_lsjson():
    payload = json.dumps([
        {"Path": "a.jpg", "Size": 100, "ModTime": "2024-01-01T00:00:00Z", "IsDir": False},
        {"Path": "sub/b.jpg", "Size": 200, "ModTime": "2024-01-02T00:00:00Z", "IsDir": False},
    ]).encode()
    rec = Recorder(stdout=payload)
    files = list(RcloneRemote("mega", runner=rec).list_files("Photos"))
    assert files == [
        RemoteFile("a.jpg", 100, "2024-01-01T00:00:00Z"),
        RemoteFile("sub/b.jpg", 200, "2024-01-02T00:00:00Z"),
    ]
    assert "lsjson" in rec.calls[0]
    assert "mega:Photos" in rec.calls[0]


def test_read_range_uses_offset_and_count():
    rec = Recorder(stdout=b"bytes")
    assert RcloneRemote("mega", runner=rec).read_range("a.jpg", 0, 65536) == b"bytes"
    argv = rec.calls[0]
    assert argv[argv.index("--offset") + 1] == "0"
    assert argv[argv.index("--count") + 1] == "65536"


def test_negative_offset_reads_the_tail():
    rec = Recorder(stdout=b"tail")
    RcloneRemote("mega", runner=rec).read_range("a.jpg", -65536, 65536)
    argv = rec.calls[0]
    assert argv[argv.index("--offset") + 1] == "-65536"


def test_nonzero_exit_raises_remote_error():
    rec = Recorder(returncode=1, stderr=b"no such file")
    with pytest.raises(RemoteError, match="no such file"):
        RcloneRemote("mega", runner=rec).read_range("missing.jpg", 0, 10)


def test_upload_uses_copyto_with_destination():
    rec = Recorder()
    RcloneRemote("mega", runner=rec).upload("/local/a.jpg", "Photos/2024/a.jpg")
    argv = rec.calls[0]
    assert "copyto" in argv
    assert "/local/a.jpg" in argv
    assert "mega:Photos/2024/a.jpg" in argv


def test_fake_remote_serves_ranges_and_records_uploads(tmp_path):
    data = bytes(range(256)) * 4
    fake = FakeRemote({"a.jpg": data})
    assert list(fake.list_files(""))[0].size == len(data)
    assert fake.read_range("a.jpg", 0, 10) == data[:10]
    assert fake.read_range("a.jpg", -10, 10) == data[-10:]
    local = tmp_path / "b.jpg"
    local.write_bytes(b"new")
    fake.upload(str(local), "Photos/b.jpg")
    assert fake.uploads == [(str(local), "Photos/b.jpg")]
    assert "Photos/b.jpg" in fake.files


def test_fake_remote_raises_for_missing_path():
    with pytest.raises(RemoteError):
        FakeRemote({}).read_range("nope.jpg", 0, 1)


def test_join_remote_path():
    assert join_remote_path("Photos", "a.jpg") == "Photos/a.jpg"
    assert join_remote_path("Photos/", "sub/a.jpg") == "Photos/sub/a.jpg"
    assert join_remote_path("", "a.jpg") == "a.jpg"
    assert join_remote_path("/Photos/", "a.jpg") == "Photos/a.jpg"
