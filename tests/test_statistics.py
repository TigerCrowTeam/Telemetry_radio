import pytest

from telemetry_radio.config import LinkConfig
from telemetry_radio.framing import PROTOCOL_VERSION, PacketKind, RadioPacket
from telemetry_radio.mavlink import TrafficPriority
from telemetry_radio.reliability import ReliableLink
from telemetry_radio.statistics import LinkStatistics
from tests.test_end_to_end import network


class Clock:
    now = 10.0

    def __call__(self):
        return self.now


def test_statistics_snapshot_contains_counters_gauges_and_uptime():
    clock = Clock()
    statistics = LinkStatistics(clock)
    statistics.increment("packets_received", 2)
    statistics.increment("mavlink_frames_bundled", 3)
    statistics.set_gauge("active_frequency_hz", 915e6)
    clock.now = 12.5
    snapshot = statistics.snapshot()
    assert snapshot["packets_received"] == 2
    assert snapshot["mavlink_frames_bundled"] == 3
    assert snapshot["active_frequency_hz"] == 915e6
    assert snapshot["queue_capacity"] == 0
    assert snapshot["uptime_seconds"] == 2.5


def test_queue_statistics_track_coalescing_drops_and_depth():
    statistics = LinkStatistics()
    link = ReliableLink(
        1,
        LinkConfig(queue_size=2, reserved_high_priority_slots=1),
        statistics=statistics,
    )
    flow = (1, 30, 255)
    assert link.enqueue(
        RadioPacket(1, 0, 1, b"old"),
        priority=TrafficPriority.NORMAL,
        flow_key=flow,
    )
    assert link.enqueue(
        RadioPacket(1, 0, 2, b"new"),
        priority=TrafficPriority.NORMAL,
        flow_key=flow,
    )
    assert not link.enqueue(
        RadioPacket(1, 0, 3, b"other"),
        priority=TrafficPriority.NORMAL,
        flow_key=(1, 31, 255),
    )
    snapshot = statistics.snapshot()
    assert snapshot["telemetry_coalesced"] == 1
    assert snapshot["queue_drops"] == 1
    assert snapshot["queue_depth"] == 1


def test_acknowledgement_counter_and_pending_gauge():
    statistics = LinkStatistics()
    link = ReliableLink(2, LinkConfig(), statistics=statistics)
    packet = RadioPacket(2, 1, 7, b"unicast")
    link.enqueue(packet)
    queued = link.next_packet(True, True)
    link.transmitted(queued)
    assert statistics.snapshot()["awaiting_acknowledgements"] == 1
    assert link.acknowledge(RadioPacket(1, 2, 7, b"", PacketKind.ACK))
    snapshot = statistics.snapshot()
    assert snapshot["acknowledgements_received"] == 1
    assert snapshot["awaiting_acknowledgements"] == 0

    ack = RadioPacket(2, 1, 8, b"", PacketKind.ACK)
    link.enqueue(ack)
    sent_ack = link.next_packet(True, True)
    link.transmitted(sent_ack)
    assert statistics.snapshot()["acknowledgements_sent"] == 1


def test_each_transmission_attempt_charges_encoded_bytes_samples_and_airtime():
    statistics = LinkStatistics()
    link = ReliableLink(
        2,
        LinkConfig(),
        statistics=statistics,
        encoded_size=lambda _packet: 100,
    )
    link.enqueue(RadioPacket(2, 1, 7, b"retry"))
    queued = link.next_packet(True, True)
    link.transmitted(queued, encoded_bytes=100, samples=3200, airtime_seconds=0.0032)
    link.transmitted(queued, encoded_bytes=100, samples=3200, airtime_seconds=0.0032)
    snapshot = statistics.snapshot()
    assert snapshot["bytes_transmitted"] == 200
    assert snapshot["samples_transmitted"] == 6400
    assert snapshot["airtime_seconds_transmitted"] == pytest.approx(0.0064)


def test_end_to_end_statistics_track_packets_acknowledgements_and_frequency():
    radio_network = network()
    try:
        frame = b"\xfdstatistics-frame"
        radio_network.inject("slave2", frame)
        radio_network.step(30)
        slave = radio_network.nodes["slave2"].app.statistics.snapshot()
        master = radio_network.nodes["master"].app.statistics.snapshot()
        assert slave["mavlink_frames_from_local"] == 1
        assert slave["queue_capacity"] == radio_network.nodes["slave2"].app.config.link.queue_size
        assert slave["packets_transmitted"] >= 1
        assert slave["samples_transmitted"] > 0
        assert slave["airtime_seconds_transmitted"] > 0
        assert slave["bytes_transmitted"] > len(frame)
        assert master["packets_received"] >= 1
        assert master["mavlink_frames_to_local"] >= 1
        assert master["active_frequency_hz"] in (905e6, 915e6, 925e6)
        assert isinstance(master["estimated_signal_power"], float)
    finally:
        radio_network.close()


def test_crc_failure_is_counted():
    radio_network = network()
    try:
        master = radio_network.nodes["master"]
        wire = bytearray(RadioPacket(2, 1, 99, b"damaged").encode())
        wire[-1] ^= 1
        master.radio.incoming.append(master.app.modem.modulate(bytes(wire)))
        master.app.step()
        snapshot = master.app.statistics.snapshot()
        assert snapshot["invalid_packets"] == 1
        assert snapshot["crc_failures"] == 1
    finally:
        radio_network.close()


def test_protocol_version_mismatch_is_counted():
    radio_network = network()
    try:
        master = radio_network.nodes["master"]
        wire = bytearray(RadioPacket(2, 1, 99, b"future frame").encode())
        wire[2] = PROTOCOL_VERSION + 1
        master.radio.incoming.append(master.app.modem.modulate(bytes(wire)))
        master.app.step()
        snapshot = master.app.statistics.snapshot()
        assert snapshot["invalid_packets"] == 1
        assert snapshot["protocol_version_mismatches"] == 1
        assert snapshot["crc_failures"] == 0
    finally:
        radio_network.close()
def test_per_message_latency_statistics_include_average_maximum_and_p95():
    clock = Clock()
    clock.now = 0
    statistics = LinkStatistics(clock)
    statistics.increment_mavlink("ATTITUDE_QUATERNION", "received", 3)
    for age in (0.01, 0.02, 0.05):
        statistics.observe_mavlink_age("ATTITUDE_QUATERNION", age)
    for interval in (0.1, 0.11, 0.09, 0.1):
        clock.now += interval
        statistics.increment_mavlink("ATTITUDE_QUATERNION", "delivered")

    values = statistics.snapshot()["mavlink_by_type"]["ATTITUDE_QUATERNION"]
    assert values["received"] == 3
    assert values["age_average_ms"] == pytest.approx(80 / 3)
    assert values["age_maximum_ms"] == pytest.approx(50)
    assert values["age_p95_ms"] == pytest.approx(20)
    assert values["interval_average_ms"] == pytest.approx(100)
    assert values["interval_maximum_ms"] == pytest.approx(110)
    assert values["interval_p95_ms"] == pytest.approx(100)
