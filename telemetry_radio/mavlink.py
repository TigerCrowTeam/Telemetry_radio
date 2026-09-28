from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MavlinkAddress:
    source_system: int | None = None
    source_component: int | None = None
    target_system: int | None = None
    target_component: int | None = None
    message_id: int | None = None
    message_type: str | None = None
    command: int | None = None


class TrafficPriority(IntEnum):
    LINK_CONTROL = 0
    EMERGENCY = 1
    COMMAND = 2
    NORMAL = 3
    BULK = 4


_COMMAND_MESSAGES = {
    "COMMAND_LONG",
    "COMMAND_INT",
    "COMMAND_ACK",
    "SET_MODE",
    "MANUAL_CONTROL",
    "RC_CHANNELS_OVERRIDE",
    "PARAM_SET",
    "PARAM_REQUEST_LIST",
    "PARAM_REQUEST_READ",
    "MISSION_CLEAR_ALL",
    "MISSION_COUNT",
    "MISSION_ITEM",
    "MISSION_ITEM_INT",
    "MISSION_REQUEST",
    "MISSION_REQUEST_INT",
    "MISSION_SET_CURRENT",
    "SET_ATTITUDE_TARGET",
    "SET_POSITION_TARGET_GLOBAL_INT",
    "SET_POSITION_TARGET_LOCAL_NED",
}
_BULK_MESSAGES = {
    "DATA_TRANSMISSION_HANDSHAKE",
    "ENCAPSULATED_DATA",
    "FILE_TRANSFER_PROTOCOL",
    "LOG_DATA",
    "LOG_DATA_85",
    "LOG_ENTRY",
}
# Housekeeping streams are useful when requested, but should not delay the
# live flight-state messages that QGroundControl needs for its primary view.
# They remain relayed through the bulk token bucket rather than being dropped.
_LOW_RATE_MESSAGES = {
    "SYS_STATUS",
    "SYSTEM_TIME",
    "GPS_RAW_INT",
    "GPS_STATUS",
    "SCALED_IMU",
    "SCALED_IMU2",
    "SCALED_IMU3",
    "RAW_IMU",
    "RAW_IMU2",
    "RAW_IMU3",
    "RC_CHANNELS",
    "SERVO_OUTPUT_RAW",
    "BATTERY_STATUS",
    "POWER_STATUS",
    "WIND",
    "WIND_COV",
    "AHRS",
    "AHRS2",
    "AHRS3",
    "VIBRATION",
    "MEMINFO",
    "STATUSTEXT",
}
_EMERGENCY_COMMANDS = {
    185,  # MAV_CMD_DO_FLIGHTTERMINATION
    400,  # MAV_CMD_COMPONENT_ARM_DISARM
}

_LINK_CONTROL_MESSAGES = {
    "HEARTBEAT",
    "ATTITUDE",
    "ATTITUDE_QUATERNION",
}

# These high-rate state samples become obsolete as soon as the next sample is
# produced. Radio ARQ would delay newer state while retrying stale display data.
_REPLACEABLE_TELEMETRY_MESSAGES = {
    "HEARTBEAT",
    "ATTITUDE",
    "ATTITUDE_QUATERNION",
    "GLOBAL_POSITION_INT",
    "VFR_HUD",
}


def traffic_priority(address: MavlinkAddress) -> TrafficPriority:
    if address.message_type == "HEARTBEAT":
        return (
            TrafficPriority.LINK_CONTROL
            if address.source_component in (None, 1)
            else TrafficPriority.NORMAL
        )
    if address.message_type in _LINK_CONTROL_MESSAGES:
        return TrafficPriority.LINK_CONTROL
    if address.command in _EMERGENCY_COMMANDS:
        return TrafficPriority.EMERGENCY
    if address.message_type in _COMMAND_MESSAGES:
        return TrafficPriority.COMMAND
    if address.message_type in _BULK_MESSAGES:
        return TrafficPriority.BULK
    if address.message_type in _LOW_RATE_MESSAGES:
        return TrafficPriority.BULK
    return TrafficPriority.NORMAL


def is_replaceable_telemetry(address: MavlinkAddress) -> bool:
    """Return whether freshness is more valuable than radio-level delivery."""
    return address.message_type in _REPLACEABLE_TELEMETRY_MESSAGES


def inspect_frame(frame: bytes) -> MavlinkAddress:
    """Decode addressing metadata from one frame without changing its wire bytes."""
    if not frame:
        return MavlinkAddress()
    try:
        if frame[0] == 0xFD:
            from pymavlink.dialects.v20 import ardupilotmega as dialect
        else:
            from pymavlink.dialects.v10 import ardupilotmega as dialect
        parser = dialect.MAVLink(None)
        message = parser.parse_char(bytes(frame))
        if message is None:
            return MavlinkAddress()
        target_system = getattr(message, "target_system", None)
        target_component = getattr(message, "target_component", None)
        return MavlinkAddress(
            source_system=int(message.get_srcSystem()),
            source_component=int(message.get_srcComponent()),
            target_system=int(target_system) if target_system is not None else None,
            target_component=int(target_component) if target_component is not None else None,
            message_id=int(message.get_msgId()),
            message_type=str(message.get_type()),
            command=(
                int(getattr(message, "command"))
                if getattr(message, "command", None) is not None
                else None
            ),
        )
    except (ImportError, ValueError, TypeError, AttributeError):
        LOG.debug("could not inspect MAVLink addressing", exc_info=True)
        return MavlinkAddress()


class MavlinkInterface:
    """Reads/writes complete MAVLink wire frames without modifying their bytes."""

    def __init__(self, connection: str, baud: int, source_system: int):
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise RuntimeError("pymavlink is required; install the project dependencies") from exc
        self.connection = mavutil.mavlink_connection(
            connection, baud=baud, source_system=source_system, autoreconnect=True
        )
        self._mavlink_constants = mavutil.mavlink

    def receive(self, timeout: float = 0.05) -> bytes | None:
        # pymavlink's blocking=True, timeout=0 path can return before polling
        # the underlying serial descriptor.  A zero timeout is an explicit
        # nonblocking poll so the SDR loop can drain IQ continuously.
        blocking = timeout > 0
        message = self.connection.recv_match(
            blocking=blocking,
            timeout=timeout if blocking else None,
        )
        if message is None or message.get_type() == "BAD_DATA":
            return None
        return bytes(message.get_msgbuf())

    def send(self, frame: bytes) -> None:
        # mavfile.write is the raw transport path; no decode/re-encode occurs.
        self.connection.write(frame)

    def request_message_interval(
        self,
        message_id: int,
        rate_hz: float,
        target_system: int = 1,
        target_component: int = 1,
    ) -> None:
        """Request a real autopilot measurement rate using MAVLink command 511."""
        if rate_hz <= 0:
            raise ValueError("message rate must be positive")
        self.connection.mav.command_long_send(
            target_system,
            target_component,
            self._mavlink_constants.MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            float(message_id),
            1_000_000.0 / rate_hz,
            0,
            0,
            0,
            0,
            0,
        )

    def close(self) -> None:
        self.connection.close()
