"""N42-2012, NPESv2, CSV, IAEA SPE, and RadiaCode XML exporters."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from radiacode_app.exporters.models import ExportSpectrum

N42_NAMESPACE = "http://physics.nist.gov/N42/2011/N42"
N42_FORMAT = "n42-2012"
NPES_FORMAT = "npes-v2"
CSV_FORMAT = "csv"
IAEA_SPE_FORMAT = "iaea-spe"
RADIACODE_XML_FORMAT = "radiacode-xml"

_XML_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _require_spectra(spectra: Iterable[ExportSpectrum]) -> tuple[ExportSpectrum, ...]:
    result = tuple(spectra)
    if not result:
        raise ValueError("at least one spectrum is required")
    return result


def _xml_id(prefix: str, value: str) -> str:
    normalized = _XML_ID_RE.sub("-", value).strip("-.")
    if not normalized:
        normalized = "value"
    if not normalized[0].isalpha():
        normalized = f"v-{normalized}"
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{normalized}-{suffix}"


def _utc_iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _duration(value: float) -> str:
    return f"PT{value:.6f}S"


def _float_list(values: Sequence[float]) -> str:
    return " ".join(format(value, ".12g") for value in values)


def _add_xml(parent: ET.Element, name: str, text: str | None = None, **attributes: str) -> ET.Element:
    child = ET.SubElement(parent, f"{{{N42_NAMESPACE}}}{name}", attributes)
    if text is not None:
        child.text = text
    return child


def export_n42_2012(spectra: Iterable[ExportSpectrum]) -> bytes:
    """Serialize one or more spectra as schema-valid ANSI N42.42-2011/2012 XML."""

    items = _require_spectra(spectra)
    ET.register_namespace("", N42_NAMESPACE)

    document_fingerprint = "|".join(
        f"{item.device_slug}:{item.calibration_key}:{item.content_sha256}:{_utc_iso(item.utc_start())}"
        for item in items
    )
    document_uuid = uuid5(NAMESPACE_URL, document_fingerprint)
    root = ET.Element(
        f"{{{N42_NAMESPACE}}}RadInstrumentData",
        {
            "n42DocUUID": str(document_uuid),
            "n42DocDateTime": _utc_iso(max(item.utc_end() for item in items)),
        },
    )
    _add_xml(root, "RadInstrumentDataCreatorName", "unixfg/radiacode")

    models = sorted({item.device_model for item in items})
    instrument = _add_xml(root, "RadInstrumentInformation", id="instrument")
    _add_xml(instrument, "RadInstrumentManufacturerName", "RadiaCode")
    _add_xml(
        instrument, "RadInstrumentModelName", models[0] if len(models) == 1 else "RadiaCode detector network"
    )
    _add_xml(instrument, "RadInstrumentClassCode", "Spectroscopic Personal Radiation Detector")
    version = _add_xml(instrument, "RadInstrumentVersion")
    _add_xml(version, "RadInstrumentComponentName", "Export software")
    _add_xml(version, "RadInstrumentComponentVersion", "unixfg/radiacode")

    detector_ids: dict[str, str] = {}
    for item in items:
        if item.device_slug in detector_ids:
            continue
        detector_id = _xml_id("detector", item.device_slug)
        detector_ids[item.device_slug] = detector_id
        detector = _add_xml(root, "RadDetectorInformation", id=detector_id)
        _add_xml(detector, "RadDetectorName", item.device_slug)
        _add_xml(detector, "RadDetectorCategoryCode", "Gamma")
        _add_xml(detector, "RadDetectorKindCode", "CsI")
        _add_xml(detector, "RadDetectorDescription", item.device_model)

    calibration_ids: dict[tuple[str, str], str] = {}
    for item in items:
        key = (item.device_slug, item.calibration_key)
        if key in calibration_ids:
            continue
        calibration_id = _xml_id("cal", f"{item.device_slug}-{item.calibration_key}")
        calibration_ids[key] = calibration_id
        calibration = _add_xml(root, "EnergyCalibration", id=calibration_id)
        _add_xml(calibration, "CoefficientValues", _float_list(item.calibration))

    for index, item in enumerate(items, start=1):
        measurement_id = _xml_id("measurement", f"{index}-{item.device_slug}")
        measurement = _add_xml(root, "RadMeasurement", id=measurement_id)
        _add_xml(measurement, "MeasurementClassCode", "Foreground")
        _add_xml(measurement, "StartDateTime", _utc_iso(item.utc_start()))
        _add_xml(measurement, "RealTimeDuration", _duration(item.real_time_seconds))
        spectrum = _add_xml(
            measurement,
            "Spectrum",
            id=_xml_id("spectrum", f"{index}-{item.device_slug}"),
            energyCalibrationReference=calibration_ids[(item.device_slug, item.calibration_key)],
            radDetectorInformationReference=detector_ids[item.device_slug],
        )
        remark = (
            "RadiaCode under/overflow metadata count: "
            f"{item.overflow_count}; excluded from calibrated channel data."
        )
        if item.rebinned:
            remark += " Counts were count-conservingly energy rebinned."
        if item.quality_flags:
            remark += f" Quality flags: {', '.join(item.quality_flags)}."
        _add_xml(spectrum, "Remark", remark)
        _add_xml(spectrum, "LiveTimeDuration", _duration(item.duration_seconds))
        _add_xml(
            spectrum,
            "ChannelData",
            " ".join(str(value) for value in item.counts[:-1]),
            compressionCode="None",
        )

    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def export_npes_v2(spectra: Iterable[ExportSpectrum]) -> bytes:
    """Serialize one or more independent spectra using the NPESv2 JSON schema."""

    items = _require_spectra(spectra)
    packages: list[dict[str, object]] = []
    for item in items:
        energy_spectrum: dict[str, object] = {
            "numberOfChannels": item.channel_count - 1,
            "energyCalibration": {
                "polynomialOrder": 2,
                "coefficients": list(item.calibration),
            },
            "measurementTime": max(1, round(item.duration_seconds)),
            "spectrum": list(item.counts[:-1]),
        }
        # NPESv2 defines validPulseCount as a positive integer, so an empty
        # calibrated spectrum must omit the optional field rather than emit 0.
        if item.valid_count:
            energy_spectrum["validPulseCount"] = item.valid_count
        packages.append(
            {
                "deviceData": {
                    "deviceName": item.device_model,
                    "softwareName": "unixfg/radiacode",
                    "publicIdentifier": item.device_slug,
                },
                "sampleInfo": {
                    "name": item.title,
                    "note": (
                        "RadiaCode under/overflow metadata count: "
                        f"{item.overflow_count}; excluded from calibrated channel data."
                    ),
                },
                "resultData": {
                    "startTime": _utc_iso(item.utc_start()),
                    "endTime": _utc_iso(item.utc_end()),
                    "energySpectrum": energy_spectrum,
                },
            }
        )
    return json.dumps({"schemaVersion": "NPESv2", "data": packages}, separators=(",", ":")).encode("utf-8")


def export_csv(spectra: Iterable[ExportSpectrum]) -> bytes:
    """Serialize tidy spectra with Gamma MCA-compatible energy/count leading columns."""

    items = _require_spectra(spectra)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "energy_kev",
            "count",
            "channel",
            "device",
            "calibration_epoch",
            "start_at_utc",
            "end_at_utc",
            "duration_seconds",
            "under_overflow_metadata",
            "rebinned",
        )
    )
    for item in items:
        for channel, count in enumerate(item.counts):
            is_overflow = channel == item.channel_count - 1
            writer.writerow(
                (
                    "" if is_overflow else format(item.energy_kev(channel), ".12g"),
                    count,
                    channel,
                    item.device_slug,
                    item.calibration_key,
                    _utc_iso(item.utc_start()),
                    _utc_iso(item.utc_end()),
                    format(item.duration_seconds, ".6f"),
                    str(is_overflow).lower(),
                    str(item.rebinned).lower(),
                )
            )
    return output.getvalue().encode("utf-8")


def export_iaea_spe(spectrum: ExportSpectrum) -> bytes:
    """Serialize one spectrum in the block-oriented IAEA SPE interchange format."""

    start = spectrum.utc_start().strftime("%m/%d/%Y %H:%M:%S")
    coefficients = _float_list(spectrum.calibration)
    remarks = [
        "Generated by unixfg/radiacode",
        f"Public detector: {spectrum.device_slug}",
        "RadiaCode under/overflow metadata count: "
        f"{spectrum.overflow_count}; excluded from calibrated channel data.",
    ]
    if spectrum.rebinned:
        remarks.append("Counts were count-conservingly energy rebinned.")
    if spectrum.quality_flags:
        remarks.append(f"Quality flags: {', '.join(spectrum.quality_flags)}")
    safe_title = (
        spectrum.title.replace("\r", " ").replace("\n", " ").encode("ascii", "replace").decode("ascii")
    )
    lines = [
        "$SPEC_ID:",
        safe_title,
        "$SPEC_REM:",
        *remarks,
        "$DATE_MEA:",
        start,
        "$MEAS_TIM:",
        f"{spectrum.duration_seconds:.5f} {spectrum.real_time_seconds:.5f}",
        "$DATA:",
        f"0 {spectrum.channel_count - 2}",
        *(str(value) for value in spectrum.counts[:-1]),
        "$ENER_FIT:",
        coefficients,
        "$MCA_CAL:",
        "3",
        coefficients,
        "$ENDRECORD:",
        "",
    ]
    return "\r\n".join(lines).encode("ascii", errors="strict")


def _rc_element(parent: ET.Element, name: str, text: str | int | float | None = None) -> ET.Element:
    child = ET.SubElement(parent, name)
    if text is not None:
        child.text = str(text)
    return child


def export_radiacode_xml(spectrum: ExportSpectrum) -> bytes:
    """Serialize one public-safe RadiaCode/BecqMoni-compatible spectrum XML file."""

    root = ET.Element(
        "ResultDataFile",
        {
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        },
    )
    _rc_element(root, "FormatVersion", "120920")
    result_list = _rc_element(root, "ResultDataList")
    result = _rc_element(result_list, "ResultData")
    device = _rc_element(result, "DeviceConfigReference")
    _rc_element(device, "Name", spectrum.device_model)
    sample = _rc_element(result, "SampleInfo")
    _rc_element(sample, "Name", spectrum.title)
    _rc_element(sample, "Note", "Public export; hardware serial removed")
    _rc_element(result, "BackgroundSpectrumFile", "")
    _rc_element(result, "StartTime", spectrum.utc_start().strftime("%Y-%m-%dT%H:%M:%S"))
    _rc_element(result, "EndTime", spectrum.utc_end().strftime("%Y-%m-%dT%H:%M:%S"))
    energy = _rc_element(result, "EnergySpectrum")
    _rc_element(energy, "NumberOfChannels", spectrum.channel_count - 1)
    _rc_element(energy, "ChannelPitch", 1)
    _rc_element(energy, "SpectrumName", spectrum.title)
    _rc_element(
        energy,
        "Comment",
        "RadiaCode under/overflow metadata count: "
        f"{spectrum.overflow_count}; excluded from calibrated channel data.",
    )
    _rc_element(energy, "SerialNumber", spectrum.device_slug)
    calibration = _rc_element(energy, "EnergyCalibration")
    _rc_element(calibration, "PolynomialOrder", 2)
    coefficients = _rc_element(calibration, "Coefficients")
    for value in spectrum.calibration:
        _rc_element(coefficients, "Coefficient", format(value, ".12g"))
    _rc_element(energy, "MeasurementTime", max(1, round(spectrum.duration_seconds)))
    data = _rc_element(energy, "Spectrum")
    for value in spectrum.counts[:-1]:
        _rc_element(data, "DataPoint", value)
    _rc_element(result, "Visible", "true")
    pulses = _rc_element(result, "PulseCollection")
    _rc_element(pulses, "Format", "Base64 encoded binary")
    _rc_element(pulses, "Pulses", "")
    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))
