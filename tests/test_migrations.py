from __future__ import annotations

import hashlib
import unittest

from radiacode_app.database import stable_device_id
from radiacode_app.migrator import bundled_migrations


class MigrationTests(unittest.TestCase):
    def test_bundled_migrations_are_sorted_unique_and_checksummed(self) -> None:
        migrations = bundled_migrations()
        versions = [migration.version for migration in migrations]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(len(versions), len(set(versions)))
        self.assertIn("0001_initial", versions)
        for migration in migrations:
            self.assertEqual(migration.sha256_hex, hashlib.sha256(migration.sql.encode()).hexdigest())

    def test_partition_functions_are_hardened_for_non_owner_maintenance(self) -> None:
        initial = next(
            migration.sql for migration in bundled_migrations() if migration.version == "0001_initial"
        )
        self.assertEqual(initial.count("SECURITY DEFINER"), 2)
        self.assertEqual(initial.count("SET search_path = pg_catalog, pg_temp"), 2)

    def test_public_slug_has_stable_non_database_identifier(self) -> None:
        self.assertEqual(stable_device_id("rc-test"), stable_device_id("rc-test"))
        self.assertNotEqual(stable_device_id("rc-test"), stable_device_id("rc-other"))

    def test_device_metadata_migration_hides_legacy_decoder_gaps(self) -> None:
        metadata = next(
            migration.sql
            for migration in bundled_migrations()
            if migration.version == "0003_device_metadata_and_event_labels"
        )
        self.assertIn("firmware.firmware_version", metadata)
        self.assertIn("gaps.gap_kind <> 'data_buf_sequence_gap'", metadata)


if __name__ == "__main__":
    unittest.main()
