from telemetry_radio.simulation import SimulatedChannel, SimulatedNetwork
from tests.test_end_to_end import node


def two_node_network(*, channel=None, offsets=None, cycle_seconds=0.02):
    network = SimulatedNetwork(
        {
            "master": node("master", 1, relay=True),
            "slave": node("slave", 2),
        },
        channel=channel,
        clock_offsets=offsets,
        cycle_seconds=cycle_seconds,
    )
    network.step(4)
    return network


def test_fec_recovers_short_corruption_burst():
    channel = SimulatedChannel(corruption_rate=1.0, slotted=True, seed=10)
    network = two_node_network(channel=channel)
    try:
        frame = b"\xfdthis-frame-can-be-recovered"
        network.inject("slave", frame)
        network.step(20)
        assert frame in network.received("master")
        assert channel.stats["corrupted"] > 0
        assert network.nodes["master"].app.statistics.snapshot()["fec_corrected_bits"] > 0
    finally:
        network.close()


def test_uncorrectable_corruption_is_not_delivered_to_mavlink():
    channel = SimulatedChannel(
        corruption_rate=1.0,
        corruption_burst_samples=128,
        slotted=True,
        seed=10,
    )
    network = two_node_network(channel=channel)
    try:
        frame = b"\xfdthis-frame-must-fail-fec-or-radio-crc"
        network.inject("slave", frame)
        network.step(20)
        assert frame not in network.received("master")
        assert channel.stats["corrupted"] > 0
        assert network.nodes["master"].app.statistics.snapshot()["fec_uncorrectable_frames"] > 0
    finally:
        network.close()


def test_dropped_ack_causes_retry_without_duplicate_mavlink_delivery():
    network = two_node_network(cycle_seconds=0.02)
    try:
        master = network.nodes["master"]
        master.app.next_sync_at = float("inf")
        master.app.link.queue.clear()
        master.radio.drop_next()  # selectively lose the next ACK transmission
        frame = b"\xfdreliable-slave-frame"
        before = network.channel.stats["transmissions"]
        network.inject("slave", frame)
        network.step(20)
        assert network.received("master").count(frame) == 1
        assert network.channel.stats["transmissions"] - before >= 3  # data, ACK, retry
        assert not network.nodes["slave"].app.link.awaiting_ack
        assert network.nodes["slave"].app.statistics.snapshot()["retries"] >= 1
        assert network.nodes["master"].app.statistics.snapshot()["duplicates_suppressed"] >= 1
    finally:
        network.close()


def test_temporary_disconnect_enters_scan_and_reacquires_master():
    network = two_node_network(cycle_seconds=0.05)
    try:
        network.disconnect("slave")
        network.step(120)  # six seconds, beyond the configured sync-loss timeout
        assert network.nodes["slave"].app.recovering
        network.reconnect("slave")
        network.step(160)  # scan until a periodic master clock beacon is heard
        slave = network.nodes["slave"].app
        assert not slave.recovering
        assert slave.statistics.snapshot()["reacquisitions"] >= 1
        assert slave.current_frequency == network.nodes["master"].app.current_frequency
        frame = b"\xfdafter-reconnect"
        network.inject("slave", frame)
        network.step(20)
        assert network.received("master").count(frame) == 1
    finally:
        network.close()


def test_clock_skew_misses_channel_then_master_beacon_corrects_schedule():
    # A five-minute slow clock selects the previous hop in the frequency list.
    network = two_node_network(offsets={"slave": -300.0}, cycle_seconds=0.05)
    try:
        master = network.nodes["master"].app
        slave = network.nodes["slave"].app
        assert master.current_frequency != slave.current_frequency
        network.step(320)
        assert not slave.recovering
        assert abs(slave.scheduler.clock_offset - 300.0) < 0.1
        assert slave.current_frequency == master.current_frequency
    finally:
        network.close()


def test_disconnected_node_independently_catches_missed_frequency_boundary():
    network = two_node_network(cycle_seconds=0.01)
    try:
        network.disconnect("slave")
        previous = network.nodes["master"].app.current_frequency
        network.advance_time(301)
        network.step(2)
        assert network.nodes["master"].app.current_frequency != previous
        network.reconnect("slave")
        network.step(2)
        assert (
            network.nodes["slave"].app.current_frequency
            == network.nodes["master"].app.current_frequency
        )
    finally:
        network.close()
