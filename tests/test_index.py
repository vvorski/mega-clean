import pytest
from megaclean.index import (
    open_index, upsert_node, set_signature, set_error,
    iter_nodes, size_collision_paths, pending_paths,
)


@pytest.fixture
def conn(tmp_path):
    return open_index(tmp_path / "index.db")


def test_upsert_is_idempotent_and_updates_size(conn):
    upsert_node(conn, "remote", "/Photos/a.jpg", 100, "2024-01-01T00:00:00Z")
    upsert_node(conn, "remote", "/Photos/a.jpg", 120, "2024-01-02T00:00:00Z")
    rows = list(iter_nodes(conn, "remote"))
    assert len(rows) == 1
    assert rows[0]["size"] == 120
    assert rows[0]["mtime"] == "2024-01-02T00:00:00Z"


def test_scopes_are_independent(conn):
    upsert_node(conn, "remote", "/a.jpg", 1, "t")
    upsert_node(conn, "local:/tmp/pics", "/a.jpg", 1, "t")
    assert len(list(iter_nodes(conn, "remote"))) == 1
    assert len(list(iter_nodes(conn, "local:/tmp/pics"))) == 1


def test_size_collision_paths_ignores_unique_sizes(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    upsert_node(conn, "remote", "/b.jpg", 100, "t")
    upsert_node(conn, "remote", "/c.jpg", 999, "t")
    assert sorted(size_collision_paths(conn, "remote")) == ["/a.jpg", "/b.jpg"]


def test_pending_excludes_signed_and_errored(conn):
    for p in ("/a.jpg", "/b.jpg", "/c.jpg"):
        upsert_node(conn, "remote", p, 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="deadbeef", exif_key=None,
                  phash=None, phash_src=None, width=None, height=None)
    set_error(conn, "remote", "/b.jpg", "timeout")
    assert pending_paths(conn, "remote") == ["/c.jpg"]


def test_set_signature_clears_previous_error(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    set_error(conn, "remote", "/a.jpg", "timeout")
    set_signature(conn, "remote", "/a.jpg", sig="beef", exif_key=None,
                  phash=None, phash_src=None, width=None, height=None)
    row = next(iter_nodes(conn, "remote"))
    assert row["error"] is None
    assert row["sig"] == "beef"


def test_upsert_preserves_signature_when_size_unchanged(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="beef", exif_key="k",
                  phash="ff", phash_src="thumb", width=10, height=20)
    upsert_node(conn, "remote", "/a.jpg", 100, "t2")
    assert next(iter_nodes(conn, "remote"))["sig"] == "beef"


def test_upsert_invalidates_signature_when_size_changes(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="beef", exif_key="k",
                  phash="ff", phash_src="thumb", width=10, height=20)
    upsert_node(conn, "remote", "/a.jpg", 101, "t2")
    row = next(iter_nodes(conn, "remote"))
    assert row["sig"] is None
    assert row["exif_key"] is None
