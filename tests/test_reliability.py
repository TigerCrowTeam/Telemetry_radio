from telemetry_radio.config import LinkConfig
from telemetry_radio.framing import PacketKind, RadioPacket
from telemetry_radio.mavlink import TrafficPriority
from telemetry_radio.reliability import ReliableLink


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_unicast_retries_until_acknowledged():
    clock = Clock()
    link = ReliableLink(2, LinkConfig(ack_timeout_seconds=1, max_retries=2), clock)
    packet = RadioPacket(2, 1, 7, b"mavlink")
    link.enqueue(packet)
    queued = link.next_packet(True, True)
    assert queued is not None
    link.transmitted(queued)
    clock.now = 1.1
    assert link.next_packet(True, True).packet == packet
    ack = RadioPacket(1, 2, 7, b"", PacketKind.ACK)
    assert link.acknowledge(ack)


def test_duplicate_data_is_suppressed_but_can_be_acked_again():
    link = ReliableLink(1, LinkConfig())
    packet = RadioPacket(2, 1, 9, b"data")
    assert not link.is_duplicate(packet)
    assert link.is_duplicate(packet)
    assert link.ack_for(packet).kind == PacketKind.ACK


def test_guard_interval_holds_queue():
    link = ReliableLink(1, LinkConfig())
    link.enqueue(RadioPacket(1, 2, 1, b"held"))
    assert link.next_packet(True, False) is None
    assert link.next_packet(True, True) is not None


def test_late_ack_removes_already_queued_retry():
    clock = Clock()
    link = ReliableLink(2, LinkConfig(ack_timeout_seconds=1), clock)
    packet = RadioPacket(2, 1, 8, b"data")
    link.enqueue(packet)
    sent = link.next_packet(True, True)
    link.transmitted(sent)
    clock.now = 1.1
    assert link.next_packet(False, True) is None  # retry is queued while channel is busy
    assert link.acknowledge(RadioPacket(1, 2, 8, b"", PacketKind.ACK))
    assert not link.queue


def test_new_unicast_waits_for_inflight_packet_ack():
    link = ReliableLink(2, LinkConfig())
    first = RadioPacket(2, 1, 8, b"first")
    second = RadioPacket(2, 1, 9, b"second")
    link.enqueue(first)
    link.enqueue(second)

    sent = link.next_packet(True, True)
    assert sent.packet == first
    link.transmitted(sent)
    assert link.next_packet(True, True) is None

    assert link.acknowledge(RadioPacket(1, 2, 8, b"", PacketKind.ACK))
    assert link.next_packet(True, True).packet == second


def test_ack_bypasses_inflight_unicast_wait():
    link = ReliableLink(2, LinkConfig())
    link.enqueue(RadioPacket(2, 1, 8, b"outbound"))
    sent = link.next_packet(True, True)
    link.transmitted(sent)

    acknowledgement = RadioPacket(2, 1, 42, b"", PacketKind.ACK)
    link.enqueue(acknowledgement)
    assert link.next_packet(True, True).packet == acknowledgement


def test_sequences_above_255_remain_distinct_in_duplicate_window():
    link = ReliableLink(1, LinkConfig(duplicate_window_seconds=30))
    low = RadioPacket(2, 1, 1, b"old")
    high = RadioPacket(2, 1, 257, b"new")
    assert not link.is_duplicate(low)
    assert not link.is_duplicate(high)
    assert link.is_duplicate(low)
    assert link.is_duplicate(high)


def test_queue_orders_link_emergency_command_telemetry_and_bulk():
    link = ReliableLink(1, LinkConfig(queue_size=10))
    priorities = (
        TrafficPriority.BULK,
        TrafficPriority.NORMAL,
        TrafficPriority.COMMAND,
        TrafficPriority.EMERGENCY,
        TrafficPriority.LINK_CONTROL,
    )
    for sequence, priority in enumerate(priorities):
        link.enqueue(RadioPacket(1, 2, sequence, bytes([sequence])), priority=priority)
    actual = [link.next_packet(True, True).priority for _ in priorities]
    assert actual == sorted(priorities)


def test_equal_priority_packets_remain_fifo():
    link = ReliableLink(1, LinkConfig(queue_size=4))
    link.enqueue(RadioPacket(1, 0, 10, b"first"), priority=TrafficPriority.NORMAL)
    link.enqueue(RadioPacket(1, 0, 11, b"second"), priority=TrafficPriority.NORMAL)
    assert link.next_packet(True, True).packet.sequence == 10
    assert link.next_packet(True, True).packet.sequence == 11


def test_critical_packet_evicts_lowest_priority_when_queue_is_full():
    link = ReliableLink(1, LinkConfig(queue_size=2))
    link.enqueue(RadioPacket(1, 0, 1, b"bulk"), priority=TrafficPriority.BULK)
    link.enqueue(RadioPacket(1, 0, 2, b"telemetry"), priority=TrafficPriority.NORMAL)
    assert link.enqueue(RadioPacket(1, 2, 3, b"emergency"), priority=TrafficPriority.EMERGENCY)
    queued_sequences = [item.packet.sequence for item in link.queue]
    assert queued_sequences == [3, 2]


def test_full_critical_queue_rejects_lower_priority_traffic():
    link = ReliableLink(1, LinkConfig(queue_size=1))
    link.enqueue(RadioPacket(1, 2, 1, b"ack", PacketKind.ACK))
    assert not link.enqueue(RadioPacket(1, 0, 2, b"bulk"), priority=TrafficPriority.BULK)
    assert link.queue[0].packet.kind == PacketKind.ACK


def test_telemetry_token_bucket_limits_send_rate():
    clock = Clock()
    wire_size = len(RadioPacket(1, 0, 1, b"a").encode())
    config = LinkConfig(
        telemetry_rate_bytes_per_second=wire_size,
        telemetry_burst_bytes=wire_size,
    )
    link = ReliableLink(1, config, clock)
    link.enqueue(RadioPacket(1, 0, 1, b"a"), priority=TrafficPriority.NORMAL)
    link.enqueue(RadioPacket(1, 0, 2, b"b"), priority=TrafficPriority.NORMAL)
    assert link.next_packet(True, True).packet.sequence == 1
    assert link.next_packet(True, True) is None
    clock.now = 1.0
    assert link.next_packet(True, True).packet.sequence == 2


def test_command_bypasses_exhausted_telemetry_rate_limit():
    clock = Clock()
    wire_size = len(RadioPacket(1, 0, 1, b"a").encode())
    link = ReliableLink(
        1,
        LinkConfig(
            telemetry_rate_bytes_per_second=wire_size,
            telemetry_burst_bytes=wire_size,
        ),
        clock,
    )
    link.enqueue(RadioPacket(1, 0, 1, b"a"), priority=TrafficPriority.NORMAL)
    assert link.next_packet(True, True) is not None
    link.enqueue(RadioPacket(1, 0, 2, b"b"), priority=TrafficPriority.NORMAL)
    link.enqueue(RadioPacket(1, 0, 3, b"command"), priority=TrafficPriority.COMMAND)
    assert link.next_packet(True, True).packet.sequence == 3


def test_rate_limiter_charges_complete_encoded_airtime_size():
    clock = Clock()
    link = ReliableLink(
        1,
        LinkConfig(telemetry_rate_bytes_per_second=100, telemetry_burst_bytes=99),
        clock,
        encoded_size=lambda _packet: 100,
    )
    link.enqueue(RadioPacket(1, 0, 1, b"small"), priority=TrafficPriority.NORMAL)
    assert link.next_packet(True, True) is None
    clock.now = 1
    # Capacity remains 99, so a 100-byte on-air frame can never pass this bucket.
    assert link.next_packet(True, True) is None


def test_new_telemetry_sample_coalesces_unsent_sample_from_same_flow():
    link = ReliableLink(1, LinkConfig(coalesce_telemetry=True))
    flow = (2, 30, 255)
    link.enqueue(
        RadioPacket(1, 0, 1, b"old attitude"),
        priority=TrafficPriority.NORMAL,
        flow_key=flow,
    )
    link.enqueue(
        RadioPacket(1, 0, 2, b"new attitude"),
        priority=TrafficPriority.NORMAL,
        flow_key=flow,
    )
    assert len(link.queue) == 1
    assert link.next_packet(True, True).packet.payload == b"new attitude"


def test_same_message_from_different_components_does_not_coalesce():
    link = ReliableLink(1, LinkConfig(coalesce_telemetry=True))
    first = RadioPacket(1, 255, 20, b"autopilot-heartbeat")
    peripheral = RadioPacket(1, 255, 21, b"peripheral-heartbeat")

    link.enqueue(first, flow_key=(1, 1, 0, 255), message_type="HEARTBEAT")
    link.enqueue(peripheral, flow_key=(1, 0, 0, 255), message_type="HEARTBEAT")

    assert [queued.packet for queued in link.queue] == [first, peripheral]


def test_reserved_slots_keep_capacity_available_for_control():
    link = ReliableLink(1, LinkConfig(queue_size=4, reserved_high_priority_slots=2))
    assert link.enqueue(RadioPacket(1, 0, 1, b"one"), priority=TrafficPriority.NORMAL)
    assert link.enqueue(RadioPacket(1, 0, 2, b"two"), priority=TrafficPriority.NORMAL)
    assert not link.enqueue(RadioPacket(1, 0, 3, b"three"), priority=TrafficPriority.NORMAL)
    assert link.enqueue(RadioPacket(1, 2, 4, b"command"), priority=TrafficPriority.COMMAND)
    assert link.enqueue(RadioPacket(1, 2, 5, b"emergency"), priority=TrafficPriority.EMERGENCY)
    assert len(link.queue) == 4


def test_stale_low_priority_packet_expires_but_command_does_not():
    clock = Clock()
    link = ReliableLink(1, LinkConfig(low_priority_max_age_seconds=2), clock)
    link.enqueue(RadioPacket(1, 0, 1, b"telemetry"), priority=TrafficPriority.NORMAL)
    link.enqueue(RadioPacket(1, 2, 2, b"command"), priority=TrafficPriority.COMMAND)
    clock.now = 3
    assert link.next_packet(True, True).packet.sequence == 2
    assert not link.queue


def test_mavlink_bundle_uses_data_acknowledgement_path():
    link = ReliableLink(1, LinkConfig())
    packet = RadioPacket(1, 2, 9, b"bundle", PacketKind.MAVLINK_BUNDLE)
    link.enqueue(packet, priority=TrafficPriority.NORMAL)
    queued = link.next_packet(True, True)
    link.transmitted(queued)

    acknowledgement = ReliableLink(2, LinkConfig()).ack_for(packet)
    assert acknowledgement is not None
    assert acknowledgement.kind == PacketKind.ACK
    assert link.acknowledge(acknowledgement) is True


def test_replaceable_packet_does_not_use_ack_or_block_new_state():
    link = ReliableLink(1, LinkConfig())
    first = RadioPacket(1, 2, 10, b"attitude", PacketKind.UNRELIABLE_DATA)
    second = RadioPacket(1, 2, 11, b"position", PacketKind.UNRELIABLE_DATA)
    link.enqueue(first, priority=TrafficPriority.LINK_CONTROL)
    link.enqueue(second, priority=TrafficPriority.LINK_CONTROL)

    queued = link.next_packet(True, True)
    link.transmitted(queued)

    assert not link.awaiting_ack
    assert link.next_packet(True, True).packet == second
    assert ReliableLink(2, LinkConfig()).ack_for(first) is None


def test_unreliable_bundle_does_not_request_ack():
    packet = RadioPacket(1, 2, 12, b"bundle", PacketKind.UNRELIABLE_MAVLINK_BUNDLE)
    assert ReliableLink(2, LinkConfig()).ack_for(packet) is None


def test_replaceable_link_control_telemetry_expires_by_custom_age():
    clock = Clock()
    link = ReliableLink(1, LinkConfig(), clock)
    link.enqueue(
        RadioPacket(1, 2, 13, b"attitude", PacketKind.UNRELIABLE_DATA),
        priority=TrafficPriority.LINK_CONTROL,
        message_type="ATTITUDE_QUATERNION",
        max_age_seconds=0.15,
    )
    clock.now = 0.151

    assert link.next_packet(True, True) is None
    assert link.statistics.snapshot()["mavlink_by_type"]["ATTITUDE_QUATERNION"][
        "age_dropped"
    ] == 1


def test_reserved_opportunity_selects_orientation_ahead_of_same_priority():
    link = ReliableLink(1, LinkConfig())
    link.enqueue(
        RadioPacket(1, 2, 20, b"control", PacketKind.UNRELIABLE_DATA),
        priority=TrafficPriority.LINK_CONTROL,
        message_type="OTHER_CONTROL",
    )
    attitude = RadioPacket(1, 2, 21, b"attitude", PacketKind.UNRELIABLE_DATA)
    link.enqueue(
        attitude,
        priority=TrafficPriority.LINK_CONTROL,
        message_type="ATTITUDE",
    )

    selected = link.next_packet(True, True, frozenset({"ATTITUDE"}))

    assert selected is not None
    assert selected.packet == attitude


def test_control_opportunity_defers_orientation_when_other_traffic_exists():
    link = ReliableLink(1, LinkConfig())
    attitude = RadioPacket(1, 2, 30, b"attitude", PacketKind.UNRELIABLE_DATA)
    heartbeat = RadioPacket(1, 2, 31, b"heartbeat", PacketKind.UNRELIABLE_DATA)
    link.enqueue(
        attitude,
        priority=TrafficPriority.LINK_CONTROL,
        message_type="ATTITUDE",
    )
    link.enqueue(
        heartbeat,
        priority=TrafficPriority.NORMAL,
        message_type="HEARTBEAT",
    )

    selected = link.next_packet(
        True,
        True,
        deferred_message_types=frozenset({"ATTITUDE"}),
    )

    assert selected is not None
    assert selected.packet == heartbeat
