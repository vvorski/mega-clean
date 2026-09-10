import json

import pytest

from megaclean.bin import (
    BinEntry, build_bin_plan, execute_bin_plan, read_bin_plan, write_bin_plan,
)
from megaclean.dupes import Cluster, Node, cluster_nodes


def n(path, sig="s", crc="c", node_id=None, size=100):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=None, height=None, mtime="t", crc=crc,
                node_id=node_id or path)


def test_plan_bins_every_copy_except_the_keeper():
    a, b, c = n("Lib/a.jpg"), n("Inbox/a.jpg"), n("Old/a.jpg")
    clusters = cluster_nodes([a, b, c], prefer=("Lib",))
    plan = build_bin_plan(clusters)
    assert sorted(e.path for e in plan) == ["Inbox/a.jpg", "Old/a.jpg"]
    assert all(e.kept_path == "Lib/a.jpg" for e in plan)


def test_fingerprint_only_matches_are_excluded_by_default():
    a, b = n("Lib/a.jpg", sig=""), n("Inbox/a.jpg", sig="")
    clusters = cluster_nodes([a, b], prefer=("Lib",))
    assert build_bin_plan(clusters) == []
    assert len(build_bin_plan(clusters, allow_unverified=True)) == 1
    assert build_bin_plan(clusters, allow_unverified=True)[0].evidence == "fingerprint"


def test_only_under_restricts_to_a_folder():
    a, b, c = n("Lib/a.jpg"), n("Inbox/a.jpg"), n("Old/a.jpg")
    clusters = cluster_nodes([a, b, c], prefer=("Lib",))
    plan = build_bin_plan(clusters, only_under=("Inbox",))
    assert [e.path for e in plan] == ["Inbox/a.jpg"]


def test_a_keeper_is_never_in_the_plan_even_across_clusters():
    """The invariant that makes this safe: nothing binned is the last copy."""
    a, b = n("Lib/a.jpg", sig="1", crc="1"), n("Inbox/a.jpg", sig="1", crc="1")
    c, d = n("Lib/b.jpg", sig="2", crc="2"), n("Inbox/b.jpg", sig="2", crc="2")
    plan = build_bin_plan(cluster_nodes([a, b, c, d], prefer=("Lib",)))
    binned = {e.handle for e in plan}
    kept = {e.kept_handle for e in plan}
    assert not binned & kept


def test_variant_cluster_bins_only_within_identical_subgroups():
    """Different burst frames must never be binned against each other."""
    frames = [n("Lib/f1.jpg", sig="A", crc="A", node_id="l1"),
              n("Inbox/f1.jpg", sig="A", crc="A", node_id="i1"),
              n("Lib/f2.jpg", sig="B", crc="B", node_id="l2")]
    # force them into one cluster via a shared exif key
    frames = [Node(**{**f.__dict__, "exif_key": "same-shot"}) for f in frames]
    clusters = cluster_nodes(frames, prefer=("Lib",))
    assert clusters[0].kind == "variant"
    plan = build_bin_plan(clusters)
    assert [e.handle for e in plan] == ["i1"]


def test_execute_refuses_a_plan_that_would_bin_a_keeper():
    entries = [BinEntry("h1", "a.jpg", 1, "h2", "b.jpg", "byte"),
               BinEntry("h2", "b.jpg", 1, "h1", "a.jpg", "byte")]

    class Api:
        def move_to_rubbish(self, *a, **k):
            raise AssertionError("must not be called")

    with pytest.raises(ValueError, match="keeper"):
        execute_bin_plan(Api(), entries, "bin")


def test_dry_run_moves_nothing():
    entries = [BinEntry("h1", "a.jpg", 1, "h2", "b.jpg", "byte")]

    class Api:
        def move_to_rubbish(self, *a, **k):
            raise AssertionError("must not be called")

    result = execute_bin_plan(Api(), entries, "bin", dry_run=True)
    assert result.moved == 1 and result.failed == 0


def test_execute_reports_per_file_failures():
    entries = [BinEntry("ok", "a.jpg", 1, "k1", "x.jpg", "byte"),
               BinEntry("gone", "b.jpg", 1, "k2", "y.jpg", "byte")]

    class Api:
        def move_to_rubbish(self, handles, rubbish, **k):
            assert rubbish == "bin"
            return {"ok": 0, "gone": -9}

    result = execute_bin_plan(Api(), entries, "bin")
    assert (result.moved, result.failed) == (1, 1)
    assert result.errors == [("b.jpg", -9)]


def test_plan_round_trips(tmp_path):
    entries = [BinEntry("h1", "Inbox/a.jpg", 5, "h2", "Lib/a.jpg", "byte")]
    out = tmp_path / "bin.json"
    write_bin_plan(entries, out, remote="mega", only_under=("Inbox",))
    loaded, header = read_bin_plan(out)
    assert loaded == entries
    assert header["only_under"] == ["Inbox"]
    assert json.loads(out.read_text())["entries"][0]["evidence"] == "byte"


def test_bin_plan_honours_the_clusters_avoid_constraint():
    """The executable path must pick the same survivor as the manifest; a
    keeper in a folder the folder plan removes would be binned by one and
    kept by the other."""
    a = n("Old/a.jpg", node_id="old")
    b = n("New/deep/deeper/a.jpg", node_id="new")
    clusters = cluster_nodes([a, b], avoid=("Old",))
    plan = build_bin_plan(clusters)
    assert [e.handle for e in plan] == ["old"]
    assert plan[0].kept_handle == "new"
