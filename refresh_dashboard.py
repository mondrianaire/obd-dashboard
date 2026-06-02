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
JSON_PATH = os.path.join(HERE, "telemetry_data.json")
HTML_PATH = os.path.join(HERE, "index.html")

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
    badge = f"v1 {MIDDOT} build {today} {MIDDOT} {drives} drives"
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


def main():
    print("=== Drive Telemetry Dashboard - refresh ===")
    skip_git = "--no-git" in sys.argv or os.environ.get("NO_GIT") == "1"
    meta = run_prep()
    inject(meta)
    verify(meta)
    git(meta, skip_git)
    print(f"\nDone. Open: {HTML_PATH}")


if __name__ == "__main__":
    main()
