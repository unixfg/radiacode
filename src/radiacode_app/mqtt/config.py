from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """Connection settings supplied by the runtime secret/configuration.

    Broker credentials and hardware serials are intentionally not represented
    in topic names or discovery payloads.  ``password`` should be unwrapped from
    the application's secret type only at this boundary.
    """

    host: str
    username: str
    password: str
    ca_file: Path
    port: int = 8883
    keepalive_seconds: int = 60
    publish_interval_seconds: float = 30.0
    stale_after_seconds: float = 10.0
    topic_prefix: str = "radiacode"
    discovery_prefix: str = "homeassistant"

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("MQTT host is required")
        if not self.username or not self.password:
            raise ValueError("dedicated MQTT username and password are required")
        if not 1 <= self.port <= 65_535:
            raise ValueError("MQTT port must be in the range 1..65535")
        if self.keepalive_seconds <= 0:
            raise ValueError("MQTT keepalive must be positive")
        if self.publish_interval_seconds <= 0:
            raise ValueError("MQTT publish interval must be positive")
        if self.stale_after_seconds <= 0:
            raise ValueError("MQTT staleness interval must be positive")
        if not self.topic_prefix.strip("/") or "+" in self.topic_prefix or "#" in self.topic_prefix:
            raise ValueError("invalid MQTT topic prefix")
        if (
            not self.discovery_prefix.strip("/")
            or "+" in self.discovery_prefix
            or "#" in self.discovery_prefix
        ):
            raise ValueError("invalid Home Assistant discovery prefix")

    @property
    def normalized_topic_prefix(self) -> str:
        return self.topic_prefix.strip("/")

    @property
    def normalized_discovery_prefix(self) -> str:
        return self.discovery_prefix.strip("/")
