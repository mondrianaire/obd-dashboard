#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archive_old_csvs.py - materialize + rotate aging OBD CSVs out of Dropbox.

Why: the free Dropbox tier is small, and Car Scanner "all PIDs" logs run
70+ MB each. Once a drive is more than ARCHIVE_AFTER_DAYS old it doesn't
need to live in the synced folder anymore — prep_drives.py also scans the
local archive folder, so the dashboard keeps showing every drive.

What it does (in order):
  1. MATERIALIZE: walks Dropbox dir, finds any CSVLog_*.csv that Dropbox
     Smart Sync is holding as online-only (cloud placeholder, not downloaded
     to disk), and forces it local by reading one byte. This means
     prep_drives.py won't ever block mid-read waiting for the cloud.
  2. ARCHIVE: any CSV older than ARCHIVE_AFTER_DAYS (default 14) is moved
     out of Dropbox into the local archive at data/obd_fusion/.
  3. Prints a summary (files materialized, files archived, MB freed).

Run standalone, or wire it into refresh_dashboard.py as a pre-step.
"""
from __future__ import annotations
import os, glob, shutil, datetime, sys, time

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


# Windows file attribute bits we care about for Dropbox / OneDrive placeholders.
# Reference: https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants
FILE_ATTRIBUTE_OFFLINE             = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_RECALL_ON_OPEN      = 0x00040000


def _get_windows_attrs(path):
    """Return the Windows file-attribute bitfield for path, or None if unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs == 0xFFFFFFFF:
            return None
        return attrs
    except Exception:
        return None


def _is_placeholder(path):
    """True if this file is a Dropbox/OneDrive cloud placeholder (not downloaded)."""
    a = _get_windows_attrs(path)
    if a is None:
        return False
    return bool(a & (FILE_ATTRIBUTE_OFFLINE
                     | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                     | FILE_ATTRIBUTE_RECALL_ON_OPEN))


def materialize_offline_files(directory):
    """Force-download any Dropbox online-only CSVs in `directory` so subsequent
    reads don't block on the network. Reads exactly one byte from each
    placeholder file; the cloud provider streams the rest down in response.

    On non-Windows systems this is a no-op (no placeholder concept).
    """
    if sys.platform != "win32":
        print(f"[materialize] not on Windows — skipping (sys.platform={sys.platform})")
        return
    candidates = sorted(glob.glob(os.path.join(directory, 'CSVLog_*.csv')))
    if not candidates:
        print(f"[materialize] no CSVLog_*.csv in {directory}")
        return
    placeholders = [p for p in candidates if _is_placeholder(p)]
    if not placeholders:
        print(f"[materialize] all {len(candidates)} CSV(s) already local in Dropbox dir")
        return
    print(f"[materialize] {len(placeholders)} of {len(candidates)} CSV(s) are online-only; "
          f"pulling down...")
    pulled = 0
    pulled_mb = 0.0
    for path in placeholders:
        fn = os.path.basename(path)
        sz_mb = os.path.getsize(path) / 1024 / 1024
        t0 = time.time()
        try:
            with open(path, 'rb') as fh:
                _ = fh.read(1)  # triggers Dropbox to materialize the rest
            # Verify it's actually materialized now (placeholder bit cleared)
            if _is_placeholder(path):
                # Some clients need a few more bytes pulled to clear the flag;
                # read the whole file in a streaming loop with a short timeout.
                with open(path, 'rb') as fh:
                    while fh.read(1024 * 1024):
                        pass
        except OSError as e:
            print(f"  ! {fn}: read failed: {e}")
            continue
        dt = time.time() - t0
        pulled += 1
        pulled_mb += sz_mb
        print(f"  pulled: {fn}  ({sz_mb:.1f} MB in {dt:.1f}s)")
    print(f"[materialize] pulled {pulled} file(s), {pulled_mb:.1f} MB total.")


def main():
    if not os.path.isdir(DROPBOX_DIR):
        print(f"[archive] Dropbox dir not found: {DROPBOX_DIR}")
        return 0
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    # Step 1: force-download any online-only CSVs so prep_drives.py never
    # blocks mid-read waiting for Dropbox to stream a placeholder file.
    materialize_offline_files(DROPBOX_DIR)


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
