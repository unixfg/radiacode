CREATE SCHEMA IF NOT EXISTS radiacode_private;
CREATE SCHEMA IF NOT EXISTS radiacode_api;

CREATE TABLE IF NOT EXISTS radiacode_private.devices (
    device_id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    display_name text NOT NULL,
    model text NOT NULL,
    usb_serial text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS radiacode_private.connections (
    connection_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    connected_at timestamptz NOT NULL,
    disconnected_at timestamptz,
    close_reason text,
    firmware_major integer,
    firmware_minor integer,
    app_version text NOT NULL,
    CHECK (disconnected_at IS NULL OR disconnected_at >= connected_at)
);
CREATE INDEX IF NOT EXISTS connections_device_time
    ON radiacode_private.connections(device_id, connected_at DESC);

CREATE TABLE IF NOT EXISTS radiacode_private.raw_buffer_batches (
    received_at timestamptz NOT NULL,
    batch_id uuid NOT NULL,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    connection_id uuid NOT NULL REFERENCES radiacode_private.connections(connection_id),
    payload bytea NOT NULL,
    sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32),
    first_sequence smallint,
    last_sequence smallint,
    record_count integer NOT NULL CHECK (record_count >= 0),
    decode_status text NOT NULL CHECK (decode_status IN ('ok', 'warning', 'truncated', 'unknown_tail')),
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (received_at, batch_id)
) PARTITION BY RANGE (received_at);

CREATE TABLE IF NOT EXISTS radiacode_private.buffer_records (
    received_at timestamptz NOT NULL,
    batch_id uuid NOT NULL,
    record_index integer NOT NULL CHECK (record_index >= 0),
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    connection_id uuid NOT NULL REFERENCES radiacode_private.connections(connection_id),
    sequence smallint CHECK (sequence BETWEEN 0 AND 255),
    event_id smallint CHECK (event_id BETWEEN 0 AND 255),
    group_id smallint CHECK (group_id BETWEEN 0 AND 255),
    device_tick integer,
    sample_at timestamptz,
    timestamp_quality text NOT NULL CHECK (
        timestamp_quality IN ('batch_relative', 'invalid_tick', 'not_available')
    ),
    kind text NOT NULL,
    flags integer,
    raw_record bytea NOT NULL,
    raw_payload bytea NOT NULL,
    values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (received_at, batch_id, record_index)
) PARTITION BY RANGE (received_at);

CREATE TABLE IF NOT EXISTS radiacode_private.scalar_samples (
    received_at timestamptz NOT NULL,
    inserted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    batch_id uuid NOT NULL,
    record_index integer NOT NULL,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    sample_at timestamptz,
    timestamp_quality text NOT NULL,
    sample_kind text NOT NULL CHECK (sample_kind IN ('real_time', 'raw', 'dose_rate_db')),
    count_value bigint,
    count_rate double precision,
    dose_rate double precision,
    count_rate_error_pct double precision,
    dose_rate_error_pct double precision,
    flags integer,
    real_time_flags integer,
    PRIMARY KEY (received_at, batch_id, record_index)
) PARTITION BY RANGE (received_at);

CREATE TABLE IF NOT EXISTS radiacode_private.status_samples (
    status_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    received_at timestamptz NOT NULL,
    sample_at timestamptz,
    timestamp_quality text NOT NULL,
    duration_seconds bigint NOT NULL CHECK (duration_seconds >= 0),
    accumulated_dose double precision NOT NULL,
    temperature_c double precision NOT NULL,
    charge_pct double precision NOT NULL,
    charging boolean,
    flags integer
);
CREATE INDEX IF NOT EXISTS status_samples_device_time
    ON radiacode_private.status_samples(device_id, received_at DESC);

CREATE TABLE IF NOT EXISTS radiacode_private.device_runtime_state (
    device_id uuid PRIMARY KEY REFERENCES radiacode_private.devices(device_id),
    charging boolean,
    charging_observed_at timestamptz,
    next_buffer_sequence smallint CHECK (next_buffer_sequence BETWEEN 0 AND 255),
    buffer_sequence_observed_at timestamptz
);

CREATE TABLE IF NOT EXISTS radiacode_private.device_events (
    event_row_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    received_at timestamptz NOT NULL,
    sample_at timestamptz,
    timestamp_quality text NOT NULL,
    event_code smallint NOT NULL,
    event_name text,
    event_parameter smallint,
    flags integer
);
CREATE INDEX IF NOT EXISTS device_events_device_time
    ON radiacode_private.device_events(device_id, received_at DESC);

CREATE INDEX IF NOT EXISTS scalar_samples_device_realtime
    ON radiacode_private.scalar_samples(device_id, received_at DESC)
    WHERE sample_kind = 'real_time';
CREATE INDEX IF NOT EXISTS scalar_samples_realtime_inserted
    ON radiacode_private.scalar_samples(inserted_at)
    WHERE sample_kind = 'real_time';

-- A transactionally written dirty marker makes late commits visible to the
-- rollup worker without depending on an insertion-time lookback window.
CREATE TABLE IF NOT EXISTS radiacode_private.scalar_rollup_dirty (
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    bucket_at timestamptz NOT NULL,
    PRIMARY KEY (device_id, bucket_at)
);

CREATE TABLE IF NOT EXISTS radiacode_private.calibration_epochs (
    calibration_epoch_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    channel_count integer NOT NULL CHECK (channel_count >= 2),
    coefficient_a0 real NOT NULL,
    coefficient_a1 real NOT NULL,
    coefficient_a2 real NOT NULL,
    fingerprint bytea NOT NULL CHECK (octet_length(fingerprint) = 32),
    UNIQUE (device_id, calibration_epoch_id),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS calibration_epochs_one_open
    ON radiacode_private.calibration_epochs(device_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS radiacode_private.spectrum_sessions (
    session_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    calibration_epoch_id uuid NOT NULL REFERENCES radiacode_private.calibration_epochs(calibration_epoch_id),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    end_reason text,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS spectrum_sessions_one_open
    ON radiacode_private.spectrum_sessions(device_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS radiacode_private.spectrum_cursors (
    device_id uuid PRIMARY KEY REFERENCES radiacode_private.devices(device_id),
    session_id uuid NOT NULL REFERENCES radiacode_private.spectrum_sessions(session_id),
    connection_id uuid NOT NULL REFERENCES radiacode_private.connections(connection_id),
    calibration_epoch_id uuid NOT NULL REFERENCES radiacode_private.calibration_epochs(calibration_epoch_id),
    observed_at timestamptz NOT NULL,
    duration_seconds bigint NOT NULL CHECK (duration_seconds >= 0),
    channel_count integer NOT NULL CHECK (channel_count >= 2),
    counts_encoding_version smallint NOT NULL CHECK (counts_encoding_version = 1),
    counts bytea NOT NULL CHECK (octet_length(counts) = channel_count * 4),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32)
);

CREATE TABLE IF NOT EXISTS radiacode_private.spectrum_frame_accumulators (
    device_id uuid PRIMARY KEY REFERENCES radiacode_private.devices(device_id),
    session_id uuid NOT NULL REFERENCES radiacode_private.spectrum_sessions(session_id),
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL,
    duration_seconds bigint NOT NULL CHECK (duration_seconds >= 0),
    channel_count integer NOT NULL CHECK (channel_count >= 2),
    counts_encoding_version smallint NOT NULL CHECK (counts_encoding_version = 1),
    counts bytea NOT NULL CHECK (octet_length(counts) = channel_count * 4),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    source_intervals integer NOT NULL CHECK (source_intervals > 0),
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS radiacode_private.spectrum_frames (
    frame_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    session_id uuid NOT NULL REFERENCES radiacode_private.spectrum_sessions(session_id),
    calibration_epoch_id uuid NOT NULL REFERENCES radiacode_private.calibration_epochs(calibration_epoch_id),
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL,
    duration_seconds bigint NOT NULL CHECK (duration_seconds >= 0),
    channel_count integer NOT NULL CHECK (channel_count >= 2),
    counts_encoding_version smallint NOT NULL CHECK (counts_encoding_version = 1),
    counts bytea NOT NULL CHECK (octet_length(counts) = channel_count * 4),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32),
    source_intervals integer NOT NULL CHECK (source_intervals > 0),
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    CHECK (ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS spectrum_frames_device_time
    ON radiacode_private.spectrum_frames(device_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS spectrum_frames_calibration_time
    ON radiacode_private.spectrum_frames(calibration_epoch_id, ended_at);

CREATE TABLE IF NOT EXISTS radiacode_private.spectrum_snapshots (
    snapshot_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    connection_id uuid NOT NULL REFERENCES radiacode_private.connections(connection_id),
    calibration_epoch_id uuid NOT NULL REFERENCES radiacode_private.calibration_epochs(calibration_epoch_id),
    observed_at timestamptz NOT NULL,
    snapshot_kind text NOT NULL CHECK (snapshot_kind IN ('connection', 'six_hour_audit')),
    duration_seconds bigint NOT NULL CHECK (duration_seconds >= 0),
    channel_count integer NOT NULL CHECK (channel_count >= 2),
    coefficient_a0 real NOT NULL,
    coefficient_a1 real NOT NULL,
    coefficient_a2 real NOT NULL,
    counts_encoding_version smallint NOT NULL CHECK (counts_encoding_version = 1),
    counts bytea NOT NULL CHECK (octet_length(counts) = channel_count * 4),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32),
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS radiacode_private.scalar_minute_rollups (
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    bucket_at timestamptz NOT NULL,
    sample_count integer NOT NULL CHECK (sample_count > 0),
    count_rate_min double precision,
    count_rate_max double precision,
    count_rate_avg double precision,
    count_rate_latest double precision,
    dose_rate_min double precision,
    dose_rate_max double precision,
    dose_rate_avg double precision,
    dose_rate_latest double precision,
    rolled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (device_id, bucket_at)
);

CREATE TABLE IF NOT EXISTS radiacode_private.spectrum_rollups (
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    calibration_epoch_id uuid NOT NULL REFERENCES radiacode_private.calibration_epochs(calibration_epoch_id),
    resolution text NOT NULL CHECK (resolution IN ('hour', 'day')),
    bucket_at timestamptz NOT NULL,
    actual_started_at timestamptz NOT NULL,
    actual_ended_at timestamptz NOT NULL,
    duration_seconds bigint NOT NULL CHECK (duration_seconds > 0),
    channel_count integer NOT NULL CHECK (channel_count >= 2),
    counts_encoding_version smallint NOT NULL CHECK (counts_encoding_version = 1),
    counts bytea NOT NULL CHECK (octet_length(counts) = channel_count * 4),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32),
    source_frame_count integer NOT NULL CHECK (source_frame_count > 0),
    quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    bucket_assignment text NOT NULL DEFAULT 'frame_end',
    segment_index integer NOT NULL DEFAULT 0 CHECK (segment_index >= 0),
    PRIMARY KEY (device_id, calibration_epoch_id, resolution, bucket_at, segment_index)
);

CREATE TABLE IF NOT EXISTS radiacode_private.rollup_watermarks (
    resolution text PRIMARY KEY CHECK (resolution IN ('minute', 'hour', 'day')),
    processed_before timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS radiacode_private.data_gaps (
    gap_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES radiacode_private.devices(device_id),
    session_id uuid REFERENCES radiacode_private.spectrum_sessions(session_id),
    detected_at timestamptz NOT NULL,
    gap_kind text NOT NULL,
    started_at timestamptz,
    ended_at timestamptz,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS data_gaps_device_time
    ON radiacode_private.data_gaps(device_id, detected_at DESC);

CREATE OR REPLACE FUNCTION radiacode_private.ensure_daily_partitions(p_start date, p_end date)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    parent_name text;
    partition_day date;
    partition_name text;
BEGIN
    IF p_end < p_start THEN
        RAISE EXCEPTION 'partition end precedes start';
    END IF;
    FOREACH parent_name IN ARRAY ARRAY['raw_buffer_batches', 'buffer_records', 'scalar_samples']
    LOOP
        FOR partition_day IN SELECT generate_series(p_start, p_end, interval '1 day')::date
        LOOP
            partition_name := parent_name || '_' || to_char(partition_day, 'YYYYMMDD');
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS radiacode_private.%I PARTITION OF radiacode_private.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                partition_name, parent_name, partition_day, partition_day + 1
            );
        END LOOP;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION radiacode_private.drop_daily_partitions_before(p_cutoff date)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    partition_record record;
    suffix text;
    partition_day date;
    dropped integer := 0;
BEGIN
    FOR partition_record IN
        SELECT child.relname AS child_name, parent.relname AS parent_name
          FROM pg_inherits
          JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
          JOIN pg_class child ON pg_inherits.inhrelid = child.oid
          JOIN pg_namespace namespace ON child.relnamespace = namespace.oid
         WHERE namespace.nspname = 'radiacode_private'
           AND parent.relname IN ('raw_buffer_batches', 'buffer_records', 'scalar_samples')
    LOOP
        suffix := right(partition_record.child_name, 8);
        IF suffix ~ '^[0-9]{8}$' THEN
            partition_day := to_date(suffix, 'YYYYMMDD');
            IF partition_day < p_cutoff THEN
                EXECUTE format('DROP TABLE radiacode_private.%I', partition_record.child_name);
                dropped := dropped + 1;
            END IF;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$$;

-- A collector can replay its WAL spool after a prolonged CNPG outage. Keep the
-- entire online-retention window insertable, not only the most recent days.
SELECT radiacode_private.ensure_daily_partitions(current_date - 30, current_date + 8);

CREATE OR REPLACE VIEW radiacode_api.devices
WITH (security_barrier = true)
AS
SELECT slug, display_name, model
  FROM radiacode_private.devices
 WHERE enabled;

COMMENT ON SCHEMA radiacode_private IS 'Private acquisition data; never grant the public web role direct access';
COMMENT ON SCHEMA radiacode_api IS 'Sanitized read-only objects intended for the future public web role';
COMMENT ON COLUMN radiacode_private.devices.usb_serial IS 'Private libusb selector; never expose through API views or logs';
COMMENT ON COLUMN radiacode_private.spectrum_frames.counts IS 'Encoding v1: channel_count little-endian uint32 values';
COMMENT ON COLUMN radiacode_private.spectrum_rollups.bucket_assignment IS 'Whole frames assigned by end time; counts are never proportionally split';
