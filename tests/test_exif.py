from megaclean.signature import exif_key


def test_exif_key_identifies_the_shot(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg")
    key = exif_key(p.read_bytes())
    assert key is not None
    assert "Canon" in key and "EOS R5" in key and "2024:05:01 12:00:00" in key


def test_same_shot_re_encoded_keeps_its_key(make_jpeg, tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", quality=90)
    b = make_jpeg(tmp_path / "b.jpg", quality=40)
    assert a.read_bytes() != b.read_bytes()
    assert exif_key(a.read_bytes()) == exif_key(b.read_bytes())


def test_different_shots_differ(make_jpeg, tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", dt="2024:05:01 12:00:00")
    b = make_jpeg(tmp_path / "b.jpg", dt="2024:05:01 12:00:01")
    assert exif_key(a.read_bytes()) != exif_key(b.read_bytes())


def test_burst_frames_separated_by_subsecond(make_jpeg, tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", subsec="10")
    b = make_jpeg(tmp_path / "b.jpg", subsec="30")
    assert exif_key(a.read_bytes()) != exif_key(b.read_bytes())


def test_no_datetime_means_no_key(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", with_exif=False)
    assert exif_key(p.read_bytes()) is None


def test_truncated_head_still_parses(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(800, 600))
    full = p.read_bytes()
    assert exif_key(full[:65536]) == exif_key(full)


def test_garbage_returns_none():
    assert exif_key(b"not an image at all") is None
