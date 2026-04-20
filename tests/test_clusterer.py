from retrace.clusterer import cluster_sessions
from retrace.detectors.base import Signal


def test_cluster_groups_sessions_with_same_fingerprint():
    sig_a = [
        Signal(
            session_id="a",
            detector="console_error",
            timestamp_ms=100,
            url="https://x/checkout?q=1",
            details={"message": "TypeError: y is undefined", "level": "error"},
        )
    ]
    sig_b = [
        Signal(
            session_id="b",
            detector="console_error",
            timestamp_ms=200,
            url="https://x/checkout?q=2",
            details={"message": "TypeError: y is undefined", "level": "error"},
        )
    ]

    clusters = cluster_sessions({"a": sig_a, "b": sig_b}, min_size=1)
    assert len(clusters) == 1
    c = clusters[0]
    assert sorted(c.session_ids) == ["a", "b"]
    assert c.affected_count == 2
    assert c.primary_url == "https://x/checkout"
    assert c.signal_summary == {"console_error": 2}
    assert c.first_seen_ms == 100
    assert c.last_seen_ms == 200


def test_cluster_splits_different_detectors():
    sig_a = [Signal(session_id="a", detector="console_error", timestamp_ms=0, url="https://x/", details={"message": "m"})]
    sig_b = [Signal(session_id="b", detector="rage_click", timestamp_ms=0, url="https://x/", details={})]
    clusters = cluster_sessions({"a": sig_a, "b": sig_b}, min_size=1)
    assert len(clusters) == 2


def test_cluster_respects_min_size():
    sig = {"a": [Signal(session_id="a", detector="d", timestamp_ms=0, url="u", details={"message": "m"})]}
    assert cluster_sessions(sig, min_size=2) == []
    assert len(cluster_sessions(sig, min_size=1)) == 1
