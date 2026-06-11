#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refresh_dashboard.py - one-shot refresh + git sync.

Steps:
  1. Run prep_drives.py to scan the Dropbox folder (live OBD Fusion CSVs)
     plus the repo's data/ snapshot, producing telemetry_data.json.
  2. Inject that JSON into index.html (replacing the previous embedded data).
  3. Bump the version badge with today's date and the drive count.
  4. (optional) git add / commit / push so local + remote stay in sync.

Skip git with --no-git or by setting NO_GIT=1.
"""
from __future__ import annotations
import subprocess, sys, json, re, os, datetime, time, io

# Force UTF-8 for stdout so console prints work when the script is launched
# from Windows Task Scheduler (which inherits cp1252 by default).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PREP = os.path.join(HERE, "prep_drives.py")
ARCHIVE = os.path.join(HERE, "archive_old_csvs.py")
TESTS = os.path.join(HERE, "tests", "test_dataviews.py")
JSON_PATH = os.path.join(HERE, "telemetry_data.json")
JS_PATH   = os.path.join(HERE, "telemetry_data.js")
STATUS_PATH = os.path.join(HERE, "telemetry_status.json")
HTML_PATH = os.path.join(HERE, "index.html")

# Dashboard version, surfaced in the corner badge. Bump it whenever you ship
# a meaningful UI/feature change. Format: 'v<major>.<minor>'.
#   v1.0 - first multi-drive dashboard
#   v1.1 - Trip Overview hero + Fleet/Drive split + gear inference + shifts KPI
#   v1.2 - mobile redesign (tabs, chip+sheet, 44pt targets, small-multiples)
#   v1.3 - shifting analysis section (regime classifier, per-pair targets)
#   v1.4 - shift timeline strip + clutch-deviation histogram + clutch sync KPI
#   v1.5 - 5 sub-tabs per view (Overview / Drivetrain / Engine / Fuel / Diag)
#          + interactive rev-match calculator in Drivetrain
#   v1.6 - 100-col discovery profile (coolant, MAP, AFR, lambda, fuel trims,
#          torque, MAF, catalyst temp, rail psi, baro) + 5 new tiles
#   v1.7 - rev-match calculator becomes hover-driven (mouse position picks
#          gear + speed, shows upshift and downshift simultaneously)
#   v1.8 - ingest 6 previously-dropped channels (intercooler temp, ambient,
#          IMU accels, fuel level) + 6 new tiles (intercooler efficiency,
#          cornering G traction circle, MPG timeline, rail psi, catalyst
#          temp, altitude profile)
#   v1.9 - shift detection plausibility filter + regime now uses physics-derived
#          landing RPM (idealLandingRpm) rather than the noisy 1-sec-later toRpm,
#          fixing apparent over-redline shifts caused by sample lag
#   v2.8 - telemetry_status.json: thin manifest (~1 KB) emitted next to the
#          16 MB telemetry_data.js, containing totals + per-drive {id, date,
#          distance, duration}. Lets scheduled "any new drives?" agents read
#          a tiny file instead of falling back to jq/python against the big
#          one. Also hardens verify(): parses telemetry_data.js end-to-end
#          (strip wrapper, json.loads, assert structure), cross-checks
#          len(drives) == totals.driveCount, and validates the manifest
#          round-trips. Closes the "we only checked size, not correctness"
#          gap from v2.6.
#   v2.7 - CLAUDE.md added at repo root documenting project conventions,
#          file architecture, the data pipeline, sub-tab structure, channel-
#          availability gating, the rev-match calculator, the test suite,
#          known wrinkles, and (crucially) the Read-tool limit= workaround
#          for index.html. No code changes — pure documentation layer.
#   v2.6 - externalize embedded JSON: data moves from inline const RAW in
#          index.html (14 MB) to telemetry_data.js loaded via <script src>.
#          index.html drops to ~180 KB and stays editable; downstream agents
#          no longer hit Read-tool truncation. refresh_dashboard.py writes
#          telemetry_data.js in step 2 instead of splicing into index.html.
#   v2.5 - outlier-driven correctness fixes:
#          - knock-event definition refined: requires throttle > 30% to exclude
#            post-WOT lift-off artifacts (ECU parking ignition during fuel cut
#            was inflating knock counts ~2x); per-drive knock rates drop from
#            ~6% to ~3% (genuine knock now visible)
#          - Cornering G filtered to spd > 3 mph (drops phone-jostle outliers
#            at idle that were dragging the chart axes)
#          - Peak Moments suppressed when value=0 or RPM<200 (engine-off drives)
#          - AFR vs throttle scatter y-axis capped at 22 (wideband saturation
#            at lambda~2.0 during cruise was compressing the readable area)
#   v2.4 - bug fix: Wideband lambda + Fuel trim stability tiles were misplaced
#          in Fleet Fuel & Air panel since v1.6 (label said 'this drive' but
#          they were in Fleet view). Moved to Drive Fuel & Air where they belong.
#   v2.3 - mobile default top-level view = Fleet (was Drive), so first open
#          on phone lands on Fleet > Drivetrain > rev-match. Fleet rev-match
#          card also moved to top of Fleet Drivetrain (matching Drive view).
#   v2.2 - rev-match calculator promoted to top of Drivetrain tab as the
#          feature card + Drivetrain set as the default sub-tab on first
#          visit + iOS touch hardening (webkit-touch-callout:none,
#          user-select:none, pointerevents fallback for iPadOS Safari)
#   v2.1 - economy-focused shift framework parallel to the performance one:
#          lug/economy/wasteful/high-rev classifier targeting BSFC sweet spot
#          (1500-2200 RPM landing) + green-band targets per gear pair +
#          economy hit rate / lugging count / worst-pair KPIs (Drivetrain).
#          Reveals daily-driver shift inefficiency invisible to the perf framework.
#   v2.0 - IA reorganization for sport/performance daily-driver telos:
#          - Engine Health Summary chips (Overview)
#          - Peak Moments tile (Performance, renamed from Engine & Boost)
#          - Knock Event Timeline + KPI strip (Performance)
#          - LTFT / coolant / knock-rate fleet trends (Health, renamed from Diag)
#          + per-drive summary: knockEvents, knockEventRate, avgLTFT,
#            avgWarmCoolant, peakTqMoment, peakPwrMoment, peakBoostMoment
VERSION = "v2.8"

# Middle-dot character used in the version badge. Kept as a constant so the
# regex and the replacement string use the same byte sequence.
MIDDOT = "·"


def step(n, total, msg):
    print(f"[{n}/{total}] {msg}")


def read_text(path):
    """Always read repo text files as UTF-8."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, content):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def rotate_old_csvs():
    """Pre-step: move CSVs older than ARCHIVE_AFTER_DAYS out of Dropbox.

    Non-fatal — if rotation fails we still continue with the rebuild.
    """
    if not os.path.exists(ARCHIVE):
        return
    print("[0/4] Rotating aging CSVs out of Dropbox ...")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, ARCHIVE], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print(f"  ! archive_old_csvs.py exit {r.returncode} (continuing anyway)")
        if r.stderr.strip():
            print("  stderr:", r.stderr.rstrip())


def run_prep() -> dict:
    step(1, 4, "Running prep_drives.py (scanning Dropbox + repo data) ...")
    if not os.path.exists(PREP):
        sys.exit(f"  ! prep_drives.py not found at {PREP}")
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, PREP], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    print(r.stdout.rstrip())
    if r.stderr.strip():
        print("  stderr:", r.stderr.rstrip())
    if r.returncode != 0:
        sys.exit(f"  ! prep failed (exit {r.returncode})")
    if not os.path.exists(JSON_PATH):
        sys.exit(f"  ! telemetry_data.json not produced at {JSON_PATH}")
    print(f"  done in {time.time()-t0:.1f}s")
    return json.loads(read_text(JSON_PATH))


def write_status_manifest(meta: dict):
    """v2.8: emit a tiny telemetry_status.json next to the 16 MB telemetry_data.js.

    Purpose: scheduled "any new drives?" agents (and other lightweight consumers)
    should read this — ~1 KB — instead of the big file. The big file remains the
    source of truth for the dashboard; the manifest is a derived index.
    """
    drives_lean = []
    for d in meta.get("drives", []):
        s = d.get("summary", {}) or {}
        drives_lean.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "date": d.get("date"),
            "startTime": d.get("startTime"),
            "distance": s.get("distance"),
            "duration": s.get("duration"),
        })
    t = meta.get("totals", {}) or {}
    manifest = {
        "version": VERSION,
        "buildDate": datetime.date.today().isoformat(),
        "totals": {
            "driveCount":  t.get("driveCount"),
            "totalDist":   t.get("totalDist"),
            "totalDur":    t.get("totalDur"),
            "totalFuel":   t.get("totalFuel"),
            "maxEverSpeed": t.get("maxEverSpeed"),
            "maxEverRpm":   t.get("maxEverRpm"),
            "vin":          t.get("vin"),
            "vehicle":      t.get("vehicle"),
        },
        "drives": drives_lean,
        # Note: we deliberately omit `historical` from the manifest.
        # In the big telemetry_data.json, `historical` is a fuller per-drive
        # summary that mirrors `drives` but adds peak/boost moments, samples,
        # buckets, etc. The manifest's `drives` field already carries the IDs +
        # dates that "any new drives?" agents need to diff against. Including
        # `historical` doubled the manifest size (32 KB -> 4 KB without it)
        # with no information the lightweight consumer can't get from drives[].
    }
    write_text(STATUS_PATH, json.dumps(manifest, indent=2) + "\n")


def inject(meta: dict):
    step(2, 4, "Writing telemetry_data.js + status manifest + bumping HTML badge ...")
    if not os.path.exists(HTML_PATH):
        sys.exit(f"  ! index.html not found at {HTML_PATH}")
    data = read_text(JSON_PATH)
    # v2.6: data is no longer inlined into index.html. We write it to
    # telemetry_data.js as `window.RAW = {...};` and index.html loads it via
    # <script src="telemetry_data.js"></script>. Result: index.html shrinks
    # from ~14 MB to ~180 KB and stays editable.
    write_text(JS_PATH, "window.RAW = " + data + ";\n")
    print(f"  Data JS size: {os.path.getsize(JS_PATH):,} bytes")
    # v2.8: thin manifest for lightweight consumers
    write_status_manifest(meta)
    print(f"  Status:       {os.path.getsize(STATUS_PATH):,} bytes "
          f"({len(meta.get('drives', []))} drives)")
    # Bump the version badge in index.html
    html = read_text(HTML_PATH)
    drives = meta["totals"]["driveCount"]
    today = datetime.date.today().isoformat()
    badge = f"{VERSION} {MIDDOT} build {today} {MIDDOT} {drives} drives"
    badge_re = (
        r"v[\w.]+\s" + re.escape(MIDDOT) +
        r"\s*build\s\d{4}-\d{2}-\d{2}(?:\s" + re.escape(MIDDOT) +
        r"\s*\d+\s*drives)?"
    )
    new_html, vn = re.subn(badge_re, badge, html, count=1)
    if vn != 1:
        print("  (note: version badge anchor not found - leaving as-is)")
    if new_html != html:
        write_text(HTML_PATH, new_html)
    print(f"  HTML size: {len(new_html):,} bytes")
    print(f"  Badge:     {badge}")


def verify(meta: dict):
    step(3, 4, "Verifying ...")
    html_sz = os.path.getsize(HTML_PATH)
    js_sz = os.path.getsize(JS_PATH) if os.path.exists(JS_PATH) else 0
    if html_sz < 50_000:
        sys.exit(f"  ! HTML suspiciously small ({html_sz} bytes)")
    if js_sz < 100_000:
        sys.exit(f"  ! telemetry_data.js suspiciously small ({js_sz} bytes)")
    html_full = read_text(HTML_PATH)
    if 'src="telemetry_data.js"' not in html_full:
        sys.exit("  ! index.html is missing <script src=\"telemetry_data.js\"> reference")

    # v2.8: end-to-end correctness, not just size. Parse the JS file we just
    # wrote, strip the wrapper, and confirm it round-trips to a structure that
    # matches the in-memory meta. Catches mid-write corruption that the size
    # threshold would miss (a torn 5 MB write is > 100 KB but still broken).
    js_raw = read_text(JS_PATH)
    prefix = "window.RAW = "
    suffix = ";\n"
    if not js_raw.startswith(prefix):
        sys.exit(f"  ! telemetry_data.js missing expected prefix '{prefix!r}'")
    if not js_raw.endswith(suffix):
        sys.exit(f"  ! telemetry_data.js missing expected suffix '{suffix!r}'")
    try:
        parsed = json.loads(js_raw[len(prefix):-len(suffix)])
    except json.JSONDecodeError as e:
        sys.exit(f"  ! telemetry_data.js failed to parse: {e}")
    n_drives_js = len(parsed.get("drives", []))
    n_drives_totals = parsed.get("totals", {}).get("driveCount")
    if n_drives_js != n_drives_totals:
        sys.exit(f"  ! drive count mismatch: drives[]={n_drives_js} vs "
                 f"totals.driveCount={n_drives_totals}")
    if n_drives_js != meta["totals"]["driveCount"]:
        sys.exit(f"  ! JS drive count ({n_drives_js}) != in-memory "
                 f"meta drive count ({meta['totals']['driveCount']})")

    # Verify the status manifest is also well-formed and consistent.
    if not os.path.exists(STATUS_PATH):
        sys.exit(f"  ! telemetry_status.json not produced at {STATUS_PATH}")
    try:
        status = json.loads(read_text(STATUS_PATH))
    except json.JSONDecodeError as e:
        sys.exit(f"  ! telemetry_status.json failed to parse: {e}")
    if len(status.get("drives", [])) != meta["totals"]["driveCount"]:
        sys.exit(f"  ! status manifest drive count mismatch: "
                 f"{len(status.get('drives', []))} vs {meta['totals']['driveCount']}")

    drives = meta["totals"]["driveCount"]
    dist = meta["totals"]["totalDist"]
    dur = meta["totals"]["totalDur"] / 3600
    print(f"  [OK] {drives} drives, {dist:.1f} mi, {dur:.1f} hr, "
          f"top speed {meta['totals']['maxEverSpeed']:.1f} mph")
    print(f"  [OK] JS round-trips, manifest valid, drive counts consistent")


def git(meta: dict, skip: bool):
    if skip:
        print("[4/4] git sync skipped (--no-git or NO_GIT=1).")
        return
    step(4, 4, "Git sync (commit + push) ...")
    if not os.path.isdir(os.path.join(HERE, ".git")):
        print("  (no .git here - run from inside the repo to enable git sync)")
        return
    drives = meta["totals"]["driveCount"]
    dist = meta["totals"]["totalDist"]
    today = datetime.date.today().isoformat()
    msg = f"Refresh {today}: {drives} drives, {dist:.1f} mi total"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    def run(cmd):
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            print(f"  ! {' '.join(cmd)} -> exit {r.returncode}\n"
                  f"  stdout: {r.stdout}\n  stderr: {r.stderr}")
        return r
    run(["git", "add", "-A"])
    r = run(["git", "commit", "-m", msg])
    if "nothing to commit" in r.stdout:
        print("  (no changes to commit)")
        return
    print(f"  committed: {msg}")
    r = run(["git", "push"])
    if r.returncode == 0:
        print("  pushed to origin")
    else:
        print("  push failed - fix and re-run 'git push' manually")


def run_tests():
    """Post-inject regression suite. Logs PASS/WARN/FAIL summary.
    Non-fatal: a test failure doesn't block the git push — it surfaces in
    refresh.log so we notice the next time we check.
    """
    if not os.path.exists(TESTS):
        return
    print("[3.5/4] Running dashboard regression suite ...")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    r = subprocess.run([sys.executable, TESTS], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    # Print just the summary block (everything after the "==========" divider).
    out = r.stdout or ""
    if "==========" in out:
        summary = out.split("==========", 1)[1].strip()
        print(f"  {summary}")
    else:
        # Fall back to last 5 lines on unexpected output
        print("\n".join("  " + line for line in out.splitlines()[-5:]))
    if r.returncode != 0:
        print(f"  ! tests had FAILures (exit {r.returncode}) — review refresh.log")


def main():
    skip_git = "--no-git" in sys.argv or os.environ.get("NO_GIT") == "1"
    rotate_old_csvs()
    meta = run_prep()
    inject(meta)
    verify(meta)
    run_tests()
    git(meta, skip_git)


if __name__ == "__main__":
    main()
