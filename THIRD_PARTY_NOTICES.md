# Third-party notices

RadiaCode Observatory is distributed as a combined work under
`AGPL-3.0-only`. The components below retain their original copyright and
license notices. These notices do not change the license of original
RadiaCode Observatory code.

## Incorporated and adapted components

### cdump/radiacode 0.4.0

- Use: USB device communication and the protocol map adapted by the lossless
  `DATA_BUF` decoder.
- Copyright: 2021 Maxim Andreev.
- License: MIT; complete notice in `LICENSES/radiacode-MIT.txt`.
- Source: commit
  [`3e9a2aaec60aa1da06834310c5fb660133e734d3`](https://github.com/cdump/radiacode/tree/3e9a2aaec60aa1da06834310c5fb660133e734d3).

### React browser runtime

- Components: React 19.1.1, React DOM 19.1.1, and Scheduler 0.26.0.
- Copyright: Meta Platforms, Inc. and affiliates.
- License: MIT; complete notice in `LICENSES/React-MIT.txt`.
- Source: React `v19.1.1`, commit
  [`02ef49580922f87180f32618b9d1c70b75b968b7`](https://github.com/facebook/react/tree/02ef49580922f87180f32618b9d1c70b75b968b7).

## Vendored validation schemas

### NPES-JSON schema

- File: `schemas/npes-v2.schema.json`.
- Copyright: 2023 Open Gamma Project.
- License: MIT; complete notice in `LICENSES/NPES-JSON-MIT.txt`.
- Source: commit
  [`d2e5de1f4693dcd083045955d4742d46d5d9ea4f`](https://github.com/OpenGammaProject/NPES-JSON/blob/d2e5de1f4693dcd083045955d4742d46d5d9ea4f/schema/npes-2.schema.json).
- Local change: whitespace normalization only.

### ANSI N42.42-2011 XML schema

- File: `schemas/n42-2012.xsd`.
- Source and credit: National Institute of Standards and Technology,
  [N42-2011 project](https://www.nist.gov/pml/radiation-physics/n42-2011) and
  [authoritative schema download](https://www.nist.gov/document/n42xsd).
- Terms: NIST public-information and software notice, retained in
  `LICENSES/NIST-software-notice.txt`.
- Local change: whitespace normalization only; the normalized content hash is
  recorded in `schemas/README.md` and checked by the test suite.

## Other packaged dependencies

The container also installs unmodified Python, Debian, and base-image
components under their own compatible licenses. The release SBOM is the
authoritative inventory of the exact versions in each image. Installed Python
wheel metadata and license files remain in their `.dist-info` directories, and
Debian copyright notices remain under `/usr/share/doc`. No dependency license
is replaced by the project-level AGPL declaration.
