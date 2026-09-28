from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Role = Literal["gcs", "master", "slave"]


@dataclass(frozen=True)
class MavlinkConfig:
    connection: str
    baud: int = 115200
    source_system: int = 255
    orientation_stream_rate_hz: float = 0.0
    target_system: int = 1
    target_component: int = 1


@dataclass(frozen=True)
class SDRConfig:
    device: dict[str, str] = field(default_factory=dict)
    sample_rate: float = 1_000_000
    bandwidth: float = 250_000
    tx_gain: float = 30
    rx_gain: float = 30
    tx_antenna: str | None = None
    rx_antenna: str | None = None
    channel: int = 0
    samples_per_symbol: int = 8
    fec_interleaver_depth: int = 16
    bit_repetitions: int = 1
    rs_parity_bytes: int = 0
    rrc_rolloff: float = 0.0
    rrc_span_symbols: int = 8
    modulation: Literal["bpsk", "qpsk"] = "bpsk"
    decoder_worker_enabled: bool = False
    decoder_output_batches: int = 1024
    open_retry_attempts: int = 5
    open_retry_seconds: float = 1.0


@dataclass(frozen=True)
class FrequencyConfig:
    frequencies_hz: tuple[float, ...]
    epoch: float
    interval_seconds: float = 300
    guard_seconds: float = 0.25
    reacquire_dwell_seconds: float = 1.0
    sync_loss_seconds: float = 15.0
    sequence_mode: Literal["cycle", "ping_pong"] = "cycle"
    timed_tuning: bool = False
    tune_lead_seconds: float = 0.2


@dataclass(frozen=True)
class LinkConfig:
    ack_timeout_seconds: float = 0.6
    max_retries: int = 4
    duplicate_window_seconds: float = 30.0
    channel_busy_threshold: float = 0.02
    backoff_min_seconds: float = 0.01
    backoff_max_seconds: float = 0.08
    queue_size: int = 256
    reserved_high_priority_slots: int = 0
    telemetry_rate_bytes_per_second: float = 0
    telemetry_burst_bytes: float = 0
    bulk_rate_bytes_per_second: float = 0
    bulk_burst_bytes: float = 0
    low_priority_max_age_seconds: float = 0
    coalesce_telemetry: bool = True
    tdd_period_seconds: float = 0.0
    tdd_guard_seconds: float = 0.0
    tdd_inter_burst_seconds: float | None = None
    tdd_master_bursts_per_slot: int = 1
    tdd_gcs_bursts_per_slot: int = 2
    tdd_staggered_windows: bool = False
    tdd_receive_delay_compensation_seconds: float = 0.0
    hardware_timed_io: bool = False
    hardware_timed_tx_lead_seconds: float = 0.01
    hardware_sync_tx_lead_seconds: float = 0.01
    hardware_clock_sync: bool = False
    hardware_epoch_lead_seconds: float = 2.0
    hardware_slot_scheduling: bool = False
    hardware_slot_lookahead_seconds: float = 0.012
    hardware_slot_submission_margin_seconds: float = 0.0
    mavlink_bundle_max_frames: int = 1
    mavlink_bundle_max_bytes: int = 1200
    mavlink_drain_max_frames: int = 32
    replaceable_max_age_seconds: float = 0.15
    quaternion_fallback_seconds: float = 0.5
    orientation_pacing_seconds: float = 0.05
    orientation_control_cycle_interval: int = 0
    gcs_request_min_interval_seconds: float = 0.0
    flight_controller_heartbeat_repetitions: int = 1


@dataclass(frozen=True)
class StatusConfig:
    output: Literal["off", "text", "json"] = "text"
    interval_seconds: float = 10.0
    http_enabled: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = 8765


@dataclass(frozen=True)
class RecordingConfig:
    directory: str | None = None
    iq: bool = True
    frames: bool = True
    max_file_bytes: int = 256 * 1024 * 1024
    max_sessions: int = 10
    min_free_bytes: int = 128 * 1024 * 1024


@dataclass(frozen=True)
class AuthenticationConfig:
    private_key_file: str | None = None
    public_key_files: dict[int, tuple[str, ...]] = field(default_factory=dict)
    replay_window: int = 128
    sync_max_age_seconds: float = 10.0
    sync_max_future_seconds: float = 10.0
    replay_state_file: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "public_key_files",
            {
                node: (paths,) if isinstance(paths, str) else tuple(paths)
                for node, paths in self.public_key_files.items()
            },
        )


@dataclass(frozen=True)
class RadioConfig:
    role: Role
    node_id: int
    mavlink: MavlinkConfig
    sdr: SDRConfig
    frequency: FrequencyConfig
    relay_enabled: bool = False
    master_id: int = 1
    gcs_id: int = 255
    default_destination: int | None = None
    log_level: str = "INFO"
    link: LinkConfig = field(default_factory=LinkConfig)
    mavlink_system_to_node: dict[int, int] = field(default_factory=dict)
    relay_via_master: bool = True
    status: StatusConfig = field(default_factory=StatusConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    authentication: AuthenticationConfig = field(default_factory=AuthenticationConfig)


def _require(mapping: dict[str, Any], name: str) -> Any:
    if name not in mapping:
        raise ValueError(f"missing required configuration key: {name}")
    return mapping[name]


def load_config(path: str | Path) -> RadioConfig:
    with Path(path).open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    role = _require(raw, "role")
    if role not in {"gcs", "master", "slave"}:
        raise ValueError("role must be gcs, master, or slave")
    node_id = int(_require(raw, "node_id"))
    if not 0 <= node_id <= 255:
        raise ValueError("node_id must be between 0 and 255")

    mav = _require(raw, "mavlink")
    sdr = raw.get("sdr", {})
    freq = _require(raw, "frequency")
    frequencies = tuple(float(value) for value in _require(freq, "frequencies_hz"))
    if not frequencies:
        raise ValueError("at least one frequency is required")
    interval = float(freq.get("interval_seconds", 300))
    guard = float(freq.get("guard_seconds", 0.25))
    if interval <= 0 or guard < 0 or guard * 2 >= interval:
        raise ValueError("invalid interval/guard duration")
    mavlink_routes = {
        int(system_id): int(radio_id)
        for system_id, radio_id in raw.get("routing", {}).get("mavlink_system_to_node", {}).items()
    }
    if any(not 1 <= system_id <= 255 for system_id in mavlink_routes):
        raise ValueError("MAVLink system IDs in routing map must be between 1 and 255")
    if any(not 1 <= radio_id <= 255 for radio_id in mavlink_routes.values()):
        raise ValueError("radio node IDs in routing map must be between 1 and 255")
    link_raw = raw.get("link", {})
    status_raw = raw.get("status", {})
    recording_raw = raw.get("recording", {})
    authentication_raw = raw.get("authentication", {})
    status_output = cast(Literal["off", "text", "json"], str(status_raw.get("output", "text")))
    status_interval = float(status_raw.get("interval_seconds", 10))
    status_port = int(status_raw.get("http_port", 8765))
    if status_output not in {"off", "text", "json"}:
        raise ValueError("status output must be off, text, or json")
    if status_interval <= 0:
        raise ValueError("status interval_seconds must be positive")
    if not 1 <= status_port <= 65535:
        raise ValueError("status http_port must be between 1 and 65535")
    queue_size = int(link_raw.get("queue_size", 256))
    reserved_slots = int(link_raw.get("reserved_high_priority_slots", 0))
    if queue_size < 1 or not 0 <= reserved_slots < queue_size:
        raise ValueError("reserved_high_priority_slots must be smaller than queue_size")
    for key in (
        "telemetry_rate_bytes_per_second",
        "telemetry_burst_bytes",
        "bulk_rate_bytes_per_second",
        "bulk_burst_bytes",
        "low_priority_max_age_seconds",
        "tdd_period_seconds",
        "tdd_guard_seconds",
        "tdd_inter_burst_seconds",
    ):
        if float(link_raw.get(key, 0)) < 0:
            raise ValueError(f"{key} cannot be negative")
    tdd_period = float(link_raw.get("tdd_period_seconds", 0))
    tdd_guard = float(link_raw.get("tdd_guard_seconds", 0))
    if tdd_period == 0 and tdd_guard != 0:
        raise ValueError("tdd_guard_seconds requires tdd_period_seconds")
    if tdd_period > 0 and tdd_guard * 2 >= tdd_period / 2:
        raise ValueError("tdd_guard_seconds leaves no transmit window")
    if (
        link_raw.get("tdd_inter_burst_seconds") is not None
        and float(link_raw["tdd_inter_burst_seconds"]) < 0
    ):
        raise ValueError("tdd_inter_burst_seconds cannot be negative")
    for key in ("tdd_master_bursts_per_slot", "tdd_gcs_bursts_per_slot"):
        if int(link_raw.get(key, 1 if key.startswith("tdd_master") else 2)) < 1:
            raise ValueError(f"{key} must be positive")

    fec_depth = int(sdr.get("fec_interleaver_depth", 16))
    if fec_depth < 1:
        raise ValueError("fec_interleaver_depth must be positive")
    if not 16 <= int(sdr.get("decoder_output_batches", 1024)) <= 65536:
        raise ValueError("decoder output batches must be between 16 and 65536")
    if not 1 <= int(sdr.get("open_retry_attempts", 5)) <= 60:
        raise ValueError("SDR open retry attempts must be between 1 and 60")
    if not 0 <= float(sdr.get("open_retry_seconds", 1.0)) <= 30:
        raise ValueError("SDR open retry delay must be between 0 and 30 seconds")

    config = RadioConfig(
        role=role,
        node_id=node_id,
        mavlink=MavlinkConfig(
            connection=str(_require(mav, "connection")),
            baud=int(mav.get("baud", 115200)),
            source_system=int(mav.get("source_system", node_id)),
            orientation_stream_rate_hz=float(
                mav.get("orientation_stream_rate_hz", 0.0)
            ),
            target_system=int(mav.get("target_system", 1)),
            target_component=int(mav.get("target_component", 1)),
        ),
        sdr=SDRConfig(
            device={str(k): str(v) for k, v in sdr.get("device", {}).items()},
            sample_rate=float(sdr.get("sample_rate", 1_000_000)),
            bandwidth=float(sdr.get("bandwidth", 250_000)),
            tx_gain=float(sdr.get("tx_gain", 30)),
            rx_gain=float(sdr.get("rx_gain", 30)),
            tx_antenna=(str(sdr["tx_antenna"]) if sdr.get("tx_antenna") is not None else None),
            rx_antenna=(str(sdr["rx_antenna"]) if sdr.get("rx_antenna") is not None else None),
            channel=int(sdr.get("channel", 0)),
            samples_per_symbol=int(sdr.get("samples_per_symbol", 8)),
            fec_interleaver_depth=fec_depth,
            bit_repetitions=int(sdr.get("bit_repetitions", 1)),
            rs_parity_bytes=int(sdr.get("rs_parity_bytes", 0)),
            rrc_rolloff=float(sdr.get("rrc_rolloff", 0.0)),
            rrc_span_symbols=int(sdr.get("rrc_span_symbols", 8)),
            modulation=cast(Literal["bpsk", "qpsk"], str(sdr.get("modulation", "bpsk")).lower()),
            decoder_worker_enabled=bool(sdr.get("decoder_worker_enabled", False)),
            decoder_output_batches=int(sdr.get("decoder_output_batches", 1024)),
            open_retry_attempts=int(sdr.get("open_retry_attempts", 5)),
            open_retry_seconds=float(sdr.get("open_retry_seconds", 1.0)),
        ),
        frequency=FrequencyConfig(
            frequencies_hz=frequencies,
            epoch=float(_require(freq, "epoch")),
            sequence_mode=cast(
                Literal["cycle", "ping_pong"], str(freq.get("sequence_mode", "cycle"))
            ),
            timed_tuning=bool(freq.get("timed_tuning", False)),
            tune_lead_seconds=float(freq.get("tune_lead_seconds", 0.2)),
            interval_seconds=interval,
            guard_seconds=guard,
            reacquire_dwell_seconds=float(freq.get("reacquire_dwell_seconds", 1.0)),
            sync_loss_seconds=float(freq.get("sync_loss_seconds", 15.0)),
        ),
        link=LinkConfig(
            ack_timeout_seconds=float(link_raw.get("ack_timeout_seconds", 0.6)),
            max_retries=int(link_raw.get("max_retries", 4)),
            duplicate_window_seconds=float(link_raw.get("duplicate_window_seconds", 30.0)),
            channel_busy_threshold=float(link_raw.get("channel_busy_threshold", 0.02)),
            backoff_min_seconds=float(link_raw.get("backoff_min_seconds", 0.01)),
            backoff_max_seconds=float(link_raw.get("backoff_max_seconds", 0.08)),
            queue_size=queue_size,
            reserved_high_priority_slots=reserved_slots,
            telemetry_rate_bytes_per_second=float(
                link_raw.get("telemetry_rate_bytes_per_second", 0)
            ),
            telemetry_burst_bytes=float(link_raw.get("telemetry_burst_bytes", 0)),
            bulk_rate_bytes_per_second=float(link_raw.get("bulk_rate_bytes_per_second", 0)),
            bulk_burst_bytes=float(link_raw.get("bulk_burst_bytes", 0)),
            low_priority_max_age_seconds=float(link_raw.get("low_priority_max_age_seconds", 0)),
            coalesce_telemetry=bool(link_raw.get("coalesce_telemetry", True)),
            tdd_period_seconds=float(link_raw.get("tdd_period_seconds", 0)),
            tdd_guard_seconds=float(link_raw.get("tdd_guard_seconds", 0)),
            tdd_inter_burst_seconds=(
                float(link_raw["tdd_inter_burst_seconds"])
                if link_raw.get("tdd_inter_burst_seconds") is not None
                else None
            ),
            tdd_master_bursts_per_slot=int(
                link_raw.get("tdd_master_bursts_per_slot", 1)
            ),
            tdd_gcs_bursts_per_slot=int(link_raw.get("tdd_gcs_bursts_per_slot", 2)),
            tdd_staggered_windows=bool(link_raw.get("tdd_staggered_windows", False)),
            tdd_receive_delay_compensation_seconds=float(
                link_raw.get("tdd_receive_delay_compensation_seconds", 0.0)
            ),
            hardware_timed_io=bool(link_raw.get("hardware_timed_io", False)),
            hardware_timed_tx_lead_seconds=float(
                link_raw.get("hardware_timed_tx_lead_seconds", 0.01)
            ),
            hardware_sync_tx_lead_seconds=float(
                link_raw.get("hardware_sync_tx_lead_seconds", 0.01)
            ),
            hardware_clock_sync=bool(link_raw.get("hardware_clock_sync", False)),
            hardware_epoch_lead_seconds=float(
                link_raw.get("hardware_epoch_lead_seconds", 2.0)
            ),
            hardware_slot_scheduling=bool(
                link_raw.get("hardware_slot_scheduling", False)
            ),
            hardware_slot_lookahead_seconds=float(
                link_raw.get("hardware_slot_lookahead_seconds", 0.012)
            ),
            hardware_slot_submission_margin_seconds=float(
                link_raw.get("hardware_slot_submission_margin_seconds", 0.0)
            ),
            mavlink_bundle_max_frames=int(link_raw.get("mavlink_bundle_max_frames", 1)),
            mavlink_bundle_max_bytes=int(link_raw.get("mavlink_bundle_max_bytes", 1200)),
            mavlink_drain_max_frames=int(link_raw.get("mavlink_drain_max_frames", 32)),
            replaceable_max_age_seconds=float(
                link_raw.get("replaceable_max_age_seconds", 0.15)
            ),
            quaternion_fallback_seconds=float(
                link_raw.get("quaternion_fallback_seconds", 0.5)
            ),
            orientation_pacing_seconds=float(
                link_raw.get("orientation_pacing_seconds", 0.05)
            ),
            orientation_control_cycle_interval=int(
                link_raw.get("orientation_control_cycle_interval", 0)
            ),
            gcs_request_min_interval_seconds=float(
                link_raw.get("gcs_request_min_interval_seconds", 0.0)
            ),
            flight_controller_heartbeat_repetitions=int(
                link_raw.get("flight_controller_heartbeat_repetitions", 1)
            ),
        ),
        relay_enabled=bool(raw.get("relay_enabled", False)),
        master_id=int(raw.get("master_id", 1)),
        gcs_id=int(raw.get("gcs_id", 255)),
        default_destination=(
            int(raw["default_destination"]) if raw.get("default_destination") is not None else None
        ),
        log_level=str(raw.get("log_level", "INFO")),
        mavlink_system_to_node=mavlink_routes,
        relay_via_master=bool(raw.get("routing", {}).get("relay_via_master", True)),
        status=StatusConfig(
            output=status_output,
            interval_seconds=status_interval,
            http_enabled=bool(status_raw.get("http_enabled", True)),
            http_host=str(status_raw.get("http_host", "127.0.0.1")),
            http_port=status_port,
        ),
        recording=RecordingConfig(
            directory=(
                str(recording_raw["directory"])
                if recording_raw.get("directory") is not None
                else None
            ),
            iq=bool(recording_raw.get("iq", True)),
            frames=bool(recording_raw.get("frames", True)),
            max_file_bytes=int(recording_raw.get("max_file_bytes", 256 * 1024 * 1024)),
            max_sessions=int(recording_raw.get("max_sessions", 10)),
            min_free_bytes=int(recording_raw.get("min_free_bytes", 128 * 1024 * 1024)),
        ),
        authentication=AuthenticationConfig(
            private_key_file=(
                str(authentication_raw["private_key_file"])
                if authentication_raw.get("private_key_file") is not None
                else None
            ),
            public_key_files={
                int(node): (
                    (str(paths),) if isinstance(paths, str) else tuple(str(path) for path in paths)
                )
                for node, paths in authentication_raw.get("public_key_files", {}).items()
            },
            replay_window=int(authentication_raw.get("replay_window", 128)),
            sync_max_age_seconds=float(authentication_raw.get("sync_max_age_seconds", 10)),
            sync_max_future_seconds=float(authentication_raw.get("sync_max_future_seconds", 10)),
            replay_state_file=(
                str(authentication_raw["replay_state_file"])
                if authentication_raw.get("replay_state_file") is not None
                else None
            ),
        ),
    )
    validate_config(config)
    return config


def validate_config(config: RadioConfig) -> None:
    """Validate one node without opening MAVLink or SDR hardware."""
    if not 1 <= config.node_id <= 255:
        raise ValueError("node_id must be between 1 and 255; 0 is reserved for broadcast")
    if not 1 <= config.master_id <= 255 or not 1 <= config.gcs_id <= 255:
        raise ValueError("master_id and gcs_id must be between 1 and 255")
    if config.master_id == config.gcs_id:
        raise ValueError("master_id and gcs_id must differ")
    if config.role == "master" and config.node_id != config.master_id:
        raise ValueError("master node_id must equal master_id")
    if config.role == "gcs" and config.node_id != config.gcs_id:
        raise ValueError("GCS node_id must equal gcs_id")
    if config.role == "slave" and config.node_id in (config.master_id, config.gcs_id):
        raise ValueError("slave node_id cannot use the master or GCS node ID")
    if config.relay_enabled and config.role != "master":
        raise ValueError("relay_enabled is only valid for a master node")
    if config.default_destination is not None and not 0 <= config.default_destination <= 255:
        raise ValueError("default_destination must be between 0 and 255")
    if not config.mavlink.connection.strip():
        raise ValueError("MAVLink connection cannot be empty")
    if config.mavlink.baud <= 0:
        raise ValueError("MAVLink baud must be positive")
    if not 1 <= config.mavlink.source_system <= 255:
        raise ValueError("MAVLink source_system must be between 1 and 255")

    sdr = config.sdr
    for name, value in (
        ("sample_rate", sdr.sample_rate),
        ("bandwidth", sdr.bandwidth),
        ("tx_gain", sdr.tx_gain),
        ("rx_gain", sdr.rx_gain),
    ):
        if not math.isfinite(value):
            raise ValueError(f"SDR {name} must be finite")
    if sdr.sample_rate <= 0 or sdr.bandwidth <= 0:
        raise ValueError("SDR sample_rate and bandwidth must be positive")
    if sdr.bandwidth > sdr.sample_rate:
        raise ValueError("SDR bandwidth cannot exceed sample_rate")
    if sdr.channel < 0 or sdr.samples_per_symbol < 2 or sdr.bit_repetitions < 1 or sdr.rs_parity_bytes < 0 or sdr.rs_parity_bytes % 2:
        raise ValueError("invalid SDR channel, samples_per_symbol, or bit_repetitions")
    if not 0 <= sdr.rrc_rolloff <= 1:
        raise ValueError("SDR rrc_rolloff must be between 0 and 1")
    if sdr.rrc_rolloff and (sdr.rrc_span_symbols < 2 or sdr.rrc_span_symbols % 2):
        raise ValueError("SDR rrc_span_symbols must be a positive even value")
    if sdr.modulation not in ("bpsk", "qpsk"):
        raise ValueError("SDR modulation must be bpsk or qpsk")
    if sdr.modulation == "qpsk" and sdr.bit_repetitions != 1:
        raise ValueError("QPSK currently requires bit_repetitions to be 1")

    frequency = config.frequency
    if not frequency.frequencies_hz:
        raise ValueError("at least one frequency is required")
    if any(not math.isfinite(value) or value <= 0 for value in frequency.frequencies_hz):
        raise ValueError("frequencies must be finite positive values")
    if len(set(frequency.frequencies_hz)) != len(frequency.frequencies_hz):
        raise ValueError("frequency list cannot contain duplicates")
    if frequency.sequence_mode not in ("cycle", "ping_pong"):
        raise ValueError("frequency sequence_mode must be cycle or ping_pong")
    if not math.isfinite(frequency.epoch):
        raise ValueError("frequency epoch must be finite")
    if frequency.interval_seconds <= 0:
        raise ValueError("frequency interval must be positive")
    if frequency.guard_seconds < 0 or frequency.guard_seconds * 2 >= frequency.interval_seconds:
        raise ValueError("guard interval must be nonnegative and less than half the hop interval")
    if frequency.reacquire_dwell_seconds <= 0 or frequency.sync_loss_seconds <= 0:
        raise ValueError("reacquisition dwell and synchronization loss times must be positive")
    if frequency.timed_tuning and not 0 < frequency.tune_lead_seconds < frequency.interval_seconds:
        raise ValueError("frequency tune lead must be positive and less than the hop interval")

    link = config.link
    if not 0 <= config.mavlink.orientation_stream_rate_hz <= 100:
        raise ValueError("orientation stream rate must be between 0 and 100 Hz")
    if not 1 <= config.mavlink.target_system <= 255:
        raise ValueError("MAVLink target system must be between 1 and 255")
    if not 1 <= config.mavlink.target_component <= 255:
        raise ValueError("MAVLink target component must be between 1 and 255")
    if link.ack_timeout_seconds <= 0 or link.max_retries < 0:
        raise ValueError("ACK timeout must be positive and max_retries nonnegative")
    if link.duplicate_window_seconds <= 0 or link.channel_busy_threshold < 0:
        raise ValueError("duplicate window must be positive and busy threshold nonnegative")
    if link.backoff_min_seconds < 0 or link.backoff_max_seconds < link.backoff_min_seconds:
        raise ValueError("invalid channel backoff range")
    if link.tdd_master_bursts_per_slot < 1 or link.tdd_gcs_bursts_per_slot < 1:
        raise ValueError("TDD bursts_per_slot values must be positive")
    if not 1 <= link.mavlink_bundle_max_frames <= 255:
        raise ValueError("mavlink_bundle_max_frames must be between 1 and 255")
    if not 64 <= link.mavlink_bundle_max_bytes <= 4096:
        raise ValueError("mavlink_bundle_max_bytes must be between 64 and 4096")
    if not 1 <= link.mavlink_drain_max_frames <= 256:
        raise ValueError("mavlink_drain_max_frames must be between 1 and 256")
    if not 0.01 <= link.replaceable_max_age_seconds <= 5:
        raise ValueError("replaceable_max_age_seconds must be between 0.01 and 5")
    if not 0.05 <= link.quaternion_fallback_seconds <= 10:
        raise ValueError("quaternion_fallback_seconds must be between 0.05 and 10")
    if not 0 <= link.orientation_pacing_seconds <= 1:
        raise ValueError("orientation_pacing_seconds must be between 0 and 1")
    if not 0 <= link.orientation_control_cycle_interval <= 100:
        raise ValueError("orientation control cycle interval must be between 0 and 100")
    if not 0 <= link.gcs_request_min_interval_seconds <= 5:
        raise ValueError("GCS request minimum interval must be between 0 and 5 seconds")
    if not 1 <= link.flight_controller_heartbeat_repetitions <= 4:
        raise ValueError("flight-controller heartbeat repetitions must be between 1 and 4")
    if link.tdd_inter_burst_seconds is not None and link.tdd_inter_burst_seconds < 0:
        raise ValueError("tdd_inter_burst_seconds cannot be negative")
    if not 0 <= link.tdd_receive_delay_compensation_seconds <= 0.1:
        raise ValueError("TDD receive-delay compensation must be between 0 and 0.1")
    if not 0 <= link.hardware_slot_submission_margin_seconds <= 0.1:
        raise ValueError("hardware slot submission margin must be between 0 and 0.1")
    if not 0.002 <= link.hardware_timed_tx_lead_seconds <= 0.1:
        raise ValueError("hardware-timed TX lead must be between 0.002 and 0.1")
    if not 0.002 <= link.hardware_sync_tx_lead_seconds <= 0.1:
        raise ValueError("hardware sync TX lead must be between 0.002 and 0.1")
    if not 0.5 <= link.hardware_epoch_lead_seconds <= 10.0:
        raise ValueError("hardware epoch lead must be between 0.5 and 10 seconds")
    if not 0.003 <= link.hardware_slot_lookahead_seconds <= 0.05:
        raise ValueError("hardware slot lookahead must be between 0.003 and 0.05 seconds")
    if link.hardware_slot_scheduling and not link.hardware_timed_io:
        raise ValueError("hardware slot scheduling requires hardware-timed I/O")
    if link.hardware_clock_sync and not link.hardware_timed_io:
        raise ValueError("hardware clock synchronization requires hardware timed I/O")
    if link.tdd_period_seconds == 0 and link.tdd_guard_seconds != 0:
        raise ValueError("tdd_guard_seconds requires tdd_period_seconds")
    guard_factor = 1 if link.tdd_staggered_windows else 2
    if (
        link.tdd_period_seconds > 0
        and link.tdd_guard_seconds * guard_factor >= link.tdd_period_seconds / 2
    ):
        raise ValueError("tdd_guard_seconds leaves no transmit window")
    if link.queue_size < 1 or not 0 <= link.reserved_high_priority_slots < link.queue_size:
        raise ValueError("reserved high-priority slots must be smaller than queue size")
    for name, value in (
        ("telemetry rate", link.telemetry_rate_bytes_per_second),
        ("telemetry burst", link.telemetry_burst_bytes),
        ("bulk rate", link.bulk_rate_bytes_per_second),
        ("bulk burst", link.bulk_burst_bytes),
        ("low-priority maximum age", link.low_priority_max_age_seconds),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if link.telemetry_rate_bytes_per_second > 0 and link.telemetry_burst_bytes <= 0:
        raise ValueError(
            "telemetry_burst_bytes must be positive when telemetry limiting is enabled"
        )
    if link.bulk_rate_bytes_per_second > 0 and link.bulk_burst_bytes <= 0:
        raise ValueError("bulk_burst_bytes must be positive when bulk limiting is enabled")

    if len(set(config.mavlink_system_to_node.values())) != len(config.mavlink_system_to_node):
        raise ValueError("each radio node may map to only one MAVLink system ID")
    if config.status.output not in {"off", "text", "json"}:
        raise ValueError("status output must be off, text, or json")
    if config.status.interval_seconds <= 0 or not 1 <= config.status.http_port <= 65535:
        raise ValueError("invalid status interval or HTTP port")
    if config.status.http_enabled and not config.status.http_host.strip():
        raise ValueError("status HTTP host cannot be empty")
    if config.log_level.upper() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        raise ValueError("log_level must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
    if config.recording.directory is not None and not config.recording.directory.strip():
        raise ValueError("recording directory cannot be empty")
    if config.recording.directory is not None and not (
        config.recording.iq or config.recording.frames
    ):
        raise ValueError("recording must enable IQ, frames, or both")
    if (
        config.recording.max_file_bytes < 16 * 1024
        or config.recording.max_sessions < 1
        or config.recording.min_free_bytes < 0
    ):
        raise ValueError("invalid recording size, retention, or free-space limit")
    authentication = config.authentication
    if authentication.private_key_file is not None and not authentication.private_key_file.strip():
        raise ValueError("authentication private_key_file cannot be empty")
    if authentication.private_key_file is None and authentication.public_key_files:
        raise ValueError("public keys require an authentication private key")
    if authentication.private_key_file is not None:
        if config.node_id not in authentication.public_key_files:
            raise ValueError("authentication public keys must include this node")
        if any(not 1 <= node <= 255 for node in authentication.public_key_files):
            raise ValueError("authentication public-key node IDs must be between 1 and 255")
        if any(not paths for paths in authentication.public_key_files.values()):
            raise ValueError("each authenticated node requires at least one public key")
        if any(
            not path.strip() for paths in authentication.public_key_files.values() for path in paths
        ):
            raise ValueError("authentication public-key paths cannot be empty")
    if authentication.replay_window < 1:
        raise ValueError("authentication replay_window must be positive")
    if authentication.sync_max_age_seconds <= 0 or authentication.sync_max_future_seconds <= 0:
        raise ValueError("authentication synchronization freshness limits must be positive")
    if (
        authentication.replay_state_file is not None
        and not authentication.replay_state_file.strip()
    ):
        raise ValueError("authentication replay_state_file cannot be empty")


def validate_network(configs: dict[str, RadioConfig]) -> None:
    """Validate identities and shared air-interface settings across all nodes."""
    if not configs:
        raise ValueError("at least one node configuration is required")
    for config in configs.values():
        validate_config(config)
    roles = [config.role for config in configs.values()]
    if roles.count("gcs") != 1 or roles.count("master") != 1:
        raise ValueError("network must contain exactly one GCS and one master")
    node_ids = [config.node_id for config in configs.values()]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("network contains duplicate radio node IDs")
    source_systems = [config.mavlink.source_system for config in configs.values()]
    if len(set(source_systems)) != len(source_systems):
        raise ValueError("network contains duplicate local MAVLink source-system IDs")

    first = next(iter(configs.values()))
    authentication_enabled = first.authentication.private_key_file is not None
    schedule = (
        first.frequency.frequencies_hz,
        first.frequency.epoch,
        first.frequency.sequence_mode,
        first.frequency.timed_tuning,
        first.frequency.tune_lead_seconds,
        first.frequency.interval_seconds,
        first.frequency.guard_seconds,
    )
    waveform = (
        first.sdr.sample_rate,
        first.sdr.bandwidth,
        first.sdr.samples_per_symbol,
        first.sdr.modulation,
        first.sdr.bit_repetitions,
        first.sdr.fec_interleaver_depth,
        first.sdr.rs_parity_bytes,
        first.sdr.rrc_rolloff,
        first.sdr.rrc_span_symbols,
    )
    for name, config in configs.items():
        if (config.authentication.private_key_file is not None) != authentication_enabled:
            raise ValueError(f"{name}: authentication enablement differs from the network")
        if set(config.authentication.public_key_files) != set(
            first.authentication.public_key_files
        ):
            raise ValueError(f"{name}: trusted node identities differ from the network")
        if (config.master_id, config.gcs_id) != (first.master_id, first.gcs_id):
            raise ValueError(f"{name}: master_id/gcs_id differs from the network")
        if (
            config.frequency.frequencies_hz,
            config.frequency.epoch,
            config.frequency.sequence_mode,
            config.frequency.timed_tuning,
            config.frequency.tune_lead_seconds,
            config.frequency.interval_seconds,
            config.frequency.guard_seconds,
        ) != schedule:
            raise ValueError(f"{name}: frequency schedule differs from the network")
        if (
            config.sdr.sample_rate,
            config.sdr.bandwidth,
            config.sdr.samples_per_symbol,
            config.sdr.modulation,
            config.sdr.bit_repetitions,
            config.sdr.fec_interleaver_depth,
            config.sdr.rs_parity_bytes,
            config.sdr.rrc_rolloff,
            config.sdr.rrc_span_symbols,
        ) != waveform:
            raise ValueError(f"{name}: SDR waveform settings differ from the network")
        if config.mavlink_system_to_node != first.mavlink_system_to_node:
            raise ValueError(f"{name}: MAVLink routing map differs from the network")
    unknown_routes = set(first.mavlink_system_to_node.values()) - set(node_ids)
    if authentication_enabled and set(first.authentication.public_key_files) != set(node_ids):
        raise ValueError("authentication public keys must match all network node IDs")
    if unknown_routes:
        raise ValueError(f"routing map references unknown radio nodes: {sorted(unknown_routes)}")
