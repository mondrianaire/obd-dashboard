#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archive_old_csvs.py - rotate aging OBD CSVs out of Dropbox.

Why: the free Dropbox tier is small, and Car Scanner "all PIDs" logs run
70+ MB each. Once a drive is more than ARCHIVE_AFTER_DAYS old it doesn't
need to live in the synced folder anymore — prep_drives.py also scans the
local archive folder, so the dashboard keeps showing every drive.

What it does:
  - Looks at every CSVLog_*.csv in DROPBOX_DIR.
  - If its mtime is older than the cutoff (default 14 days), moves it to
    ARCHIVE_DIR. Stale duplicates in the archive are overwritten with the
    fresher Dropbox copy.
  - Prints a summary (files moved, MB freed).

Run standalone, or wire it into refresh_dashboard.py as a pre-step.
"""
from __future__ import annotations
import os, glob, shutil, datetime, sys

HERE        = os.path.dirname(os.path.abspath(__file__))
VIN         = '3VW5T7AU2GM058168'
DEFAULT_DBX = os.path.expanduser(f'~/Dropbox/Apps/OBD Fusion/CsvLogs/{VIN}')
DROPBOX_DIR = os.environ.get('DROPBOX_DIR', DEFAULT_DBX)
ARCHIVE_DIR = os.environ.get('ARCHIVE_DIR', os.path.join(HERE, 'data', 'obd_fusion'))
ARCHIVE_AFTER_DAYS = int(os.environ.get('ARCHIVE_AFTER_DAYS', '14'))

# Force UTF-8 stdout for Task Scheduler runs (cp1252 default breaks unicode).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    if not os.path.isdir(DROPBOX_DIR):
        print(f"[archive] Dropbox dir not found: {DROPBOX_DIR}")
        return 0
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    cutoff = datetime.datetime.now() - datetime.timedelta(days=ARCHIVE_AFTER_DAYS)
    cutoff_ts = cutoff.timestamp()

    candidates = sorted(glob.glob(os.path.join(DROPBOX_DIR, 'CSVLog_*.csv')))
    moved = 0
    freed_bytes = 0
    kept = 0

    for src in candidates:
        mtime = os.path.getmtime(src)
        if mtime >= cutoff_ts:
            kept += 1
            continue
        fn = os.path.basename(src)
        dst = os.path.join(ARCHIVE_DIR, fn)
        sz = os.path.getsize(src)
        try:
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
        except Exception as e:
            print(f"  ! failed to move {fn}: {e}")
            continue
        moved += 1
        freed_bytes += sz
        age_days = (datetime.datetime.now().timestamp() - mtime) / 86400
        print(f"  archived: {fn}  ({sz/1024/1024:.1f} MB, {age_days:.0f}d old)")

    if moved == 0:
        print(f"[archive] nothing older than {ARCHIVE_AFTER_DAYS}d in Dropbox "
              f"({kept} CSV(s) still recent)")
    else:
        print(f"[archive] moved {moved} file(s), freed {freed_bytes/1024/1024:.1f} MB. "
              f"{kept} CSV(s) still in Dropbox.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
