from megaclean.dupes import Cluster, Node
from megaclean.folders import analyse_folders, write_folder_plan


def n(path, size=100, sig="s", node_id=None):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=None, height=None, mtime="t",
                node_id=node_id or path)


def cluster(*members):
    return Cluster("exact", tuple(members), members[0])


def test_folder_wholly_duplicated_elsewhere_is_reported_as_collapsible():
    a1, a2 = n("Old/1.jpg"), n("New/1.jpg")
    b1, b2 = n("Old/2.jpg"), n("New/2.jpg")
    nodes = [a1, a2, b1, b2]
    out = {f.folder: f for f in analyse_folders([cluster(a1, a2),
                                                 cluster(b1, b2)], nodes,
                                                min_files=1)}
    old = out["Old"]
    assert old.unique_files == 0
    assert old.coverage == 1.0
    assert old.counterparts[0][0] == "New"


def test_unique_files_are_counted_and_never_hidden():
    """The whole point: what would be lost if the folder were deleted."""
    a1, a2 = n("Old/1.jpg"), n("New/1.jpg")
    only = n("Old/only-here.jpg", size=555)
    out = {f.folder: f for f in analyse_folders([cluster(a1, a2)],
                                                [a1, a2, only], min_files=1)}
    old = out["Old"]
    assert old.unique_files == 1
    assert old.unique_bytes == 555
    assert old.coverage == 0.5


def test_duplication_inside_one_folder_is_not_counted_as_elsewhere():
    """Two copies in the same folder do not make that folder redundant."""
    a1 = n("Same/1.jpg", node_id="h1")
    a2 = n("Same/1.jpg", node_id="h2")
    out = {f.folder: f for f in analyse_folders([cluster(a1, a2)], [a1, a2],
                                                min_files=1)}
    assert out["Same"].unique_files == 2
    assert out["Same"].coverage == 0.0


def test_counterparts_are_ranked_by_shared_bytes():
    a = n("Old/1.jpg", size=10)
    big = n("Big/1.jpg", size=10)
    b = n("Old/2.jpg", size=900)
    big2 = n("Big/2.jpg", size=900)
    c = n("Old/3.jpg", size=5)
    small = n("Small/3.jpg", size=5)
    out = {f.folder: f for f in analyse_folders(
        [cluster(a, big), cluster(b, big2), cluster(c, small)],
        [a, big, b, big2, c, small], min_files=1)}
    assert out["Old"].counterparts[0][0] == "Big"


def test_small_folders_are_filtered_out():
    a1, a2 = n("Tiny/1.jpg"), n("Other/1.jpg")
    assert analyse_folders([cluster(a1, a2)], [a1, a2], min_files=5) == []


def test_results_are_ranked_by_reclaimable_space():
    a1, a2 = n("Small/1.jpg", size=10), n("X/1.jpg", size=10)
    b1, b2 = n("Large/1.jpg", size=9000), n("Y/1.jpg", size=9000)
    out = analyse_folders([cluster(a1, a2), cluster(b1, b2)],
                          [a1, a2, b1, b2], min_files=1)
    assert out[0].folder in ("Large", "Y")


def test_the_folder_holding_unique_content_survives_by_default(tmp_path):
    """Collapsing toward the richer side means nothing has to move."""
    a1, a2 = n("Old/1.jpg"), n("New/1.jpg")
    only = n("Old/keepme.jpg", size=555)
    overlaps = analyse_folders([cluster(a1, a2)], [a1, a2, only], min_files=1)
    out = tmp_path / "plan.md"
    write_folder_plan(overlaps, out)
    text = out.read_text()
    assert "`New`  \u2192  `Old`" in text
    assert "Safe to remove `New`" in text


def test_plan_names_the_unique_files_when_a_move_is_required(tmp_path):
    """Forcing the direction against the richer folder must call out exactly
    what would be lost."""
    a1, a2 = n("Old/1.jpg"), n("New/1.jpg")
    only = n("Old/keepme.jpg", size=555)
    overlaps = analyse_folders([cluster(a1, a2)], [a1, a2, only], min_files=1)
    out = tmp_path / "plan.md"
    write_folder_plan(overlaps, out, prefer=("New",))
    text = out.read_text()
    assert "`Old`  \u2192  `New`" in text
    assert "keepme.jpg" in text
    assert "move" in text.lower()


def test_plan_marks_a_folder_with_nothing_unique_as_safe_to_collapse(tmp_path):
    a1, a2 = n("Old/1.jpg"), n("New/1.jpg")
    overlaps = analyse_folders([cluster(a1, a2)], [a1, a2], min_files=1)
    out = tmp_path / "plan.md"
    write_folder_plan(overlaps, out)
    assert "nothing unique" in out.read_text().lower()


def test_destination_is_the_canonical_folder_not_the_bigger_one():
    """The survivor must be the curated library even when the inbox holds more."""
    from megaclean.folders import merge_decisions
    pairs = [n(f"Inbox/{i}.jpg", size=100) for i in range(3)]
    lib = [n(f"Library/{i}.jpg", size=100) for i in range(3)]
    clusters = [cluster(a, b) for a, b in zip(pairs, lib)]
    extra = n("Inbox/only.jpg", size=50)
    overlaps = analyse_folders(clusters, pairs + lib + [extra], min_files=1)
    decisions = merge_decisions(overlaps, prefer=("Library",))
    assert len(decisions) == 1
    assert decisions[0].destination == "Library"
    assert decisions[0].source == "Inbox"


def test_reciprocal_pairs_produce_one_decision_not_two():
    from megaclean.folders import merge_decisions
    a = [n(f"A/{i}.jpg") for i in range(4)]
    b = [n(f"B/{i}.jpg") for i in range(4)]
    clusters = [cluster(x, y) for x, y in zip(a, b)]
    decisions = merge_decisions(analyse_folders(clusters, a + b, min_files=1))
    assert len(decisions) == 1
    assert {decisions[0].source, decisions[0].destination} == {"A", "B"}


def test_without_prefer_the_folder_with_more_unique_content_survives():
    """Collapsing toward the richer folder means moving fewer files."""
    from megaclean.folders import merge_decisions
    a = [n(f"Thin/{i}.jpg") for i in range(3)]
    b = [n(f"Rich/{i}.jpg") for i in range(3)]
    clusters = [cluster(x, y) for x, y in zip(a, b)]
    rich_only = [n(f"Rich/extra{i}.jpg") for i in range(5)]
    decisions = merge_decisions(
        analyse_folders(clusters, a + b + rich_only, min_files=1))
    assert decisions[0].destination == "Rich"
    assert decisions[0].source == "Thin"


def test_decision_reports_what_must_move_before_collapsing():
    from megaclean.folders import merge_decisions
    a = [n(f"Old/{i}.jpg") for i in range(3)]
    b = [n(f"New/{i}.jpg") for i in range(3)]
    only = n("Old/precious.jpg", size=777)
    decisions = merge_decisions(
        analyse_folders([cluster(x, y) for x, y in zip(a, b)],
                        a + b + [only], min_files=1), prefer=("New",))
    d = decisions[0]
    assert d.must_move_files == 1
    assert d.must_move_bytes == 777
    assert "precious.jpg" in d.must_move_examples


def test_plan_never_recommends_collapsing_the_canonical_folder(tmp_path):
    a = [n(f"Camera uploads/{i}.jpg") for i in range(3)]
    b = [n(f"Library/Camera/{i}.jpg") for i in range(3)]
    overlaps = analyse_folders([cluster(x, y) for x, y in zip(a, b)],
                               a + b, min_files=1)
    out = tmp_path / "p.md"
    write_folder_plan(overlaps, out, prefer=("Library",))
    text = out.read_text()
    assert "into `Camera uploads`" not in text
    assert "`Library/Camera`" in text


def test_an_inbox_is_drained_never_removed(tmp_path):
    """A phone's auto-sync target refills itself; deleting the folder is wrong
    advice. Its duplicated files can go; its unfiled ones are a backlog."""
    from megaclean.folders import merge_decisions
    dup_a = [n(f"Inbox/{i}.jpg", size=100) for i in range(3)]
    dup_b = [n(f"Library/{i}.jpg", size=100) for i in range(3)]
    backlog = [n(f"Inbox/new{i}.jpg", size=50) for i in range(4)]
    overlaps = analyse_folders([cluster(a, b) for a, b in zip(dup_a, dup_b)],
                               dup_a + dup_b + backlog, min_files=1)
    d = merge_decisions(overlaps, prefer=("Library",), inbox=("Inbox",))[0]
    assert d.source_is_inbox
    assert d.must_move_files == 0          # a backlog is not a blocker
    assert d.backlog_files == 4

    out = tmp_path / "p.md"
    write_folder_plan(overlaps, out, prefer=("Library",), inbox=("Inbox",))
    text = out.read_text()
    assert "remove `Inbox`" not in text
    assert "still to file" in text.lower()


def test_chained_decisions_are_flagged_so_ordering_cannot_destroy_files():
    """If B is removed by one decision but is the destination of another,
    doing them in the listed order moves files into a deleted folder."""
    from megaclean.folders import merge_decisions, chained_decisions
    # A collapses into B (B is bigger); B collapses into C (C has unique
    # content and shares more bytes with B than A does).
    ab = [n(f"A/{i}.jpg", size=10) for i in range(3)]
    bb = [n(f"B/{i}.jpg", size=10) for i in range(3)]
    bc = [n(f"B/x{i}.jpg", size=900) for i in range(3)]
    cc = [n(f"C/x{i}.jpg", size=900) for i in range(3)]
    c_only = [n(f"C/only{i}.jpg", size=5) for i in range(2)]
    clusters = ([cluster(x, y) for x, y in zip(ab, bb)]
                + [cluster(x, y) for x, y in zip(bc, cc)])
    decisions = merge_decisions(
        analyse_folders(clusters, ab + bb + bc + cc + c_only, min_files=1))
    chains = chained_decisions(decisions)
    destinations = {d.destination for d in decisions}
    sources = {d.source for d in decisions}
    assert destinations & sources                # the setup really is chained
    assert chains


def test_independent_decisions_are_not_flagged():
    from megaclean.folders import merge_decisions, chained_decisions
    a = [n(f"A/{i}.jpg") for i in range(3)]
    b = [n(f"B/{i}.jpg") for i in range(3)]
    c = [n(f"C/{i}.jpg", size=5) for i in range(3)]
    d = [n(f"D/{i}.jpg", size=5) for i in range(3)]
    clusters = ([cluster(x, y) for x, y in zip(a, b)]
                + [cluster(x, y) for x, y in zip(c, d)])
    decisions = merge_decisions(analyse_folders(clusters, a + b + c + d,
                                                min_files=1))
    assert chained_decisions(decisions) == []
