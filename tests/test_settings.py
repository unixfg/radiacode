from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr, ValidationError

from radiacode_app.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_device_commands_require_slug_and_serial(self) -> None:
        settings = Settings()
        with self.assertRaises(ValueError):
            settings.require_device_slug()
        with self.assertRaises(ValueError):
            settings.require_device_serial()
        with self.assertRaises(ValidationError):
            Settings(device_slug="contains_underscore")

    def test_split_database_settings_build_safe_conninfo(self) -> None:
        settings = Settings(
            db_host="postgres.example",
            db_port=5433,
            db_name="radiacode",
            db_user="collector",
            db_password=SecretStr("space and ' quote"),
            db_sslmode="verify-full",
        )
        parsed = conninfo_to_dict(settings.require_database_dsn())
        self.assertEqual(parsed["host"], "postgres.example")
        self.assertEqual(parsed["port"], "5433")
        self.assertEqual(parsed["password"], "space and ' quote")
        self.assertEqual(parsed["sslmode"], "verify-full")

    def test_explicit_dsn_takes_precedence(self) -> None:
        settings = Settings(database_dsn=SecretStr("dbname=direct user=reader"))
        self.assertEqual(settings.require_database_dsn(), "dbname=direct user=reader")

    def test_absent_mqtt_is_disabled_but_partial_configuration_is_invalid(self) -> None:
        self.assertIsNone(Settings().optional_mqtt_config())
        settings = Settings(
            mqtt_url="mqtts://mqtt.internal:8883",
            mqtt_username="publisher",
            mqtt_password=SecretStr("secret"),
            mqtt_ca_file=Path("/definitely/missing/mqtt-ca.crt"),
        )
        with self.assertRaisesRegex(ValueError, "CA file"):
            settings.optional_mqtt_config()
        with self.assertRaisesRegex(ValueError, "supplied together"):
            Settings(mqtt_url="mqtts://mqtt.internal").optional_mqtt_config()

    def test_mqtt_requires_tls_url_and_builds_config_from_secrets(self) -> None:
        with TemporaryDirectory() as directory:
            ca_file = Path(directory) / "ca.crt"
            ca_file.touch()
            plaintext = Settings(
                mqtt_url="mqtt://mqtt.internal:1883",
                mqtt_username="publisher",
                mqtt_password=SecretStr("secret"),
                mqtt_ca_file=ca_file,
            )
            with self.assertRaisesRegex(ValueError, "mqtts"):
                plaintext.optional_mqtt_config()

            secure = plaintext.model_copy(update={"mqtt_url": "mqtts://mqtt.internal:8884"})
            mqtt = secure.optional_mqtt_config()
            self.assertIsNotNone(mqtt)
            assert mqtt is not None
            self.assertEqual(mqtt.host, "mqtt.internal")
            self.assertEqual(mqtt.port, 8884)
            self.assertEqual(mqtt.username, "publisher")


if __name__ == "__main__":
    unittest.main()
