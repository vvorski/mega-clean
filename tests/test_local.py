from megaclean.fingerprint import fingerprint_from_parts
from megaclean.index import iter_nodes, open_index
from megaclean.local import iter_media, local_scope, read_parts, scan_local
from megaclean.signature import signature_of_bytes


def test_iter_media_filters_by_extension(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / "sub" / "b.JPG")
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "clip.mp4").write_bytes(b"\x00")
    found = {p.name for p in iter_media(tmp_path)}
    assert found == {"a.jpg", "b.JPG"}


def test_include_video_opt_in(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    (tmp_path / "clip.mp4").write_bytes(b"\x00")
    found = {p.name for p in iter_media(tmp_path, include_video=True)}
    assert found == {"a.jpg", "clip.mp4"}


def test_hidden_files_are_skipped(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / ".hidden.jpg")
    make_jpeg(tmp_path / ".cache" / "c.jpg")
    assert {p.name for p in iter_media(tmp_path)} == {"a.jpg"}


def test_read_parts_matches_whole_file_signature(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(400, 400))
    size, head, tail = read_parts(p)
    fp = fingerprint_from_parts(size, head, tail)
    assert fp.sig == signature_of_bytes(p.read_bytes())


def test_read_parts_matches_for_a_large_file(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(bytes(range(256)) * 2048)
    size, head, tail = read_parts(p)
    fp = fingerprint_from_parts(size, head, tail)
    assert fp.sig == signature_of_bytes(p.read_bytes())


def test_scan_local_records_relative_paths(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / "sub" / "b.jpg", dt="2024:07:01 10:00:00")
    conn = open_index(tmp_path / "i.db")
    scanned, failed = scan_local(conn, tmp_path)
    assert (scanned, failed) == (2, 0)
    paths = [r["path"] for r in iter_nodes(conn, local_scope(tmp_path))]
    assert paths == ["a.jpg", "sub/b.jpg"]


def test_scan_local_populates_signatures(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg")
    conn = open_index(tmp_path / "i.db")
    scan_local(conn, tmp_path)
    row = next(iter_nodes(conn, local_scope(tmp_path)))
    assert row["sig"] == signature_of_bytes(p.read_bytes())
    assert row["exif_key"] is not None


def test_unreadable_file_is_recorded_as_error(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"")
    bad.chmod(0o000)
    conn = open_index(tmp_path / "i.db")
    try:
        scanned, failed = scan_local(conn, tmp_path)
    finally:
        bad.chmod(0o644)
    assert failed == 1
