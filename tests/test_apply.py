"""Executor for the operations manifest, tested against an in-memory MEGA."""
import pytest

from megaclean.apply import apply_operations
from megaclean.ops import Operation


class FakeMega:
    """Just enough of the API: a node tree, moves, and the bin."""

    def __init__(self):
        self.nodes = {"cloud": {"h": "cloud", "p": None, "t": 2},
                      "bin": {"h": "bin", "p": None, "t": 4}}
        self.moves = []

    def folder(self, h, parent="cloud", name=None):
        self.nodes[h] = {"h": h, "p": parent, "t": 1, "name": name or h}
        return h

    def file(self, h, parent, name):
        self.nodes[h] = {"h": h, "p": parent, "t": 0, "name": name}
        return h

    def fetch_nodes(self):
        return [dict(n) for n in self.nodes.values()]

    def node_names(self, nodes):
        return {n["h"]: n.get("name") for n in nodes}

    def move_nodes(self, handles, target):
        out = {}
        for h in handles:
            if h in self.nodes and target in self.nodes:
                self.nodes[h]["p"] = target; out[h] = 0
            else:
                out[h] = -9
            self.moves.append((h, target))
        return out

    def move_to_rubbish(self, handles, rubbish, **k):
        assert rubbish == "bin"
        return self.move_nodes(handles, "bin")


def _setup():
    m = FakeMega()
    old, new = m.folder("Old"), m.folder("New")
    m.file("o1", old, "1.jpg"); m.file("n1", new, "1.jpg")       # identical pair
    m.file("only", old, "only.jpg")                               # unique to Old
    ops = [
        Operation("MOVE", "Old/only.jpg", "only", 5, destination="New",
                  destination_handle=new),
        Operation("BIN", "Old/1.jpg", "o1", 5, survivor="New/1.jpg",
                  survivor_handle="n1"),
        Operation("REMOVE_FOLDER", "Old", old),
    ]
    return m, ops


def test_dry_run_touches_nothing():
    m, ops = _setup()
    r = apply_operations(m, ops, rubbish="bin", dry_run=True)
    assert m.moves == []
    assert (r.moved, r.binned, r.removed) == (1, 1, 1)


def test_full_run_moves_bins_and_removes_in_order():
    m, ops = _setup()
    r = apply_operations(m, ops, rubbish="bin")
    assert [x[0] for x in m.moves] == ["only", "o1", "Old"]
    assert m.nodes["only"]["p"] == "New"
    assert m.nodes["o1"]["p"] == "bin"
    assert m.nodes["Old"]["p"] == "bin"
    assert r.failed == 0


def test_folder_is_not_removed_if_the_live_tree_shows_it_non_empty():
    """The index may never have seen a file; only the live tree can prove
    emptiness."""
    m, ops = _setup()
    m.file("ghost", "Old", "unindexed.jpg")
    r = apply_operations(m, ops, rubbish="bin")
    assert m.nodes["Old"]["p"] == "cloud"
    assert r.removed == 0
    assert any("not empty" in e[1] for e in r.errors)


def test_folder_is_not_removed_if_it_has_a_subfolder():
    m, ops = _setup()
    m.folder("sub", parent="Old")
    apply_operations(m, ops, rubbish="bin")
    assert m.nodes["Old"]["p"] == "cloud"


def test_bin_refuses_when_survivor_is_not_live():
    m, ops = _setup()
    m.nodes["n1"]["p"] = "bin"                  # survivor already gone
    r = apply_operations(m, ops, rubbish="bin")
    assert m.nodes["o1"]["p"] == "Old"
    assert any("survivor" in e[1] for e in r.errors)


def test_move_refuses_a_live_name_collision_at_execution_time():
    m, ops = _setup()
    m.file("clash", "New", "only.jpg")          # appeared after the manifest
    r = apply_operations(m, ops, rubbish="bin")
    assert m.nodes["only"]["p"] == "Old"
    assert any("already holds" in e[1] for e in r.errors)
    assert m.nodes["Old"]["p"] == "cloud"       # and so the folder stays


def test_move_refuses_a_destination_that_is_itself_being_removed():
    m, ops = _setup()
    ops.append(Operation("REMOVE_FOLDER", "New", "New"))
    with pytest.raises(ValueError, match="destination"):
        apply_operations(m, ops, rubbish="bin")


def test_stale_manifest_entries_are_skipped_not_guessed():
    m, ops = _setup()
    m.nodes["only"]["p"] = "New"                # someone moved it already
    m.nodes["only"]["name"] = "only.jpg"
    r = apply_operations(m, ops, rubbish="bin")
    assert not any(h == "only" for h, _ in m.moves)
    assert any("not where the manifest" in e[1] for e in r.errors)


def test_phase_filter_runs_only_the_requested_kind():
    m, ops = _setup()
    apply_operations(m, ops, rubbish="bin", phases=("BIN",))
    assert [x[0] for x in m.moves] == ["o1"]


def test_conflict_and_unverified_are_never_executed():
    m, ops = _setup()
    ops.append(Operation("CONFLICT", "Old/x.jpg", "only", destination="New"))
    ops.append(Operation("UNVERIFIED", "Old/y.jpg", "o1", destination="New"))
    apply_operations(m, ops, rubbish="bin", phases=("CONFLICT", "UNVERIFIED"))
    assert m.moves == []
