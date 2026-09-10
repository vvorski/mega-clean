import json

from megaclean.dupes import Node
from megaclean.planner import build_plan, read_plan, write_plan


def node(path, sig="s", **kw):
    base = dict(size=100, exif_key=None, phash=None, phash_src=None,
                width=None, height=None, mtime="2024-01-01")
    base.update(kw)
    return Node(path=path, sig=sig, **base)


def test_missing_file_is_planned_for_upload():
    plan = build_plan([node("a.jpg", "sig-a")], [], local_root="/pics",
                      dest_root="Photos")
    assert len(plan) == 1
    assert plan[0].action == "upload"
    assert plan[0].dest_path == "Photos/a.jpg"
    assert plan[0].remote_matches == ()


def test_nested_paths_are_mirrored():
    plan = build_plan([node("2024/spring/a.jpg", "sig-a")], [],
                      local_root="/pics", dest_root="Photos")
    assert plan[0].dest_path == "Photos/2024/spring/a.jpg"


def test_exact_match_anywhere_in_the_account_is_skipped():
    local = [node("a.jpg", "same")]
    remote = [node("Backup/old/renamed.jpg", "same")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "skip"
    assert plan[0].remote_matches == ("Backup/old/renamed.jpg",)


def test_skip_records_every_place_the_content_lives():
    local = [node("a.jpg", "same")]
    remote = [node("x/1.jpg", "same"), node("y/2.jpg", "same")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert sorted(plan[0].remote_matches) == ["x/1.jpg", "y/2.jpg"]


def test_exif_match_is_review_not_skip():
    local = [node("a.jpg", "sig-a", exif_key="canon|r5|2024")]
    remote = [node("Photos/b.jpg", "sig-b", exif_key="canon|r5|2024")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "review"
    assert plan[0].remote_matches == ("Photos/b.jpg",)
    assert "EXIF" in plan[0].reason


def test_phash_match_is_review_within_same_provenance():
    local = [node("a.jpg", "sig-a", phash="0000000000000000", phash_src="thumb")]
    remote = [node("Photos/b.jpg", "sig-b", phash="0000000000000003",
                   phash_src="thumb")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "review"


def test_phash_across_provenance_is_ignored():
    local = [node("a.jpg", "sig-a", phash="0000000000000000", phash_src="thumb")]
    remote = [node("Photos/b.jpg", "sig-b", phash="0000000000000000",
                   phash_src="full")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "upload"


def test_exact_match_wins_over_variant_match():
    local = [node("a.jpg", "same", exif_key="k")]
    remote = [node("x.jpg", "same", exif_key="k"),
              node("y.jpg", "other", exif_key="k")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "skip"


def test_plan_round_trips_through_json(tmp_path):
    entries = build_plan([node("a.jpg", "sig-a")], [], local_root="/pics",
                         dest_root="Photos")
    out = tmp_path / "plan.json"
    write_plan(entries, out, local_root="/pics", dest_root="Photos",
               remote="mega")
    loaded, header = read_plan(out)
    assert loaded == entries
    assert header["local_root"] == "/pics"
    assert header["remote"] == "mega"
    assert json.loads(out.read_text())["entries"][0]["action"] == "upload"
