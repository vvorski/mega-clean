from megaclean.fingerprint import (
    fetch_fingerprint, fingerprint_from_parts, run_fingerprint,
)
from megaclean.index import iter_nodes, open_index, pending_paths, upsert_node
from megaclean.remote import FakeRemote
from megaclean.signature import signature_of_bytes


class CountingRemote(FakeRemote):
    def __init__(self, files):
        super().__init__(files)
        self.reads = []

    def read_range(self, path, offset, count):
        self.reads.append((path, offset, count))
        return super().read_range(path, offset, count)


def test_fingerprint_matches_whole_file_signature(make_jpeg, tmp_path):
    data = (make_jpeg(tmp_path / "a.jpg", size=(400, 300))).read_bytes()
    remote = FakeRemote({"a.jpg": data})
    fp = fetch_fingerprint(remote, "a.jpg", len(data))
    assert fp.sig == signature_of_bytes(data)
    assert fp.exif_key is not None
    assert fp.width == 400 and fp.height == 300


def test_small_file_skips_the_tail_request(make_jpeg, tmp_path):
    data = (make_jpeg(tmp_path / "a.jpg", size=(32, 32))).read_bytes()
    assert len(data) <= 131072
    remote = CountingRemote({"a.jpg": data})
    fetch_fingerprint(remote, "a.jpg", len(data))
    assert len(remote.reads) == 1


def test_large_file_issues_head_and_tail_requests():
    data = b"\xff\xd8" + b"x" * 200000
    remote = CountingRemote({"a.jpg": data})
    fetch_fingerprint(remote, "a.jpg", len(data))
    assert len(remote.reads) == 2
    assert remote.reads[1][1] == -65536


def test_run_fingerprint_persists_results(make_jpeg, tmp_path):
    a = (make_jpeg(tmp_path / "a.jpg")).read_bytes()
    b = (make_jpeg(tmp_path / "b.jpg", dt="2024:06:01 09:00:00")).read_bytes()
    remote = FakeRemote({"a.jpg": a, "b.jpg": b})
    conn = open_index(tmp_path / "i.db")
    upsert_node(conn, "remote", "a.jpg", len(a), "t")
    upsert_node(conn, "remote", "b.jpg", len(b), "t")
    ok, failed = run_fingerprint(conn, remote, "remote", ["a.jpg", "b.jpg"],
                                 workers=2, sizes={"a.jpg": len(a), "b.jpg": len(b)})
    assert (ok, failed) == (2, 0)
    rows = {r["path"]: r for r in iter_nodes(conn, "remote")}
    assert rows["a.jpg"]["sig"] == signature_of_bytes(a)
    assert rows["a.jpg"]["exif_key"] != rows["b.jpg"]["exif_key"]


def test_failures_are_recorded_not_raised(tmp_path):
    remote = FakeRemote({})
    conn = open_index(tmp_path / "i.db")
    upsert_node(conn, "remote", "gone.jpg", 10, "t")
    ok, failed = run_fingerprint(conn, remote, "remote", ["gone.jpg"],
                                 workers=1, sizes={"gone.jpg": 10},
                                 max_attempts=1)
    assert (ok, failed) == (0, 1)
    row = next(iter_nodes(conn, "remote"))
    assert row["sig"] is None
    assert "no such remote file" in row["error"]


def test_rerun_skips_already_fingerprinted(make_jpeg, tmp_path):
    data = (make_jpeg(tmp_path / "a.jpg")).read_bytes()
    remote = CountingRemote({"a.jpg": data})
    conn = open_index(tmp_path / "i.db")
    upsert_node(conn, "remote", "a.jpg", len(data), "t")
    sizes = {"a.jpg": len(data)}
    run_fingerprint(conn, remote, "remote", pending_paths(conn, "remote"),
                    workers=1, sizes=sizes)
    before = len(remote.reads)
    run_fingerprint(conn, remote, "remote", pending_paths(conn, "remote"),
                    workers=1, sizes=sizes)
    assert len(remote.reads) == before
