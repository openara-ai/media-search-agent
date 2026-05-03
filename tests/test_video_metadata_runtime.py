from msa_indexer.io import video


def test_get_video_meta_tolerates_missing_pymediainfo(monkeypatch):
    monkeypatch.setattr(video, "MediaInfo", None)
    monkeypatch.setattr(video, "_extract_gps_with_exiftool", lambda path: None)

    meta = video.get_video_meta("/tmp/example.mov")

    assert meta == {"duration": None, "width": None, "height": None}
