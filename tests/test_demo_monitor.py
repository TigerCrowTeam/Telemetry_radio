from telemetry_radio.demo_monitor import DemoEvidence, render


def snapshot(frequency, delivered, synchronized=True):
    return {
        "active_frequency_hz": frequency,
        "mavlink_frames_to_local": delivered,
        "packets_received": delivered + 10,
        "crc_failures": 0,
        "synchronized": synchronized,
    }


def test_demo_evidence_requires_fresh_delivery_on_every_channel():
    evidence = DemoEvidence((900_000_000, 1_900_000_000, 2_900_000_000))
    evidence.observe(snapshot(900_000_000, 10), 0)
    evidence.observe(snapshot(900_000_000, 15), 1)
    evidence.observe(snapshot(1_900_000_000, 20), 2)
    evidence.observe(snapshot(2_900_000_000, 25), 3)

    assert evidence.hops_observed == 2
    assert evidence.continuity_ok(snapshot(2_900_000_000, 25), 4)
    assert not evidence.continuity_ok(snapshot(2_900_000_000, 25), 9)


def test_demo_render_highlights_active_channel_and_verdict():
    evidence = DemoEvidence((900_000_000, 1_900_000_000))
    evidence.observe(snapshot(900_000_000, 1), 0)
    evidence.observe(snapshot(900_000_000, 2), 1)
    evidence.observe(snapshot(1_900_000_000, 3), 2)

    output = render(snapshot(1_900_000_000, 3), evidence, 2.5)

    assert "[ 1900 MHz ]" in output
    assert "PASS - TELEMETRY CONTINUES THROUGH HOPS" in output
    assert "not flight certification" in output
