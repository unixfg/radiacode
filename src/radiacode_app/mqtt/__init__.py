"""Publish-only MQTT integration for detector telemetry.

The acquisition path owns device I/O and persistence.  This package only
accepts already-validated observations and deliberately exposes no subscribe
or command API.
"""

from .bridge import messages_for_record
from .config import MqttConfig
from .models import DeviceStateCache, TelemetryEvent, TelemetryUpdate
from .publisher import MqttPublisher, create_paho_client

__all__ = [
    "DeviceStateCache",
    "MqttConfig",
    "MqttPublisher",
    "TelemetryEvent",
    "TelemetryUpdate",
    "create_paho_client",
    "messages_for_record",
]
