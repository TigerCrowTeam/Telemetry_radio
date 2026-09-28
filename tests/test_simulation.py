import numpy as np

from telemetry_radio.simulation import SimulatedChannel


def test_simulated_radios_only_hear_same_frequency():
    channel = SimulatedChannel(seed=1)
    tx, matching, other = channel.radio(), channel.radio(), channel.radio()
    tx.tune(915e6)
    matching.tune(915e6)
    other.tune(925e6)
    tx.transmit(np.ones(16, dtype=np.complex64))
    assert len(matching.receive()) == 16
    assert len(other.receive()) == 0


def test_simulated_channel_can_drop_packets():
    channel = SimulatedChannel(packet_loss=1.0, seed=1)
    tx, rx = channel.radio(), channel.radio()
    tx.tune(905e6)
    rx.tune(905e6)
    tx.transmit(np.ones(8, dtype=np.complex64))
    assert len(rx.receive()) == 0


def tuned_radios(channel, count=3):
    radios = [channel.radio() for _ in range(count)]
    for radio in radios:
        radio.tune(915e6)
    return radios


def test_overlapping_equal_power_transmissions_collide():
    channel = SimulatedChannel(slotted=True, seed=2)
    tx1, tx2, rx = tuned_radios(channel)
    tx1.transmit(np.ones(32, dtype=np.complex64))
    tx2.transmit(-np.ones(32, dtype=np.complex64))
    channel.advance()
    assert channel.stats["collisions"] >= 1
    assert len(rx.receive()) == 32


def test_hidden_transmitters_cannot_sense_each_other_but_collide_at_receiver():
    channel = SimulatedChannel(slotted=True, seed=3)
    tx1, tx2, rx = tuned_radios(channel)
    channel.set_path_gain(tx1, tx2, 0)
    channel.set_path_gain(tx2, tx1, 0)
    tx1.transmit(np.ones(16, dtype=np.complex64))
    assert tx2.channel_power() == 0
    assert rx.channel_power() > 0
    tx2.transmit(-np.ones(16, dtype=np.complex64))
    channel.advance()
    assert channel.stats["collisions"] >= 1


def test_capture_delivers_significantly_stronger_signal():
    channel = SimulatedChannel(slotted=True, capture_ratio_db=10, seed=4)
    strong, weak, rx = tuned_radios(channel)
    channel.set_path_gain(strong, rx, 100)
    channel.set_path_gain(weak, rx, 1)
    strong.transmit(np.ones(12, dtype=np.complex64))
    weak.transmit(-np.ones(12, dtype=np.complex64))
    channel.advance()
    assert channel.stats["captures"] >= 1
    assert np.allclose(rx.receive(), 10)


def test_random_delivery_delay_uses_rf_cycles():
    channel = SimulatedChannel(slotted=True, random_delay_cycles=(2, 2), seed=5)
    tx, rx = tuned_radios(channel, 2)
    tx.transmit(np.ones(8, dtype=np.complex64))
    channel.advance()
    assert len(rx.receive()) == 0
    channel.advance()
    assert len(rx.receive()) == 0
    channel.advance()
    assert len(rx.receive()) == 8
    assert channel.stats["delayed"] == 1


def test_congestion_limit_drops_excess_transmissions():
    channel = SimulatedChannel(slotted=True, max_transmissions_per_cycle=1, seed=6)
    tx1, tx2, _rx = tuned_radios(channel)
    tx1.transmit(np.ones(4, dtype=np.complex64))
    tx2.transmit(np.ones(4, dtype=np.complex64))
    assert channel.stats["congestion_drops"] == 1
