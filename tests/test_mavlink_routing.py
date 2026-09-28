from dataclasses import replace
from types import SimpleNamespace

from pymavlink.dialects.v10 import ardupilotmega as ardupilotmega_v1
from pymavlink.dialects.v20 import ardupilotmega

from telemetry_radio.mavlink import (
    MavlinkAddress,
    MavlinkInterface,
    TrafficPriority,
    inspect_frame,
    is_replaceable_telemetry,
    traffic_priority,
)
from tests.test_end_to_end import network


def command_long(target_system: int) -> bytes:
    mavlink = ardupilotmega.MAVLink(None, srcSystem=255, srcComponent=190)
    message = mavlink.command_long_encode(
        target_system,
        1,
        ardupilotmega.MAV_CMD_COMPONENT_ARM_DISARM,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return bytes(message.pack(mavlink))


def test_message_interval_request_uses_real_attitude_period():
    calls = []
    interface = MavlinkInterface.__new__(MavlinkInterface)
    interface._mavlink_constants = SimpleNamespace(MAV_CMD_SET_MESSAGE_INTERVAL=511)
    interface.connection = SimpleNamespace(
        mav=SimpleNamespace(command_long_send=lambda *args: calls.append(args))
    )

    interface.request_message_interval(30, 20.0, 1, 1)

    assert calls[0][:6] == (1, 1, 511, 0, 30.0, 50_000.0)


def test_inspector_reads_target_without_changing_frame():
    frame = command_long(2)
    address = inspect_frame(frame)
    assert address.source_system == 255
    assert address.target_system == 2
    assert address.target_component == 1
    assert frame == command_long(2)


def test_inspector_preserves_source_component_for_flow_coalescing():
    mavlink = ardupilotmega.MAVLink(None, srcSystem=1, srcComponent=42)
    message = mavlink.heartbeat_encode(1, 3, 0, 0, 3)
    address = inspect_frame(bytes(message.pack(mavlink)))

    assert address.source_system == 1
    assert address.source_component == 42


def test_inspector_supports_mavlink_1_target_fields():
    mavlink = ardupilotmega_v1.MAVLink(None, srcSystem=255, srcComponent=190)
    message = mavlink.command_long_encode(
        3, 1, ardupilotmega_v1.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0
    )
    frame = bytes(message.pack(mavlink))
    assert inspect_frame(frame).target_system == 3


def test_mavlink_traffic_priority_classification():
    assert (
        traffic_priority(MavlinkAddress(message_type="HEARTBEAT"))
        == TrafficPriority.LINK_CONTROL
    )
    assert (
        traffic_priority(MavlinkAddress(message_type="HEARTBEAT", source_component=0))
        == TrafficPriority.NORMAL
    )
    assert (
        traffic_priority(MavlinkAddress(message_type="HEARTBEAT", source_component=1))
        == TrafficPriority.LINK_CONTROL
    )
    assert traffic_priority(MavlinkAddress(message_type="ATTITUDE")) == TrafficPriority.LINK_CONTROL
    assert (
        traffic_priority(MavlinkAddress(message_type="ATTITUDE_QUATERNION"))
        == TrafficPriority.LINK_CONTROL
    )
    assert (
        traffic_priority(MavlinkAddress(message_type="GLOBAL_POSITION_INT"))
        == TrafficPriority.NORMAL
    )
    assert traffic_priority(MavlinkAddress(message_type="VFR_HUD")) == TrafficPriority.NORMAL
    assert (
        traffic_priority(MavlinkAddress(message_type="FILE_TRANSFER_PROTOCOL"))
        == TrafficPriority.BULK
    )
    assert (
        traffic_priority(MavlinkAddress(message_type="COMMAND_LONG", command=22))
        == TrafficPriority.COMMAND
    )
    assert (
        traffic_priority(MavlinkAddress(message_type="COMMAND_LONG", command=400))
        == TrafficPriority.EMERGENCY
    )


def test_only_freshness_sensitive_display_telemetry_is_replaceable():
    for message_type in (
        "HEARTBEAT",
        "ATTITUDE",
        "ATTITUDE_QUATERNION",
        "GLOBAL_POSITION_INT",
        "VFR_HUD",
    ):
        assert is_replaceable_telemetry(MavlinkAddress(message_type=message_type))
    for message_type in ("COMMAND_LONG", "PARAM_VALUE", "MISSION_ITEM_INT"):
        assert not is_replaceable_telemetry(MavlinkAddress(message_type=message_type))


def test_gcs_command_is_reliably_relayed_only_to_target_slave():
    radio_network = network()
    try:
        mapping = {1: 1, 2: 2, 3: 3}
        for simulated_node in radio_network.nodes.values():
            simulated_node.app.config = replace(
                simulated_node.app.config,
                mavlink_system_to_node=mapping,
                relay_via_master=True,
            )
            simulated_node.app.router.config = simulated_node.app.config
        frame = command_long(2)
        radio_network.inject("gcs", frame)
        radio_network.step(40)
        assert radio_network.received("slave2").count(frame) == 1
        assert frame not in radio_network.received("slave3")
        assert frame not in radio_network.received("master")
    finally:
        radio_network.close()
