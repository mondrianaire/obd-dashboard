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
VERSION = "v2.4"

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


def inject(meta: dict):
    step(2, 4, "Injecting data into index.html ...")
    if not os.path.exists(HTML_PATH):
        sys.exit(f"  ! index.html not found at {HTML_PATH}")
    data = read_text(JSON_PATH)
    html = read_text(HTML_PATH)
    # Replace the embedded `const RAW = {...};`
    new_html, n = re.subn(r"const RAW = \{.*?\};",
                          "const RAW = " + data + ";",
                          html, count=1, flags=re.S)
    if n != 1:
        sys.exit("  ! could not find 'const RAW = {...};' anchor in index.html")
    # Bump the version badge. The badge contains middle-dot separators (U+00B7);
    # we re-write the whole substring instead of trying to splice in pieces.
    drives = meta["totals"]["driveCount"]
    today = datetime.date.today().isoformat()
    badge = f"{VERSION} {MIDDOT} build {today} {MIDDOT} {drives} drives"
    badge_re = (
        r"v[\w.]+\s" + re.escape(MIDDOT) +
        r"\s*build\s\d{4}-\d{2}-\d{2}(?:\s" + re.escape(MIDDOT) +
        r"\s*\d+\s*drives)?"
    )
    new_html, vn = re.subn(badge_re, badge, new_html, count=1)
    if vn != 1:
        print("  (note: version badge anchor not found - leaving as-is)")
    # Strip any trailing nulls from previous larger writes.
    end = new_html.rfind("</html>") + len("</html>") + 1
    new_html = new_html[:end].rstrip("\x00").rstrip() + "\n"
    write_text(HTML_PATH, new_html)
    print(f"  HTML size: {len(new_html):,} bytes")
    print(f"  Badge:     {badge}")


def verify(meta: dict):
    step(3, 4, "Verifying ...")
    sz = os.path.getsize(HTML_PATH)
    if sz < 100_000:
        sys.exit(f"  ! HTML suspiciously small ({sz} bytes)")
    head = read_text(HTML_PATH)[:20000]
    if "__DATA__" in head:
        sys.exit("  ! '__DATA__' placeholder still present")
    drives = meta["totals"]["driveCount"]
    dist = meta["totals"]["totalDist"]
    dur = meta["totals"]["totalDur"] / 3600
    print(f"  [OK] {drives} drives, {dist:.1f} mi, {dur:.1f} hr, "
          f"top speed {meta['totals']['maxEverSpeed']:.1f} mph")


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
