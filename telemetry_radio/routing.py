from __future__ import annotations

from .config import RadioConfig
from .framing import BROADCAST_ID, RadioPacket
from .mavlink import MavlinkAddress


class Router:
    def __init__(self, config: RadioConfig):
        self.config = config

    def _node_for_target(self, address: MavlinkAddress) -> int | None:
        if address.target_system in (None, 0):
            return None
        return self.config.mavlink_system_to_node.get(address.target_system)

    def destination_for_local_mavlink(self, address: MavlinkAddress = MavlinkAddress()) -> int:
        if self.config.default_destination is not None:
            return self.config.default_destination
        target_node = self._node_for_target(address)
        if self.config.role == "gcs" and target_node is not None:
            if self.config.relay_via_master and target_node != self.config.master_id:
                return self.config.master_id
            return target_node
        if self.config.role == "slave":
            return self.config.master_id
        return BROADCAST_ID

    def deliver_locally(
        self, packet: RadioPacket, address: MavlinkAddress = MavlinkAddress()
    ) -> bool:
        if packet.destination not in (BROADCAST_ID, self.config.node_id):
            return False
        target_node = self._node_for_target(address)
        return target_node is None or target_node == self.config.node_id

    def relay(self, packet: RadioPacket, address: MavlinkAddress = MavlinkAddress()) -> int | None:
        if self.config.role != "master" or not self.config.relay_enabled:
            return None
        if packet.source == self.config.gcs_id and packet.destination == self.config.node_id:
            return self._node_for_target(address) or BROADCAST_ID
        if packet.source not in (self.config.gcs_id, self.config.node_id):
            return self.config.gcs_id
        return None
