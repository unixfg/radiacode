from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any, Protocol, cast

from .config import MqttConfig
from .models import DeviceStateCache, TelemetryEvent, TelemetryUpdate, validate_device_slug


class PublishResult(Protocol):
    rc: int

    def wait_for_publish(self, timeout: float | None = None) -> None: ...

    def is_published(self) -> bool: ...


ConnectCallback = Callable[[Any, Any, Any, Any, Any], None]
ConnectFailCallback = Callable[[Any, Any], None]
DisconnectCallback = Callable[[Any, Any, Any, Any, Any], None]


class MqttClient(Protocol):
    on_connect: ConnectCallback | None
    on_connect_fail: ConnectFailCallback | None
    on_disconnect: DisconnectCallback | None

    def username_pw_set(self, username: str, password: str | None = None) -> None: ...

    def tls_set_context(self, context: ssl.SSLContext) -> None: ...

    def tls_insecure_set(self, value: bool) -> None: ...

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None: ...

    def max_queued_messages_set(self, queue_size: int) -> None: ...

    def max_inflight_messages_set(self, inflight: int) -> None: ...

    def will_set(
        self,
        topic: str,
        payload: str | None = None,
        qos: int = 0,
        retain: bool = False,
    ) -> None: ...

    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> Any: ...

    def loop_start(self) -> Any: ...

    def loop_stop(self) -> Any: ...

    def disconnect(self) -> Any: ...

    def publish(
        self,
        topic: str,
        payload: str,
        qos: int = 0,
        retain: bool = False,
    ) -> PublishResult: ...


ErrorHandler = Callable[[str, Exception | None], None]
Clock = Callable[[], datetime]
SHUTDOWN_PUBLISH_TIMEOUT_SECONDS = 2.0


def create_paho_client(config: MqttConfig, client_id: str) -> MqttClient:
    """Create a Paho client with certificate and hostname verification enabled."""

    try:
        mqtt = import_module("paho.mqtt.client")
    except ImportError as error:  # pragma: no cover - depends on deployment extras
        raise RuntimeError("MQTT support requires the paho-mqtt package") from error

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )
    tls_context = ssl.create_default_context(cafile=str(config.ca_file))
    tls_context.check_hostname = True
    tls_context.verify_mode = ssl.CERT_REQUIRED
    client.tls_set_context(tls_context)
    client.tls_insecure_set(False)
    client.username_pw_set(config.username, config.password)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    # An unreachable or unauthorized broker must never create an unbounded
    # in-memory QoS 1 queue inside the acquisition process.
    client.max_queued_messages_set(20)
    client.max_inflight_messages_set(5)
    return cast(MqttClient, client)


class MqttPublisher:
    """Failure-isolated, publish-only Home Assistant MQTT adapter."""

    def __init__(
        self,
        config: MqttConfig,
        device_slug: str,
        display_name: str,
        client: MqttClient,
        *,
        device_model: str | None = None,
        on_error: ErrorHandler | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.device_slug = validate_device_slug(device_slug)
        self.display_name = display_name.strip()
        if not self.display_name:
            raise ValueError("display_name is required")
        self.device_model = (device_model or self.display_name).strip()
        if not self.device_model:
            raise ValueError("device_model is required")
        self.client = client
        self.cache = DeviceStateCache(self.device_slug)
        self._on_error = on_error or (lambda _operation, _error: None)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._last_availability: bool | None = None
        self._network_started = False
        self._broker_connected = False
        self._closing = False
        self.client.on_connect = self._handle_connect
        self.client.on_connect_fail = self._handle_connect_fail
        self.client.on_disconnect = self._handle_disconnect

    @property
    def state_topic(self) -> str:
        return f"{self.config.normalized_topic_prefix}/{self.device_slug}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.config.normalized_topic_prefix}/{self.device_slug}/availability"

    @property
    def event_topic(self) -> str:
        return f"{self.config.normalized_topic_prefix}/{self.device_slug}/event"

    @property
    def _stale_after(self) -> timedelta:
        return timedelta(seconds=self.config.stale_after_seconds)

    def start(self) -> bool:
        """Start Paho's reconnecting network loop without failing acquisition."""

        with self._lock:
            self._network_started = True
            self._broker_connected = False
            self._closing = False
        try:
            self.client.will_set(self.availability_topic, payload="offline", qos=1, retain=True)
            self.client.connect_async(
                self.config.host,
                port=self.config.port,
                keepalive=self.config.keepalive_seconds,
            )
            self.client.loop_start()
        except Exception as error:  # broker setup must never abort acquisition
            with self._lock:
                self._network_started = False
            self._report_error("connect", error)
            return False
        return True

    def close(self) -> None:
        with self._lock:
            self._closing = True
        offline_acknowledged = self.publish_availability(
            now=self._clock(),
            force=True,
            connected_override=False,
            wait_for_ack_seconds=SHUTDOWN_PUBLISH_TIMEOUT_SECONDS,
        )
        if offline_acknowledged:
            try:
                self.client.disconnect()
            except Exception as error:
                self._report_error("disconnect", error)
        # If the PUBACK was not observed, deliberately avoid a graceful MQTT
        # DISCONNECT. The broker can then apply the retained offline last will
        # when the process closes its transport instead of preserving "online".
        try:
            self.client.loop_stop()
        except Exception as error:
            self._report_error("loop_stop", error)
        finally:
            with self._lock:
                self._broker_connected = False
                self._network_started = False

    def _handle_connect(
        self,
        _client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        """Handle Paho VERSION2 CONNACK callbacks without escaping its loop thread."""

        try:
            if bool(getattr(reason_code, "is_failure", reason_code != 0)):
                with self._lock:
                    self._broker_connected = False
                self._report_error("connack", None)
                return
            with self._lock:
                if self._closing:
                    return
                self._broker_connected = True
                self._last_availability = None
            # Discovery and the complete cached state are retained. Republishing
            # them after every successful reconnect heals broker restarts and
            # expired retained data without replaying historical events.
            self.publish_discovery()
            self.publish_cycle(self._clock())
        except Exception as error:
            self._report_error("connect_callback", error)

    def _handle_connect_fail(self, _client: Any, _userdata: Any) -> None:
        """Handle pre-CONNACK TCP, DNS, and TLS failures from Paho VERSION2."""

        try:
            with self._lock:
                self._broker_connected = False
                closing = self._closing
            if not closing:
                self._report_error("connect_fail", None)
        except Exception as error:
            self._report_error("connect_fail_callback", error)

    def _handle_disconnect(
        self,
        _client: Any,
        _userdata: Any,
        _flags: Any,
        _reason_code: Any,
        _properties: Any,
    ) -> None:
        """Record unexpected Paho VERSION2 disconnect callbacks."""

        try:
            with self._lock:
                self._broker_connected = False
                closing = self._closing
            if not closing:
                self._report_error("disconnect", None)
        except Exception as error:
            self._report_error("disconnect_callback", error)

    def _report_error(self, operation: str, error: Exception | None) -> None:
        # A monitoring callback is never allowed to terminate acquisition or
        # Paho's reconnecting network-loop thread.
        with suppress(Exception):
            self._on_error(operation, error)

    def record(self, update: TelemetryUpdate) -> None:
        with self._lock:
            self.cache.apply(update)

    def publish_event(self, event: TelemetryEvent) -> bool:
        payload: dict[str, object] = {
            "device": self.device_slug,
            "kind": event.kind,
            "observed_at": event.observed_at.isoformat().replace("+00:00", "Z"),
        }
        if event.message is not None:
            payload["message"] = event.message
        if event.details:
            payload["details"] = event.details
        return self._publish(self.event_topic, payload, retain=False)

    def publish_cycle(self, now: datetime | None = None) -> bool:
        checked_at = now or self._clock()
        with self._lock:
            payload = self.cache.snapshot(checked_at, self._stale_after)
        state_ok = self._publish(self.state_topic, payload, retain=True)
        availability_ok = self.publish_availability(now=checked_at, force=True)
        return state_ok and availability_ok

    def publish_availability(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        connected_override: bool | None = None,
        wait_for_ack_seconds: float | None = None,
    ) -> bool:
        checked_at = now or self._clock()
        with self._lock:
            connected = (
                connected_override
                if connected_override is not None
                else self.cache.is_connected(checked_at, self._stale_after)
            )
            if not force and connected == self._last_availability:
                return True
        published = self._publish(
            self.availability_topic,
            "online" if connected else "offline",
            retain=True,
            encode_json=False,
            wait_for_ack_seconds=wait_for_ack_seconds,
        )
        if published:
            with self._lock:
                self._last_availability = connected
        return published

    def publish_discovery(self) -> bool:
        device = {
            "identifiers": [f"radiacode_{self.device_slug}"],
            "manufacturer": "RadiaCode",
            "model": self.device_model,
            "name": self.display_name,
        }
        entities = (
            ("sensor", "cps", "Count rate", "cps", "measurement", None),
            ("sensor", "cps_uncertainty_pct", "Count-rate uncertainty", "%", "measurement", None),
            ("sensor", "dose_rate_usv_h", "Dose rate", "µSv/h", "measurement", None),
            (
                "sensor",
                "dose_rate_uncertainty_pct",
                "Dose-rate uncertainty",
                "%",
                "measurement",
                None,
            ),
            ("sensor", "accumulated_dose_usv", "Accumulated dose", "µSv", "total_increasing", None),
            (
                "sensor",
                "accumulated_duration_seconds",
                "Accumulated duration",
                "s",
                "total_increasing",
                "duration",
            ),
            ("sensor", "temperature_c", "Temperature", "°C", "measurement", "temperature"),
            ("sensor", "battery_percent", "Battery", "%", "measurement", "battery"),
            ("binary_sensor", "charging", "Charging", None, None, "battery_charging"),
            ("binary_sensor", "connected", "Connectivity", None, None, "connectivity"),
        )
        success = True
        for component, key, label, unit, state_class, device_class in entities:
            unique_id = f"radiacode_{self.device_slug}_{key}"
            payload: dict[str, object] = {
                "availability_topic": self.availability_topic,
                "device": device,
                "name": label,
                "object_id": unique_id,
                "origin": {
                    "name": "unixfg/radiacode",
                    "support_url": "https://github.com/unixfg/radiacode",
                },
                "state_topic": self.state_topic,
                "unique_id": unique_id,
                "value_template": f"{{{{ value_json.{key} }}}}",
            }
            if unit is not None:
                payload["unit_of_measurement"] = unit
            if state_class is not None:
                payload["state_class"] = state_class
            if device_class is not None:
                payload["device_class"] = device_class
            if component == "binary_sensor":
                payload["payload_on"] = "True"
                payload["payload_off"] = "False"
            if key == "connected":
                # Connectivity must remain readable as false rather than becoming
                # unavailable because it is its own availability explanation.
                payload.pop("availability_topic")
            topic = (
                f"{self.config.normalized_discovery_prefix}/{component}/"
                f"radiacode_{self.device_slug}/{key}/config"
            )
            success = self._publish(topic, payload, retain=True) and success
        return success

    def run(self, stop_event: threading.Event) -> None:
        """Publish state every 30s and availability transitions within 1s."""

        next_state_publish = 0.0
        while not stop_event.is_set():
            now = self._clock()
            monotonic_now = time.monotonic()
            if monotonic_now >= next_state_publish:
                self.publish_cycle(now)
                next_state_publish = monotonic_now + self.config.publish_interval_seconds
            else:
                with self._lock:
                    connected = self.cache.is_connected(now, self._stale_after)
                    changed = self._last_availability is not None and connected != self._last_availability
                if changed:
                    # Keep the retained state's connectivity field in lockstep
                    # with the retained availability topic at the 10s boundary.
                    self.publish_cycle(now)
                else:
                    self.publish_availability(now=now)
            stop_event.wait(min(1.0, max(0.05, next_state_publish - monotonic_now)))

    def _publish(
        self,
        topic: str,
        payload: object,
        *,
        retain: bool,
        encode_json: bool = True,
        wait_for_ack_seconds: float | None = None,
    ) -> bool:
        body = (
            json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
            if encode_json
            else str(payload)
        )
        with self._lock:
            if self._network_started and not self._broker_connected:
                return False
        try:
            result = self.client.publish(topic, body, qos=1, retain=retain)
            return_code = result.rc
            if return_code != 0:
                self._report_error(f"publish:{topic}", None)
                return False
            if wait_for_ack_seconds is not None:
                result.wait_for_publish(timeout=wait_for_ack_seconds)
                if not result.is_published():
                    self._report_error(f"publish_timeout:{topic}", None)
                    return False
        except Exception as error:
            self._report_error(f"publish:{topic}", error)
            return False
        return True
