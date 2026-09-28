from telemetry_radio.config import FrequencyConfig
from telemetry_radio.scheduler import FrequencyScheduler


def scheduler():
    return FrequencyScheduler(FrequencyConfig((905e6, 915e6, 925e6), 1000, 300, 0.25))


def test_nodes_compute_same_frequency_from_time():
    assert scheduler().state(1300).frequency_hz == 915e6
    assert scheduler().state(1600).frequency_hz == 925e6
    assert scheduler().state(1900).frequency_hz == 905e6


def test_guard_suppresses_tx_around_boundary():
    assert not scheduler().state(1299.9).transmit_allowed
    assert not scheduler().state(1300.1).transmit_allowed
    assert scheduler().state(1301).transmit_allowed


def test_ping_pong_schedule_never_wraps_between_endpoints():
    hopping = FrequencyScheduler(
        FrequencyConfig(
            (800e6, 850e6, 900e6, 950e6, 1000e6),
            epoch=0,
            sequence_mode="ping_pong",
            interval_seconds=1,
        )
    )

    observed = [hopping.state(second + 0.5).frequency_hz for second in range(9)]

    assert observed == [800e6, 850e6, 900e6, 950e6, 1000e6, 950e6, 900e6, 850e6, 800e6]
