import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from telemetry_radio.config import SDRConfig
from telemetry_radio.sdr import BufferedRadio, SoapyRadio, make_soapy_device


class Range:
    def __init__(self, minimum, maximum):
        self._minimum = minimum
        self._maximum = maximum

    def minimum(self):
        return self._minimum

    def maximum(self):
        return self._maximum


class FakeDevice:
    def __init__(self, _arguments):
        self.calls = []
        self.write_results = []
        self.read_result = SimpleNamespace(ret=0)

    def getNumChannels(self, direction):
        return 1

    def getSampleRateRange(self, direction, channel):
        return [Range(100_000, 10_000_000)]

    def getBandwidthRange(self, direction, channel):
        return [Range(10_000, 5_000_000)]

    def getFrequencyRange(self, direction, channel):
        return [Range(100_000_000, 2_000_000_000)]

    def getGainRange(self, direction, channel):
        return Range(0, 70)

    def listAntennas(self, direction, channel):
        return ["TX/RX", "RX2"] if direction == 1 else ["TX/RX"]

    def setAntenna(self, *arguments):
        self.calls.append(("antenna", *arguments))

    def setSampleRate(self, *arguments):
        self.calls.append(("sample_rate", *arguments))

    def setBandwidth(self, *arguments):
        self.calls.append(("bandwidth", *arguments))

    def setGain(self, *arguments):
        self.calls.append(("gain", *arguments))

    def setFrequency(self, *arguments):
        self.calls.append(("frequency", *arguments))

    def setupStream(self, direction, sample_format, channels):
        stream = f"stream-{direction}"
        self.calls.append(("setup", direction, sample_format, channels))
        return stream

    def getStreamMTU(self, stream):
        return 3

    def activateStream(self, stream, flags=0, timeNs=0, numElems=0):
        self.calls.append(("activate", stream, flags, timeNs, numElems))

    def deactivateStream(self, stream):
        self.calls.append(("deactivate", stream))

    def writeStream(self, stream, buffers, count, flags=0, timeNs=0):
        self.calls.append(("write", stream, count, flags, timeNs))
        result = self.write_results.pop(0) if self.write_results else count
        return SimpleNamespace(ret=result)

    def readStream(self, stream, buffers, count, timeoutUs):
        self.calls.append(("read", stream, count, timeoutUs))
        if self.read_result.ret > 0:
            buffers[0][: self.read_result.ret] = np.arange(self.read_result.ret)
        return self.read_result

    def closeStream(self, stream):
        self.calls.append(("close", stream))

    def hasHardwareTime(self, what):
        return True

    def getHardwareTime(self, what):
        return 1_000_000_000

    def setHardwareTime(self, time_ns, what):
        self.calls.append(("set_hardware_time", time_ns, what))

    def setCommandTime(self, time_ns, what):
        self.calls.append(("set_command_time", time_ns, what))


@pytest.fixture
def soapy(monkeypatch):
    device = FakeDevice({})
    module = SimpleNamespace(
        SOAPY_SDR_RX=1,
        SOAPY_SDR_TX=2,
        SOAPY_SDR_CF32="CF32",
        SOAPY_SDR_TIMEOUT=-1,
        SOAPY_SDR_OVERFLOW=-4,
        SOAPY_SDR_END_BURST=4,
        SOAPY_SDR_HAS_TIME=8,
        SOAPY_SDR_TIME_ERROR=-6,
        SOAPY_SDR_STREAM_ERROR=-2,
        SOAPY_SDR_CORRUPTION=-3,
        Device=lambda _arguments: device,
    )
    monkeypatch.setitem(sys.modules, "SoapySDR", module)
    return device


def test_adapter_applies_configuration_tunes_both_directions_and_closes(soapy):
    config = SDRConfig(
        sample_rate=2_000_000,
        bandwidth=500_000,
        rx_gain=20,
        tx_gain=25,
        rx_antenna="TX/RX",
        tx_antenna="TX/RX",
    )
    radio = SoapyRadio(config, (915_000_000,))

    radio.tune(915_000_000)
    radio.close()

    assert ("sample_rate", 1, 0, 2_000_000) in soapy.calls
    assert ("gain", 2, 0, 25) in soapy.calls
    assert ("antenna", 1, 0, "TX/RX") in soapy.calls
    assert ("antenna", 2, 0, "TX/RX") in soapy.calls
    assert ("frequency", 1, 0, 915_000_000) in soapy.calls
    assert ("frequency", 2, 0, 915_000_000) in soapy.calls
    assert ("close", "stream-1") in soapy.calls
    assert ("close", "stream-2") in soapy.calls


def test_adapter_queues_timed_rx_and_tx_tune(soapy):
    radio = SoapyRadio(SDRConfig())

    radio.tune_at(925_000_000, 1_100_000_000)
    radio.close()

    timed = soapy.calls.index(("set_command_time", 1_100_000_000, ""))
    cleared = soapy.calls.index(("set_command_time", 0, ""))
    rx = soapy.calls.index(("frequency", 1, 0, 925_000_000))
    tx = soapy.calls.index(("frequency", 2, 0, 925_000_000))
    assert timed < rx < tx < cleared


def test_device_open_retries_transient_release_race(monkeypatch):
    attempts = 0
    device = object()

    def make(_selector):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise IndexError("map::at")
        return device

    api = SimpleNamespace(Device=make)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    assert make_soapy_device(api, {}, attempts=3, retry_seconds=1.0) is device
    assert attempts == 3


def test_transmit_completes_partial_writes_and_restores_receive(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.write_results = [3, 2]

    radio.transmit(np.ones(5, np.complex64))

    writes = [call for call in soapy.calls if call[0] == "write"]
    assert writes == [
        ("write", "stream-2", 3, 0, 0),
        ("write", "stream-2", 2, 4, 0),
    ]
    assert soapy.calls[-2:] == [
        ("deactivate", "stream-2"),
        ("activate", "stream-1", 0, 0, 0),
    ]
    assert radio.rx_active
    assert not radio.tx_active


def test_transmit_error_still_restores_receive(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.write_results = [-2]

    with pytest.raises(OSError, match="TX failed: -2"):
        radio.transmit(np.ones(5, np.complex64))

    assert radio.rx_active
    assert not radio.tx_active
    assert soapy.calls[-2:] == [
        ("deactivate", "stream-2"),
        ("activate", "stream-1", 0, 0, 0),
    ]


def test_timed_transmit_sets_hardware_time_on_first_write(soapy, monkeypatch):
    radio = SoapyRadio(SDRConfig())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    radio.transmit_at(np.ones(5, np.complex64), 1_010_000_000)

    writes = [call for call in soapy.calls if call[0] == "write"]
    assert writes == [
        ("write", "stream-2", 3, 8, 1_010_000_000),
        ("write", "stream-2", 2, 4, 1_010_000_000),
    ]
    assert radio.receive_diagnostics()["timed_transmissions"] == 1
    assert radio.receive_diagnostics()["timed_receive_windows"] == 1


def test_timed_transmit_with_lead_chooses_target_at_call_time(soapy, monkeypatch):
    radio = SoapyRadio(SDRConfig())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    target = radio.transmit_with_lead(np.ones(2, np.complex64), 3_000_000)

    assert target == 1_003_000_000
    assert ("write", "stream-2", 2, 12, target) in soapy.calls


def test_timed_transmit_rejects_past_device_time(soapy):
    radio = SoapyRadio(SDRConfig())

    with pytest.raises(OSError, match="late"):
        radio.transmit_at(np.ones(2, np.complex64), 999_000_000)

    assert radio.receive_diagnostics()["timed_transmit_late"] == 1


def test_receive_returns_samples_and_treats_timeout_as_idle(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.read_result = SimpleNamespace(ret=3)
    assert np.array_equal(radio.receive(8, 1234), np.arange(3, dtype=np.complex64))

    soapy.read_result = SimpleNamespace(ret=-1)
    assert radio.receive().size == 0


def test_receive_timed_preserves_first_sample_hardware_timestamp(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.read_result = SimpleNamespace(ret=3, flags=8, timeNs=4_200_000_000)

    block = radio.receive_timed(8, 1234)

    assert np.array_equal(block.samples, np.arange(3, dtype=np.complex64))
    assert block.time_ns == 4_200_000_000


def test_hardware_time_can_be_set_to_common_epoch(soapy):
    radio = SoapyRadio(SDRConfig())
    radio.set_hardware_time(1_700_000_000_000_000_000)
    assert ("set_hardware_time", 1_700_000_000_000_000_000, "") in soapy.calls


def test_receive_treats_overflow_as_recoverable(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.read_result = SimpleNamespace(ret=-4)

    assert radio.receive().size == 0
    assert radio.receive_diagnostics()["rx_overflows"] == 1


def test_receive_recovers_unknown_stream_error_after_half_duplex_transition(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.read_result = SimpleNamespace(ret=-2)

    assert radio.receive().size == 0
    assert radio.receive_diagnostics()["rx_stream_errors"] == 1


class SourceRadio:
    def __init__(self):
        self.next_sample = 0
        self.transmitted = []
        self.tuned = []
        self.closed = False

    def receive(self, count=4096, timeout_us=50_000):
        time.sleep(0.001)
        self.next_sample += 1
        return np.full(4, self.next_sample, np.complex64)

    def transmit(self, samples):
        self.transmitted.append(samples.copy())

    def tune(self, frequency_hz):
        self.tuned.append(frequency_hz)

    def close(self):
        self.closed = True


def test_buffered_radio_drains_in_background_and_serializes_transmit():
    source = SourceRadio()
    radio = BufferedRadio(source, queue_blocks=8, read_timeout_us=1_000)
    try:
        samples = radio.receive(timeout_us=100_000)
        assert samples.size == 4
        radio.transmit(np.ones(3, np.complex64))
        radio.tune(915e6)
        assert len(source.transmitted) == 1
        assert source.tuned == [915e6]
    finally:
        radio.close()
    assert source.closed


def test_buffered_radio_counts_dropped_blocks_when_consumer_stalls():
    source = SourceRadio()
    radio = BufferedRadio(source, queue_blocks=1, read_timeout_us=1_000)
    try:
        time.sleep(0.02)
        assert radio.receive_diagnostics()["rx_buffer_drops"] > 0
        assert radio.receive().size == 4
    finally:
        radio.close()


def test_receive_reports_non_recoverable_driver_error(soapy):
    radio = SoapyRadio(SDRConfig())
    soapy.read_result = SimpleNamespace(ret=-5)

    with pytest.raises(OSError, match="RX failed: -5"):
        radio.receive()


def test_device_selector_uses_swig_kwargs_when_available():
    selector = object()
    module = SimpleNamespace(
        SoapySDRKwargs=lambda arguments: selector if arguments == {"driver": "uhd"} else None,
        Device=lambda arguments: ("device", arguments),
    )

    assert make_soapy_device(module, {"driver": "uhd"}) == ("device", selector)
