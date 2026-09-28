from dataclasses import replace

from telemetry_radio.config import FrequencyConfig, MavlinkConfig, RadioConfig, SDRConfig
from telemetry_radio.framing import BROADCAST_ID, RadioPacket
from telemetry_radio.mavlink import MavlinkAddress
from telemetry_radio.routing import Router


def config(role="master", node_id=1, relay=True):
    return RadioConfig(
        role, node_id, MavlinkConfig("dummy"), SDRConfig(), FrequencyConfig((1,), 0), relay
    )


def test_master_relays_slave_to_gcs():
    router = Router(config())
    assert router.relay(RadioPacket(2, 1, 0, b"mav")) == 255


def test_broadcast_is_locally_delivered():
    assert Router(config("slave", 2, False)).deliver_locally(
        RadioPacket(255, BROADCAST_ID, 0, b"x")
    )


def test_gcs_routes_targeted_command_via_master():
    mapped = replace(config("gcs", 255, False), mavlink_system_to_node={2: 2})
    address = MavlinkAddress(source_system=255, target_system=2, target_component=1)
    assert Router(mapped).destination_for_local_mavlink(address) == 1


def test_master_relays_targeted_gcs_command_only_to_mapped_slave():
    mapped = replace(config(), mavlink_system_to_node={1: 1, 2: 2})
    packet = RadioPacket(255, 1, 4, b"command")
    address = MavlinkAddress(source_system=255, target_system=2, target_component=1)
    router = Router(mapped)
    assert not router.deliver_locally(packet, address)
    assert router.relay(packet, address) == 2
