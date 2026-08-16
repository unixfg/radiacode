from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from radiacode_app.exporters import (
    ExportSpectrum,
    export_csv,
    export_iaea_spe,
    export_n42_2012,
    export_npes_v2,
    export_radiacode_xml,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    started = datetime(2026, 8, 16, 12, tzinfo=UTC)
    spectrum = ExportSpectrum(
        device_slug="radiacode-110",
        device_model="RC-110",
        calibration_key="representative-epoch",
        start_at=started,
        end_at=started + timedelta(seconds=300),
        duration_seconds=300,
        counts=(*tuple(index % 17 for index in range(1_023)), 3),
        calibration=(-0.42, 2.51, 0.00031),
        title="Representative interoperability spectrum",
    )
    outputs = {
        "representative.n42": export_n42_2012((spectrum,)),
        "representative.npes.json": export_npes_v2((spectrum,)),
        "representative.csv": export_csv((spectrum,)),
        "representative.spe": export_iaea_spe(spectrum),
        "representative.xml": export_radiacode_xml(spectrum),
        # This oracle is serialized directly from the transport-neutral source
        # model, not parsed back out of either exporter. External-tool checks can
        # therefore detect matching corruption in more than one output format.
        "representative.expected.json": json.dumps(
            {
                "calibration": list(spectrum.calibration),
                "counts": list(spectrum.counts[:-1]),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    }
    for name, payload in outputs.items():
        (args.output / name).write_bytes(payload)


if __name__ == "__main__":
    main()
