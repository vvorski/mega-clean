import csv

from megaclean.dupes import Cluster, Node
from megaclean.report import summarize, write_csv, write_html


def n(path, size=100, sig="s"):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=10, height=10, mtime="2024-01-01")


def clusters():
    a, b = n("a.jpg", 500, "same"), n("b.jpg", 500, "same")
    c, d = n("c.jpg", 300, "x"), n("d.jpg", 200, "y")
    return [Cluster("exact", (a, b), a), Cluster("variant", (c, d), c)]


def test_summary_counts_redundancy_not_totals():
    s = summarize(clusters())
    assert s["clusters"] == 2
    assert s["exact_clusters"] == 1
    assert s["variant_clusters"] == 1
    assert s["redundant_files"] == 2
    assert s["redundant_bytes"] == 700


def test_csv_marks_keeper_and_duplicate_roles(tmp_path):
    out = tmp_path / "r.csv"
    write_csv(clusters(), out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 4
    assert {r["role"] for r in rows} == {"keeper", "duplicate"}
    keepers = [r["path"] for r in rows if r["role"] == "keeper"]
    assert sorted(keepers) == ["a.jpg", "c.jpg"]
    assert len({r["cluster_id"] for r in rows}) == 2


def test_html_is_self_contained_and_escapes_paths(tmp_path):
    out = tmp_path / "r.html"
    weird = Node(path="<script>x</script>.jpg", size=1, sig="z", exif_key=None,
                 phash=None, phash_src=None, width=1, height=1, mtime="t")
    write_html([Cluster("exact", (weird, n("ok.jpg")), weird)], out)
    text = out.read_text()
    assert "<script>x</script>.jpg" not in text
    assert "&lt;script&gt;" in text
    assert "http://" not in text and "https://" not in text
    assert "exact" in text


def test_empty_report_is_valid(tmp_path):
    write_csv([], tmp_path / "r.csv")
    write_html([], tmp_path / "r.html")
    assert summarize([])["clusters"] == 0
    assert "No duplicates" in (tmp_path / "r.html").read_text()
