from types import SimpleNamespace

from telemetry_radio.app import TelemetryRadioApp
from telemetry_radio.config import LinkConfig
from telemetry_radio.framing import PacketKind, RadioPacket, decode_mavlink_bundle
from telemetry_radio.mavlink import MavlinkAddress, TrafficPriority
from telemetry_radio.reliability import ReliableLink
from telemetry_radio.statistics import LinkStatistics


def application_with_queue():
    app = TelemetryRadioApp.__new__(TelemetryRadioApp)
    link_config = LinkConfig(mavlink_bundle_max_frames=3, mavlink_bundle_max_bytes=300)
    app.config = SimpleNamespace(link=link_config)
    app.statistics = LinkStatistics()
    app.link = ReliableLink(1, link_config, statistics=app.statistics)
    return app


def test_bundle_combines_compatible_mixed_priority_telemetry():
    app = application_with_queue()
    first = RadioPacket(1, 255, 1, b"normal")
    second = RadioPacket(1, 255, 2, b"bulk")
    app.link.enqueue(first, priority=TrafficPriority.NORMAL)
    app.link.enqueue(second, priority=TrafficPriority.BULK)
    queued = app.link.next_packet(True, True)

    app._bundle_for_transmit(queued)

    assert queued.packet.kind == PacketKind.MAVLINK_BUNDLE
    assert decode_mavlink_bundle(queued.packet.payload) == [b"normal", b"bulk"]
    assert not app.link.queue


def test_bundle_preserves_unreliable_behavior_for_fresh_state():
    app = application_with_queue()
    first = RadioPacket(1, 255, 1, b"attitude", PacketKind.UNRELIABLE_DATA)
    second = RadioPacket(1, 255, 2, b"position", PacketKind.UNRELIABLE_DATA)
    app.link.enqueue(first, priority=TrafficPriority.LINK_CONTROL)
    app.link.enqueue(second, priority=TrafficPriority.NORMAL)
    queued = app.link.next_packet(True, True)

    app._bundle_for_transmit(queued)

    assert queued.packet.kind == PacketKind.UNRELIABLE_MAVLINK_BUNDLE
    assert decode_mavlink_bundle(queued.packet.payload) == [b"attitude", b"position"]


def test_command_is_not_bundled_with_telemetry():
    app = application_with_queue()
    command = RadioPacket(1, 255, 1, b"command")
    telemetry = RadioPacket(1, 255, 2, b"telemetry")
    app.link.enqueue(command, priority=TrafficPriority.COMMAND)
    app.link.enqueue(telemetry, priority=TrafficPriority.NORMAL)
    queued = app.link.next_packet(True, True)

    app._bundle_for_transmit(queued)

    assert queued.packet == command
    assert len(app.link.queue) == 1


def test_deferred_bundle_is_restored_for_fresh_coalescing():
    app = application_with_queue()
    first = RadioPacket(1, 255, 1, b"old attitude", PacketKind.UNRELIABLE_DATA)
    second = RadioPacket(1, 255, 2, b"old position", PacketKind.UNRELIABLE_DATA)
    app.link.enqueue(
        first,
        priority=TrafficPriority.LINK_CONTROL,
        flow_key=(1, 31, 255),
        message_type="ATTITUDE_QUATERNION",
    )
    app.link.enqueue(
        second,
        priority=TrafficPriority.NORMAL,
        flow_key=(1, 33, 255),
        message_type="GLOBAL_POSITION_INT",
    )
    queued = app.link.next_packet(True, True)
    original = queued.packet
    selected = app._bundle_for_transmit(queued)

    app._restore_deferred_bundle(queued, original, selected)
    app.link.requeue(queued)
    app.link.enqueue(
        RadioPacket(1, 255, 3, b"new attitude", PacketKind.UNRELIABLE_DATA),
        priority=TrafficPriority.LINK_CONTROL,
        flow_key=(1, 31, 255),
        message_type="ATTITUDE_QUATERNION",
    )

    assert len(app.link.queue) == 2
    assert app.link.queue[0].packet.payload == b"new attitude"


def test_quaternion_is_authoritative_with_timed_attitude_fallback():
    app = application_with_queue()
    app.config.link = LinkConfig(quaternion_fallback_seconds=0.5)
    app.last_quaternion_at = None

    assert app._orientation_allowed(MavlinkAddress(message_type="ATTITUDE"), 1.0)
    assert app._orientation_allowed(MavlinkAddress(message_type="ATTITUDE_QUATERNION"), 1.1)
    assert not app._orientation_allowed(MavlinkAddress(message_type="ATTITUDE"), 1.4)
    assert app._orientation_allowed(MavlinkAddress(message_type="ATTITUDE"), 1.7)
