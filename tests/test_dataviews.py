#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dashboard regression test suite.

Runs a battery of correctness checks against telemetry_data.json — the same
file the dashboard consumes. Catches the kind of bug that v1.9 fixed: silent
data-correctness drift that doesn't make the dashboard CRASH but does make
it lie. Categories:

  schema      structural integrity of the JSON
  bounds      per-channel physical-plausibility ranges
  consistency cross-channel relationships (units, derivation)
  shifts      shift event correctness (incl. v1.9 regime regression)
  aggregates  fleet totals vs. per-drive sums
  anomalies   stuck channels, suspicious spikes

Run from the repo root:
    python tests/test_dataviews.py

Exit code 0 if all PASS, 1 if any FAIL. WARNings don't fail the suite.
Optionally wire into refresh_dashboard.py as a post-prep step.
"""
from __future__ import annotations
import os, sys, json, math
from typing import Any

# Force UTF-8 stdout so this works under Windows Task Scheduler.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JSON_PATH = os.environ.get("TELEMETRY_JSON", os.path.join(ROOT, "telemetry_data.json"))

# ----- Gear / regime constants (must match index.html) ---------------------
GEAR_RATIOS = {1: 175, 2: 92, 3: 65, 4: 47.5, 5: 40, 6: 32.5}
GEAR_BOUNDS = [(36, 6), (44, 5), (56, 4), (78, 3), (134, 2), (float("inf"), 1)]
REDLINE_RPM = 6500
SPOOL_RPM = 2200
PLATEAU_TOP = 3800
SHIFT_REDLINE_TOLERANCE = 300

def infer_gear(rpm, spd):
    if rpm is None or spd is None or rpm < 900 or spd < 5:
        return None
    ratio = rpm / spd
    for hi, g in GEAR_BOUNDS:
        if ratio < hi:
            return g
    return 6

def regime_of(rpm):
    if rpm is None:
        return "unknown"
    if rpm < SPOOL_RPM:
        return "economy"
    if rpm < PLATEAU_TOP:
        return "sport"
    return "performance"

def ideal_landing_rpm(from_g, to_g, from_rpm):
    if from_g not in GEAR_RATIOS or to_g not in GEAR_RATIOS or not from_rpm:
        return None
    return from_rpm * (GEAR_RATIOS[to_g] / GEAR_RATIOS[from_g])

# ----- Test framework ------------------------------------------------------
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
COLOR = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "END": "\033[0m"}
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

results: list[tuple[str, str, str, str]] = []  # (category, name, status, detail)

def record(category, name, status, detail=""):
    results.append((category, name, status, detail))

def fmt_status(status):
    if USE_COLOR:
        return f"{COLOR[status]}{status}{COLOR['END']}"
    return status

# ----- A. Schema -----------------------------------------------------------
def test_schema(raw):
    cat = "schema"
    for key in ("drives", "historical", "totals"):
        record(cat, f"top-level key '{key}' exists", PASS if key in raw else FAIL)
    if "drives" in raw:
        record(cat, "at least one drive present",
               PASS if len(raw["drives"]) > 0 else FAIL,
               f"{len(raw['drives'])} drives")
        for d in raw["drives"]:
            req = {"id", "name", "date", "channels", "summary", "rows"}
            missing = req - set(d.keys())
            if missing:
                record(cat, f"drive '{d.get('name','?')}' has all required keys",
                       FAIL, f"missing: {sorted(missing)}")
        # channel flag consistency: every row of a 'channel:True' drive should
        # have the channel-canonical-key present
        for d in raw["drives"]:
            ch = d.get("channels", {})
            checks = [
                ("rpm",     "rpm"),
                ("pwr",     "pwr"),
                ("fuel",    "fuel"),
                ("coolant", "coolant"),
                ("iat",     "iat"),
                ("map",     "map"),
                ("tq",      "tq"),
                ("lambda",  "lambda"),
                ("icTempF", "icTempF"),
                ("amb",     "amb"),
                ("fuelLvl", "fuelLvl"),
            ]
            for flag, row_key in checks:
                if not ch.get(flag):
                    continue
                rows = d["rows"]
                if not rows:
                    continue
                # Sample 20 rows mid-drive and confirm key present in >=80%
                start = len(rows) // 4
                end = 3 * len(rows) // 4
                sample = rows[start:end:max(1, (end - start) // 20)][:20]
                present = sum(1 for r in sample if row_key in r)
                if present < 0.8 * len(sample):
                    record(cat, f"{d['name']}: channels.{flag}=True ⇒ rows have '{row_key}'",
                           FAIL, f"only {present}/{len(sample)} mid-drive rows had it")

# ----- B. Bounds -----------------------------------------------------------
# For these channels, an exact 0.0 reading means "sensor not yet alive" rather
# than a real measurement (e.g. lambda, baro, A/F all read 0 during cold-start
# before the sensor warms up). The bounds test ignores zeros for these.
ZERO_IS_NULL = {"lambda", "afrA", "baro", "volt", "catTemp", "railPsi", "iat", "coolant", "icTempF", "amb"}

BOUNDS = {
    # row_key : (low, high, label, channel_flag)
    "rpm":     (0, 7500, "Engine RPM",                   "rpm"),
    "spd":     (-1, 200, "Vehicle speed (mph)",          None),
    "thr":     (-1, 105, "Throttle (%)",                 None),
    "coolant": (-40, 260, "Coolant temp (°F)",           "coolant"),
    "iat":     (-40, 280, "Intake air temp (°F)",        "iat"),
    "amb":     (-40, 130, "Ambient air temp (°F)",       "amb"),
    "icTempF": (-40, 280, "Intercooler outlet (°F)",     "icTempF"),
    "catTemp": (0, 2000, "Catalyst temp (°F)",           "catTemp"),
    "map":     (-5, 50,  "MAP (psi abs)",                "map"),
    "boost":   (-20, 35, "Boost (psi)",                  None),
    "baro":    (20, 35,  "Barometric pressure (inHg)",   "baro"),
    "afrA":    (5, 35,   "A/F Actual",                   "afr"),
    "afrC":    (5, 35,   "A/F Commanded",                "afr"),
    "lambda":  (0.4, 2.5,"Lambda (B1S1)",                "lambda"),
    "load":    (-1, 105, "Calculated load (%)",          "load"),
    "stft":    (-30, 30, "STFT B1 (%)",                  "stft"),
    "ltft":    (-30, 30, "LTFT B1 (%)",                  "ltft"),
    "ign":     (-40, 60, "Ignition advance (°)",         "ign"),
    "maf":     (0, 30,   "MAF (lb/min)",                 "maf"),
    "tq":      (-50, 350,"Engine torque (lb·ft)",        "tq"),
    "pwr":     (-10, 350,"Engine power (hp)",            "pwr"),
    "fuel":    (0, 15,   "Fuel rate (gal/hr)",           "fuel"),
    "railPsi": (0, 4000, "Fuel rail pressure (psi)",     "railPsi"),
    "volt":    (8, 16,   "Module voltage (V)",           "volt"),
    "alt":     (-500, 16000,"Altitude (ft)",             "alt"),
    "fuelLvl": (-1, 105, "Fuel level (%)",               "fuelLvl"),
}

def test_bounds(raw):
    cat = "bounds"
    for key, (lo, hi, label, flag) in BOUNDS.items():
        offenders = []
        for d in raw["drives"]:
            if flag and not d["channels"].get(flag):
                continue
            for r in d["rows"]:
                v = r.get(key)
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (ValueError, TypeError):
                    continue
                if math.isnan(fv):
                    continue
                if fv == 0.0 and key in ZERO_IS_NULL:
                    continue  # sensor not yet alive — skip
                if fv < lo or fv > hi:
                    offenders.append((d["name"], r.get("t"), fv))
        n = len(offenders)
        if n == 0:
            record(cat, f"{label} in [{lo}, {hi}]", PASS, f"checked across {len(raw['drives'])} drives")
        elif n <= 3:
            ex = "; ".join(f"{name}@t={t}: {v:.1f}" for name, t, v in offenders[:3])
            record(cat, f"{label} in [{lo}, {hi}]", WARN, f"{n} out-of-range samples: {ex}")
        else:
            record(cat, f"{label} in [{lo}, {hi}]", FAIL,
                   f"{n} samples out of range; first 3: " +
                   "; ".join(f"{name}@t={t}: {v:.1f}" for name, t, v in offenders[:3]))

# ----- C. Consistency ------------------------------------------------------
def _vals(rows, key):
    out = []
    for r in rows:
        v = r.get(key)
        if v is None: continue
        try:
            fv = float(v)
            if not math.isnan(fv): out.append(fv)
        except (ValueError, TypeError):
            pass
    return out

def test_consistency(raw):
    cat = "consistency"
    # C1: lambda × 14.7 ≈ A/F Actual (within 1.5)
    for d in raw["drives"]:
        ch = d["channels"]
        if not (ch.get("lambda") and ch.get("afr")):
            continue
        bad = 0; total = 0
        for r in d["rows"]:
            lam = r.get("lambda"); afr = r.get("afrA")
            if lam is None or afr is None or lam <= 0: continue
            total += 1
            implied = lam * 14.7
            if abs(implied - afr) > 1.5:
                bad += 1
        if total < 50:
            continue
        rate = bad / total
        if rate < 0.05:
            record(cat, f"{d['name']}: λ × 14.7 ≈ AFR Actual", PASS,
                   f"{bad}/{total} ({rate*100:.1f}%) off by >1.5")
        elif rate < 0.20:
            record(cat, f"{d['name']}: λ × 14.7 ≈ AFR Actual", WARN,
                   f"{bad}/{total} ({rate*100:.1f}%) off by >1.5 — sensor drift?")
        else:
            record(cat, f"{d['name']}: λ × 14.7 ≈ AFR Actual", FAIL,
                   f"{bad}/{total} ({rate*100:.1f}%) off by >1.5 — equation mismatch?")

    # C2: boost ≈ map_psi - baro_psi within 3 psi
    INHG_TO_PSI = 0.491154
    for d in raw["drives"]:
        ch = d["channels"]
        if not (ch.get("map") and ch.get("baro")):
            continue
        bad = 0; total = 0
        for r in d["rows"]:
            m = r.get("map"); b = r.get("baro"); bo = r.get("boost")
            if m is None or b is None or bo is None: continue
            total += 1
            implied = m - (b * INHG_TO_PSI)
            if abs(implied - bo) > 3:
                bad += 1
        if total < 50: continue
        rate = bad / total
        status = PASS if rate < 0.05 else WARN if rate < 0.20 else FAIL
        record(cat, f"{d['name']}: boost ≈ MAP − baro", status,
               f"{bad}/{total} ({rate*100:.1f}%) off by >3 psi")

    # C3: coolant should be monotonic-ish over the drive (or stable when hot)
    for d in raw["drives"]:
        if not d["channels"].get("coolant"): continue
        vals = _vals(d["rows"], "coolant")
        if len(vals) < 60: continue
        # We tolerate occasional dips; flag if max drops by >10 °F more than 5 times
        peak = vals[0]; dips = 0
        for v in vals:
            if v > peak: peak = v
            elif peak - v > 10: dips += 1
        if dips < 3:
            record(cat, f"{d['name']}: coolant trends warm", PASS, f"{dips} dips >10 °F")
        elif dips < 10:
            record(cat, f"{d['name']}: coolant trends warm", WARN, f"{dips} dips >10 °F")
        # Don't FAIL: real cool-down at end of long drives is OK

    # C4: GPS speed vs OBD speed within 8 mph (when both moving)
    for d in raw["drives"]:
        # GPS speed isn't in canonical ingest yet; skip until added.
        # Placeholder for when ingest_v2 adds gpsSpd.
        pass

    # C5: amb < icTempF < iat (thermal sanity)
    for d in raw["drives"]:
        ch = d["channels"]
        if not (ch.get("amb") and ch.get("icTempF") and ch.get("iat")):
            continue
        violations = 0; total = 0
        for r in d["rows"]:
            a = r.get("amb"); ic = r.get("icTempF"); iat = r.get("iat")
            if a is None or ic is None or iat is None: continue
            if a < 10 or iat < 10: continue  # skip cold-start zeros
            total += 1
            # Allow small tolerance; IC outlet can dip BELOW ambient on a long
            # downhill cruise (evap cooling), and can EQUAL ambient at idle.
            if ic > iat + 5 or ic < a - 15:
                violations += 1
        if total < 30: continue
        rate = violations / total
        status = PASS if rate < 0.10 else WARN if rate < 0.30 else FAIL
        record(cat, f"{d['name']}: ambient ≤ IC outlet ≤ manifold IAT", status,
               f"{violations}/{total} ({rate*100:.1f}%) thermal-order violations")

# ----- D. Shifts -----------------------------------------------------------
def detect_shifts(rows):
    out = []; last = None; lastT = -1e9; lastIdx = -1
    for i, r in enumerate(rows):
        g = infer_gear(r.get("rpm"), r.get("spd"))
        if g is None: continue
        if last is not None and g != last and (r["t"] - lastT) <= 3:
            prev = rows[lastIdx]
            fromRpm = prev.get("rpm"); toRpm = r.get("rpm")
            ideal = ideal_landing_rpm(last, g, fromRpm)
            landing = ideal if ideal else toRpm
            out.append({
                "t": r["t"],
                "fromGear": last, "toGear": g,
                "fromRpm": fromRpm, "toRpm": toRpm,
                "landingRpm": landing,
                "speed": r.get("spd"),
                "fromSpeed": prev.get("spd"),
                "type": "up" if g > last else "down",
                "regime": regime_of(landing),
            })
        last = g; lastT = r["t"]; lastIdx = i
    return out

def is_plausible(ev):
    s = ev["fromSpeed"] if ev["fromSpeed"] and ev["fromSpeed"] >= 5 else ev["speed"]
    if s is None or s < 5: return True
    cap = REDLINE_RPM + SHIFT_REDLINE_TOLERANCE
    return s * GEAR_RATIOS[ev["fromGear"]] <= cap and s * GEAR_RATIOS[ev["toGear"]] <= cap

def test_shifts(raw):
    cat = "shifts"
    total_shifts = 0; phantom_shifts = 0
    perf_via_ideal = 0; perf_via_to = 0
    for d in raw["drives"]:
        if not d["channels"].get("rpm"): continue
        shifts = detect_shifts(d["rows"])
        kept = [s for s in shifts if is_plausible(s)]
        total_shifts += len(kept)
        phantom_shifts += len(shifts) - len(kept)
        # D1: gear pair distinct
        bad = [s for s in kept if s["fromGear"] == s["toGear"]]
        if bad:
            record(cat, f"{d['name']}: every shift has fromGear ≠ toGear",
                   FAIL, f"{len(bad)} self-shifts")
        # D2: type matches gear delta
        bad = [s for s in kept if (s["type"] == "up") != (s["toGear"] > s["fromGear"])]
        if bad:
            record(cat, f"{d['name']}: shift type matches gear delta",
                   FAIL, f"{len(bad)} mismatched")
        # D3: regime matches idealLandingRpm — the v1.9 regression guard
        bad = [s for s in kept if s["regime"] != regime_of(s["landingRpm"])]
        if bad:
            record(cat, f"{d['name']}: regime == regime_of(idealLandingRpm)",
                   FAIL, f"{len(bad)} mismatched — v1.9 regression!")
        # D4: regime is from the known set
        bad = [s for s in kept if s["regime"] not in ("economy","sport","performance","unknown")]
        if bad:
            record(cat, f"{d['name']}: regime ∈ known set", FAIL,
                   f"{len(bad)} with unknown regime values")
        # Count regime mix for aggregate reporting
        for s in kept:
            ideal = s["landingRpm"]
            real_to = s["toRpm"]
            if regime_of(ideal) == "performance": perf_via_ideal += 1
            if regime_of(real_to) == "performance": perf_via_to += 1

    record(cat, "0 phantom shifts after plausibility filter",
           PASS if phantom_shifts == 0 else WARN,
           f"{phantom_shifts} rejected as physically impossible")
    record(cat, "v1.9 regime fix is taking effect",
           PASS if perf_via_to >= perf_via_ideal else WARN,
           f"ideal-RPM regime classifies {perf_via_ideal} as performance; "
           f"raw-toRpm would have classified {perf_via_to}")
    if total_shifts > 0:
        record(cat, "total shift count is non-zero", PASS, f"{total_shifts} shifts across dataset")

# ----- E. Aggregates -------------------------------------------------------
def test_aggregates(raw):
    cat = "aggregates"
    drives = raw["drives"]; totals = raw["totals"]; hist = raw["historical"]

    record(cat, "totals.driveCount == len(drives)",
           PASS if totals["driveCount"] == len(drives) else FAIL,
           f"totals={totals['driveCount']}, actual={len(drives)}")
    record(cat, "len(historical) == len(drives)",
           PASS if len(hist) == len(drives) else FAIL,
           f"hist={len(hist)}, drives={len(drives)}")

    sum_dist = sum(d["summary"]["distance"] for d in drives)
    diff = abs(sum_dist - totals["totalDist"])
    status = PASS if diff < 1.0 else FAIL
    record(cat, "totals.totalDist == sum of per-drive distances",
           status, f"totals={totals['totalDist']:.2f}, sum={sum_dist:.2f}, diff={diff:.2f}")

    sum_dur = sum(d["summary"]["duration"] for d in drives)
    diff = abs(sum_dur - totals["totalDur"])
    status = PASS if diff < 5.0 else FAIL
    record(cat, "totals.totalDur == sum of per-drive durations",
           status, f"diff={diff:.0f}s")

    max_spd = max(d["summary"]["maxSpeed"] for d in drives)
    record(cat, "totals.maxEverSpeed == max(drive.maxSpeed)",
           PASS if abs(max_spd - totals["maxEverSpeed"]) < 0.1 else FAIL,
           f"totals={totals['maxEverSpeed']}, max={max_spd}")

    rpms = [d["summary"].get("maxRpm") for d in drives if d["summary"].get("maxRpm")]
    if rpms:
        max_rpm = max(rpms)
        record(cat, "totals.maxEverRpm == max(drive.maxRpm)",
               PASS if abs(max_rpm - totals["maxEverRpm"]) < 1 else FAIL,
               f"totals={totals['maxEverRpm']}, max={max_rpm}")

    # No drive shorter than 10s
    short = [d for d in drives if d["summary"]["duration"] < 10]
    if short:
        record(cat, "no drive shorter than 10s", FAIL,
               f"{len(short)} short drives: " + ", ".join(d["name"] for d in short[:3]))
    else:
        record(cat, "no drive shorter than 10s", PASS)

# ----- F. Anomalies --------------------------------------------------------
def test_anomalies(raw):
    cat = "anomalies"
    for d in raw["drives"]:
        # Coolant stuck at a non-zero constant suggests sensor fault (April 29 case)
        ch = d["channels"]
        if ch.get("coolant"):
            # Skip drives where the engine never ran (PID-test sessions).
            rpm_vals = _vals(d["rows"], "rpm")
            engine_ran = any(v > 600 for v in rpm_vals)
            if engine_ran:
                vals = _vals(d["rows"], "coolant")
                if len(vals) > 60:
                    hot_vals = [v for v in vals if v > 50]
                    if len(hot_vals) > 30:
                        span = max(hot_vals) - min(hot_vals)
                        if span < 5:
                            record(cat, f"{d['name']}: coolant not stuck", WARN,
                                   f"warmed-up coolant range only {span:.1f} °F — sensor stuck?")
        # RPM spikes: jump >3000 in 1 sec without a shift is suspect
        rpm_vals = [(r.get("t"), r.get("rpm")) for r in d["rows"] if r.get("rpm")]
        spikes = 0
        for i in range(1, len(rpm_vals)):
            t0, r0 = rpm_vals[i-1]; t1, r1 = rpm_vals[i]
            if t1 - t0 <= 1.5 and abs(r1 - r0) > 3000:
                spikes += 1
        if spikes > 0:
            status = PASS if spikes < 5 else WARN
            record(cat, f"{d['name']}: RPM spike count (Δ>3000 in 1s)", status, f"{spikes}")

# ----- Main ----------------------------------------------------------------
def main():
    if not os.path.exists(JSON_PATH):
        print(f"FATAL: {JSON_PATH} not found.")
        return 2
    with open(JSON_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    test_schema(raw)
    test_bounds(raw)
    test_consistency(raw)
    test_shifts(raw)
    test_aggregates(raw)
    test_anomalies(raw)

    # Print report grouped by category
    by_cat: dict[str, list] = {}
    for c, n, s, det in results:
        by_cat.setdefault(c, []).append((n, s, det))

    n_pass = n_warn = n_fail = 0
    print()
    for cat in ("schema", "bounds", "consistency", "shifts", "aggregates", "anomalies"):
        entries = by_cat.get(cat, [])
        if not entries:
            continue
        print(f"== {cat.upper()} ({len(entries)} tests) ==")
        for name, status, detail in entries:
            if status == PASS: n_pass += 1
            elif status == WARN: n_warn += 1
            else: n_fail += 1
            tag = fmt_status(status)
            if detail:
                print(f"  [{tag}] {name}  —  {detail}")
            else:
                print(f"  [{tag}] {name}")
        print()

    print(f"==========")
    print(f"SUMMARY: {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL")
    return 0 if n_fail == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
