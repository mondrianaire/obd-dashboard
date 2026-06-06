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
# Local cold-storage for OBD Fusion CSVs moved out of Dropbox to save quota.
# Scanned in addition to DROPBOX_DIR; Dropbox wins on duplicate (date, hh:mm).
ARCHIVE_DIR = os.environ.get('ARCHIVE_DIR', os.path.join(REPO_DATA, 'obd_fusion'))
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
    # Canonical column list. Anything missing in `rows` is created as NaN so the
    # rest of the pipeline doesn't trip on KeyError.
    for c in ['spd','rpm','thr','pwr','fuel','boost','acc','lat','lon',
              'alt','coolant','iat','volt','afrA','afrC','map',
              # v1.6 channels (100-col OBD Fusion profile):
              'load','stft','ltft','ign','maf','tq','lambda','catTemp','railPsi','baro',
              # v1.8 channels (intercooler temp + ambient + IMU + fuel level):
              'icTempF','amb','ax','ay','az','fuelLvl']:
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

    # Vectorized bucket aggregation. Groupby('bk') once, then build named
    # mean/sum/max columns in a single pass instead of looping in Python.
    for c in ['spd','thr','boost','acc','rpm','pwr','fuel','alt','coolant','iat','volt',
              'afrA','afrC','map','load','stft','ltft','ign','maf','tq','lambda',
              'catTemp','railPsi','baro','lat','lon']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    g = df.groupby('bk', sort=True)
    aggs = {
        't_last':  ('t',       'last'),
        'spd_mean':('spd',     'mean'),
        'spd_max': ('spd',     'max'),
        'thr_mean':('thr',     'mean'),
        'boost_m': ('boost',   'mean'),
        'acc_m':   ('acc',     'mean'),
        'lat_l':   ('lat',     'last'),
        'lon_l':   ('lon',     'last'),
        'distInc': ('distInc', 'sum'),
        'fuelInc': ('fuelInc', 'sum'),
        'fuelIdle':('fuelIdle','sum'),
        'fuelMove':('fuelMove','sum'),
        'dur':     ('dt',      'sum'),
        'moveDur': ('moveDur', 'sum'),
    }
    for c in ['rpm','pwr','fuel','alt','coolant','iat','volt','afrA','afrC','map',
              'load','stft','ltft','ign','maf','tq','lambda','catTemp','railPsi','baro',
              # v1.8 additions:
              'amb','fuelLvl','ax','ay','az','icTempF']:
        if c in df.columns:
            aggs[f'{c}_m'] = (c, 'mean')
    if 'rpm' in df.columns: aggs['rpm_max'] = ('rpm', 'max')
    if 'pwr' in df.columns: aggs['pwr_max'] = ('pwr', 'max')
    if 'tq'  in df.columns: aggs['tq_max']  = ('tq',  'max')
    agg = g.agg(**aggs).reset_index(drop=False)

    def _rnd(v, d=2):
        if pd.isna(v): return None
        return round(float(v), d)
    def _round_or_none(v, d):
        return _rnd(v, d)

    buckets = []
    for _, r in agg.iterrows():
        row = {
            't':       round(float(r['t_last']), 1),
            'spd':     _rnd(r['spd_mean'], 2) or 0,
            'thr':     _rnd(r['thr_mean'], 2) or 0,
            'boost':   _rnd(r['boost_m'], 2),
            'acc':     _rnd(r['acc_m'], 2),
            'lat':     round(float(r['lat_l']),6) if (pd.notna(r['lat_l']) and float(r['lat_l']) != 0) else None,
            'lon':     round(float(r['lon_l']),6) if (pd.notna(r['lon_l']) and float(r['lon_l']) != 0) else None,
            'smax':    round(float(r['spd_max'] or 0), 1) if pd.notna(r['spd_max']) else 0,
            'distInc': round(float(r['distInc']), 6),
            'fuelInc': round(float(r['fuelInc']), 7),
            'fuelIdle':round(float(r['fuelIdle']), 7),
            'fuelMove':round(float(r['fuelMove']), 7),
            'dur':     round(float(r['dur']), 3),
            'moveDur': round(float(r['moveDur']), 3),
        }
        if channels.get('rpm'):
            row['rpm']  = _rnd(r['rpm_m'], 2) or 0
            row['rmax'] = round(float(r['rpm_max'] or 0), 0) if pd.notna(r['rpm_max']) else 0
        if channels.get('pwr'):
            row['pwr']  = _rnd(r['pwr_m'], 2) or 0
            row['pmax'] = round(float(r['pwr_max'] or 0), 1) if pd.notna(r['pwr_max']) else 0
        if channels.get('fuel'):
            row['fuel'] = _rnd(r['fuel_m'], 2) or 0
        if channels.get('alt'):     row['alt']     = _rnd(r['alt_m'])
        if channels.get('coolant'): row['coolant'] = _rnd(r['coolant_m'])
        if channels.get('iat'):     row['iat']     = _rnd(r['iat_m'])
        if channels.get('volt'):    row['volt']    = _rnd(r['volt_m'])
        if channels.get('afr'):
            row['afrA'] = _rnd(r['afrA_m'])
            row['afrC'] = _rnd(r['afrC_m'])
        if channels.get('map'):     row['map']     = _rnd(r['map_m'])
        if channels.get('load'):    row['load']    = _rnd(r['load_m'])
        if channels.get('stft'):    row['stft']    = _rnd(r['stft_m'])
        if channels.get('ltft'):    row['ltft']    = _rnd(r['ltft_m'])
        if channels.get('ign'):     row['ign']     = _rnd(r['ign_m'])
        if channels.get('maf'):     row['maf']     = _rnd(r['maf_m'], 3)
        if channels.get('tq'):
            row['tq']  = _rnd(r['tq_m'], 1)
            row['tmax']= round(float(r['tq_max'] or 0), 1) if pd.notna(r['tq_max']) else 0
        if channels.get('lambda'):  row['lambda']  = _rnd(r['lambda_m'], 3)
        if channels.get('catTemp'): row['catTemp'] = _rnd(r['catTemp_m'], 1)
        if channels.get('railPsi'): row['railPsi'] = _rnd(r['railPsi_m'], 0)
        if channels.get('baro'):    row['baro']    = _rnd(r['baro_m'], 2)
        # v1.8 channels:
        if channels.get('amb'):     row['amb']     = _rnd(r['amb_m'], 1)
        if channels.get('fuelLvl'): row['fuelLvl'] = _rnd(r['fuelLvl_m'], 1)
        if channels.get('imu'):
            row['ax'] = _rnd(r['ax_m'], 2)
            row['ay'] = _rnd(r['ay_m'], 2)
            row['az'] = _rnd(r['az_m'], 2)
        if channels.get('icTempF'): row['icTempF'] = _rnd(r['icTempF_m'], 1)
        buckets.append(row)

    real_dur = float(df['t'].iloc[-1] - df['t'].iloc[0])
    def maxof(col):
        s = pd.to_numeric(df[col], errors='coerce').dropna()
        return round(float(s.max()), 1) if len(s) else None
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
        # v1.6 summary peaks (None on drives that don't have the channel):
        'peakTq':       maxof('tq')      if channels.get('tq')      else None,
        'peakBoost':    maxof('boost')   if channels.get('boost')   else None,
        'maxMap':       maxof('map')     if channels.get('map')     else None,
        'maxCoolant':   maxof('coolant') if channels.get('coolant') else None,
        'maxIat':       maxof('iat')     if channels.get('iat')     else None,
        'maxCatTemp':   maxof('catTemp') if channels.get('catTemp') else None,
        # v1.8 summary peaks:
        'maxIcTempF':   maxof('icTempF') if channels.get('icTempF') else None,
        'maxAmb':       maxof('amb')     if channels.get('amb')     else None,
        'minFuelLvl':   round(float(pd.to_numeric(df['fuelLvl'], errors='coerce').dropna().min()), 1) if channels.get('fuelLvl') and pd.to_numeric(df['fuelLvl'], errors='coerce').notna().any() else None,
        'samples': int(len(df)),
        'buckets': len(buckets),
    }
    # v2.0 — health + peak metrics. Computed from the 1Hz buckets (not raw df)
    # since the buckets are what the dashboard sees and what we want to surface.
    bdf = pd.DataFrame(buckets)
    def _bk_max_with_t(col):
        """(peak_value, t_of_peak) or (None, None) — for surfacing 'best moment' tiles."""
        if col not in bdf.columns: return (None, None)
        s = pd.to_numeric(bdf[col], errors='coerce')
        if not s.notna().any(): return (None, None)
        idx = s.idxmax()
        return (round(float(s.iloc[idx]), 1), round(float(bdf['t'].iloc[idx]), 0))
    # Best torque/power/boost moments — what speed, what RPM, what gear inferred,
    # the t of the moment so the UI can jump to it.
    def _peak_context(col):
        v, t = _bk_max_with_t(col)
        if v is None: return None
        s = pd.to_numeric(bdf[col], errors='coerce')
        if not s.notna().any(): return None
        idx_label = s.idxmax()
        # idx_label is a pandas index label; use it with .loc, then float() everything.
        ctx = {'value': float(v), 't': float(t)}
        for k in ('rpm', 'spd', 'thr', 'boost'):
            if k not in bdf.columns:
                continue
            try:
                raw = bdf[k].loc[idx_label]
                if pd.isna(raw):
                    continue
                ctx[k] = round(float(raw), 1)
            except Exception:
                continue
        return ctx
    summary['peakTqMoment']    = _peak_context('tq') if channels.get('tq') else None
    summary['peakPwrMoment']   = _peak_context('pwr') if channels.get('pwr') else None
    summary['peakBoostMoment'] = _peak_context('boost')
    # Knock events: how many bucket-seconds had boost>2 AND ign<-5
    if channels.get('ign') and 'boost' in bdf.columns:
        ig = pd.to_numeric(bdf['ign'], errors='coerce')
        bo = pd.to_numeric(bdf['boost'], errors='coerce')
        boost_mask = bo > 2
        knock_mask = boost_mask & (ig < -5)
        boost_s = int(boost_mask.sum())
        knock_s = int(knock_mask.sum())
        summary['boostTimeS']     = int(boost_s)
        summary['knockEvents']    = int(knock_s)
        summary['knockEventRate'] = float(round(100 * knock_s / boost_s, 2)) if boost_s else None
    else:
        summary['boostTimeS']     = None
        summary['knockEvents']    = None
        summary['knockEventRate'] = None
    # Mean LTFT for trend analysis across drives
    if channels.get('ltft'):
        ltft_s = pd.to_numeric(bdf['ltft'], errors='coerce').dropna()
        summary['avgLTFT'] = round(float(ltft_s.mean()), 2) if len(ltft_s) else None
    else:
        summary['avgLTFT'] = None
    # Mean coolant temperature once warm (>180°F) — for cruise-coolant trend
    if channels.get('coolant'):
        c = pd.to_numeric(bdf['coolant'], errors='coerce').dropna()
        warm = c[c > 180]
        summary['avgWarmCoolant'] = round(float(warm.mean()), 1) if len(warm) > 30 else None
    else:
        summary['avgWarmCoolant'] = None
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

# v1.6 — full 100-column OBD Fusion profile (from the test-drive brief).
# Header names we map to canonical channels; everything else is dropped.
OBD_FUSION_V2_COLS = {
    'alt':     'Altitude (ft)',
    'coolant': 'Engine coolant temperature (°F)',
    'iat':     'Intake air temperature (°F)',
    'volt':    'Control module voltage (V)',
    'load':    'Calculated load value (%)',
    'stft':    'Short term fuel % trim - Bank 1 (%)',
    'ltft':    'Long term fuel % trim - Bank 1 (%)',
    'ign':     'Ignition timing advance for #1 cylinder (deg)',
    'maf':     'Mass air flow rate (lb/min)',
    'tq':      'Engine Torque (lb•ft)',
    'afrA':    'A/F Actual',
    'afrC':    'A/F Commanded',
    'lambda':  'O2 sensor lambda wide range (current probe)  (Bank 1  Sensor 1)',
    'catTemp': 'Catalyst temperature (Bank 1 Sensor 1) (°F)',
    'railPsi': 'Fuel rail pressure (psi)',
    'baro':    'Barometric pressure (inHg)',
    'mapInHg': 'Intake manifold absolute pressure (inHg)',  # converted to psi below
    # v1.8 additions: 4 channels already in the SAE profile but never previously ingested,
    # plus the user-added custom PID for intercooler outlet temperature.
    'amb':     'Ambient air temperature (°F)',
    'fuelLvl': 'Fuel level input (%)',
    'ax':      'Accel X (ft/s²)',
    'ay':      'Accel Y (ft/s²)',
    'az':      'Accel Z (ft/s²)',
    'icTempF': 'Intercooler air temp (f)',  # user's custom Mode-22 PID; post-IC outlet
}

def parse_obd_fusion(path):
    df = pd.read_csv(path, skiprows=1, encoding='utf-8-sig')
    df.columns = [c.strip() for c in df.columns]
    cols = set(df.columns)
    has_full = 'Engine RPM (RPM)' in cols
    # v2 = the 100-col discovery profile, identified by MAP + wideband lambda.
    has_v2 = ('Intake manifold absolute pressure (inHg)' in cols
              and 'O2 sensor lambda wide range (current probe)  (Bank 1  Sensor 1)' in cols)
    # Build a renamed view with ONLY the canonical channels we care about.
    # Vectorized rename + to_dict('records') is ~100x faster than iterrows().
    rename = {'Time (sec)': 't'}
    for k, c in OBD_FUSION_FULL_COLS.items():
        if c in cols: rename[c] = k
    if has_v2:
        for k, c in OBD_FUSION_V2_COLS.items():
            if c in cols: rename[c] = k
    keep_src = [c for c in rename.keys() if c in df.columns]
    sub = df[keep_src].rename(columns=rename).copy()
    # Force numeric where applicable
    for col in sub.columns:
        if col == 't':
            sub[col] = pd.to_numeric(sub[col], errors='coerce')
        else:
            sub[col] = pd.to_numeric(sub[col], errors='coerce')
    sub = sub.dropna(subset=['t']).reset_index(drop=True)
    if has_v2 and 'mapInHg' in sub.columns:
        sub['map'] = sub['mapInHg'] * INHG_TO_PSI
    # Pre-bucket 20Hz v2 logs down to 1Hz to keep the pipeline fast.
    # 1Hz logs pass through unchanged (one row per second already).
    if has_v2 and len(sub) > 10000:
        sub['_bk'] = sub['t'].astype(int)
        agg = sub.groupby('_bk', as_index=False).mean(numeric_only=True)
        agg['t'] = agg['_bk'].astype(float)
        sub = agg.drop(columns=['_bk'])
    rows = sub.to_dict('records')
    # The user added "Intercooler air temp (f)" as a custom PID partway through
    # — flag it independently so older v2 drives without the custom PID still parse.
    has_ic = 'Intercooler air temp (f)' in cols
    has_imu = 'Accel X (ft/s²)' in cols
    channels = {
        'rpm':   has_full, 'pwr':   has_full, 'fuel':  has_full, 'boost': has_full,
        # v2-only channels turned on iff the discovery profile is detected
        'alt':   has_v2, 'coolant': has_v2, 'iat': has_v2, 'volt': has_v2,
        'afr':   has_v2, 'map':     has_v2,
        'load':  has_v2, 'stft':    has_v2, 'ltft': has_v2, 'ign':  has_v2,
        'maf':   has_v2, 'tq':      has_v2, 'lambda': has_v2,
        'catTemp': has_v2, 'railPsi': has_v2, 'baro': has_v2,
        # v1.8 channels:
        'amb':     has_v2,
        'fuelLvl': has_v2,
        'imu':     has_imu,    # all three of ax/ay/az move together
        'icTempF': has_ic,
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

# 2) OBD Fusion CSV bulk import — Dropbox (live) + archive (cold storage).
#    Dropbox is scanned first so it wins on duplicates; the archive lets us
#    move old CSVs out of Dropbox without losing drives from the dashboard.
csv_sources = [('Dropbox', DROPBOX_DIR), ('archive', ARCHIVE_DIR)]
csv_seen_keys = set()
for label, src_dir in csv_sources:
    if not os.path.isdir(src_dir):
        print(f"  ({label} dir not present: {src_dir})"); continue
    csv_paths = sorted(glob.glob(os.path.join(src_dir, 'CSVLog_*.csv')))
    print(f"Found {len(csv_paths)} OBD Fusion CSVs in {label} ({src_dir}).")
    for path in csv_paths:
        parsed = parse_obd_fusion_startTime(path)
        if not parsed:
            print(f"  skip {os.path.basename(path)}: no parseable StartTime"); continue
        date, st = parsed
        key = (date, st[:5])
        if key in dlg_taken_keys:
            print(f"  skip {os.path.basename(path)}: .dlg covers {date} {st}"); continue
        if key in csv_seen_keys:
            print(f"  skip {os.path.basename(path)}: already imported from earlier source"); continue
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
        csv_seen_keys.add(key)
        v2 = ' [v2 100-col]' if channels.get('lambda') else ''
        print(f"  + {os.path.basename(path)} -> {date} {st}  {d['summary']['distance']:.1f}mi  {d['summary']['duration']:.0f}s{v2}")

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
def _maxnonnull(key):
    vals = [h.get(key) for h in hist if h.get(key) is not None]
    return max(vals) if vals else None
totals = {
    'driveCount':   len(DRIVES),
    'totalDist':    round(sum(h['distance'] for h in hist), 2),
    'totalDur':     round(sum(h['duration'] for h in hist), 0),
    'totalFuel':    round(sum((h['fuel'] or 0) for h in hist), 3),
    'maxEverSpeed': max(h['maxSpeed'] for h in hist),
    'maxEverRpm':   max(((h['maxRpm'] or 0) for h in hist)),
    # v1.6 fleet-wide peaks (None where no drive has logged the channel):
    'maxEverTq':       _maxnonnull('peakTq'),
    'maxEverBoost':    _maxnonnull('peakBoost'),
    'maxEverMap':      _maxnonnull('maxMap'),
    'maxEverCoolant':  _maxnonnull('maxCoolant'),
    'maxEverIat':      _maxnonnull('maxIat'),
    'maxEverCatTemp':  _maxnonnull('maxCatTemp'),
    # v1.8 fleet-wide:
    'maxEverIcTempF':  _maxnonnull('maxIcTempF'),
    'maxEverAmb':      _maxnonnull('maxAmb'),
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
