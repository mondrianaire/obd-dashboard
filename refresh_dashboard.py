#!/usr/bin/env python3
"""refresh_dashboard.py  ·  one-shot refresh + git sync.

Steps:
  1. Run prep_drives.py to scan the Dropbox folder (live OBD Fusion CSVs)
     plus the repo's data/ snapshot, producing telemetry_data.json.
  2. Inject that JSON into index.html (replacing the previous embedded data).
  3. Bump the version badge with today's date and the drive count.
  4. (optional) git add / commit / push so local + remote stay in sync.

Skip git with --no-git or by setting NO_GIT=1.
"""
from __future__ import annotations
import subprocess, sys, json, re, os, datetime, time

HERE = os.path.dirname(os.path.abspath(__file__))
PREP = os.path.join(HERE, "prep_drives.py")
JSON_PATH = os.path.join(HERE, "telemetry_data.json")
HTML_PATH = os.path.join(HERE, "index.html")


def step(n, total, msg):
    print(f"[{n}/{total}] {msg}")


def run_prep() -> dict:
    step(1, 4, "Running prep_drives.py (scanning Dropbox + repo data) …")
    if not os.path.exists(PREP):
        sys.exit(f"  ! prep_drives.py not found at {PREP}")
    t0 = time.time()
    r = subprocess.run([sys.executable, PREP], capture_output=True, text=True)
    print(r.stdout.rstrip())
    if r.stderr.strip():
        print("  stderr:", r.stderr.rstrip())
    if r.returncode != 0:
        sys.exit(f"  ! prep failed (exit {r.returncode})")
    if not os.path.exists(JSON_PATH):
        sys.exit(f"  ! telemetry_data.json not produced at {JSON_PATH}")
    print(f"  done in {time.time()-t0:.1f}s")
    return json.loads(open(JSON_PATH).read())


def inject(meta: dict):
    step(2, 4, "Injecting data into index.html …")
    if not os.path.exists(HTML_PATH):
        sys.exit(f"  ! index.html not found at {HTML_PATH}")
    data = open(JSON_PATH).read()
    html = open(HTML_PATH).read()
    new_html, n = re.subn(r"const RAW = \{.*?\};",
                          "const RAW = " + data + ";",
                          html, count=1, flags=re.S)
    if n != 1:
        sys.exit("  ! could not find 'const RAW = {...};' anchor in index.html")
    drives = meta["totals"]["driveCount"]
    today = datetime.date.today().isoformat()
    badge = f"v1 · build {today} · {drives} drives"
    new_html, vn = re.subn(
        r"v[\w.]+ · build \d{4}-\d{2}-\d{2}(?: · \d+ drives)?",
        badge, new_html, count=1)
    if vn != 1:
        print("  (note: version badge anchor not found — leaving as-is)")
    end = new_html.rfind("</html>") + len("</html>") + 1
    new_html = new_html[:end].rstrip("\x00").rstrip() + "\n"
    open(HTML_PATH, "w").write(new_html)
    print(f"  HTML size: {len(new_html):,} bytes")
    print(f"  Badge:     {badge}")


def verify(meta: dict):
    step(3, 4, "Verifying …")
    sz = os.path.getsize(HTML_PATH)
    if sz < 100_000:
        sys.exit(f"  ! HTML suspiciously small ({sz} bytes)")
    with open(HTML_PATH, "r") as f:
        head = f.read(20000)
    if "__DATA__" in head:
        sys.exit("  ! '__DATA__' placeholder still present")
    drives = meta["totals"]["driveCount"]
    dist = meta["totals"]["totalDist"]
    dur = meta["totals"]["totalDur"] / 3600
    print(f"  ✓ {drives} drives · {dist:.1f} mi · {dur:.1f} hr · "
          f"top speed {meta['totals']['maxEverSpeed']:.1f} mph")


def git(meta: dict, skip: bool):
    if skip:
        print("[4/4] git sync skipped (--no-git or NO_GIT=1).")
        return
    step(4, 4, "Git sync (commit + push) …")
    if not os.path.isdir(os.path.join(HERE, ".git")):
        print("  (no .git here — run from inside the repo to enable git sync)")
        return
    drives = meta["totals"]["driveCount"]
    dist = meta["totals"]["totalDist"]
    today = datetime.date.today().isoformat()
    msg = f"Refresh {today}: {drives} drives, {dist:.1f} mi total"
    def run(cmd):
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            print(f"  ! {' '.join(cmd)} -> exit {r.returncode}\n  stdout: {r.stdout}\n  stderr: {r.stderr}")
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
        print("  push failed — fix and re-run `git push` manually")


def main():
    print("=== Drive Telemetry Dashboard · refresh ===")
    skip_git = "--no-git" in sys.argv or os.environ.get("NO_GIT") == "1"
    meta = run_prep()
    inject(meta)
    verify(meta)
    git(meta, skip_git)
    print(f"\nDone. Open: {HTML_PATH}")


if __name__ == "__main__":
    main()
