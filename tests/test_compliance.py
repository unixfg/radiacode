from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _normalized_hash(path: Path) -> str:
    normalized = re.sub(rb"\s+", b"", path.read_bytes())
    return hashlib.sha256(normalized).hexdigest()


def test_pep639_metadata_includes_all_legal_notices() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "AGPL-3.0-only"
    assert project["license-files"] == [
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "LICENSES/*.txt",
    ]
    for path in (
        ROOT / "LICENSE",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "LICENSES" / "radiacode-MIT.txt",
        ROOT / "LICENSES" / "React-MIT.txt",
        ROOT / "LICENSES" / "NPES-JSON-MIT.txt",
        ROOT / "LICENSES" / "NIST-software-notice.txt",
    ):
        assert path.is_file(), f"missing legal notice: {path.relative_to(ROOT)}"


def test_vendored_schemas_match_pinned_upstream_content() -> None:
    assert _normalized_hash(ROOT / "schemas" / "n42-2012.xsd") == (
        "d256aa094fb1cdd91fc3db7f584024f33bcce36d890ded8b7675f338a4cf64df"
    )
    assert _normalized_hash(ROOT / "schemas" / "npes-v2.schema.json") == (
        "a96c9c9a97853193ee22f19720461d6e573bb4d13aaf29be8a2e63cc4ab316c3"
    )


def test_image_embeds_notices_and_immutable_source_metadata() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "org.opencontainers.image.revision" in dockerfile
    assert "COPY --chmod=0644 pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./" in dockerfile
    assert "COPY --chmod=0644 LICENSES/*.txt ./LICENSES/" in dockerfile
    assert "COPY --chmod=0644 LICENSE SOURCE.md THIRD_PARTY_NOTICES.md" in dockerfile
    assert "COPY --chmod=0644 LICENSES/*.txt /usr/share/licenses/radiacode/LICENSES/" in dockerfile
    assert "SOURCE_REVISION=${{ github.sha }}" in workflow
    assert "SOURCE_URL=https://github.com/${{ github.repository }}" in workflow
