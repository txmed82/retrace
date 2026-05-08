from retrace.detectors import all_detectors


def test_all_detectors_ignore_malformed_live_rrweb_shapes():
    events = [
        "not-an-event",
        None,
        {"type": 4, "timestamp": "bad", "data": "not-a-dict"},
        {"type": 2, "timestamp": 10, "data": {"node": "not-a-node"}},
        {
            "type": 2,
            "timestamp": 20,
            "data": {
                "node": {
                    "type": 2,
                    "attributes": "not-a-dict",
                    "childNodes": ["not-a-child-node", None],
                }
            },
        },
        {
            "type": 3,
            "timestamp": 30,
            "data": {"source": 0, "adds": ["not-a-node", None, {"node": "bad"}]},
        },
        {
            "type": 3,
            "timestamp": 40,
            "data": {"source": 2, "type": 2, "id": "button-1"},
        },
        {"type": 6, "timestamp": 50, "data": {"plugin": "rrweb/console@1"}},
        {"type": 6, "timestamp": 60, "data": {"plugin": "retrace/network", "payload": []}},
    ]

    for detector in all_detectors():
        detector.detect("sess-live-ish", events)  # type: ignore[arg-type]
