---
description: Report what's new in the OBD dashboard since the last digest run. Diffs current state against .last_seen_state.json and writes a fresh one.
---

# OBD daily digest

You're producing a short, scannable summary of what's changed in the OBD dataset since the last digest ran. This is the on-demand version of the daily report.

## Step 1: Trigger a fresh refresh

Run the full pipeline so any new CSVs land in the dataset before you diff:

```powershell
cd 'C:\Users\mondr\Documents\Claude\obd-dashboard'; powershell -ExecutionPolicy Bypass -File .\auto_refresh.ps1
```

Allow up to 5 minutes. If it fails (push rejected, prep crashed, whatever) — don't abort. Note the failure one line and continue reporting against whatever `telemetry_data.json` + `telemetry_status.json` already exist on disk. Stale-but-real beats nothing.

## Step 2: Read current state

Read `telemetry_status.json` for headline totals (version, buildDate, totals, drives summary list). It's small — safe to Read directly.

If you need per-drive peaks (maxSpeed / mpg / hp) for the "new drives" bullets, Read `telemetry_data.json` and pull from `historical`. telemetry_data.json is ~4 MB — pass `limit=50000` if the first few thousand lines aren't enough.

Capture from totals: `driveCount`, `totalDist`, `totalDur` (seconds), `totalFuel`, `maxEverSpeed`, `maxEverRpm`. New in v2.13: the `trips` field (top level of telemetry_data.json) — capture `tripCount` and the IDs of any multi-leg trips (where `legCount > 1`).

## Step 3: Read the prior state

Try to Read `.last_seen_state.json` at the repo root.

If missing → first run. Skip to Step 5 to write baseline, report Case C, exit.

If present, parse it. Expected fields: `driveCount`, `totalDist`, `totalDur`, `totalFuel`, `maxEverSpeed`, `maxEverRpm`, `historicalIds`, optionally `tripCount` and `multiLegTripIds`.

## Step 4: Compute diffs

- new drives = currentDriveCount − savedDriveCount
- new miles  = currentTotalDist − savedTotalDist
- new fuel   = currentTotalFuel − savedTotalFuel
- speed record broken? currentMaxEverSpeed > savedMaxEverSpeed
- RPM record broken?   currentMaxEverRpm   > savedMaxEverRpm
- new trips stitched: multi-leg trip IDs not in savedMultiLegTripIds

For new drives specifically: drives whose `id` is NOT in savedHistoricalIds.

## Step 5: Report (SHORT, SCANNABLE)

**Case A — new drives found:**
```
Imported {N} new drive(s):
  • {date} {startTime} · {distance} mi · {duration} · {maxSpeed} mph top{, mpg if available}{, hp if available}
  • ...
Dataset now: {driveCount} drives · {totalDist} mi · {hours} hr.
{If new multi-leg trip: '🧭 New trip stitched: {date} · {legCount} legs · {dist} mi.'}
{If a record was broken: '🏁 New top speed: {maxEverSpeed} mph.'}
Live: https://mondrianaire.github.io/obd-dashboard/
```

**Case B — nothing new:**
```
No new drives. Still at {driveCount} drives · {totalDist} mi · {hours} hr.
```

**Case C — first run:**
```
Digest baseline captured: {driveCount} drives · {totalDist} mi · {hours} hr. Top speed of record: {maxEverSpeed} mph. {tripCount} trips total ({multiLegCount} multi-leg).
```

**Case D — refresh failed:**
```
Couldn't refresh today — {brief error summary}. Current data is from {buildDate} with {driveCount} drives.
```

Under 120 words. No JSON. No methodology. Just the summary.

## Step 6: Write new state

Write `.last_seen_state.json` (overwrite):

```json
{
  "checkedAt": "<ISO 8601 now>",
  "driveCount": N,
  "totalDist": X,
  "totalDur": X,
  "totalFuel": X,
  "maxEverSpeed": X,
  "maxEverRpm": X,
  "historicalIds": [...],
  "tripCount": N,
  "multiLegTripIds": [...]
}
```

This file is gitignored. It's the input to the next digest.

## Constraints

- Step 1 is the ONLY place you run the refresh. Use auto_refresh.ps1, not prep_drives.py / the injector / git by hand.
- Do NOT hand-edit telemetry_data.{json,js}, drives/*.json, or index.html.
- Format MPG as one decimal, distance as one decimal, duration mm:ss under an hour or X.Y hr for longer.
