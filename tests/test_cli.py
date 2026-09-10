import json

import pytest

import megaclean.cli as cli
from megaclean.cli import main
from megaclean.index import iter_nodes, open_index
from megaclean.remote import FakeRemote


@pytest.fixture
def account(make_jpeg, tmp_path):
    """A fake MEGA account holding one photo plus a byte-identical copy."""
    src = tmp_path / "src"
    a = make_jpeg(src / "a.jpg", size=(200, 200))
    data = a.read_bytes()
    return FakeRemote({
        "Photos/a.jpg": data,
        "Photos/backup/a-copy.jpg": data,
        # A distinct pixel size guarantees a distinct byte size, so this file
        # is genuinely outside the collision set.
        "Photos/other.jpg": make_jpeg(src / "o.jpg", size=(640, 480),
                                      color=(0, 255, 0),
                                      dt="2024:09:09 09:09:09").read_bytes(),
    })


@pytest.fixture
def factory(account):
    return lambda name: account


@pytest.fixture(autouse=True)
def no_real_server(monkeypatch, account):
    """The served transport must never launch a real rclone in tests."""
    import contextlib

    import megaclean.cli as cli
    monkeypatch.setattr(cli, "ServedRemote",
                        lambda name: contextlib.nullcontext(account))


def test_scan_remote_populates_the_index(tmp_path, factory):
    db = tmp_path / "i.db"
    assert main(["--db", str(db), "scan-remote", "--remote", "mega",
                 "--root", "Photos"], remote_factory=factory) == 0
    paths = [r["path"] for r in iter_nodes(open_index(db), "remote")]
    assert paths == ["Photos/a.jpg", "Photos/backup/a-copy.jpg",
                     "Photos/other.jpg"]


def test_fingerprint_collisions_scope_covers_only_size_collisions(tmp_path,
                                                                  factory):
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "collisions"], remote_factory=factory)
    rows = {r["path"]: r for r in iter_nodes(open_index(db), "remote")}
    assert rows["Photos/a.jpg"]["sig"] == rows["Photos/backup/a-copy.jpg"]["sig"]
    assert rows["Photos/other.jpg"]["sig"] is None   # unique size, never fetched


def test_full_scope_fingerprints_everything(tmp_path, factory):
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    rows = {r["path"]: r for r in iter_nodes(open_index(db), "remote")}
    assert all(r["sig"] for r in rows.values())


def test_dupes_writes_a_report(tmp_path, factory):
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    out = tmp_path / "r.html"
    assert main(["--db", str(db), "dupes", "--out", str(out),
                 "--csv", str(tmp_path / "r.csv")], remote_factory=factory) == 0
    html = out.read_text()
    assert "Photos/a.jpg" in html and "Photos/backup/a-copy.jpg" in html
    assert (tmp_path / "r.csv").exists()


def test_plan_upload_skips_content_already_present(tmp_path, factory, make_jpeg,
                                                   account):
    db = tmp_path / "i.db"
    local = tmp_path / "local"
    local.mkdir()
    (local / "already.jpg").write_bytes(account.files["Photos/a.jpg"])
    make_jpeg(local / "brand-new.jpg", size=(123, 91), dt="2025:01:01 00:00:00")

    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    main(["--db", str(db), "scan-local", str(local)], remote_factory=factory)

    plan = tmp_path / "plan.json"
    assert main(["--db", str(db), "plan-upload", str(local), "--dest-root",
                 "Photos", "--out", str(plan)], remote_factory=factory) == 0
    actions = {e["local_path"]: e["action"]
               for e in json.loads(plan.read_text())["entries"]}
    assert actions["already.jpg"] == "skip"
    assert actions["brand-new.jpg"] == "upload"


def test_upload_executes_the_plan(tmp_path, factory, make_jpeg, account):
    db = tmp_path / "i.db"
    local = tmp_path / "local"
    local.mkdir()
    make_jpeg(local / "brand-new.jpg", size=(123, 91), dt="2025:01:01 00:00:00")
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    main(["--db", str(db), "scan-local", str(local)], remote_factory=factory)
    plan = tmp_path / "plan.json"
    main(["--db", str(db), "plan-upload", str(local), "--dest-root", "Photos",
          "--out", str(plan)], remote_factory=factory)
    assert main(["--db", str(db), "upload", "--plan", str(plan), "--remote",
                 "mega"], remote_factory=factory) == 0
    assert "Photos/brand-new.jpg" in account.files


def test_upload_dry_run_transfers_nothing(tmp_path, factory, make_jpeg, account):
    db = tmp_path / "i.db"
    local = tmp_path / "local"
    local.mkdir()
    make_jpeg(local / "brand-new.jpg", size=(123, 91), dt="2025:01:01 00:00:00")
    main(["--db", str(db), "scan-local", str(local)], remote_factory=factory)
    plan = tmp_path / "plan.json"
    main(["--db", str(db), "plan-upload", str(local), "--dest-root", "Photos",
          "--out", str(plan)], remote_factory=factory)
    main(["--db", str(db), "upload", "--plan", str(plan), "--remote", "mega",
          "--dry-run"], remote_factory=factory)
    assert account.uploads == []


def test_unknown_command_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "i.db"), "nonsense"])
    assert exc.value.code != 0


def test_fingerprint_cat_transport_still_works(tmp_path, factory):
    """The per-process transport remains available as a fallback."""
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    assert main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
                 "Photos", "--scope", "all", "--transport", "cat"],
                remote_factory=factory) == 0
    rows = {r["path"]: r for r in iter_nodes(open_index(db), "remote")}
    assert all(r["sig"] for r in rows.values())
