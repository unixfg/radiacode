from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .mqtt import MqttConfig


class Settings(BaseSettings):
    """Runtime settings. Secret values must only be unwrapped at their use site."""

    model_config = SettingsConfigDict(
        env_prefix="RADIACODE_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    device_slug: str = Field(default="service", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    device_serial: SecretStr | None = None
    database_dsn: SecretStr | None = None
    db_host: str | None = None
    db_port: int = Field(default=5432, ge=1, le=65_535)
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_sslmode: str = "verify-full"
    spool_path: Path = Path("/var/lib/radiacode/spool/spool.sqlite3")
    reallocation_marker: Path = Path("/run/radiacode/reallocate")
    reallocation_delay_seconds: float = Field(default=20.0, ge=1, le=300)
    kubernetes_serviceaccount_path: Path = Path("/var/run/secrets/radiacode-reallocator")
    metrics_host: str = "0.0.0.0"
    metrics_port: int = Field(default=9090, ge=1, le=65_535)
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8080, ge=1, le=65_535)
    static_dir: Path = Path("/opt/radiacode/static")
    mqtt_url: str | None = None
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_ca_file: Path = Path("/var/run/secrets/radiacode-mqtt/ca.crt")
    mqtt_keepalive_seconds: int = Field(default=60, ge=5, le=3600)
    mqtt_publish_interval_seconds: float = Field(default=30.0, ge=1, le=3600)
    mqtt_stale_after_seconds: float = Field(default=10.0, ge=1, le=300)
    device_display_name: str | None = None
    device_model: str = "RadiaCode"
    maintenance_advisory_lock_id: int = 7_243_944_686_731_002_002
    expected_channel_count: int | None = Field(default=None, ge=2, le=65_536)
    probe_on_start: bool = False
    migration_wait_seconds: float = Field(default=840.0, ge=0, le=3600)
    data_poll_seconds: float = Field(default=1.0, gt=0.05, le=60)
    spectrum_poll_seconds: float = Field(default=60.0, ge=5, le=3600)
    accumulator_audit_seconds: float = Field(default=21_600.0, ge=300)
    database_retry_seconds: float = Field(default=5.0, ge=0.1, le=300)
    usb_retry_cap_seconds: float = Field(default=30.0, ge=1, le=300)
    frame_target_seconds: int = Field(default=300, ge=60, le=3600)
    spool_max_bytes: int = Field(default=900 * 1024 * 1024, ge=1024 * 1024)
    spool_reserved_batch_bytes: int = Field(default=4 * 1024 * 1024, ge=256)
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    def require_device_serial(self) -> str:
        if self.device_serial is None:
            raise ValueError("RADIACODE_DEVICE_SERIAL is required")
        return self.device_serial.get_secret_value()

    def require_device_slug(self) -> str:
        if self.device_slug == "service":
            raise ValueError("RADIACODE_DEVICE_SLUG is required for device commands")
        return self.device_slug

    def require_database_dsn(self) -> str:
        if self.database_dsn is not None:
            return self.database_dsn.get_secret_value()
        if not all((self.db_host, self.db_name, self.db_user, self.db_password)):
            raise ValueError(
                "RADIACODE_DB_HOST, DB_NAME, DB_USER, and DB_PASSWORD are required when DATABASE_DSN is unset"
            )
        from psycopg.conninfo import make_conninfo

        assert self.db_password is not None
        return make_conninfo(
            "",
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_user,
            password=self.db_password.get_secret_value(),
            sslmode=self.db_sslmode,
        )

    def optional_mqtt_config(self) -> MqttConfig | None:
        """Return verified-TLS MQTT configuration when explicitly configured.

        A completely absent optional secret disables MQTT. Any partial or invalid
        configuration is an observable error, but the caller still isolates it
        from acquisition.
        """

        supplied = (self.mqtt_url, self.mqtt_username, self.mqtt_password)
        if not any(value is not None for value in supplied):
            return None
        if not all(value is not None for value in supplied):
            raise ValueError("MQTT URL, username, and password must be supplied together")
        if not self.mqtt_ca_file.is_file():
            raise ValueError("MQTT CA file is missing")
        assert self.mqtt_url is not None
        assert self.mqtt_username is not None
        assert self.mqtt_password is not None
        try:
            parsed = urlsplit(self.mqtt_url)
            port = parsed.port or 8883
        except ValueError as error:
            raise ValueError("MQTT URL is invalid") from error
        if (
            parsed.scheme != "mqtts"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("MQTT URL must be a credential-free mqtts:// endpoint")
        return MqttConfig(
            host=parsed.hostname,
            port=port,
            username=self.mqtt_username,
            password=self.mqtt_password.get_secret_value(),
            ca_file=self.mqtt_ca_file,
            keepalive_seconds=self.mqtt_keepalive_seconds,
            publish_interval_seconds=self.mqtt_publish_interval_seconds,
            stale_after_seconds=self.mqtt_stale_after_seconds,
        )
