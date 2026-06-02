# OBD Dashboard

A self-contained interactive dashboard for 12+ months of OBD-II / GPS / IMU drive logs from a **2016 VW Golf GTI (2.0T EA888)**, sourced from three different logging apps (OBD Fusion CSV exports, Car Scanner SQLite `.dlg` files, and a third app's CSV output).

Live view: **https://mondrianaire.github.io/obd-dashboard/**

## What you get

A single self-contained HTML file (`index.html`) that renders:

- A **Fleet overview** section — aggregate behaviour across every drive (totals, compare-drives chart, pooled speed/RPM histograms, pooled boost-vs-RPM and AFR-vs-throttle scatters, pooled power curve and fuel-band).
- A **Drive detail** section — drill into any single trip with route map, auto-picked highlights, KPIs, timeline, per-drive scatters and histograms, engine diagnostics, and a per-second detail table.
- A drive selector with `date · time · distance · duration · name` labels.
- A time-window slider that scopes the drive section to any slice of the selected trip.
- A small version badge in the bottom-right showing the build date and current drive count.

Everything (data + chart logic) is embedded in the HTML, so the page works offline and you can host it anywhere static.

## Repo layout

```
.
├── index.html                  the dashboard (data embedded)
├── telemetry_data.json         the embedded data, also kept as a separate file
├── prep_drives.py              builds telemetry_data.json from raw logs
├── refresh_dashboard.py        one-shot refresh: prep → inject → verify → git push
├── refresh_dashboard.bat       Windows double-click wrapper
├── setup_github.ps1            one-time GitHub remote + Pages setup
└── data/                       (LOCAL ONLY, gitignored)
    ├── obd_fusion/             snapshot of OBD Fusion CSV exports
    ├── car_scanner/            Car Scanner .dlg SQLite logs
    └── third_party/            CSVs from a third app (April 29 + May 5)
```

### What ships and what doesn't

The repo carries only the **outputs** that GitHub Pages needs: the rendered
`index.html` (with all telemetry baked into a single embedded JSON), a copy of
that JSON as a standalone file for other tools to consume, and the Python
scripts that build them. The raw per-drive CSV / .dlg sources stay on your
local machine — the `data/` folder is git-ignored — so the repo stays small
(~14 MB instead of ~370 MB) and the raw driving traces aren't published.

`prep_drives.py` reads from two places when you refresh:
- `~/Dropbox/Apps/OBD Fusion/CsvLogs/<VIN>/` for *live* OBD Fusion CSVs as
  they sync from your phone
- `./data/car_scanner/` and `./data/third_party/` for the one-off .dlg and
  third-app uploads that don't change

Anyone who clones the repo without those local source files will get the
shipped `telemetry_data.json` as-is and won't be able to regenerate it; the
dashboard itself still works because the data is embedded in the HTML.

## Refreshing the dashboard

Drop new log files into `~/Dropbox/Apps/OBD Fusion/CsvLogs/3VW5T7AU2GM058168/` (the path is configurable via the `DROPBOX_DIR` env var) and run:

```bash
python refresh_dashboard.py
```

or on Windows, double-click `refresh_dashboard.bat`. The script will:

1. Re-scan the Dropbox folder for new CSVs.
2. Merge with the snapshot in `data/` (with the dedupe rule: a `.dlg` file beats a Dropbox CSV at the same timestamp because it carries wideband AFR + MAP).
3. Rebuild `telemetry_data.json`.
4. Inject the fresh data into `index.html`.
5. Bump the version badge with today's date and the new drive count.
6. `git add / commit / push` so the GitHub repo and Pages site stay in sync.

Pass `--no-git` or set `NO_GIT=1` to skip the git step.

## Data quality notes

- The April 29 log exposes coolant / engine load / short-term fuel trim PIDs but they're stuck or default — likely an app-side decoding issue, not a car fault. Values are shown as logged.
- The original CSV-only app couldn't read positive boost; the `.dlg` files and the new OBD Fusion logs show real boost behaviour (vacuum at idle, ~17–19 psi at WOT) which confirms the turbo.
- Engine coolant temperature does **not** appear in either `.dlg` file — consistent with the known oil-temp-sensor fault on the dash.

## License

Personal project. Code under MIT-style permissive use; the per-drive data is included for reproducibility only and is not a representation of anything meaningful beyond what's labeled.
