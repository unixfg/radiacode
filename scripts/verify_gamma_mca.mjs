import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const [rawDataSource, schemaDirectory, npesPath, csvPath, expectedPath] = process.argv.slice(2);
if (!rawDataSource || !schemaDirectory || !npesPath || !csvPath || !expectedPath) {
  throw new Error(
    "usage: verify_gamma_mca.mjs RAW_DATA_TS SCHEMA_DIRECTORY NPES_JSON CSV EXPECTED_JSON",
  );
}

const expected = JSON.parse(await readFile(expectedPath, "utf8"));
assert.ok(Array.isArray(expected.counts));
assert.ok(Array.isArray(expected.calibration));

function assertCalibration(actual, label, { allowTrailingZeroes = false } = {}) {
  assert.ok(Array.isArray(actual), `${label} calibration is absent`);
  assert.ok(
    allowTrailingZeroes
      ? actual.length >= expected.calibration.length
      : actual.length === expected.calibration.length,
    `${label} calibration has an unexpected order`,
  );
  expected.calibration.forEach((coefficient, index) => {
    const observed = actual[index];
    assert.ok(Number.isFinite(observed), `${label} coefficient ${index} is not finite`);
    assert.ok(
      Math.abs(observed - coefficient) <= 1e-8 * Math.max(1, Math.abs(coefficient)),
      `${label} coefficient ${index} changed: expected ${coefficient}, got ${observed}`,
    );
  });
  if (allowTrailingZeroes) {
    actual.slice(expected.calibration.length).forEach((coefficient, index) => {
      assert.ok(
        Number.isFinite(coefficient) && Math.abs(coefficient) <= 1e-8,
        `${label} introduced non-zero coefficient ${index + expected.calibration.length}`,
      );
    });
  }
}

globalThis.fetch = async (input) => {
  const requested = String(input);
  const schemaName = requested.endsWith("npes-1.schema.json")
    ? "npes-1.schema.json"
    : requested.endsWith("npes-2.schema.json")
      ? "npes-2.schema.json"
      : null;
  if (schemaName === null) return new Response(null, { status: 404 });
  return new Response(await readFile(`${schemaDirectory}/${schemaName}`, "utf8"), {
    headers: { "content-type": "application/json" },
    status: 200,
  });
};

const { RawData } = await import(pathToFileURL(rawDataSource).href);
const importer = new RawData(1, ",");
const parsed = await importer.jsonToObject(await readFile(npesPath, "utf8"));
assert.equal(parsed.length, 1);
assert.ok(!("code" in parsed[0]), JSON.stringify(parsed));

const energySpectrum = parsed[0].resultData.energySpectrum;
assert.ok(energySpectrum);
assert.equal(energySpectrum.numberOfChannels, energySpectrum.spectrum.length);
assert.deepEqual(energySpectrum.spectrum, expected.counts);
assertCalibration(energySpectrum.energyCalibration?.coefficients, "NPESv2");
assert.equal(
  energySpectrum.validPulseCount,
  expected.counts.reduce((total, count) => total + count, 0),
);

const reparsed = await importer.jsonToObject(
  JSON.stringify({ schemaVersion: "NPESv2", data: parsed }),
);
assert.deepEqual(reparsed, parsed);

const csv = importer.csvToArray(await readFile(csvPath, "utf8"));
assert.deepEqual(csv.histogramData, expected.counts);
// Gamma MCA fits a fourth-order polynomial to CSV energy/channel pairs. The
// source fixture is quadratic, so the first three coefficients must match and
// the two higher-order terms must remain numerically zero.
assertCalibration(csv.calibrationCoefficients, "CSV", { allowTrailingZeroes: true });

process.stdout.write(
  `Gamma MCA imported NPESv2 and CSV with ${energySpectrum.numberOfChannels} calibrated channels\n`,
);
