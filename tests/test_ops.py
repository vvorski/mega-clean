from megaclean.dupes import Node
from megaclean.ops import build_operations, resolve_destination, write_operations


def n(path, sig="s", crc="c", node_id=None, size=100, parent=None):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=None, height=None, mtime="t", crc=crc,
                node_id=node_id or path, parent_id=parent or ("dir:" + path.rsplit("/", 1)[0]))


def test_resolve_destination_follows_a_chain_to_the_final_survivor():
    edges = {"A": "B", "B": "C"}
    assert resolve_destination("A", edges) == "C"
    assert resolve_destination("C", edges) == "C"


def test_resolve_destination_survives_a_cycle():
    edges = {"A": "B", "B": "A"}
    assert resolve_destination("A", edges) in ("A", "B")


def _inbox_and_library():
    # Inbox holds 2 files also in Library, plus 1 unfiled.
    lib = [n(f"Lib/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(2)]
    inb = [n(f"Inbox/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(2)]
    unfiled = n("Inbox/new.jpg", sig="u", crc="u")
    return lib + inb + [unfiled]


def test_inbox_copies_are_binned_and_the_backlog_is_left_alone():
    ops = build_operations(_inbox_and_library(), prefer=("Lib",),
                           inbox=("Inbox",), min_files=1)
    bins = [o for o in ops if o.kind == "BIN"]
    moves = [o for o in ops if o.kind == "MOVE"]
    assert sorted(o.path for o in bins) == ["Inbox/0.jpg", "Inbox/1.jpg"]
    assert all(o.survivor.startswith("Lib/") for o in bins)
    assert moves == []                          # backlog is not an operation
    assert not any(o.kind == "REMOVE_FOLDER" and o.path == "Inbox" for o in ops)


def _mutual_backup():
    # Old and New share 3 files; Old also has one unique. New is canonical.
    old = [n(f"Old/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(3)]
    new = [n(f"New/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(3)]
    only = n("Old/only.jpg", sig="o", crc="o")
    return old + new + [only]


def test_removed_folder_moves_its_unique_files_then_bins_the_rest():
    ops = build_operations(_mutual_backup(), prefer=("New",), min_files=1)
    moves = [o for o in ops if o.kind == "MOVE"]
    bins = [o for o in ops if o.kind == "BIN"]
    assert [(o.path, o.destination) for o in moves] == [("Old/only.jpg", "New")]
    assert sorted(o.path for o in bins) == ["Old/0.jpg", "Old/1.jpg", "Old/2.jpg"]
    assert all(o.survivor.startswith("New/") for o in bins)


def test_keepers_never_sit_in_a_removed_folder():
    """Without this, the folder plan would say 'remove Old' while the file
    plan kept Old's copy and binned New's."""
    old = [n(f"Old/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(3)]
    new = [n(f"New/deep/deeper/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(3)]
    new_only = [n(f"New/deep/deeper/x{i}.jpg", sig=f"x{i}", crc=f"x{i}") for i in range(2)]
    ops = build_operations(old + new + new_only, min_files=1)   # no prefer
    bins = [o for o in ops if o.kind == "BIN"]
    assert all(o.path.startswith("Old/") for o in bins), [o.path for o in bins]


def test_folder_is_removed_only_when_everything_in_it_is_handled():
    ops = build_operations(_mutual_backup(), prefer=("New",), min_files=1)
    kinds = [o.kind for o in ops]
    assert kinds.index("REMOVE_FOLDER") > max(i for i, k in enumerate(kinds) if k in ("MOVE", "BIN"))
    rm = [o for o in ops if o.kind == "REMOVE_FOLDER"]
    assert [o.path for o in rm] == ["Old"]


def test_move_into_a_folder_holding_a_different_same_named_file_is_a_conflict():
    old = [n(f"Old/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(3)]
    new = [n(f"New/{i}.jpg", sig=f"s{i}", crc=f"c{i}") for i in range(3)]
    clash_old = n("Old/DSC.jpg", sig="p", crc="p")           # unique to Old
    clash_new = n("New/DSC.jpg", sig="q", crc="q")           # different photo, same name
    ops = build_operations(old + new + [clash_old, clash_new], prefer=("New",),
                           min_files=1)
    conflicts = [o for o in ops if o.kind == "CONFLICT"]
    assert [o.path for o in conflicts] == ["Old/DSC.jpg"]
    assert not any(o.kind == "MOVE" and o.path == "Old/DSC.jpg" for o in ops)
    # a folder with an unresolved conflict is not removed
    assert not any(o.kind == "REMOVE_FOLDER" for o in ops)


def test_chain_moves_go_to_the_final_survivor():
    a = [n(f"A/{i}.jpg", sig=f"s{i}", crc=f"c{i}", size=10) for i in range(3)]
    b = [n(f"B/{i}.jpg", sig=f"s{i}", crc=f"c{i}", size=10) for i in range(3)]
    b2 = [n(f"B/x{i}.jpg", sig=f"t{i}", crc=f"d{i}", size=900) for i in range(3)]
    c = [n(f"C/x{i}.jpg", sig=f"t{i}", crc=f"d{i}", size=900) for i in range(3)]
    c_only = [n(f"C/only{i}.jpg", sig=f"o{i}", crc=f"o{i}", size=5) for i in range(2)]
    a_only = n("A/rescue.jpg", sig="r", crc="r")
    # Rank C above B above A so the chain is A -> B -> C by preference.
    ops = build_operations(a + b + b2 + c + c_only + [a_only], min_files=1,
                           prefer=("C", "B"))
    move = next(o for o in ops if o.kind == "MOVE" and o.path == "A/rescue.jpg")
    assert move.destination == "C"


def test_manifest_is_written_in_execution_order(tmp_path):
    ops = build_operations(_mutual_backup(), prefer=("New",), min_files=1)
    out = tmp_path / "ops"
    write_operations(ops, out)
    import json
    payload = json.loads((out.with_suffix(".json")).read_text())
    assert [o["kind"] for o in payload["operations"]] == ["MOVE", "BIN", "BIN", "BIN", "REMOVE_FOLDER"]
    text = out.with_suffix(".md").read_text()
    assert "Old/only.jpg" in text and "New" in text
