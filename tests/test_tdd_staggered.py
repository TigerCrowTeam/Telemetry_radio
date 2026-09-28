from types import SimpleNamespace

from telemetry_radio.app import TelemetryRadioApp
from telemetry_radio.config import LinkConfig


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def app(role: str, clock: Clock):
    radio = TelemetryRadioApp.__new__(TelemetryRadioApp)
    radio.config = SimpleNamespace(
        role=role,
        link=LinkConfig(
            tdd_period_seconds=0.1,
            tdd_guard_seconds=0.02,
            tdd_inter_burst_seconds=0.02,
            tdd_master_bursts_per_slot=1,
            tdd_gcs_bursts_per_slot=1,
            tdd_staggered_windows=True,
        ),
        sdr=SimpleNamespace(sample_rate=2_000_000),
    )
    radio.wall_clock = clock
    radio.monotonic_clock = clock
    radio.scheduler = SimpleNamespace(clock_offset=0.0)
    radio._last_tx_end = float("-inf")
    radio._tdd_slot_index = -1
    radio._tdd_bursts_sent = 0
    return radio


def test_staggered_windows_preserve_guard_at_both_direction_changes():
    clock = Clock()
    master = app("master", clock)
    gcs = app("gcs", clock)

    clock.now = 0.01
    assert master._tdd_transmit_allowed()
    assert not gcs._tdd_transmit_allowed()
    clock.now = 0.04
    assert not master._tdd_transmit_allowed()
    assert not gcs._tdd_transmit_allowed()
    clock.now = 0.06
    assert not master._tdd_transmit_allowed()
    assert gcs._tdd_transmit_allowed()
    clock.now = 0.09
    assert not master._tdd_transmit_allowed()
    assert not gcs._tdd_transmit_allowed()


def test_staggered_burst_must_fit_before_guard():
    clock = Clock()
    master = app("master", clock)
    # 8 ms airtime plus the 1 ms SDR drain margin.
    samples = 16_000
    clock.now = 0.02
    assert master._tdd_burst_fits(samples)
    clock.now = 0.022
    assert not master._tdd_burst_fits(samples)


def test_receive_delay_compensation_cancels_beacon_processing_offset():
    clock = Clock()
    gcs = app("gcs", clock)
    gcs.config.link = LinkConfig(
        tdd_period_seconds=0.1,
        tdd_guard_seconds=0.02,
        tdd_staggered_windows=True,
        tdd_receive_delay_compensation_seconds=0.02,
    )
    gcs.scheduler.clock_offset = -0.02
    clock.now = 0.06

    assert gcs._tdd_time() == clock.now
    assert gcs._tdd_transmit_allowed()


def test_hardware_scheduler_returns_fixed_opportunity_center():
    clock = Clock()
    master = app("master", clock)
    master.config.link = LinkConfig(
        tdd_period_seconds=0.1,
        tdd_guard_seconds=0.01,
        tdd_master_bursts_per_slot=3,
        hardware_timed_io=True,
        hardware_slot_scheduling=True,
        hardware_timed_tx_lead_seconds=0.003,
        hardware_slot_lookahead_seconds=0.012,
    )
    master.sdr = SimpleNamespace(hardware_time=lambda: 1_002_000_000)
    master.hardware_clock_sync = SimpleNamespace(ready=False)

    target = master._next_hardware_tdd_opportunity()

    assert target == (1_006_666_666, 1_006_666_666, 0, 30)


def test_hardware_scheduler_waits_outside_lookahead():
    clock = Clock()
    master = app("master", clock)
    master.config.link = LinkConfig(
        tdd_period_seconds=0.1,
        tdd_guard_seconds=0.01,
        tdd_master_bursts_per_slot=3,
        hardware_timed_io=True,
        hardware_slot_scheduling=True,
        hardware_timed_tx_lead_seconds=0.003,
        hardware_slot_lookahead_seconds=0.005,
    )
    master.sdr = SimpleNamespace(hardware_time=lambda: 1_010_000_000)
    master.hardware_clock_sync = SimpleNamespace(ready=False)

    assert master._next_hardware_tdd_opportunity() is None


def test_20_hz_scheduler_targets_two_bursts_inside_master_window():
    clock = Clock()
    master = app("master", clock)
    master.config.link = LinkConfig(
        tdd_period_seconds=0.05,
        tdd_guard_seconds=0.005,
        tdd_master_bursts_per_slot=2,
        hardware_timed_io=True,
        hardware_slot_scheduling=True,
        hardware_timed_tx_lead_seconds=0.003,
        hardware_slot_lookahead_seconds=0.008,
    )
    master.sdr = SimpleNamespace(hardware_time=lambda: 1_001_000_000)
    master.hardware_clock_sync = SimpleNamespace(ready=False)

    target = master._next_hardware_tdd_opportunity()

    assert target == (1_005_000_000, 1_005_000_000, 0, 40)
