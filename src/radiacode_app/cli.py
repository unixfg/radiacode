from __future__ import annotations

import json
import re
import signal
import time
from dataclasses import replace
from threading import Event, Thread
from typing import Annotated

import typer
from pydantic import SecretStr

from .collector import build_collector
from .device import classify_usb_error
from .logging import configure_logging, logger
from .maintenance import Maintenance
from .metrics import MQTT_FAILURES
from .migrator import migrate as run_migrations
from .mqtt import MqttPublisher, create_paho_client
from .reallocator import KubernetesIdentity, run_reallocator
from .settings import Settings
from .usb_probe import probe_usb

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


def _settings_for_device(device: str | None) -> Settings:
    settings = Settings()
    if device is not None:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", device) is None:
            raise typer.BadParameter("device must be a lowercase slug")
        settings = settings.model_copy(update={"device_slug": device})
    settings.require_device_slug()
    return settings


def _install_signal_handlers(stop: Event) -> None:
    def request_stop(_: int, __: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _start_optional_mqtt(
    settings: Settings,
    stop: Event,
    bootstrap_updates: tuple[object, ...] = (),
) -> tuple[MqttPublisher | None, Thread | None]:
    device_slug = settings.require_device_slug()
    try:
        config = settings.optional_mqtt_config()
    except ValueError as error:
        MQTT_FAILURES.labels(device_slug).inc()
        logger().warning(
            "mqtt_configuration_invalid",
            device=device_slug,
            error_class=type(error).__name__,
        )
        return None, None
    if config is None:
        logger().info("mqtt_disabled", reason="configuration_absent")
        return None, None

    def report_error(operation: str, error: Exception | None) -> None:
        MQTT_FAILURES.labels(device_slug).inc()
        logger().warning(
            "mqtt_operation_failed",
            device=device_slug,
            operation=operation,
            error_class=type(error).__name__ if error is not None else "client_return_code",
        )

    try:
        client = create_paho_client(config, client_id=f"radiacode-{device_slug}")
        publisher = MqttPublisher(
            config,
            device_slug,
            settings.device_display_name or device_slug,
            client,
            device_model=settings.device_model,
            on_error=report_error,
        )
        for update in bootstrap_updates:
            publisher.record(update)  # type: ignore[arg-type]
        if not publisher.start():
            return None, None
        thread = Thread(
            target=publisher.run,
            args=(stop,),
            name=f"mqtt-{device_slug}",
            daemon=True,
        )
        thread.start()
        return publisher, thread
    except Exception as error:
        report_error("setup", error)
        return None, None


@app.command("collector")
def collector_command(
    device: Annotated[str | None, typer.Option("--device", help="Configured public device slug")] = None,
) -> None:
    from prometheus_client import start_http_server

    settings = _settings_for_device(device)
    serial = settings.require_device_serial()
    dsn = settings.require_database_dsn()
    secrets = [serial, dsn]
    if settings.mqtt_password is not None:
        secrets.append(settings.mqtt_password.get_secret_value())
    configure_logging(settings.log_level, tuple(secrets))
    stop = Event()
    _install_signal_handlers(stop)
    if settings.probe_on_start:
        probe_usb(settings)
    # The collector's Kubernetes startup probe targets this listener.  Start it
    # only after optional USB validation so a failed device probe cannot make
    # the pod appear started before the process exits.
    start_http_server(settings.metrics_port, addr=settings.metrics_host)
    collector, sink, spool = build_collector(settings)
    try:
        bootstrap_updates = sink.telemetry_bootstrap_updates()
    except Exception as error:
        bootstrap_updates = ()
        logger().warning(
            "mqtt_bootstrap_unavailable",
            device=settings.device_slug,
            error_class=type(error).__name__,
        )
    mqtt_publisher, mqtt_thread = _start_optional_mqtt(
        settings,
        stop,
        bootstrap_updates,
    )
    collector.telemetry_publisher = mqtt_publisher
    try:
        collector.run(stop)
    finally:
        stop.set()
        if mqtt_thread is not None:
            mqtt_thread.join(timeout=5)
        if mqtt_publisher is not None:
            mqtt_publisher.close()
        spool.close()
        sink.close()


@app.command("migrate")
def migrate_command() -> None:
    import psycopg

    settings = Settings()
    dsn = settings.require_database_dsn()
    configure_logging(settings.log_level, (dsn,))
    deadline = time.monotonic() + settings.migration_wait_seconds
    while True:
        try:
            applied = run_migrations(dsn)
            break
        except (psycopg.OperationalError, psycopg.errors.UndefinedObject) as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            logger().warning(
                "migration_prerequisite_unavailable",
                error_class=type(error).__name__,
                retry_seconds=min(settings.database_retry_seconds, remaining),
            )
            time.sleep(min(settings.database_retry_seconds, remaining))
    logger().info("migrations_complete", applied=list(applied))


@app.command("maintenance")
def maintenance_command() -> None:
    settings = Settings()
    dsn = settings.require_database_dsn()
    configure_logging(settings.log_level, (dsn,))
    report = Maintenance(
        dsn,
        advisory_lock_id=settings.maintenance_advisory_lock_id,
    ).run()
    logger().info(
        "maintenance_complete",
        acquired_lock=report.acquired_lock,
        scalar_minutes_upserted=report.scalar_minutes_upserted,
        spectrum_rollups_upserted=report.spectrum_rollups_upserted,
        partitions_dropped=report.partitions_dropped,
    )


@app.command("web")
def web_command() -> None:
    """Run the public read-only API and bundled dashboard."""

    from prometheus_client import start_http_server
    from uvicorn import Config, Server

    from .api.app import WebSettings, create_app

    settings = Settings()
    dsn = settings.require_database_dsn()
    configure_logging(settings.log_level, (dsn,))
    start_http_server(settings.metrics_port, addr=settings.metrics_host)
    web_settings = WebSettings(
        database_dsn=SecretStr(dsn),
        static_dir=settings.static_dir,
    )
    Server(
        Config(
            create_app(web_settings),
            host=settings.http_host,
            port=settings.http_port,
            access_log=False,
            server_header=False,
        )
    ).run()


@app.command("reallocator")
def reallocator_command() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    identity = KubernetesIdentity.from_environment()
    identity = replace(
        identity,
        token_path=settings.kubernetes_serviceaccount_path / "token",
        ca_path=settings.kubernetes_serviceaccount_path / "ca.crt",
    )
    stop = Event()
    _install_signal_handlers(stop)
    run_reallocator(
        settings.reallocation_marker,
        identity,
        wait_seconds=settings.reallocation_delay_seconds,
        stop_event=stop,
    )


@app.command("usb-probe")
def usb_probe_command(
    device: Annotated[str | None, typer.Option("--device", help="Configured public device slug")] = None,
) -> None:
    settings = _settings_for_device(device)
    serial = settings.require_device_serial()
    configure_logging(settings.log_level, (serial,))
    try:
        result = probe_usb(settings)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        logger().error(
            "usb_probe_failed",
            device=settings.device_slug,
            usb_failure=classify_usb_error(error).value,
            error_class=type(error).__name__,
        )
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    app()
