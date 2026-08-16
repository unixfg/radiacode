from __future__ import annotations

import argparse
import math
from pathlib import Path
from xml.etree import ElementTree as ET


def _first(root: ET.Element, name: str) -> ET.Element:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return element
    raise ValueError(f"missing {name}")


def _counts(root: ET.Element) -> tuple[int, ...]:
    channel_data = _first(root, "ChannelData")
    values = [int(value) for value in (channel_data.text or "").split()]
    compression = channel_data.attrib.get("compressionCode", "None")
    if compression == "None":
        return tuple(values)
    if compression != "CountedZeroes":
        raise ValueError(f"unsupported ChannelData compression {compression}")
    decoded: list[int] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value != 0:
            decoded.append(value)
            index += 1
            continue
        if index + 1 >= len(values):
            raise ValueError("truncated CountedZeroes run")
        decoded.extend([0] * values[index + 1])
        index += 2
    return tuple(decoded)


def _coefficients(root: ET.Element) -> tuple[float, ...]:
    return tuple(float(value) for value in (_first(root, "CoefficientValues").text or "").split())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("expected", type=Path)
    parser.add_argument("converted", type=Path, nargs="+")
    args = parser.parse_args()

    expected_root = ET.parse(args.expected).getroot()
    expected_counts = _counts(expected_root)
    expected_coefficients = _coefficients(expected_root)
    for path in args.converted:
        root = ET.parse(path).getroot()
        if _counts(root) != expected_counts:
            raise ValueError(f"SpecUtils changed channel counts in {path}")
        observed_coefficients = _coefficients(root)
        if len(observed_coefficients) != len(expected_coefficients) or any(
            not math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-8)
            for left, right in zip(observed_coefficients, expected_coefficients, strict=True)
        ):
            raise ValueError(f"SpecUtils changed calibration coefficients in {path}")
    print(
        f"SpecUtils preserved {len(expected_counts)} channels and calibration across "
        f"{len(args.converted)} conversions"
    )


if __name__ == "__main__":
    main()
