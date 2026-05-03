from pathlib import Path

from msa_indexer.io import video


def test_should_extract_video_gps_track_only_matches_gopro_mp4_names():
    assert video.should_extract_video_gps_track("/tmp/GX010123.MP4") is True
    assert video.should_extract_video_gps_track("/tmp/gh020123.mp4") is True
    assert video.should_extract_video_gps_track("/tmp/GOPR0123.mp4") is True
    assert video.should_extract_video_gps_track("/tmp/trimmed_gopro_gps_01.mp4") is True
    assert video.should_extract_video_gps_track("/tmp/GX010123.mov") is False
    assert video.should_extract_video_gps_track("/tmp/IMG_0123.mp4") is False


def test_likely_has_embedded_gps_track_uses_exiftool_probe(monkeypatch):
    monkeypatch.setattr(
        video,
        "_probe_embedded_gps_via_exiftool",
        lambda path: True,
    )
    assert video.likely_has_embedded_gps_track("/tmp/example.mp4") is True


def test_extract_video_gps_track_parses_grouped_exiftool_records(monkeypatch):
    monkeypatch.setattr(
        video,
        "_extract_embedded_gps_records",
        lambda path: [{
            "SourceFile": str(path),
            "Doc1:GPSDateTime": "2026:02:27 01:17:13.200",
            "Doc1:GPSLatitude": 37.4170104,
            "Doc1:GPSLongitude": -121.9927020,
            "Doc1:GPSAltitude": 11.182,
            "Doc1:GPSMeasureMode": 3,
            "Doc1:SampleTime": 0.0,
            "Doc1-1:GPSDateTime": "2026:02:27 01:17:13.300",
            "Doc1-1:GPSLatitude": 37.4170113,
            "Doc1-1:GPSLongitude": -121.9926964,
            "Doc1-1:GPSAltitude": 11.282,
            "Doc1-1:GPSMeasureMode": 3,
            "Doc1-1:SampleTime": 0.1,
        }],
    )

    samples = video.extract_video_gps_track(Path("/tmp/example.mp4"))

    assert len(samples) == 2
    assert samples[0]["t_offset_sec"] == 0.0
    assert samples[1]["t_offset_sec"] == 0.1
    assert samples[0]["gps_lat"] == 37.4170104
    assert samples[1]["gps_lon"] == -121.9926964
    assert samples[0]["gps_datetime_utc"] == "2026-02-27T01:17:13.200Z"
    assert samples[0]["gps_source"] == "exiftool-ee3"


def test_extract_video_gps_track_returns_empty_when_no_gps_samples(monkeypatch):
    monkeypatch.setattr(
        video,
        "_extract_embedded_gps_records",
        lambda path: [{
            "SourceFile": str(path),
            "Doc1:SampleTime": 0.0,
            "Doc2:SampleTime": 1.0,
        }],
    )

    samples = video.extract_video_gps_track(Path("/tmp/example.mp4"))

    assert samples == []


def test_sample_video_gps_at_timestamp_interpolates_between_samples():
    samples = [
        {
            "t_offset_sec": 0.0,
            "gps_lat": 10.0,
            "gps_lon": 20.0,
            "gps_alt": 100.0,
            "gps_datetime_utc": "2026:02:27 01:17:13.200",
            "gps_fix": 3,
        },
        {
            "t_offset_sec": 10.0,
            "gps_lat": 20.0,
            "gps_lon": 40.0,
            "gps_alt": 200.0,
            "gps_datetime_utc": "2026:02:27 01:17:23.200",
            "gps_fix": 3,
        },
    ]

    out = video.sample_video_gps_at_timestamp(samples, 5.0, interpolate_max_gap_sec=10.0)

    assert out is not None
    assert out["gps_lat"] == 15.0
    assert out["gps_lon"] == 30.0
    assert out["gps_alt"] == 150.0
    assert out["gps_source"] == "interpolated"


def test_sample_video_gps_at_timestamp_uses_nearest_at_edges():
    samples = [
        {"t_offset_sec": 1.0, "gps_lat": 1.0, "gps_lon": 2.0, "gps_alt": None, "gps_datetime_utc": None, "gps_fix": 3},
        {"t_offset_sec": 2.0, "gps_lat": 3.0, "gps_lon": 4.0, "gps_alt": None, "gps_datetime_utc": None, "gps_fix": 3},
    ]

    before = video.sample_video_gps_at_timestamp(samples, 0.5)
    after = video.sample_video_gps_at_timestamp(samples, 2.5)

    assert before["gps_lat"] == 1.0
    assert before["gps_source"] == "nearest"
    assert after["gps_lon"] == 4.0
    assert after["gps_source"] == "nearest"


def test_sample_video_gps_at_timestamp_returns_none_when_too_far_from_track():
    samples = [
        {"t_offset_sec": 1.0, "gps_lat": 1.0, "gps_lon": 2.0, "gps_alt": None, "gps_datetime_utc": None, "gps_fix": 3},
        {"t_offset_sec": 2.0, "gps_lat": 3.0, "gps_lon": 4.0, "gps_alt": None, "gps_datetime_utc": None, "gps_fix": 3},
    ]

    assert video.sample_video_gps_at_timestamp(samples, 9.0) is None


def test_sample_video_gps_at_timestamp_handles_degenerate_sample_window():
    samples = [
        {"t_offset_sec": 5.0, "gps_lat": 1.0, "gps_lon": 2.0, "gps_alt": None, "gps_datetime_utc": None, "gps_fix": 3},
        {"t_offset_sec": 5.0, "gps_lat": 3.0, "gps_lon": 4.0, "gps_alt": None, "gps_datetime_utc": None, "gps_fix": 3},
    ]

    out = video.sample_video_gps_at_timestamp(samples, 5.0)

    assert out is not None
    assert out["gps_lat"] == 1.0
    assert out["gps_lon"] == 2.0
    assert out["gps_source"] == "nearest"
