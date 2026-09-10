import json

from megaclean.dupes import Cluster, Node
from megaclean.gallery import build_payload, write_gallery


def n(path, size=100, sig="s", crc=None, node_id=""):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=800, height=600, mtime="2024-01-01",
                crc=crc, node_id=node_id or path)


def clusters():
    a, b = n("x/beach.jpg", 500, "same", node_id="h1"), n("y/beach.jpg", 500, "same", node_id="h2")
    c, d = n("c.jpg", 300, "p", node_id="h3"), n("d.jpg", 200, "q", node_id="h4")
    return [Cluster("exact", (a, b), a), Cluster("variant", (c, d), c)]


def test_payload_ranks_groups_by_reclaimable_bytes():
    groups = build_payload(clusters())["groups"]
    assert groups[0]["wasted"] >= groups[1]["wasted"]
    assert groups[0]["wasted"] == 500


def test_payload_keeps_one_copy_of_each_distinct_content():
    groups = {g["kind"]: g for g in build_payload(clusters())["groups"]}
    # Identical copies: keep one.
    assert sum(1 for m in groups["exact"]["members"] if m["keep"]) == 1
    # Different pictures that merely matched as variants: keep them all.
    assert all(m["keep"] for m in groups["variant"]["members"])


def test_payload_distinguishes_same_named_siblings_by_node_id():
    a = n("Photos/a.jpg", 100, "same", node_id="handleA")
    b = n("Photos/a.jpg", 100, "same", node_id="handleB")
    members = build_payload([Cluster("exact", (a, b), a)])["groups"][0]["members"]
    assert {m["id"] for m in members} == {"handleA", "handleB"}
    assert sum(1 for m in members if m["keep"]) == 1


def test_payload_reports_verification_state():
    a = n("a.jpg", 10, "s1", crc="C", node_id="h1")
    b = n("b.jpg", 10, "s1", crc="C", node_id="h2")
    unver_a = n("c.jpg", 10, "", crc="D", node_id="h3")
    unver_b = n("d.jpg", 10, "", crc="D", node_id="h4")
    payload = build_payload([Cluster("exact", (a, b), a),
                             Cluster("identical", (unver_a, unver_b), unver_a)])
    kinds = {g["kind"] for g in payload["groups"]}
    assert kinds == {"exact", "identical"}
    assert payload["totals"]["verified_groups"] == 1
    assert payload["totals"]["unverified_groups"] == 1


def test_totals_count_only_removable_copies(tmp_path):
    t = build_payload(clusters())["totals"]
    assert t["groups"] == 2
    assert t["redundant_files"] == 1
    assert t["reclaimable"] == 500


def test_gallery_writes_a_page_with_embedded_data(tmp_path):
    out = tmp_path / "report"
    write_gallery(clusters(), out, thumbs_dir=None)
    html = (out / "index.html").read_text()
    assert "<!doctype html>" in html.lower()
    assert "beach.jpg" in html
    # The data is embedded so the page works from the filesystem, with no server.
    assert "application/json" in html
    payload = json.loads(html.split('id="data">')[1].split("</script>")[0])
    assert len(payload["groups"]) == 2


def test_gallery_escapes_paths(tmp_path):
    out = tmp_path / "report"
    evil = n("<script>alert(1)</script>.jpg", 10, "z", node_id="h9")
    write_gallery([Cluster("exact", (evil, n("ok.jpg", 10, "z", node_id="h8")), evil)],
                  out, thumbs_dir=None)
    html = (out / "index.html").read_text()
    assert "<script>alert(1)</script>.jpg" not in html


def test_gallery_links_thumbnails_when_present(tmp_path):
    from megaclean.thumbs import thumb_path
    thumbs = tmp_path / "thumbs"
    for nid in ("h1", "h2"):
        p = thumb_path(thumbs, nid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8\xff")
    out = tmp_path / "report"
    write_gallery(clusters(), out, thumbs_dir=thumbs)
    assert (out / "thumbs").exists()
    html = (out / "index.html").read_text()
    payload = json.loads(html.split('id="data">')[1].split("</script>")[0])
    thumbed = [m for g in payload["groups"] for m in g["members"] if m.get("thumb")]
    assert len(thumbed) == 2
    assert all((out / m["thumb"]).exists() for m in thumbed)


def test_identical_group_members_share_the_keeper_picture(tmp_path):
    """Fetching one image per group is enough when the members are the same
    file; the page should still show a picture for each."""
    from megaclean.thumbs import thumb_path
    thumbs = tmp_path / "thumbs"
    p = thumb_path(thumbs, "h1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xff")
    a = n("x/a.jpg", 500, "same", node_id="h1")
    b = n("y/a.jpg", 500, "same", node_id="h2")
    payload = build_payload([Cluster("exact", (a, b), a)], thumbs_dir=thumbs)
    members = payload["groups"][0]["members"]
    assert all(m.get("thumb") for m in members)
    assert [m.get("shared_thumb") for m in members].count(True) == 1


def test_variant_members_never_share_a_picture(tmp_path):
    from megaclean.thumbs import thumb_path
    thumbs = tmp_path / "thumbs"
    p = thumb_path(thumbs, "h1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xff")
    a = n("a.jpg", 500, "one", node_id="h1")
    b = n("b.jpg", 300, "two", node_id="h2")
    payload = build_payload([Cluster("variant", (a, b), a)], thumbs_dir=thumbs)
    members = {m["id"]: m for m in payload["groups"][0]["members"]}
    assert members["h1"].get("thumb")
    assert not members["h2"].get("thumb")
