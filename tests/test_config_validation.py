from dataclasses import replace

import pytest

from telemetry_radio.config import (
    AuthenticationConfig,
    load_config,
    validate_config,
    validate_network,
)
from telemetry_radio.sdr import validate_device_capabilities


def examples():
    return {
        "gcs": load_config("configs/gcs.yaml"),
        "master": load_config("configs/master.yaml"),
        "slave": load_config("configs/slave.yaml"),
    }


def test_example_configs_form_a_consistent_network():
    validate_network(examples())


def test_network_rejects_duplicate_node_and_mavlink_system_ids():
    configs = examples()
    configs["another_slave"] = configs["slave"]
    with pytest.raises(ValueError, match="duplicate radio node"):
        validate_network(configs)
    configs = examples()
    configs["slave"] = replace(
        configs["slave"],
        mavlink=replace(configs["slave"].mavlink, source_system=1),
    )
    with pytest.raises(ValueError, match="duplicate local MAVLink"):
        validate_network(configs)


def test_network_rejects_inconsistent_schedule_waveform_and_routes():
    configs = examples()
    configs["slave"] = replace(
        configs["slave"],
        frequency=replace(configs["slave"].frequency, epoch=123),
    )
    with pytest.raises(ValueError, match="frequency schedule"):
        validate_network(configs)
    configs = examples()
    configs["slave"] = replace(
        configs["slave"], sdr=replace(configs["slave"].sdr, samples_per_symbol=16)
    )
    with pytest.raises(ValueError, match="waveform"):
        validate_network(configs)
    configs = examples()
    configs["slave"] = replace(
        configs["slave"], sdr=replace(configs["slave"].sdr, modulation="qpsk")
    )
    with pytest.raises(ValueError, match="waveform"):
        validate_network(configs)
    configs = examples()
    configs["slave"] = replace(configs["slave"], mavlink_system_to_node={1: 1})
    with pytest.raises(ValueError, match="routing map"):
        validate_network(configs)
    configs = examples()
    configs["slave"] = replace(configs["slave"], authentication=AuthenticationConfig())
    with pytest.raises(ValueError, match="authentication enablement"):
        validate_network(configs)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: replace(c, node_id=0), "reserved for broadcast"),
        (
            lambda c: replace(c, sdr=replace(c.sdr, bandwidth=c.sdr.sample_rate * 2)),
            "bandwidth cannot exceed",
        ),
        (
            lambda c: replace(c, frequency=replace(c.frequency, frequencies_hz=(915e6, 915e6))),
            "duplicates",
        ),
        (
            lambda c: replace(c, frequency=replace(c.frequency, guard_seconds=200)),
            "guard interval",
        ),
        (
            lambda c: replace(
                c, link=replace(c.link, backoff_min_seconds=2, backoff_max_seconds=1)
            ),
            "backoff",
        ),
        (
            lambda c: replace(c, sdr=replace(c.sdr, tx_gain=float("nan"))),
            "finite",
        ),
        (
            lambda c: replace(c, sdr=replace(c.sdr, modulation="8psk")),
            "modulation",
        ),
        (
            lambda c: replace(
                c, sdr=replace(c.sdr, modulation="qpsk", bit_repetitions=3)
            ),
            "bit_repetitions",
        ),
        (
            lambda c: replace(c, link=replace(c.link, tdd_master_bursts_per_slot=0)),
            "bursts_per_slot",
        ),
        (
            lambda c: replace(c, link=replace(c.link, tdd_inter_burst_seconds=-0.001)),
            "inter_burst",
        ),
    ],
)
def test_single_config_rejects_invalid_values(mutate, message):
    with pytest.raises(ValueError, match=message):
        validate_config(mutate(examples()["slave"]))


class Range:
    def __init__(self, minimum, maximum):
        self._minimum = minimum
        self._maximum = maximum

    def minimum(self):
        return self._minimum

    def maximum(self):
        return self._maximum


class Device:
    def getNumChannels(self, _direction):
        return 1

    def getSampleRateRange(self, _direction, _channel):
        return [Range(500_000, 2_000_000)]

    def getBandwidthRange(self, _direction, _channel):
        return [Range(100_000, 1_000_000)]

    def getFrequencyRange(self, _direction, _channel):
        return [Range(900e6, 930e6)]

    def getGainRange(self, _direction, _channel):
        return Range(0, 60)


def test_soapy_capability_validation_accepts_supported_settings():
    config = examples()["master"]
    validate_device_capabilities(Device(), config.sdr, config.frequency.frequencies_hz, rx=0, tx=1)


def test_soapy_capability_validation_rejects_frequency_gain_and_channel():
    config = examples()["master"]
    with pytest.raises(ValueError, match="frequency"):
        validate_device_capabilities(Device(), config.sdr, (2.4e9,), rx=0, tx=1)
    with pytest.raises(ValueError, match="gain"):
        validate_device_capabilities(
            Device(), replace(config.sdr, tx_gain=100), config.frequency.frequencies_hz, 0, 1
        )
    with pytest.raises(ValueError, match="channel"):
        validate_device_capabilities(
            Device(), replace(config.sdr, channel=2), config.frequency.frequencies_hz, 0, 1
        )
