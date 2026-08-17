# Vendored interchange schemas

- `n42-2012.xsd` is the ANSI N42.42-2011 schema version 0.0.54 dated
  2012-02-01. It is whitespace-normalized from NIST's authoritative download
  at <https://www.nist.gov/document/n42xsd>; its non-whitespace content matches
  SHA-256 `d256aa094fb1cdd91fc3db7f584024f33bcce36d890ded8b7675f338a4cf64df`.
  NIST is credited as the source and its redistribution and software notice is
  retained in `LICENSES/NIST-software-notice.txt`.
- `npes-v2.schema.json` is the NPESv2 Draft 7 JSON Schema vendored from
  Open Gamma Project's NPES-JSON commit
  [`d2e5de1f4693dcd083045955d4742d46d5d9ea4f`](https://github.com/OpenGammaProject/NPES-JSON/blob/d2e5de1f4693dcd083045955d4742d46d5d9ea4f/schema/npes-2.schema.json).
  It is whitespace-normalized only and remains MIT-licensed; the complete
  notice is retained in `LICENSES/NPES-JSON-MIT.txt`.

The exporter test suite validates representative output against these files.
