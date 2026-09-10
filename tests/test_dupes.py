from megaclean.dupes import BKTree, Node, choose_keeper, cluster_nodes


def node(path, **kw):
    base = dict(size=100, sig="s-" + path, exif_key=None, phash=None,
                phash_src=None, width=None, height=None, mtime="2024-01-01",
                crc=None)
    base.update(kw)
    return Node(path=path, **base)


def test_bktree_finds_near_neighbours():
    tree = BKTree()
    for h in ("0000000000000000", "0000000000000003", "ffffffffffffffff"):
        tree.add(h)
    near = tree.query("0000000000000000", 6)
    assert set(near) == {"0000000000000000", "0000000000000003"}


def test_bktree_empty_query_is_safe():
    assert BKTree().query("00", 6) == []


def test_identical_signatures_form_an_exact_cluster():
    nodes = [node("a.jpg", sig="same"), node("b.jpg", sig="same"),
             node("c.jpg", sig="other")]
    clusters = cluster_nodes(nodes)
    assert len(clusters) == 1
    assert clusters[0].kind == "exact"
    assert {n.path for n in clusters[0].members} == {"a.jpg", "b.jpg"}


def test_singletons_are_not_reported():
    assert cluster_nodes([node("a.jpg"), node("b.jpg")]) == []


def test_shared_exif_key_forms_a_variant_cluster():
    nodes = [node("a.jpg", exif_key="canon|r5|2024", size=900),
             node("b.jpg", exif_key="canon|r5|2024", size=300)]
    clusters = cluster_nodes(nodes)
    assert len(clusters) == 1
    assert clusters[0].kind == "variant"


def test_close_phash_forms_a_variant_cluster():
    nodes = [node("a.jpg", phash="0000000000000000", phash_src="thumb"),
             node("b.jpg", phash="0000000000000003", phash_src="thumb")]
    assert cluster_nodes(nodes)[0].kind == "variant"


def test_phash_across_provenance_is_never_compared():
    nodes = [node("a.jpg", phash="0000000000000000", phash_src="thumb"),
             node("b.jpg", phash="0000000000000000", phash_src="full")]
    assert cluster_nodes(nodes) == []


def test_distant_phash_does_not_cluster():
    nodes = [node("a.jpg", phash="0000000000000000", phash_src="thumb"),
             node("b.jpg", phash="ffffffffffffffff", phash_src="thumb")]
    assert cluster_nodes(nodes) == []


def test_exact_plus_variant_merges_into_one_variant_cluster():
    nodes = [node("a.jpg", sig="same", exif_key="k"),
             node("b.jpg", sig="same", exif_key="k"),
             node("c.jpg", sig="different", exif_key="k")]
    clusters = cluster_nodes(nodes)
    assert len(clusters) == 1
    assert clusters[0].kind == "variant"
    assert len(clusters[0].members) == 3


def test_keeper_prefers_largest_pixel_area():
    a = node("a.jpg", width=100, height=100, size=999999)
    b = node("b.jpg", width=400, height=400, size=10)
    assert choose_keeper([a, b]).path == "b.jpg"


def test_keeper_falls_back_to_size_then_mtime_then_depth():
    a = node("deep/nested/a.jpg", size=100, mtime="2024-01-02")
    b = node("b.jpg", size=100, mtime="2024-01-01")
    assert choose_keeper([a, b]).path == "b.jpg"
    c = node("x/c.jpg", size=100, mtime="2024-01-01")
    d = node("d.jpg", size=100, mtime="2024-01-01")
    assert choose_keeper([c, d]).path == "d.jpg"


def test_keeper_is_deterministic_regardless_of_input_order():
    nodes = [node(f"{c}.jpg", size=100, mtime="2024-01-01") for c in "abcd"]
    assert choose_keeper(nodes).path == choose_keeper(list(reversed(nodes))).path


def test_nodes_from_rows_skips_unfingerprinted():
    from megaclean.dupes import nodes_from_rows
    rows = [
        {"path": "a.jpg", "size": 10, "sig": "s", "exif_key": None,
         "phash": None, "phash_src": None, "width": None, "height": None,
         "mtime": "t"},
        {"path": "b.jpg", "size": 10, "sig": None, "exif_key": None,
         "phash": None, "phash_src": None, "width": None, "height": None,
         "mtime": "t"},
    ]
    nodes = nodes_from_rows(rows)
    assert [n.path for n in nodes] == ["a.jpg"]


def test_exact_cluster_has_one_exact_group():
    nodes = [node("a.jpg", sig="same"), node("b.jpg", sig="same")]
    groups = cluster_nodes(nodes)[0].exact_groups
    assert len(groups) == 1
    assert [n.path for n in groups[0]] == ["a.jpg", "b.jpg"]


def test_variant_cluster_surfaces_its_byte_identical_subgroup():
    """A resize sitting alongside two identical copies must not hide the fact
    that those two are provably the same bytes."""
    nodes = [node("beach.jpg", sig="same", exif_key="k"),
             node("beach-copy.jpg", sig="same", exif_key="k"),
             node("beach-small.jpg", sig="resized", exif_key="k")]
    cluster = cluster_nodes(nodes)[0]
    assert cluster.kind == "variant"
    assert len(cluster.exact_groups) == 1
    assert [n.path for n in cluster.exact_groups[0]] == ["beach-copy.jpg",
                                                         "beach.jpg"]


def test_multiple_exact_groups_within_one_variant_cluster():
    nodes = [node("a1.jpg", sig="A", exif_key="k"),
             node("a2.jpg", sig="A", exif_key="k"),
             node("b1.jpg", sig="B", exif_key="k"),
             node("b2.jpg", sig="B", exif_key="k")]
    groups = cluster_nodes(nodes)[0].exact_groups
    assert len(groups) == 2
    assert [[n.path for n in g] for g in groups] == [["a1.jpg", "a2.jpg"],
                                                     ["b1.jpg", "b2.jpg"]]


def test_variant_cluster_with_no_identical_members_has_no_exact_groups():
    nodes = [node("a.jpg", sig="A", exif_key="k"),
             node("b.jpg", sig="B", exif_key="k")]
    assert cluster_nodes(nodes)[0].exact_groups == ()


def test_fingerprint_only_match_is_identical_not_exact():
    """A CRC match is a candidate, not a proof; the report must say which."""
    nodes = [node("a.jpg", sig="", crc="ABC"), node("b.jpg", sig="", crc="ABC")]
    cluster = cluster_nodes(nodes)[0]
    assert cluster.kind == "identical"
    assert len(cluster.members) == 2


def test_byte_verified_match_outranks_the_fingerprint():
    nodes = [node("a.jpg", sig="same", crc="ABC"),
             node("b.jpg", sig="same", crc="ABC")]
    assert cluster_nodes(nodes)[0].kind == "exact"


def test_fingerprint_agreeing_but_bytes_differing_is_a_variant():
    """The measured 1-in-80 CRC false positive must not be reported as exact."""
    nodes = [node("a.jpg", sig="one", crc="ABC"),
             node("b.jpg", sig="two", crc="ABC")]
    assert cluster_nodes(nodes)[0].kind == "variant"


def test_variant_cluster_keeps_one_of_each_distinct_photo():
    """Burst frames cluster together because they share EXIF, but they are
    different pictures. Marking all but one 'duplicate' would destroy them."""
    frames = [
        node("burst1-a.jpg", sig="A", exif_key="k"),
        node("burst1-b.jpg", sig="A", exif_key="k"),
        node("burst2-a.jpg", sig="B", exif_key="k"),
        node("burst2-b.jpg", sig="B", exif_key="k"),
        node("burst3.jpg", sig="C", exif_key="k"),
    ]
    cluster = cluster_nodes(frames)[0]
    assert cluster.kind == "variant"
    # One keeper per distinct content, not one per cluster.
    assert len(cluster.keeper_keys) == 3
    redundant = [m for m in cluster.members if m.key not in cluster.keeper_keys]
    assert len(redundant) == 2
    kept_sigs = {m.sig for m in cluster.members if m.key in cluster.keeper_keys}
    assert kept_sigs == {"A", "B", "C"}


def test_a_unique_photo_in_a_variant_cluster_is_never_a_duplicate():
    nodes = [node("a.jpg", sig="A", exif_key="k"),
             node("b.jpg", sig="B", exif_key="k")]
    cluster = cluster_nodes(nodes)[0]
    assert len(cluster.keeper_keys) == 2      # nothing is redundant here


def test_exact_cluster_still_keeps_exactly_one():
    nodes = [node("a.jpg", sig="same"), node("b.jpg", sig="same"),
             node("c.jpg", sig="same")]
    assert len(cluster_nodes(nodes)[0].keeper_keys) == 1


def test_primary_keeper_is_among_the_keepers():
    nodes = [node("a.jpg", sig="A", exif_key="k"),
             node("b.jpg", sig="B", exif_key="k")]
    c = cluster_nodes(nodes)[0]
    assert c.keeper.key in c.keeper_keys


def test_prefer_keeps_the_copy_in_the_canonical_folder():
    """An auto-syncing inbox is machine-managed; the keeper belongs in the
    curated library even when the inbox path is shorter."""
    inbox = node("Camera uploads/a.jpg", sig="same")
    library = node("Library/2024/a.jpg", sig="same")
    cluster = cluster_nodes([inbox, library], prefer=("Library",))[0]
    assert cluster.keeper.path == "Library/2024/a.jpg"


def test_without_prefer_the_shallower_path_still_wins():
    inbox = node("Camera uploads/a.jpg", sig="same")
    library = node("Library/2024/a.jpg", sig="same")
    assert cluster_nodes([inbox, library])[0].keeper.path == "Camera uploads/a.jpg"


def test_prefer_order_breaks_ties_between_two_preferred_folders():
    a = node("First/a.jpg", sig="same")
    b = node("Second/a.jpg", sig="same")
    cluster = cluster_nodes([a, b], prefer=("Second", "First"))[0]
    assert cluster.keeper.path == "Second/a.jpg"


def test_prefer_applies_to_every_distinct_photo_in_a_variant_cluster():
    nodes = [node("Camera uploads/x.jpg", sig="A", exif_key="k"),
             node("Library/x.jpg", sig="A", exif_key="k"),
             node("Camera uploads/y.jpg", sig="B", exif_key="k"),
             node("Library/y.jpg", sig="B", exif_key="k")]
    cluster = cluster_nodes(nodes, prefer=("Library",))[0]
    kept = {m.path for m in cluster.members if m.key in cluster.keeper_keys}
    assert kept == {"Library/x.jpg", "Library/y.jpg"}


def test_prefer_never_overrides_image_quality():
    """A bigger version outside the preferred folder is still the better copy."""
    small = node("Library/a.jpg", sig="A", exif_key="k", width=100, height=100)
    big = node("Camera uploads/a.jpg", sig="B", exif_key="k", width=900, height=900)
    keeper = choose_keeper([small, big], prefer=("Library",))
    assert keeper.path == "Camera uploads/a.jpg"
