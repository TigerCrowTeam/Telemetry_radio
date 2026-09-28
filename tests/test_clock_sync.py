from telemetry_radio.clock_sync import HardwareClockSynchronizer


def test_hardware_clock_sync_estimates_offset_drift_and_translates():
    sync = HardwareClockSynchronizer(window=16, smoothing=1.0)
    offset = 7_000_000
    slope = 1.0 + 12e-6
    origin = 1_700_000_000_000_000_000
    for seconds in range(16):
        master = origin + seconds * 1_000_000_000
        local = origin + offset + round(seconds * 1_000_000_000 * slope)
        estimate = sync.observe(master, local)

    assert sync.ready
    assert abs(estimate.offset_ns - (offset + 15_000_000_000 * 12e-6)) < 200
    assert abs(estimate.drift_ppm - 12.0) < 0.01
    future_master = origin + 20_000_000_000
    future_local = sync.master_to_local(future_master)
    assert abs(sync.local_to_master(future_local) - future_master) <= 1
