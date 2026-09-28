from telemetry_radio.config import (
    FrequencyConfig,
    LinkConfig,
    MavlinkConfig,
    RadioConfig,
    SDRConfig,
)
from telemetry_radio.simulation import SimulatedNetwork


def node(role, node_id, *, relay=False):
    return RadioConfig(
        role=role,
        node_id=node_id,
        mavlink=MavlinkConfig("virtual"),
        sdr=SDRConfig(samples_per_symbol=4),
        frequency=FrequencyConfig(
            frequencies_hz=(905e6, 915e6, 925e6),
            epoch=1_799_999_970,
            interval_seconds=300,
            guard_seconds=0,
            reacquire_dwell_seconds=0.01,
            sync_loss_seconds=5,
        ),
        relay_enabled=relay,
        link=LinkConfig(
            ack_timeout_seconds=0.01,
            max_retries=2,
            duplicate_window_seconds=30,
            channel_busy_threshold=0.02,
            backoff_min_seconds=0,
            backoff_max_seconds=0,
        ),
    )


def network():
    radio_network = SimulatedNetwork(
        {
            "gcs": node("gcs", 255),
            "master": node("master", 1, relay=True),
            "slave2": node("slave", 2),
            "slave3": node("slave", 3),
        }
    )
    # Let every node perform its initial tune before application traffic begins.
    radio_network.step(3)
    return radio_network


def test_master_mavlink_reaches_gcs_unchanged():
    radio_network = network()
    try:
        frame = b"\xfd\x05master-mavlink-wire-frame"
        radio_network.inject("master", frame)
        radio_network.step(12)
        assert frame in radio_network.received("gcs")
    finally:
        radio_network.close()


def test_multiple_slaves_relay_to_gcs_and_preserve_payloads():
    radio_network = network()
    try:
        slave2_frame = b"\xfdslave-two"
        slave3_frame = b"\xfeslave-three"
        radio_network.inject("slave2", slave2_frame)
        radio_network.inject("slave3", slave3_frame)
        radio_network.step(30)
        received = radio_network.received("gcs")
        assert slave2_frame in received
        assert slave3_frame in received
        assert radio_network.received("master").count(slave2_frame) == 1
        assert radio_network.received("master").count(slave3_frame) == 1
    finally:
        radio_network.close()


def test_gcs_broadcast_reaches_master_and_all_slaves_once():
    radio_network = network()
    try:
        command = b"\xfdgcs-command"
        radio_network.inject("gcs", command)
        radio_network.step(20)
        for name in ("master", "slave2", "slave3"):
            assert radio_network.received(name).count(command) == 1
    finally:
        radio_network.close()


def test_duplicate_node_ids_are_rejected():
    configs = {"master": node("master", 1), "bad_slave": node("slave", 1)}
    try:
        SimulatedNetwork(configs)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate node IDs should fail")
