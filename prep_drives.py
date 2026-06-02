"""Build the multi-drive telemetry_data.json from all source files.

Path conventions (overridable via environment variables):
  DROPBOX_DIR   - live OBD Fusion CSV folder (default: ~/Dropbox/Apps/OBD Fusion/CsvLogs/<VIN>/)
  REPO_DATA     - this repo's data/ folder, holds the snapshot of car_scanner/* and third_party/*
  OUT_JSON      - where to write telemetry_data.json (default: alongside this script)

Dedupe rule: if a Dropbox CSV matches a .dlg file by start timestamp,
the .dlg version wins (it has wideband AFR/MAP).
"""
import pandas as pd, numpy as np, json, math, sqlite3, os, glob, re

HERE        = os.path.dirname(os.path.abspath(__file__))
VIN         = '3VW5T7AU2GM058168'
DEFAULT_DBX = os.path.expanduser(f'~/Dropbox/Apps/OBD Fusion/CsvLogs/{VIN}')
DROPBOX_DIR = os.environ.get('DROPBOX_DIR', DEFAULT_DBX)
REPO_DATA   = os.environ.get('REPO_DATA',   os.path.join(HERE, 'data'))
DLG_DIR     = os.path.join(REPO_DATA, 'car_scanner')
THIRD_DIR   = os.path.join(REPO_DATA, 'third_party')
OUT         = os.environ.get('OUT_JSON',    os.path.join(HERE, 'telemetry_data.json'))

# unit conversions
KPH_TO_MPH = 0.621371
KPA_TO_PSI = 0.145038
MS2_TO_FTS2 = 3.28084
LHR_TO_GALHR = 0.264172
PS_TO_HP = 0.98632
INHG_TO_PSI = 0.491154
def c_to_f(c): return c * 9/5 + 32

# ---------------- helpers ----------------

def clean_time_csv(df, col='time', fmt='%H:%M:%S.%f', min_span=10.0):
    t = pd.to_datetime(df[col], format=fmt, errors='coerce')
    df = df.assign(sec=(t - t.iloc[0]).dt.total_seconds()).dropna(subset=['sec']).reset_index(drop=True)
    secs = df['sec'].values
    diffs = np.diff(secs, prepend=secs[0])
    breaks = (diffs > 30) | (diffs < 0)
    grp = np.cumsum(breaks)
    df['_g'] = grp
    for gid in sorted(df['_g'].unique()):
        sub = df[df['_g'] == gid]
        if sub['sec'].iloc[-1] - sub['sec'].iloc[0] >= min_span:
            sub = sub.drop(columns=['_g']).reset_index(drop=True)
            sub['sec'] = sub['sec'] - sub['sec'].iloc[0]
            return sub
    return df.iloc[0:0]


def bucket(rows, channels, name, date, startTime, source):
    df = pd.DataFrame(rows)
    for c in ['spd','rpm','thr','pwr','fuel','boost','acc','lat','lon',
              'alt','coolant','iat','volt','afrA','afrC','map']:
        if c not in df.columns:
            df[c] = np.nan
    df['dt'] = df['t'].diff().fillna(0).clip(0, 2.0)
    for c in ['spd','rpm','thr','pwr','fuel','boost','acc']:
        df[c] = pd.to_numeric(df[c], errors='coerce').ffill()
    df['fuelInc'] = (pd.to_numeric(df['fuel'], errors='coerce').fillna(0) * df['dt'] / 3600.0) if channels.get('fuel') else 0.0
    df['distInc'] = pd.to_numeric(df['spd'], errors='coerce').fillna(0) * df['dt'] / 3600.0
    df['idle']    = pd.to_numeric(df['spd'], errors='coerce').fillna(0) <= 0.1
    df['fuelIdle']= np.where(df['idle'], df['fuelInc'], 0.0)
    df['fuelMove']= np.where(df['idle'], 0.0, df['fuelInc'])
    df['moveDur'] = np.where(df['idle'], 0.0, df['dt'])
    df['bk'] = (df['t'] // 1.0).astype(int)

    def avg(s):
        s = pd.to_numeric(s, errors='coerce')
        return None if s.notna().sum() == 0 else round(float(s.mean()), 2)
    def last(s):
        s = pd.to_numeric(s, errors='coerce').dropna()
        return None if len(s) == 0 else float(s.iloc[-1])

    buckets = []
    for bk, g in df.groupby('bk'):
        row = {
            't': round(float(g['t'].iloc[-1]), 1),
            'spd':   avg(g['spd']) or 0,
            'thr':   avg(g['thr']) or 0,
            'boost': avg(g['boost']),
            'acc':   avg(g['acc']),
            'lat':   round(last(g['lat']),6) if last(g['lat']) not in (None, 0) else None,
            'lon':   round(last(g['lon']),6) if last(g['lon']) not in (None, 0) else None,
            'smax':  round(float(pd.to_numeric(g['spd'], errors='coerce').max() or 0), 1),
            'distInc': round(float(g['distInc'].sum()), 6),
            'fuelInc': round(float(g['fuelInc'].sum()), 7),
            'fuelIdle':round(float(g['fuelIdle'].sum()), 7),
            'fuelMove':round(float(g['fuelMove'].sum()), 7),
            'dur':     round(float(g['dt'].sum()), 3),
            'moveDur': round(float(g['moveDur'].sum()), 3),
        }
        if channels.get('rpm'):
            row['rpm']  = avg(g['rpm']) or 0
            row['rmax'] = round(float(pd.to_numeric(g['rpm'], errors='coerce').max() or 0), 0)
        if channels.get('pwr'):
            row['pwr']  = avg(g['pwr']) or 0
            row['pmax'] = round(float(pd.to_numeric(g['pwr'], errors='coerce').max() or 0), 1)
        if channels.get('fuel'):
            row['fuel'] = avg(g['fuel']) or 0
        if channels.get('alt'):     row['alt']     = avg(g['alt'])
        if channels.get('coolant'): row['coolant'] = avg(g['coolant'])
        if channels.get('iat'):     row['iat']     = avg(g['iat'])
        if channels.get('volt'):    row['volt']    = avg(g['volt'])
        if channels.get('afr'):
            row['afrA'] = avg(g['afrA'])
            row['afrC'] = avg(g['afrC'])
        if channels.get('map'):     row['map']     = avg(g['map'])
        buckets.append(row)

    real_dur = float(df['t'].iloc[-1] - df['t'].iloc[0])
    summary = {
        'duration': round(real_dur, 1),
        'distance': round(float(df['distInc'].sum()), 3),
        'maxSpeed': round(float(pd.to_numeric(df['spd'], errors='coerce').max()), 1),
        'avgMoveSpeed': round(float(df['distInc'].sum() / (df['moveDur'].sum() / 3600)) if df['moveDur'].sum() > 0 else 0, 1),
        'fuel': round(float(df['fuelInc'].sum()), 5) if channels.get('fuel') else None,
        'mpg':  round(float(df['distInc'].sum() / df['fuelInc'].sum()), 1) if channels.get('fuel') and df['fuelInc'].sum() > 0 else None,
        'maxRpm':  round(float(pd.to_numeric(df['rpm'], errors='coerce').max()), 0) if channels.get('rpm') else None,
        'peakPwr': round(float(pd.to_numeric(df['pwr'], errors='coerce').max()), 1) if channels.get('pwr') else None,
        'maxThr':  round(float(pd.to_numeric(df['thr'], errors='coerce').max()), 1),
        'samples': int(len(df)),
        'buckets': len(buckets),
    }
    drive_id = re.sub(r'[^a-z0-9_]', '_', name.lower())
    return {'id': drive_id, 'name': name, 'date': date, 'startTime': startTime,
            'source': source, 'channels': channels, 'summary': summary, 'rows': buckets}


# ---------------- OBD Fusion CSV (CsvLog_*) ----------------

OBD_FUSION_FULL_COLS = {
    'spd':   'Vehicle speed (MPH)',
    'rpm':   'Engine RPM (RPM)',
    'thr':   'Absolute throttle position (%)',
    'pwr':   'Engine Power (hp)',
    'fuel':  'Fuel Rate (gal/hr)',
    'boost': 'Boost (psi)',
    'acc':   'Acceleration (ft/s²)',
    'lat':   'Latitude (deg)',
    'lon':   'Longitude (deg)',
}

def parse_obd_fusion(path):
    df = pd.read_csv(path, skiprows=1, encoding='utf-8-sig')
    df.columns = [c.strip() for c in df.columns]
    has_full = 'Engine RPM (RPM)' in set(df.columns)
    rows = []
    for _, r in df.iterrows():
        row = {'t': r['Time (sec)']}
        for k, c in OBD_FUSION_FULL_COLS.items():
            row[k] = r.get(c)
        rows.append(row)
    channels = {
        'rpm':   has_full, 'pwr':   has_full, 'fuel':  has_full, 'boost': has_full,
        'alt':   False,'coolant':False,'iat':False,'volt':False,'afr':False,'map':False,
    }
    return rows, channels

def parse_obd_fusion_startTime(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        first = f.readline().strip()
    m = re.search(r'StartTime\s*=\s*(\d+)/(\d+)/(\d+)\s+(\d+):(\d+):([\d.]+)\s*(AM|PM)?', first)
    if not m: return None
    mo, d, y, h, mi, s, ap = m.groups()
    h = int(h); mi = int(mi); s = int(float(s))
    if ap == 'PM' and h != 12: h += 12
    if ap == 'AM' and h == 12: h = 0
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}", f"{h:02d}:{mi:02d}:{s:02d}"


# ---------------- DLG SQLite parser ----------------

def parse_dlg(path):
    con = sqlite3.connect(path)
    pids = pd.read_sql('SELECT UniqueId, PidName FROM PidMetadataEntry', con)
    data = pd.read_sql('SELECT UniqueId, Time, Value FROM PidDataEntry', con)
    con.close()
    t0 = int(data['Time'].min())
    data['t'] = (data['Time'].astype('int64') - t0) / 1e7
    name_by_uid = dict(zip(pids['UniqueId'], pids['PidName']))
    tmax = float(data['t'].max())
    grid = pd.DataFrame({'t': np.arange(0.0, math.ceil(tmax) + 1.0, 1.0)})
    for uid, name in name_by_uid.items():
        sub = data[data['UniqueId'] == uid][['t', 'Value']].sort_values('t')
        if len(sub) == 0: continue
        merged = pd.merge_asof(grid[['t']], sub, on='t', direction='backward')
        grid[name] = merged['Value']
    return grid

def dlg_to_rows(dlg_df):
    out = []
    for _, r in dlg_df.iterrows():
        spd_kph = r.get('VehicleSpeed')
        rpm     = r.get('EngineRPM')
        thr     = r.get('ThrottlePosition')
        pwr_ps  = r.get('Engine Power')
        fuel_l  = r.get('FuelRate')
        boost_k = r.get('Boost')
        acc_ms2 = r.get('Acceleration')
        iat_c   = r.get('IntakeAirTemperature')
        map_k   = r.get('IntakeManifoldPressure')
        out.append({
            't': float(r['t']),
            'spd':   (spd_kph * KPH_TO_MPH) if pd.notna(spd_kph) else None,
            'rpm':   rpm, 'thr': thr,
            'pwr':   (pwr_ps * PS_TO_HP) if pd.notna(pwr_ps) else None,
            'fuel':  (fuel_l * LHR_TO_GALHR) if pd.notna(fuel_l) else None,
            'boost': (boost_k * KPA_TO_PSI) if pd.notna(boost_k) else None,
            'acc':   (acc_ms2 * MS2_TO_FTS2) if pd.notna(acc_ms2) else None,
            'lat':   r.get('Latitude'), 'lon': r.get('Longitude'),
            'iat':   c_to_f(iat_c) if pd.notna(iat_c) else None,
            'map':   (map_k * KPA_TO_PSI) if pd.notna(map_k) else None,
            'afrA':  r.get('A/F Actual'), 'afrC': r.get('A/F Commanded'),
        })
    return out


# ---------------- Build the dataset ----------------

DRIVES = []

# 1) DLG files from repo's data/car_scanner/
dlg_files = sorted(glob.glob(os.path.join(DLG_DIR, '*.dlg')))
dlg_taken_keys = set()
for path in dlg_files:
    fn = os.path.basename(path)
    m = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{2})(\d{2})(\d{2})', fn)
    if m:
        date = m.group(1); st = f"{m.group(2)}:{m.group(3)}:{m.group(4)}"
        name = f"Car Scanner {date} {m.group(2)}:{m.group(3)}"
    else:
        date = '2026-01-01'; st = '00:00:00'; name = fn
    dlg = parse_dlg(path)
    rows = dlg_to_rows(dlg)
    d = bucket(rows,
        {'rpm':True,'pwr':True,'fuel':True,'alt':False,'coolant':False,
         'iat':True,'volt':False,'afr':True,'map':True},
        name, date, st, fn)
    DRIVES.append(d)
    dlg_taken_keys.add((date, st[:5]))

# 2) OBD Fusion CSV bulk import — skip if .dlg already covers that timestamp
csv_paths = sorted(glob.glob(os.path.join(DROPBOX_DIR, 'CSVLog_*.csv')))
print(f"Found {len(csv_paths)} OBD Fusion CSVs in {DROPBOX_DIR}.")
for path in csv_paths:
    parsed = parse_obd_fusion_startTime(path)
    if not parsed:
        print(f"  skip {os.path.basename(path)}: no parseable StartTime"); continue
    date, st = parsed
    key = (date, st[:5])
    if key in dlg_taken_keys:
        print(f"  skip {os.path.basename(path)}: .dlg covers {date} {st}"); continue
    rows, channels = parse_obd_fusion(path)
    if not rows: continue
    name = f"OBD Fusion {date} {st[:5]}"
    try:
        d = bucket(rows, channels, name, date, st, os.path.basename(path))
    except Exception as e:
        print(f"  FAIL {os.path.basename(path)}: {e}"); continue
    if d['summary']['duration'] < 10:
        print(f"  skip {os.path.basename(path)}: too short ({d['summary']['duration']}s)"); continue
    DRIVES.append(d)
    print(f"  + {os.path.basename(path)} -> {date} {st}  {d['summary']['distance']:.1f}mi  {d['summary']['duration']:.0f}s")

# 3) Third-party CSVs (April 29 rich + May 5 stripped) from repo's data/third_party/
path = os.path.join(THIRD_DIR, '2026-04-29 19-56-26.csv')
if os.path.exists(path):
    df = pd.read_csv(path); df = clean_time_csv(df); rows = []
    for _, r in df.iterrows():
        accg = pd.to_numeric(r.get('Vehicle acceleration (g)'), errors='coerce')
        rows.append({
            't': float(r['sec']),
            'spd':  pd.to_numeric(r.get('Vehicle speed (mph)'), errors='coerce'),
            'rpm':  pd.to_numeric(r.get('Engine RPM (rpm)'), errors='coerce'),
            'thr':  pd.to_numeric(r.get('Throttle position (%)'), errors='coerce'),
            'pwr':  pd.to_numeric(r.get('Instant engine power (based on fuel consumption) (hp)'), errors='coerce'),
            'fuel': pd.to_numeric(r.get('Calculated instant fuel rate (gal./h)'), errors='coerce'),
            'boost':pd.to_numeric(r.get('Calculated boost (psi)'), errors='coerce'),
            'acc':  (accg * 32.174) if pd.notna(accg) else None,
            'lat':  pd.to_numeric(r.get('Latitude'), errors='coerce'),
            'lon':  pd.to_numeric(r.get('Longtitude'), errors='coerce'),
            'alt':  pd.to_numeric(r.get('Altitude (GPS) (feet)'), errors='coerce'),
            'coolant': pd.to_numeric(r.get('Engine coolant temperature (℉)'), errors='coerce'),
            'iat':     pd.to_numeric(r.get('Intake air temperature (℉)'), errors='coerce'),
            'volt':    pd.to_numeric(r.get('OBD Module Voltage (V)'), errors='coerce'),
            'map':     pd.to_numeric(r.get('Intake manifold absolute pressure (psi)'), errors='coerce'),
        })
    DRIVES.append(bucket(rows,
        {'rpm':True,'pwr':True,'fuel':True,'alt':True,'coolant':True,'iat':True,'volt':True,'afr':False,'map':True},
        'April 29 drive', '2026-04-29', '19:56:43', os.path.basename(path)))

path = os.path.join(THIRD_DIR, '2026-05-05 16-38-36.csv')
if os.path.exists(path):
    df = pd.read_csv(path); df = clean_time_csv(df); rows = []
    for _, r in df.iterrows():
        accg = pd.to_numeric(r.get('Vehicle acceleration (g)'), errors='coerce')
        rows.append({
            't': float(r['sec']),
            'spd':   pd.to_numeric(r.get('Vehicle speed (mph)'), errors='coerce'),
            'thr':   pd.to_numeric(r.get('Throttle position (%)'), errors='coerce'),
            'boost': pd.to_numeric(r.get('Calculated boost (psi)'), errors='coerce'),
            'acc':   (accg * 32.174) if pd.notna(accg) else None,
            'lat':   pd.to_numeric(r.get('Latitude'), errors='coerce'),
            'lon':   pd.to_numeric(r.get('Longtitude'), errors='coerce'),
            'alt':   pd.to_numeric(r.get('Altitude (GPS) (feet)'), errors='coerce'),
            'map':   pd.to_numeric(r.get('Intake manifold absolute pressure (psi)'), errors='coerce'),
        })
    DRIVES.append(bucket(rows,
        {'rpm':False,'pwr':False,'fuel':False,'alt':True,'coolant':False,'iat':False,'volt':False,'afr':False,'map':True},
        'May 5 drive', '2026-05-05', '16:38:36', os.path.basename(path)))

# ---------------- assemble JSON ----------------

DRIVES.sort(key=lambda d: (d['date'], d['startTime']))
seen = {}
for d in DRIVES:
    base = d['id']; i = 0
    while d['id'] in seen:
        i += 1; d['id'] = f"{base}_{i}"
    seen[d['id']] = True

hist = [{'id': d['id'], 'name': d['name'], 'date': d['date'], **d['summary']} for d in DRIVES]
totals = {
    'driveCount':   len(DRIVES),
    'totalDist':    round(sum(h['distance'] for h in hist), 2),
    'totalDur':     round(sum(h['duration'] for h in hist), 0),
    'totalFuel':    round(sum((h['fuel'] or 0) for h in hist), 3),
    'maxEverSpeed': max(h['maxSpeed'] for h in hist),
    'maxEverRpm':   max(((h['maxRpm'] or 0) for h in hist)),
    'vehicle':      '2016 VW Golf GTI (2.0T EA888)',
    'vin':          VIN,
}

out = {'drives': DRIVES, 'historical': hist, 'totals': totals}
with open(OUT, 'w') as f:
    json.dump(out, f, separators=(',', ':'),
              default=lambda x: None if (isinstance(x, float) and math.isnan(x)) else x)

print()
print('=== final drives ===')
for d in DRIVES:
    s = d['summary']
    fmt = f"{d['date']} {d['startTime'][:5]}  {d['name']:32s}  dur={s['duration']:>5.0f}s  dist={s['distance']:>6.2f}mi  top={s['maxSpeed']:>5.1f}mph"
    if s['mpg']: fmt += f"  mpg={s['mpg']:.1f}"
    if s['peakPwr']: fmt += f"  hp={s['peakPwr']:.0f}"
    print('  '+fmt)
print()
print('totals:', totals)
print('json bytes:', os.path.getsize(OUT))
