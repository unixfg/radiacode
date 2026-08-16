from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from jsonschema.validators import validator_for
from lxml import etree

from radiacode_app.exporters import (
    ExportSpectrum,
    export_csv,
    export_iaea_spe,
    export_n42_2012,
    export_npes_v2,
    export_radiacode_xml,
)
from radiacode_app.exporters.formats import N42_NAMESPACE


@pytest.fixture
def spectrum() -> ExportSpectrum:
    start = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    return ExportSpectrum(
        device_slug="radiacode-110",
        device_model="RadiaCode-110",
        calibration_key="epoch-1",
        start_at=start,
        end_at=start + timedelta(seconds=300),
        duration_seconds=300,
        counts=(1, 2, 3, 9),
        calibration=(-1.25, 2.5, 0.0004),
        title="Five minute frame",
    )


def test_model_excludes_metadata_channel_from_valid_count(spectrum: ExportSpectrum) -> None:
    assert spectrum.total_count == 15
    assert spectrum.valid_count == 6
    assert spectrum.overflow_count == 9
    with pytest.raises(ValueError, match="metadata"):
        spectrum.energy_kev(3)


def test_n42_contains_public_identity_and_multiple_measurements(spectrum: ExportSpectrum) -> None:
    data = export_n42_2012((spectrum, spectrum))
    root = ET.fromstring(data)
    ns = {"n42": N42_NAMESPACE}
    assert root.tag == f"{{{N42_NAMESPACE}}}RadInstrumentData"
    assert len(root.findall("n42:RadMeasurement", ns)) == 2
    assert root.findtext("n42:RadDetectorInformation/n42:RadDetectorName", namespaces=ns) == "radiacode-110"
    assert b"RC-110-" not in data
    assert root.findtext("n42:RadMeasurement/n42:Spectrum/n42:ChannelData", namespaces=ns) == "1 2 3"


def test_npes_v2_uses_schema_shape_and_valid_count(spectrum: ExportSpectrum) -> None:
    parsed = json.loads(export_npes_v2((spectrum,)))
    assert parsed["schemaVersion"] == "NPESv2"
    exported = parsed["data"][0]["resultData"]["energySpectrum"]
    assert exported["numberOfChannels"] == 3
    assert exported["validPulseCount"] == 6
    assert exported["spectrum"] == [1, 2, 3]


def test_n42_and_npes_validate_against_bundled_upstream_schemas(
    spectrum: ExportSpectrum,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    n42_schema = etree.XMLSchema(etree.parse(project_root / "schemas" / "n42-2012.xsd"))
    n42_schema.assertValid(etree.fromstring(export_n42_2012((spectrum,))))

    npes_schema = json.loads((project_root / "schemas" / "npes-v2.schema.json").read_text())
    validator = validator_for(npes_schema)
    validator.check_schema(npes_schema)
    validator(npes_schema).validate(json.loads(export_npes_v2((spectrum,))))


def test_csv_marks_final_channel_as_metadata(spectrum: ExportSpectrum) -> None:
    exported = export_csv((spectrum,)).decode()
    reader = csv.DictReader(io.StringIO(exported))
    assert reader.fieldnames is not None
    assert reader.fieldnames[:2] == ["energy_kev", "count"]
    rows = list(reader)
    assert len(rows) == 4
    assert [int(row["count"]) for row in rows[:-1]] == [1, 2, 3]
    assert rows[-1]["under_overflow_metadata"] == "true"
    assert rows[-1]["energy_kev"] == ""


def test_iaea_spe_has_required_blocks_and_no_hardware_serial(spectrum: ExportSpectrum) -> None:
    text = export_iaea_spe(spectrum).decode("ascii")
    assert "$MEAS_TIM:\r\n300.00000 300.00000" in text
    assert "$DATA:\r\n0 2\r\n1\r\n2\r\n3" in text
    assert "under/overflow metadata count: 9" in text
    assert "$ENER_FIT:" in text
    assert "RC-110-" not in text


def test_exports_distinguish_live_exposure_from_gapped_wall_time(
    spectrum: ExportSpectrum,
) -> None:
    gapped = replace(
        spectrum,
        end_at=spectrum.end_at + timedelta(seconds=120),
    )
    n42 = ET.fromstring(export_n42_2012((gapped,)))
    ns = {"n42": N42_NAMESPACE}
    assert n42.findtext("n42:RadMeasurement/n42:RealTimeDuration", namespaces=ns) == "PT420.000000S"
    assert (
        n42.findtext("n42:RadMeasurement/n42:Spectrum/n42:LiveTimeDuration", namespaces=ns) == "PT300.000000S"
    )
    spe = export_iaea_spe(gapped).decode("ascii")
    assert "$MEAS_TIM:\r\n300.00000 420.00000" in spe


def test_radiacode_xml_round_trip_shape_is_public_safe(spectrum: ExportSpectrum) -> None:
    root = ET.fromstring(export_radiacode_xml(spectrum))
    result = root.find("./ResultDataList/ResultData")
    assert result is not None
    energy = result.find("EnergySpectrum")
    assert energy is not None
    assert energy.findtext("NumberOfChannels") == "3"
    assert energy.findtext("SerialNumber") == "radiacode-110"
    assert [int(node.text or 0) for node in energy.findall("./Spectrum/DataPoint")] == [1, 2, 3]
    assert "under/overflow metadata count: 9" in (energy.findtext("Comment") or "")
