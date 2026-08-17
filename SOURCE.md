# Corresponding source

Published container images are tagged `sha-<git-commit>` and carry the same
commit in the `org.opencontainers.image.revision` OCI label. The preferred
source for an image is:

```text
https://github.com/unixfg/radiacode/tree/<git-commit>
```

An archive of that exact application source is available at:

```text
https://github.com/unixfg/radiacode/archive/<git-commit>.tar.gz
```

The repository contains the Dockerfile, frontend sources, database migrations,
tests, and CI workflow used to build and validate the image. The release SBOM
and provenance attestation record the exact resolved dependency closure.
Installed Python distributions retain their `.dist-info` metadata and license
files; Debian package notices remain under `/usr/share/doc` in the image.

Source and license locations for incorporated, adapted, and browser-bundled
components are pinned in `THIRD_PARTY_NOTICES.md`. The application license and
all curated third-party notices are installed under
`/usr/share/licenses/radiacode` and are also shipped in the browser assets.

Downstream image builders must set both build arguments so remote users receive
the source for the version they are actually running:

```text
SOURCE_URL=https://example.invalid/owner/repository
SOURCE_REVISION=<full-40-character-git-commit>
```

Operators who modify the application must update these values to point to the
Corresponding Source of their modified version.
