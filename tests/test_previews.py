from pathlib import Path

from megaclean.dupes import Cluster, Node
from megaclean.previews import preview_targets, fetch_previews
from megaclean.remote import FakeRemote
from megaclean.thumbs import thumb_path


def n(path, size=1000, sig="s", node_id=None):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=None, height=None, mtime="t",
                node_id=node_id or path)


def test_identical_group_needs_only_its_keeper():
    """Members are the same file, so one picture describes the whole group."""
    a, b, c = n("a.jpg"), n("b.jpg"), n("c.jpg")
    targets = preview_targets([Cluster("exact", (a, b, c), a)], Path("/t"))
    assert [t[1] for t in targets] == ["a.jpg"]


def test_variant_group_needs_every_member():
    """Variants differ by definition, so each one has to be seen."""
    a, b = n("a.jpg", sig="1"), n("b.jpg", sig="2")
    targets = preview_targets([Cluster("variant", (a, b), a)], Path("/t"))
    assert sorted(t[1] for t in targets) == ["a.jpg", "b.jpg"]


def test_non_images_are_skipped():
    a, b = n("clip.mp4"), n("clip2.mp4")
    assert preview_targets([Cluster("exact", (a, b), a)], Path("/t")) == []


def test_groups_are_ranked_by_reclaimable_space():
    small = Cluster("exact", (n("s1.jpg", 10), n("s2.jpg", 10)), n("s1.jpg", 10))
    big = Cluster("exact", (n("b1.jpg", 900), n("b2.jpg", 900)), n("b1.jpg", 900))
    targets = preview_targets([small, big], Path("/t"))
    assert targets[0][1] == "b1.jpg"


def test_limit_takes_the_most_valuable_groups():
    groups = [Cluster("exact", (n(f"{i}.jpg", i * 100), n(f"{i}b.jpg", i * 100)),
                      n(f"{i}.jpg", i * 100)) for i in range(1, 6)]
    targets = preview_targets(groups, Path("/t"), limit=2)
    assert len(targets) == 2
    assert targets[0][1] == "5.jpg"


def test_existing_full_size_preview_is_not_refetched(tmp_path):
    a, b = n("a.jpg"), n("b.jpg")
    p = thumb_path(tmp_path, "a.jpg", large=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    assert preview_targets([Cluster("exact", (a, b), a)], tmp_path) == []


def test_grid_only_thumbnail_is_refetched_for_the_full_size_view(tmp_path):
    """A 200px thumbnail made from a head buffer cannot serve the click-through
    view, so those files are fetched again in full."""
    a, b = n("a.jpg"), n("b.jpg")
    p = thumb_path(tmp_path, "a.jpg")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    assert [t[1] for t in preview_targets([Cluster("exact", (a, b), a)],
                                          tmp_path)] == ["a.jpg"]


def test_fetch_writes_thumbnails_from_full_bytes(tmp_path, make_jpeg):
    data = make_jpeg(tmp_path / "src.jpg", size=(900, 700)).read_bytes()
    remote = FakeRemote({"a.jpg": data})
    root = tmp_path / "thumbs"
    ok, failed = fetch_previews(remote, [("a.jpg", "a.jpg", len(data))], root,
                                workers=1)
    assert (ok, failed) == (1, 0)
    assert thumb_path(root, "a.jpg").is_file()


def test_fetch_records_failures_without_raising(tmp_path):
    remote = FakeRemote({})
    ok, failed = fetch_previews(remote, [("gone.jpg", "gone.jpg", 10)],
                                tmp_path / "t", workers=1)
    assert (ok, failed) == (0, 1)


def test_oversized_files_are_capped(tmp_path, make_jpeg):
    data = make_jpeg(tmp_path / "src.jpg", size=(900, 700)).read_bytes()

    class Recording(FakeRemote):
        def __init__(self, files):
            super().__init__(files)
            self.counts = []

        def read_range(self, path, offset, count):
            self.counts.append(count)
            return super().read_range(path, offset, count)

    remote = Recording({"a.jpg": data})
    fetch_previews(remote, [("a.jpg", "a.jpg", len(data))], tmp_path / "t",
                   workers=1, max_bytes=4096)
    assert remote.counts == [4096]
