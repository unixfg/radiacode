CREATE OR REPLACE VIEW radiacode_api.device_status
WITH (security_barrier = true)
AS
SELECT devices.slug,
       devices.display_name,
       devices.model,
       realtime.received_at AS last_seen_at,
       realtime.received_at AS realtime_observed_at,
       realtime.count_rate AS cps,
       realtime.dose_rate,
       realtime.count_rate_error_pct AS cps_uncertainty_pct,
       realtime.dose_rate_error_pct AS dose_rate_uncertainty_pct,
       status.received_at AS status_observed_at,
       status.accumulated_dose,
       status.duration_seconds AS accumulated_duration_seconds,
       status.temperature_c,
       status.charge_pct AS battery_pct,
       runtime.charging,
       runtime.charging_observed_at
  FROM radiacode_private.devices AS devices
  LEFT JOIN LATERAL (
      SELECT samples.received_at,
             samples.count_rate,
             samples.dose_rate,
             samples.count_rate_error_pct,
             samples.dose_rate_error_pct
        FROM radiacode_private.scalar_samples AS samples
       WHERE samples.device_id = devices.device_id
         AND samples.sample_kind = 'real_time'
         AND samples.count_rate IS NOT NULL
         AND samples.dose_rate IS NOT NULL
       ORDER BY samples.received_at DESC
       LIMIT 1
  ) AS realtime ON true
  LEFT JOIN LATERAL (
      SELECT samples.received_at,
             samples.accumulated_dose,
             samples.duration_seconds,
             samples.temperature_c,
             samples.charge_pct
        FROM radiacode_private.status_samples AS samples
       WHERE samples.device_id = devices.device_id
       ORDER BY samples.received_at DESC
       LIMIT 1
  ) AS status ON true
  LEFT JOIN radiacode_private.device_runtime_state AS runtime
    ON runtime.device_id = devices.device_id
 WHERE devices.enabled;

CREATE OR REPLACE VIEW radiacode_api.scalar_history
WITH (security_barrier = true)
AS
SELECT devices.slug,
       samples.received_at AS observed_at,
       1::bigint AS sample_count,
       samples.count_rate AS cps_min,
       samples.count_rate AS cps_max,
       samples.count_rate AS cps_avg,
       samples.count_rate AS cps_latest,
       samples.dose_rate AS dose_rate_min,
       samples.dose_rate AS dose_rate_max,
       samples.dose_rate AS dose_rate_avg,
       samples.dose_rate AS dose_rate_latest
  FROM radiacode_private.scalar_samples AS samples
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
   AND samples.sample_kind = 'real_time';

CREATE OR REPLACE VIEW radiacode_api.scalar_minute_history
WITH (security_barrier = true)
AS
SELECT devices.slug,
       rollups.bucket_at AS observed_at,
       rollups.sample_count::bigint AS sample_count,
       rollups.count_rate_min AS cps_min,
       rollups.count_rate_max AS cps_max,
       rollups.count_rate_avg AS cps_avg,
       rollups.count_rate_latest AS cps_latest,
       rollups.dose_rate_min,
       rollups.dose_rate_max,
       rollups.dose_rate_avg,
       rollups.dose_rate_latest
  FROM radiacode_private.scalar_minute_rollups AS rollups
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
UNION ALL
SELECT devices.slug,
       samples.received_at AS observed_at,
       1::bigint AS sample_count,
       samples.count_rate AS cps_min,
       samples.count_rate AS cps_max,
       samples.count_rate AS cps_avg,
       samples.count_rate AS cps_latest,
       samples.dose_rate AS dose_rate_min,
       samples.dose_rate AS dose_rate_max,
       samples.dose_rate AS dose_rate_avg,
       samples.dose_rate AS dose_rate_latest
  FROM radiacode_private.scalar_samples AS samples
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
   AND samples.sample_kind = 'real_time'
   AND NOT EXISTS (
       SELECT 1
         FROM radiacode_private.scalar_minute_rollups AS rollups
        WHERE rollups.device_id = samples.device_id
          AND rollups.bucket_at = date_trunc('minute', samples.received_at)
          AND rollups.rolled_at >= samples.inserted_at
   );

CREATE OR REPLACE VIEW radiacode_api.events
WITH (security_barrier = true)
AS
SELECT devices.slug,
       events.received_at AS observed_at,
       events.event_code::text AS code,
       COALESCE(events.event_name, 'Device event') AS name,
       NULL::text AS parameter
  FROM radiacode_private.device_events AS events
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
UNION ALL
SELECT devices.slug,
       gaps.detected_at AS observed_at,
       gaps.gap_kind AS code,
       'Spectrum acquisition gap'::text AS name,
       NULL::text AS parameter
  FROM radiacode_private.data_gaps AS gaps
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
UNION ALL
SELECT devices.slug,
       connections.connected_at AS observed_at,
       'connected'::text AS code,
       'Detector connected'::text AS name,
       NULL::text AS parameter
  FROM radiacode_private.connections AS connections
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
UNION ALL
SELECT devices.slug,
       connections.disconnected_at AS observed_at,
       'disconnected'::text AS code,
       'Detector disconnected'::text AS name,
       NULL::text AS parameter
  FROM radiacode_private.connections AS connections
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
   AND connections.disconnected_at IS NOT NULL;

CREATE OR REPLACE VIEW radiacode_api.spectrum_frames
WITH (security_barrier = true)
AS
SELECT devices.slug,
       devices.model,
       substr(encode(calibrations.fingerprint, 'hex'), 1, 16)
           || '-'
           || (extract(epoch FROM calibrations.started_at) * 1000000)::bigint::text
           AS calibration_epoch,
       calibrations.started_at AS calibration_started_at,
       frames.started_at AS start_at,
       frames.ended_at AS end_at,
       frames.duration_seconds,
       frames.channel_count,
       calibrations.coefficient_a0,
       calibrations.coefficient_a1,
       calibrations.coefficient_a2,
       frames.counts,
       frames.quality_flags
  FROM radiacode_private.spectrum_frames AS frames
  JOIN radiacode_private.devices AS devices USING (device_id)
  JOIN radiacode_private.calibration_epochs AS calibrations USING (calibration_epoch_id)
 WHERE devices.enabled
   AND frames.duration_seconds > 0;

CREATE OR REPLACE VIEW radiacode_api.spectrum_rollups
WITH (security_barrier = true)
AS
SELECT devices.slug,
       devices.model,
       substr(encode(calibrations.fingerprint, 'hex'), 1, 16)
           || '-'
           || (extract(epoch FROM calibrations.started_at) * 1000000)::bigint::text
           AS calibration_epoch,
       calibrations.started_at AS calibration_started_at,
       rollups.bucket_at,
       rollups.actual_started_at AS start_at,
       rollups.actual_ended_at AS end_at,
       rollups.duration_seconds,
       rollups.channel_count,
       calibrations.coefficient_a0,
       calibrations.coefficient_a1,
       calibrations.coefficient_a2,
       rollups.counts,
       rollups.quality_flags,
       rollups.resolution,
       rollups.segment_index,
       rollups.source_frame_count
  FROM radiacode_private.spectrum_rollups AS rollups
  JOIN radiacode_private.devices AS devices USING (device_id)
  JOIN radiacode_private.calibration_epochs AS calibrations USING (calibration_epoch_id)
 WHERE devices.enabled;

REVOKE ALL ON SCHEMA radiacode_private FROM PUBLIC;
REVOKE ALL ON SCHEMA radiacode_api FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA radiacode_private FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA radiacode_api FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA radiacode_private FROM PUBLIC;

GRANT USAGE ON SCHEMA radiacode_private TO radiacode_writer, radiacode_maintenance;
GRANT SELECT (device_id, slug, usb_serial),
      INSERT (device_id, slug, display_name, model, usb_serial),
      UPDATE (display_name, model)
    ON radiacode_private.devices TO radiacode_writer;
GRANT SELECT (connection_id, disconnected_at),
      INSERT, UPDATE (disconnected_at, close_reason)
    ON radiacode_private.connections TO radiacode_writer;
GRANT SELECT (received_at, batch_id, sha256), INSERT
    ON radiacode_private.raw_buffer_batches TO radiacode_writer;
GRANT SELECT (
        device_id, received_at, sample_kind, count_rate, dose_rate,
        count_rate_error_pct, dose_rate_error_pct
    ) ON radiacode_private.scalar_samples TO radiacode_writer;
GRANT SELECT (
        device_id, received_at, accumulated_dose, duration_seconds,
        temperature_c, charge_pct
    ) ON radiacode_private.status_samples TO radiacode_writer;
GRANT INSERT ON radiacode_private.buffer_records,
                radiacode_private.scalar_samples,
                radiacode_private.scalar_rollup_dirty,
                radiacode_private.status_samples,
                radiacode_private.device_events,
                radiacode_private.spectrum_frames,
                radiacode_private.spectrum_snapshots,
                radiacode_private.data_gaps
    TO radiacode_writer;
-- ON CONFLICT must inspect only the dirty marker's conflict key.
GRANT SELECT (device_id, bucket_at)
    ON radiacode_private.scalar_rollup_dirty TO radiacode_writer;
GRANT SELECT, INSERT, UPDATE (ended_at)
    ON radiacode_private.calibration_epochs TO radiacode_writer;
GRANT SELECT (session_id, ended_at),
      INSERT, UPDATE (ended_at, end_reason)
    ON radiacode_private.spectrum_sessions TO radiacode_writer;
GRANT SELECT, INSERT, UPDATE
    ON radiacode_private.spectrum_cursors TO radiacode_writer;
GRANT SELECT (
        device_id, charging, charging_observed_at,
        next_buffer_sequence, buffer_sequence_observed_at
      ), INSERT,
      UPDATE (
        charging, charging_observed_at,
        next_buffer_sequence, buffer_sequence_observed_at
      )
    ON radiacode_private.device_runtime_state TO radiacode_writer;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON radiacode_private.spectrum_frame_accumulators TO radiacode_writer;
GRANT SELECT ON radiacode_private.scalar_samples,
                radiacode_private.spectrum_frames
    TO radiacode_maintenance;
GRANT SELECT, DELETE ON radiacode_private.scalar_rollup_dirty
    TO radiacode_maintenance;
GRANT SELECT, INSERT, UPDATE ON radiacode_private.scalar_minute_rollups,
                                radiacode_private.spectrum_rollups,
                                radiacode_private.rollup_watermarks
    TO radiacode_maintenance;
GRANT DELETE ON radiacode_private.spectrum_rollups TO radiacode_maintenance;
GRANT EXECUTE ON FUNCTION radiacode_private.ensure_daily_partitions(date, date) TO radiacode_maintenance;
GRANT EXECUTE ON FUNCTION radiacode_private.drop_daily_partitions_before(date) TO radiacode_maintenance;

GRANT USAGE ON SCHEMA radiacode_api TO radiacode_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA radiacode_api TO radiacode_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA radiacode_private REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA radiacode_api REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA radiacode_api GRANT SELECT ON TABLES TO radiacode_reader;

COMMENT ON VIEW radiacode_api.device_status IS 'Public-safe current detector state; no hardware serials or database identifiers';
COMMENT ON VIEW radiacode_api.events IS 'Public-safe device timeline with operational error details removed';
