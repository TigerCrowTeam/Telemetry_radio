import json
from types import SimpleNamespace

from telemetry_radio.framing import RadioPacket
from telemetry_radio.modulation import BPSKModem, QPSKModem
from telemetry_radio.recording import (
    CaptureRecorder,
    describe_frames,
    replay_frames,
    replay_iq,
)


def test_iq_capture_replays_through_demodulator(tmp_path):
    wire = RadioPacket(2, 1, 42, b"captured telemetry").encode()
    samples = BPSKModem(4).modulate(wire)
    recorder = CaptureRecorder(tmp_path, sample_rate=1_000_000, samples_per_symbol=4)
    recorder.record_iq(samples[:37])
    recorder.record_iq(samples[37:])
    recorder.close()

    assert list(replay_iq(tmp_path, chunk_samples=31)) == [wire]
    assert json.loads((tmp_path / "manifest.json").read_text())["sample_rate"] == 1_000_000


def test_qpsk_iq_capture_replays_with_fec_waveform_settings(tmp_path):
    wire = RadioPacket(2, 1, 43, b"captured qpsk telemetry").encode()
    samples = QPSKModem(8, interleaver_depth=32, rs_parity_bytes=16).modulate(wire)
    recorder = CaptureRecorder(
        tmp_path,
        sample_rate=1_000_000,
        samples_per_symbol=8,
        modulation="qpsk",
        interleaver_depth=32,
        rs_parity_bytes=16,
    )
    recorder.record_iq(samples)
    recorder.close()

    assert list(replay_iq(tmp_path, chunk_samples=257)) == [wire]


def test_decoded_frame_capture_preserves_valid_and_invalid_wire_bytes(tmp_path):
    valid = RadioPacket(2, 1, 7, b"valid").encode()
    invalid = b"bad frame"
    recorder = CaptureRecorder(tmp_path, sample_rate=1_000_000, samples_per_symbol=4)
    recorder.record_frame(valid)
    recorder.record_frame(invalid, "truncated packet")
    recorder.close()

    assert list(replay_frames(tmp_path)) == [valid, invalid]
    entries = [
        json.loads(line)
        for path in sorted(tmp_path.glob("frames-*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert entries[0]["valid"] is True
    assert entries[1]["valid"] is False
    assert entries[1]["error"] == "truncated packet"


def test_replay_description_reports_packet_metadata_and_decode_failures(tmp_path):
    wire = RadioPacket(2, 1, 7, b"payload").encode()
    results = list(describe_frames([wire, b"bad"]))
    assert results[0] == {
        "index": 0,
        "valid": True,
        "kind": "DATA",
        "source": 2,
        "destination": 1,
        "sequence": 7,
        "session_id": 0,
        "payload_base64": "cGF5bG9hZA==",
    }
    assert results[1]["valid"] is False
    assert "truncated" in results[1]["error"]


def test_existing_capture_rejects_incompatible_settings(tmp_path):
    CaptureRecorder(tmp_path, sample_rate=1_000_000, samples_per_symbol=4).close()
    try:
        CaptureRecorder(tmp_path, sample_rate=2_000_000, samples_per_symbol=4)
    except ValueError as exc:
        assert "incompatible manifest" in str(exc)
    else:
        raise AssertionError("mixed capture settings should be rejected")


def test_capture_rotates_bounded_iq_and_frame_segments(tmp_path):
    recorder = CaptureRecorder(
        tmp_path,
        sample_rate=1_000_000,
        samples_per_symbol=4,
        max_file_bytes=16 * 1024,
        min_free_bytes=0,
    )
    samples = BPSKModem(4).modulate(b"x" * 200)
    recorder.record_iq(samples)
    wire = RadioPacket(2, 1, 7, b"x" * 4096).encode()
    for _ in range(4):
        recorder.record_frame(wire)
    recorder.close()
    segments = list(tmp_path.glob("iq-*.c64")) + list(tmp_path.glob("frames-*.jsonl"))
    assert len(list(tmp_path.glob("iq-*.c64"))) > 1
    assert len(list(tmp_path.glob("frames-*.jsonl"))) > 1
    assert all(path.stat().st_size <= 16 * 1024 for path in segments)


def test_capture_session_retention_removes_only_old_capture_sessions(tmp_path):
    unrelated = tmp_path / "do-not-remove"
    unrelated.mkdir()
    for _ in range(3):
        CaptureRecorder(
            tmp_path,
            sample_rate=1_000_000,
            samples_per_symbol=4,
            create_session=True,
            max_sessions=2,
            min_free_bytes=0,
        ).close()
    assert len(list(tmp_path.glob("capture-*"))) == 2
    assert unrelated.is_dir()


def test_low_space_disables_recording_without_raising(tmp_path, monkeypatch):
    failures = []
    recorder = CaptureRecorder(
        tmp_path,
        sample_rate=1_000_000,
        samples_per_symbol=4,
        min_free_bytes=100,
        on_disable=failures.append,
    )
    monkeypatch.setattr(
        "telemetry_radio.recording.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=50),
    )
    recorder.record_iq(BPSKModem(4).modulate(b"still keep radio running"))
    recorder.record_frame(b"ignored after disable")
    assert recorder.disabled_reason == "minimum free-space threshold reached"
    assert failures == ["minimum free-space threshold reached"]
