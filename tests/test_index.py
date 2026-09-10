import pytest
from megaclean.index import (
    open_index, upsert_node, set_signature, set_error,
    iter_nodes, size_collision_ids, pending_ids, targets_for,
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
    assert sorted(size_collision_ids(conn, "remote")) == ["/a.jpg", "/b.jpg"]


def test_pending_excludes_signed_and_errored(conn):
    for p in ("/a.jpg", "/b.jpg", "/c.jpg"):
        upsert_node(conn, "remote", p, 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="deadbeef", exif_key=None,
                  phash=None, phash_src=None, width=None, height=None)
    set_error(conn, "remote", "/b.jpg", "timeout")
    assert pending_ids(conn, "remote") == ["/c.jpg"]


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


def test_same_path_different_node_are_both_kept(conn):
    """MEGA allows two files with the same name in one folder, and those pairs
    are usually the duplicates worth finding."""
    upsert_node(conn, "remote", "Photos/a.jpg", 100, "t", node_id="handleA")
    upsert_node(conn, "remote", "Photos/a.jpg", 100, "t", node_id="handleB")
    rows = list(iter_nodes(conn, "remote"))
    assert len(rows) == 2
    assert {r["node_id"] for r in rows} == {"handleA", "handleB"}
    assert {r["path"] for r in rows} == {"Photos/a.jpg"}


def test_signature_addresses_one_node_not_a_path(conn):
    upsert_node(conn, "remote", "Photos/a.jpg", 100, "t", node_id="handleA")
    upsert_node(conn, "remote", "Photos/a.jpg", 100, "t", node_id="handleB")
    set_signature(conn, "remote", "handleA", sig="sigA", exif_key=None,
                  phash=None, phash_src=None, width=None, height=None)
    rows = {r["node_id"]: r for r in iter_nodes(conn, "remote")}
    assert rows["handleA"]["sig"] == "sigA"
    assert rows["handleB"]["sig"] is None


def test_targets_for_returns_id_path_size(conn):
    upsert_node(conn, "remote", "Photos/a.jpg", 100, "t", node_id="h1")
    assert targets_for(conn, "remote", ["h1"]) == [("h1", "Photos/a.jpg", 100)]


def test_prune_missing_drops_rows_for_nodes_a_rescan_no_longer_sees(conn):
    """A file moved to the bin must stop counting as a live copy."""
    from megaclean.index import prune_missing
    upsert_node(conn, "remote", "Photos/a.jpg", 1, "t", node_id="h1")
    upsert_node(conn, "remote", "Photos/b.jpg", 1, "t", node_id="h2")
    upsert_node(conn, "remote", "Other/c.jpg", 1, "t", node_id="h3")
    removed = prune_missing(conn, "remote", seen_ids={"h1"}, roots=("Photos",))
    assert removed == 1
    assert {r["node_id"] for r in iter_nodes(conn, "remote")} == {"h1", "h3"}
