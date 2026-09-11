"""The crypto is tested against nodes this test encrypts itself, so the whole
decode path is exercised with no account and no network."""
import base64
import json

import pytest
from Crypto.Cipher import AES

from megaclean.megaapi import (
    MegaApi, attribute_key, b64decode, b64encode, decrypt_attributes,
    fingerprint_crc, node_paths,
)

MASTER = bytes(range(16))


def _make_file_node(handle, parent, name, fingerprint=None, size=1000):
    """Encrypt a node the way MEGA does, so decoding it proves the real path."""
    filekey = bytes((i * 7 + 3) % 256 for i in range(32))
    akey = attribute_key(filekey)
    attrs = {"n": name}
    if fingerprint:
        attrs["c"] = fingerprint
    raw = b"MEGA" + json.dumps(attrs).encode()
    raw += b"\0" * (-len(raw) % 16)
    enc_attr = AES.new(akey, AES.MODE_CBC, b"\0" * 16).encrypt(raw)
    enc_key = AES.new(MASTER, AES.MODE_ECB).encrypt(filekey)
    return {"h": handle, "p": parent, "t": 0, "s": size,
            "k": f"me:{b64encode(enc_key)}", "a": b64encode(enc_attr)}


def _make_folder_node(handle, parent, name):
    folderkey = bytes((i * 11 + 5) % 256 for i in range(16))
    raw = b"MEGA" + json.dumps({"n": name}).encode()
    raw += b"\0" * (-len(raw) % 16)
    enc_attr = AES.new(folderkey, AES.MODE_CBC, b"\0" * 16).encrypt(raw)
    enc_key = AES.new(MASTER, AES.MODE_ECB).encrypt(folderkey)
    return {"h": handle, "p": parent, "t": 1,
            "k": f"me:{b64encode(enc_key)}", "a": b64encode(enc_attr)}


def test_b64_round_trip_uses_mega_url_alphabet():
    data = bytes(range(32))
    encoded = b64encode(data)
    assert "+" not in encoded and "/" not in encoded and "=" not in encoded
    assert b64decode(encoded) == data


def test_attribute_key_folds_the_32_byte_file_key():
    key = bytes(range(32))
    folded = attribute_key(key)
    assert len(folded) == 16
    assert folded == bytes(a ^ b for a, b in zip(key[:16], key[16:]))


def test_fingerprint_crc_strips_the_mtime():
    """Two copies of one file with different timestamps share a CRC but not a
    whole fingerprint; grouping must use the CRC alone."""
    crc = bytes(range(16))
    a = b64encode(crc + b"\x01\x02\x03")
    b = b64encode(crc + b"\x09\x08\x07\x06")
    assert fingerprint_crc(a) == fingerprint_crc(b) == crc


def test_fingerprint_crc_rejects_a_short_value():
    assert fingerprint_crc(b64encode(b"tooshort")) is None
    assert fingerprint_crc(None) is None


def test_decrypt_attributes_reads_name_and_fingerprint():
    node = _make_file_node("h1", "root", "photo.jpg", fingerprint=b64encode(bytes(19)))
    cipher = AES.new(MASTER, AES.MODE_ECB)
    attrs = decrypt_attributes(node, cipher)
    assert attrs["n"] == "photo.jpg"
    assert "c" in attrs


def test_decrypt_attributes_returns_none_for_a_foreign_key():
    node = _make_file_node("h1", "root", "photo.jpg")
    other = AES.new(bytes(range(16, 32)), AES.MODE_ECB)
    assert decrypt_attributes(node, other) is None


def test_node_paths_builds_full_paths_through_folders():
    nodes = [
        {"h": "root", "p": None, "t": 2},
        _make_folder_node("f1", "root", "Photos"),
        _make_folder_node("f2", "f1", "2024"),
        _make_file_node("n1", "f2", "a.jpg", fingerprint=b64encode(bytes(19))),
        _make_file_node("n2", "root", "top.jpg", fingerprint=b64encode(bytes(19))),
    ]
    files = node_paths(nodes, MASTER)
    by_path = {f.path: f for f in files}
    assert "Photos/2024/a.jpg" in by_path
    assert "top.jpg" in by_path
    assert by_path["Photos/2024/a.jpg"].crc is not None


def test_node_paths_skips_undecodable_nodes_without_failing():
    """Inbound shares are encrypted with keys we do not hold; they must be
    reported as skipped, not crash the scan."""
    foreign = _make_file_node("x", "root", "theirs.jpg")
    foreign["k"] = "them:" + b64encode(
        AES.new(bytes(range(16, 32)), AES.MODE_ECB).encrypt(bytes(range(32))))
    nodes = [{"h": "root", "p": None, "t": 2},
             _make_file_node("n1", "root", "mine.jpg",
                             fingerprint=b64encode(bytes(19))),
             foreign]
    files = node_paths(nodes, MASTER)
    assert [f.path for f in files] == ["mine.jpg"]


def test_fetch_nodes_posts_the_tree_request():
    calls = []

    def opener(url, data, timeout):
        calls.append((url, json.loads(data)))
        return [{"f": [{"h": "root", "p": None, "t": 2}]}]

    api = MegaApi("SESSION", MASTER, poster=opener)
    nodes = api.fetch_nodes()
    assert nodes == [{"h": "root", "p": None, "t": 2}]
    url, payload = calls[0]
    assert "sid=SESSION" in url
    assert payload == [{"a": "f", "c": 1, "r": 1}]


def test_fetch_nodes_raises_on_a_numeric_error():
    """MEGA signals failure as a bare negative number, not an HTTP error."""
    api = MegaApi("SESSION", MASTER, poster=lambda *a, **k: -9)
    with pytest.raises(RuntimeError, match="-9"):
        api.fetch_nodes()


def test_node_paths_records_the_parent_handle():
    nodes = [
        {"h": "root", "p": None, "t": 2},
        _make_folder_node("f1", "root", "Photos"),
        _make_file_node("n1", "f1", "a.jpg", fingerprint=b64encode(bytes(19))),
    ]
    f = node_paths(nodes, MASTER)[0]
    assert f.parent == "f1"      # lets the report link to the containing folder


def test_rubbish_handle_is_the_type_4_root():
    nodes = [{"h": "cloud", "p": None, "t": 2}, {"h": "inbox", "p": None, "t": 3},
             {"h": "bin", "p": None, "t": 4}]
    from megaclean.megaapi import rubbish_handle
    assert rubbish_handle(nodes) == "bin"


def test_move_to_rubbish_sends_one_move_command_per_node_in_batches():
    calls = []

    def poster(url, data, timeout):
        payload = json.loads(data)
        calls.append(payload)
        return [0] * len(payload)          # MEGA answers 0 per succeeded command

    api = MegaApi("SESSION", MASTER, poster=poster)
    results = api.move_to_rubbish(["h1", "h2", "h3"], "bin", batch_size=2)
    assert calls[0] == [{"a": "m", "n": "h1", "t": "bin"},
                        {"a": "m", "n": "h2", "t": "bin"}]
    assert calls[1] == [{"a": "m", "n": "h3", "t": "bin"}]
    assert results == {"h1": 0, "h2": 0, "h3": 0}


def test_move_to_rubbish_reports_per_node_failures_without_raising():
    def poster(url, data, timeout):
        return [0, -9]                     # second node: not found

    api = MegaApi("SESSION", MASTER, poster=poster)
    results = api.move_to_rubbish(["ok", "gone"], "bin", batch_size=10)
    assert results == {"ok": 0, "gone": -9}


def test_move_to_rubbish_never_targets_anything_but_the_bin():
    """Moving to an arbitrary folder is not deletion; this API is only ever
    allowed to move into the Rubbish Bin, so a caller cannot misuse it."""
    api = MegaApi("SESSION", MASTER, poster=lambda *a, **k: [0])
    with pytest.raises(ValueError):
        api.move_to_rubbish(["h1"], "")


def test_nodes_in_the_rubbish_bin_are_not_indexed():
    """Once a copy is binned it must stop counting as a copy, or the next scan
    would call the survivor redundant against a file in the bin."""
    nodes = [
        {"h": "cloud", "p": None, "t": 2},
        {"h": "bin", "p": None, "t": 4},
        _make_file_node("live", "cloud", "a.jpg", fingerprint=b64encode(bytes(19))),
        _make_file_node("dead", "bin", "a.jpg", fingerprint=b64encode(bytes(19))),
    ]
    assert [f.handle for f in node_paths(nodes, MASTER)] == ["live"]


def test_move_nodes_targets_an_arbitrary_folder_in_batches():
    calls = []
    def poster(url, data, timeout):
        calls.append(json.loads(data)); return [0] * len(calls[-1])
    api = MegaApi("S", MASTER, poster=poster)
    out = api.move_nodes(["a", "b", "c"], "folderX", batch_size=2)
    assert calls[0] == [{"a": "m", "n": "a", "t": "folderX"},
                        {"a": "m", "n": "b", "t": "folderX"}]
    assert out == {"a": 0, "b": 0, "c": 0}


def test_node_names_decrypts_names_for_folders_and_files():
    from megaclean.megaapi import node_names
    nodes = [{"h": "root", "p": None, "t": 2},
             _make_folder_node("f1", "root", "Photos"),
             _make_file_node("n1", "f1", "a.jpg")]
    names = node_names(nodes, MASTER)
    assert names["f1"] == "Photos" and names["n1"] == "a.jpg"


def test_decrypt_ctr_range_matches_a_reference_encryption():
    """Handle-based downloads decrypt AES-CTR with nonce = key[16:24] and a
    counter that starts at offset/16, so a ranged read must line up."""
    from Crypto.Cipher import AES as _AES
    from Crypto.Util import Counter
    from megaclean.megaapi import attribute_key, decrypt_ctr
    keyblob = bytes(range(32))
    plain = bytes((i * 7) % 256 for i in range(5000))
    k = attribute_key(keyblob)
    ctr = Counter.new(64, prefix=keyblob[16:24], initial_value=0)
    cipher = _AES.new(k, _AES.MODE_CTR, counter=ctr).encrypt(plain)
    assert decrypt_ctr(cipher, keyblob, 0) == plain
    off = 1024                                       # 16-byte aligned
    assert decrypt_ctr(cipher[off:off + 700], keyblob, off) == plain[off:off + 700]


def test_download_range_requests_g_then_gets_the_range():
    calls = []
    def poster(url, data, timeout):
        calls.append(("post", json.loads(data)))
        return [{"g": "https://dl.example/abc", "s": 5000}]
    def getter(url, timeout):
        calls.append(("get", url))
        return b"\0" * 16
    api = MegaApi("S", MASTER, poster=poster, getter=getter)
    keyblob = bytes(range(32))
    out = api.download_range("H1", keyblob, 32, 16)
    assert calls[0] == ("post", [{"a": "g", "g": 1, "n": "H1"}])
    assert calls[1][1] == "https://dl.example/abc/32-47"
    assert len(out) == 16


def test_handle_remote_reads_ranges_by_handle_not_path():
    """When two folders share a name, a path is ambiguous; a handle is not."""
    from Crypto.Cipher import AES as _AES
    from Crypto.Util import Counter
    from megaclean.megaapi import HandleRemote, attribute_key
    keyblob = bytes(range(32))
    plain = bytes((i * 3) % 256 for i in range(3000))
    ctr = Counter.new(64, prefix=keyblob[16:24], initial_value=0)
    cipher = _AES.new(attribute_key(keyblob), _AES.MODE_CTR, counter=ctr).encrypt(plain)
    def poster(url, data, timeout):
        return [{"g": "https://dl/x", "s": len(plain)}]
    def getter(url, timeout):
        lo, hi = map(int, url.rsplit("/", 1)[1].split("-"))
        return cipher[lo:hi + 1]
    api = MegaApi("S", MASTER, poster=poster, getter=getter)
    remote = HandleRemote(api, {"H1": keyblob})
    assert remote.read_range("H1", 0, 100) == plain[:100]
    assert remote.read_range("H1", -100, 100) == plain[-100:]


def _multi_key_folder_node(handle, parent, name, *, valid_first=True):
    """A folder node whose `k` field carries two /-separated key entries, the
    way MEGA re-encodes a node's key once it has ever been made into a public
    or folder link. `valid_first` controls which entry is the one that
    actually decrypts, to prove order does not matter."""
    folderkey = bytes((i * 11 + 5) % 256 for i in range(16))
    raw = b"MEGA" + json.dumps({"n": name}).encode()
    raw += b"\0" * (-len(raw) % 16)
    enc_attr = AES.new(folderkey, AES.MODE_CBC, b"\0" * 16).encrypt(raw)
    good = f"owner:{b64encode(AES.new(MASTER, AES.MODE_ECB).encrypt(folderkey))}"
    junk = f"{handle}:{b64encode(bytes(range(16)))}"       # decrypts, wrong key
    k = f"{good}/{junk}" if valid_first else f"{junk}/{good}"
    return {"h": handle, "p": parent, "t": 1, "k": k, "a": b64encode(enc_attr)}


def test_multi_segment_key_field_resolves_regardless_of_order():
    """The real account had a node whose `k` was '<owner>:<key>/<handle>:<key>'
    -- naively taking the text after the first colon decrypts the WRONG
    segment and the folder's name comes back undecodable."""
    first = _multi_key_folder_node("f1", "root", "The Gathering", valid_first=True)
    second = _multi_key_folder_node("f2", "root", "The Gathering", valid_first=False)
    cipher = AES.new(MASTER, AES.MODE_ECB)
    assert decrypt_attributes(first, cipher)["n"] == "The Gathering"
    assert decrypt_attributes(second, cipher)["n"] == "The Gathering"


def test_node_paths_recovers_full_paths_through_a_multi_key_ancestor():
    """The exact shape of the real bug: a file three levels under a
    multi-key-field folder used to come back with that folder's name missing
    from its path entirely, rather than failing loudly."""
    root_folder = _multi_key_folder_node("linked", "root", "The Gathering",
                                         valid_first=False)
    nodes = [
        {"h": "root", "p": None, "t": 2},
        root_folder,
        _make_folder_node("sub", "linked", "Mexico"),
        _make_file_node("n1", "sub", "clip.mp4", fingerprint=b64encode(bytes(19))),
    ]
    files = node_paths(nodes, MASTER)
    assert [f.path for f in files] == ["The Gathering/Mexico/clip.mp4"]


def test_node_paths_never_returns_a_truncated_path():
    """If an ancestor is genuinely undecodable, the file must be skipped
    entirely -- never reported at a path missing that ancestor's name, which
    would look like a real (wrong) location instead of an unknown one."""
    foreign_folder = _make_folder_node("locked", "root", "Shared")
    foreign_folder["k"] = "someoneelse:" + b64encode(bytes(range(16, 32)))
    nodes = [
        {"h": "root", "p": None, "t": 2},
        foreign_folder,
        _make_folder_node("sub", "locked", "Mexico"),
        _make_file_node("n1", "sub", "clip.mp4", fingerprint=b64encode(bytes(19))),
    ]
    assert node_paths(nodes, MASTER) == []


def test_node_paths_still_works_for_the_ordinary_single_key_case():
    """Regression guard: the common case (one key entry per node) must be
    unaffected by the multi-segment handling."""
    nodes = [
        {"h": "root", "p": None, "t": 2},
        _make_folder_node("f1", "root", "Photos"),
        _make_file_node("n1", "f1", "a.jpg", fingerprint=b64encode(bytes(19))),
    ]
    assert [f.path for f in node_paths(nodes, MASTER)] == ["Photos/a.jpg"]
