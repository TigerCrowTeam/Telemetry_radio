from telemetry_radio.framing import RadioPacket
from telemetry_radio.replay_protection import ReplayProtector, ReplayResult


def packet(sequence, session=10):
    return RadioPacket(2, 1, sequence, b"data", session_id=session)


def test_replay_window_accepts_new_and_out_of_order_packets_once():
    protector = ReplayProtector(window_size=8)
    assert protector.observe(packet(5)) is ReplayResult.NEW
    assert protector.observe(packet(7)) is ReplayResult.NEW
    assert protector.observe(packet(6)) is ReplayResult.NEW
    assert protector.observe(packet(6)) is ReplayResult.DUPLICATE


def test_replay_window_rejects_stale_packets_and_zero_session():
    protector = ReplayProtector(window_size=4)
    assert protector.observe(packet(10)) is ReplayResult.NEW
    assert protector.observe(packet(6)) is ReplayResult.REJECTED
    assert protector.observe(packet(11, session=0)) is ReplayResult.REJECTED


def test_retired_session_cannot_return_after_sender_restart():
    protector = ReplayProtector()
    assert protector.observe(packet(10, session=100)) is ReplayResult.NEW
    assert protector.observe(packet(0, session=200)) is ReplayResult.NEW
    assert protector.observe(packet(11, session=100)) is ReplayResult.REJECTED


def test_unseen_older_session_cannot_replace_current_identity_session():
    protector = ReplayProtector()
    assert protector.observe(packet(1, session=200)) is ReplayResult.NEW
    assert protector.observe(packet(99, session=150)) is ReplayResult.REJECTED


def test_replay_window_survives_receiver_restart(tmp_path):
    state = tmp_path / "replay.json"
    first = ReplayProtector(window_size=8, state_file=str(state))
    assert first.observe(packet(5, session=200)) is ReplayResult.NEW
    restarted = ReplayProtector(window_size=8, state_file=str(state))
    assert restarted.observe(packet(5, session=200)) is ReplayResult.DUPLICATE
    assert restarted.observe(packet(4, session=100)) is ReplayResult.REJECTED
