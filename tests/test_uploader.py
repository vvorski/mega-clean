import pytest

from megaclean.index import iter_nodes, open_index
from megaclean.planner import PlanEntry
from megaclean.remote import FakeRemote, RemoteError
from megaclean.uploader import execute_plan


def entry(local_path, action="upload", dest=None):
    return PlanEntry(local_path=local_path, size=3, sig="sig-" + local_path,
                     action=action, dest_path=dest or f"Photos/{local_path}",
                     remote_matches=(), reason="")


@pytest.fixture
def root(tmp_path, make_jpeg):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / "b.jpg")
    return tmp_path


def test_uploads_only_upload_actions(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg"), entry("b.jpg", "skip")],
                          local_root=root, scope="remote")
    assert (result.uploaded, result.skipped, result.failed) == (1, 1, 0)
    assert remote.uploads == [(str(root / "a.jpg"), "Photos/a.jpg")]


def test_review_entries_are_skipped_by_default(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg", "review")],
                          local_root=root, scope="remote")
    assert result.uploaded == 0 and result.skipped == 1
    assert remote.uploads == []


def test_review_entries_upload_when_explicitly_included(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg", "review")],
                          local_root=root, scope="remote", include_review=True)
    assert result.uploaded == 1
    assert remote.uploads == [(str(root / "a.jpg"), "Photos/a.jpg")]


def test_dry_run_transfers_nothing(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg")], local_root=root,
                          scope="remote", dry_run=True)
    assert result.uploaded == 1
    assert remote.uploads == []


def test_successful_upload_lands_in_the_remote_index(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    execute_plan(conn, remote, [entry("a.jpg")], local_root=root, scope="remote")
    rows = {r["path"]: r for r in iter_nodes(conn, "remote")}
    assert "Photos/a.jpg" in rows
    assert rows["Photos/a.jpg"]["sig"] == "sig-a.jpg"


def test_failure_is_collected_and_the_run_continues(root, tmp_path):
    class Failing(FakeRemote):
        def upload(self, local_path, dest_path):
            if "a.jpg" in local_path:
                raise RemoteError("quota exceeded")
            return super().upload(local_path, dest_path)

    remote, conn = Failing({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg"), entry("b.jpg")],
                          local_root=root, scope="remote")
    assert (result.uploaded, result.failed) == (1, 1)
    assert result.errors[0][0] == "a.jpg"
    assert "quota exceeded" in result.errors[0][1]


def test_missing_local_file_is_an_error_not_a_crash(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("gone.jpg")], local_root=root,
                          scope="remote")
    assert result.failed == 1
    assert remote.uploads == []
