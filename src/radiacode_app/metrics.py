from prometheus_client import Counter, Gauge, Histogram

COLLECTOR_STATE = Gauge(
    "radiacode_collector_state",
    "Collector state (1 for the current state, 0 otherwise)",
    ("device", "state"),
)
DETECTOR_AVAILABLE = Gauge(
    "radiacode_detector_available",
    "Whether the detector has delivered valid real-time data within the freshness window",
    ("device",),
)
DATA_BATCHES = Counter(
    "radiacode_data_batches_total",
    "Raw DATA_BUF batches by result",
    ("device", "result"),
)
DECODE_WARNINGS = Counter(
    "radiacode_decode_warnings_total",
    "DATA_BUF decoding warnings",
    ("device", "kind"),
)
SPOOL_BYTES = Gauge("radiacode_spool_backlog_bytes", "Unacknowledged spool payload bytes", ("device",))
SPOOL_BATCHES = Gauge("radiacode_spool_backlog_batches", "Unacknowledged spool batch count", ("device",))
LAST_VALID_REALTIME = Gauge(
    "radiacode_acquisition_last_sample_timestamp_seconds",
    "Host receive time of the last valid real-time record",
    ("device",),
)
SPECTRUM_GAPS = Counter(
    "radiacode_spectrum_gaps_total",
    "Spectrum session gaps by reason",
    ("device", "reason"),
)
ACCUMULATED_SNAPSHOT_FAILURES = Counter(
    "radiacode_accumulated_snapshot_errors_total",
    "Best-effort undocumented accumulated-spectrum audit failures",
    ("device",),
)
USB_ERRORS = Counter("radiacode_usb_errors_total", "USB failures by class", ("device", "class"))
DATABASE_FAILURES = Counter("radiacode_database_errors_total", "Database operation failures", ("device",))
MQTT_FAILURES = Counter(
    "radiacode_mqtt_errors_total",
    "MQTT publish or connection failures",
    ("device",),
)
OPERATION_SECONDS = Histogram(
    "radiacode_operation_seconds",
    "Collector operation duration",
    ("device", "operation"),
)


def set_collector_state(device: str, state: str) -> None:
    for known in ("starting", "waiting_database", "connecting", "active", "backoff", "stopped"):
        COLLECTOR_STATE.labels(device, known).set(1 if known == state else 0)
