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
       runtime.charging_observed_at,
       firmware.firmware_version
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
  LEFT JOIN LATERAL (
      SELECT connections.firmware_major::text
                 || '.' || connections.firmware_minor::text AS firmware_version
        FROM radiacode_private.connections AS connections
       WHERE connections.device_id = devices.device_id
         AND connections.firmware_major IS NOT NULL
         AND connections.firmware_minor IS NOT NULL
       ORDER BY connections.connected_at DESC
       LIMIT 1
  ) AS firmware ON true
 WHERE devices.enabled;

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
       CASE
           WHEN gaps.gap_kind IN (
               'channel_count_change',
               'calibration_change',
               'duration_regression',
               'count_regression',
               'counts_changed_without_duration_change'
           ) THEN 'Spectrum acquisition gap'
           ELSE 'Acquisition gap'
       END AS name,
       NULL::text AS parameter
  FROM radiacode_private.data_gaps AS gaps
  JOIN radiacode_private.devices AS devices USING (device_id)
 WHERE devices.enabled
   -- These legacy rows were produced by decoder alignment and cross-read
   -- sequence bugs, not detector acquisition loss. Keep them private/auditable.
   AND gaps.gap_kind <> 'data_buf_sequence_gap'
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

GRANT SELECT ON radiacode_api.device_status, radiacode_api.events TO radiacode_reader;

COMMENT ON VIEW radiacode_api.device_status IS
    'Public-safe current detector state and latest known target firmware; no hardware identities';
COMMENT ON VIEW radiacode_api.events IS
    'Public-safe device timeline; known-false legacy DATA_BUF decoder gaps remain private';
