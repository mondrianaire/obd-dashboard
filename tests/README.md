# Dashboard regression tests

A battery of correctness checks that run against `telemetry_data.json` — the same file the live dashboard consumes. The point is to catch silent data-correctness drift: bugs that don't make the dashboard *crash* but do make it *lie*. The v1.9 regime-classification bug (shifts being labeled "performance" because the toRpm sample was 1 second late) is the canonical example of what this suite is designed to catch in the future.

## Run it

```bash
python tests/test_dataviews.py
```

Reads `telemetry_data.json` from the repo root by default. Override with `TELEMETRY_JSON=/path/to/other.json`.

Exit code `0` if all PASS, `1` if any FAIL. WARNings don't fail the suite — they're "noticed but acceptable" cases like single-sample sensor transients at engine-on.

The Windows scheduled refresh task runs the suite as a post-prep step automatically, logging the summary line to `refresh.log`.

## Categories

**schema** — structural integrity of the JSON. Top-level keys present, every drive has the required fields, and crucially: any drive that claims `channels.X = true` actually has the `X` key in its rows (catches "we said we ingested coolant but rows don't have it" bugs).

**bounds** — every channel has a physically-plausible min/max range. RPM in [0, 7500], coolant in [-40, 260]°F, lambda in [0.4, 2.5], etc. Caught fully: any units mismatch (kPa logged where psi expected, °C where °F, etc.), broken equations, or sensor faults producing impossible values. Special-case: certain sensors report exactly `0` for the first few seconds after engine-on (lambda, baro, voltage); those zeros are treated as "sensor not yet alive" rather than out-of-range failures.

**consistency** — cross-channel mathematical relationships. The three currently checked:
- `λ × 14.7 ≈ A/F Actual` — wideband O2 sensor and AFR readout must agree (catches AFR equation drift)
- `boost ≈ MAP_psi − baro_psi` — calculated boost should match the implied gauge pressure (catches unit mismatches between MAP and baro PIDs)
- `coolant trends warm` — coolant temperature shouldn't drop by >10°F repeatedly during an engine-on drive (catches sensor failures like the original April 29 G62 issue)
- `ambient ≤ IC outlet ≤ manifold IAT` — thermal ordering for the intercooler triangle (catches mislabeled IC sensors)

**shifts** — shift event correctness, including the v1.9 regression guard:
- Every shift has `fromGear ≠ toGear` (no self-shifts)
- Shift type (up/down) matches the sign of the gear delta
- **`regime == regime_of(idealLandingRpm)`** — this is the v1.9 fix made into a test. If anyone ever changes the regime classifier back to using the noisy `toRpm`, this test fails.
- Plausibility filter rejects zero shifts on the current dataset (acceptable baseline; non-zero would mean phantom shifts are being detected)
- v1.9 is taking effect: the ideal-RPM classifier should classify *fewer or equal* shifts as "performance" than the raw-toRpm one would

**aggregates** — fleet totals match per-drive sums. `totals.driveCount`, `totals.totalDist`, `totals.totalDur`, `totals.maxEverSpeed`, `totals.maxEverRpm` all must equal the corresponding aggregation across `drives[].summary`. Catches: prep script bugs where the rollup gets out of sync with the per-drive data.

**anomalies** — pattern-based suspicion checks:
- Coolant stuck at a non-zero constant (the original G62 sensor fault from April 29)
- RPM spikes (Δ > 3000 RPM in 1 second when not shifting) — could indicate logging glitches or actual misfires

## When tests fail

Each failure prints the offending samples with drive name and timestamp (`@t=583.0: 4138`). Use that to navigate directly to the suspect row in the source CSV or in the dashboard's per-second detail table.

If a failure is real (e.g. you introduced a bug), fix the data pipeline or chart. If a failure is acceptable (e.g. a known sensor quirk on a specific drive), either bound it tighter in the test or add an explicit exception with a comment explaining why.

If the test design itself is too strict (e.g. you legitimately drove through a snowstorm and ambient temp went to -10°F), widen the bound — but document the change.

## Adding new tests

Tests are organized into category functions (`test_schema`, `test_bounds`, ...). Each calls `record(category, name, status, detail)` for each check. Add a new check by either:

- adding an entry to the `BOUNDS` dict (for new physical channels)
- adding a new function call inside an existing `test_*` function (for new consistency / shift / aggregate checks)
- writing a whole new `test_X(raw)` function and adding it to the `main()` call list (for whole new categories)

Tests should be **deterministic** and **fast** — the whole suite runs in under 2 seconds on the current 20-drive dataset and should stay that way.

## Architectural notes

The suite intentionally re-implements the dashboard's gear inference, regime classification, and ideal-landing-RPM math in Python, rather than calling into the JS. This is the right tradeoff: it's a separate independent implementation, so the test acts as a cross-check on the JS logic. If the test's Python implementation and the dashboard's JS implementation ever disagree on what a regime is, that disagreement IS the bug.
