from megaclean.compare import compare_local, write_comparison
from megaclean.index import open_index, set_crc, upsert_node
from megaclean.megacrc import mega_crc_bytes


def _remote(conn, path, data):
    upsert_node(conn, "remote", path, len(data), "t", node_id="h:" + path)
    set_crc(conn, "remote", "h:" + path, mega_crc_bytes(data).hex())
    conn.commit()


def test_classifies_present_and_missing_by_content(tmp_path):
    conn = open_index(tmp_path / "i.db")
    a = bytes((i * 3) % 256 for i in range(20_000))
    _remote(conn, "Lib/2004/a.jpg", a)
    local = tmp_path / "drive"
    (local / "2004").mkdir(parents=True)
    (local / "2004" / "renamed.jpg").write_bytes(a)             # in MEGA, other name
    (local / "2004" / "new.jpg").write_bytes(b"\x01" * 20_000)  # not in MEGA
    result = compare_local(conn, local)
    by = {r.path: r for r in result}
    assert by["2004/renamed.jpg"].status == "present"
    assert by["2004/renamed.jpg"].remote_path == "Lib/2004/a.jpg"
    assert by["2004/new.jpg"].status == "missing"


def test_hidden_files_are_ignored(tmp_path):
    conn = open_index(tmp_path / "i.db")
    local = tmp_path / "drive"
    local.mkdir()
    (local / ".DS_Store").write_bytes(b"x" * 100)
    (local / "real.jpg").write_bytes(b"y" * 100)
    assert [r.path for r in compare_local(conn, local)] == ["real.jpg"]


def test_report_lists_only_missing_files_with_sizes(tmp_path):
    conn = open_index(tmp_path / "i.db")
    a = bytes(range(256)) * 100
    _remote(conn, "Lib/a.jpg", a)
    local = tmp_path / "drive"
    local.mkdir()
    (local / "a.jpg").write_bytes(a)
    (local / "only-here.jpg").write_bytes(b"z" * 5000)
    result = compare_local(conn, local)
    out = tmp_path / "missing"
    write_comparison(result, out, local_root=local)
    md = (out.parent / "missing.md").read_text()
    assert "only-here.jpg" in md and "a.jpg\n" not in md.replace("only-here.jpg", "")
    import csv
    rows = list(csv.DictReader((out.parent / "missing.csv").open()))
    assert [r["path"] for r in rows] == ["only-here.jpg"]
    assert rows[0]["size"] == "5000"
