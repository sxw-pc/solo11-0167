"""Offline lab capture: raw spectra intake, tray attribution, certificate routes.

A capture bundle carries the raw files exported offline from the lab's
spectrometers: peak lists per run, blank/standard calibration batches, the
sample-tray slot log, seal scans and the signing analyst.  Sample identity
never comes from result file names or peak magnitudes: a run is bound to a
pledged sample only through the tray-slot timeline constrained by seal
observations and declared clock-drift windows, and every feasible global
attribution is enumerated.  When two feasible interpretations disagree
about a sample's authenticity no definite certificate may be issued.
Independent evidence (seal video or an independent tray log) can shrink
the feasible set again; revoking that evidence reopens the uncertainty and
sends affected loans to risk review without erasing the original decision.
A contaminated calibration batch only isolates the runs that depend on it.
"""
import itertools
import json
from datetime import datetime, timedelta

from .store import open_db
from .evidence import at, read_source, unique_event

ROUTES = ('video_primary', 'dual_instrument')
EVIDENCE_KINDS = ('seal_video', 'tray_log')


def _moment(value):
    return datetime.fromisoformat(value[:-1] + '+00:00')


def _stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _segment(reference, position):
    for lo, hi in reference['valid_segments']:
        if lo <= position <= hi:
            return (lo, hi)
    return None


def _level(peaks, segment):
    lo, hi = segment
    return max((p['height'] for p in peaks if lo <= p['position'] <= hi), default=0)


def calibration_status(calibration, reference):
    """A batch is contaminated when its blank shows residue inside a valid segment."""
    threshold = reference['residue_threshold']
    for segment in reference['valid_segments']:
        standard = _level(calibration['standard'], segment)
        if standard <= 0 or _level(calibration['blank'], segment) / standard > threshold:
            return 'contaminated'
    return 'valid'


def run_match(run, calibration, reference):
    """Match blank-corrected, standard-normalised peaks against the reference profile."""
    pos_tol = reference['position_tolerance']
    height_tol = reference['height_tolerance']
    for ref in reference['peaks']:
        segment = _segment(reference, ref['position'])
        standard = _level(calibration['standard'], segment)
        candidates = [p['height'] for p in run['peaks'] if abs(p['position'] - ref['position']) <= pos_tol]
        if standard <= 0 or not candidates:
            return 'inauthentic'
        ratio = (max(candidates) - _level(calibration['blank'], segment)) / standard
        if abs(ratio - ref['ratio']) > height_tol * ref['ratio']:
            return 'inauthentic'
    for peak in run['peaks']:
        segment = _segment(reference, peak['position'])
        if segment is None:
            continue
        if any(abs(peak['position'] - ref['position']) <= pos_tol for ref in reference['peaks']):
            continue
        standard = _level(calibration['standard'], segment)
        if standard > 0 and (peak['height'] - _level(calibration['blank'], segment)) / standard > reference['residue_threshold']:
            return 'inauthentic'
    return 'authentic'


def import_capture(path, file):
    """Ingest one offline capture bundle: instruments, calibrations, tray log, runs, scans."""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'lab_capture', file)
        if unique_event(db, 'lab_capture', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if d['analyst'] not in d['authorized_analysts']:
            raise ValueError('unauthorized_analyst')
        reference = d['reference']
        scans = d.get('seal_scans', [])
        seen_seals = {}
        for scan in scans:
            sample = db.execute('SELECT * FROM lab_sample WHERE id=?', (scan['sample_id'],)).fetchone()
            if not sample:
                raise ValueError('unknown_sample')
            if sample['seal'] != scan['seal'] or scan['seal'] in seen_seals:
                raise ValueError('seal_chain_broken')
            if sample['collector'] == d['analyst']:
                raise ValueError('analyst_not_independent')
            seen_seals[scan['seal']] = scan['sample_id']
            at(scan['at'])
        instruments = {i['id']: i for i in d['instruments']}
        calibrations = {}
        for cal in d['calibrations']:
            if cal['instrument'] not in instruments:
                raise ValueError('unknown_instrument')
            calibrations[cal['id']] = cal
            at(cal['at'])
        runs = {}
        for run in d['runs']:
            if run['instrument'] not in instruments:
                raise ValueError('unknown_instrument')
            cal = calibrations.get(run['calibration_id'])
            if not cal or cal['instrument'] != run['instrument']:
                raise ValueError('calibration_instrument_mismatch')
            runs[run['id']] = run
            at(run['at'])
        for frame in d['tray_frames']:
            at(frame['at'])
            if not frame['slots']:
                raise ValueError('empty_tray_frame')
        moment = at(d['at'])
        db.execute('INSERT INTO lab_capture VALUES(?,?,?,?,?)',
                   (d['id'], d['analyst'], moment,
                    json.dumps({'instruments': d['instruments'], 'reference': reference}, sort_keys=True), sid))
        for cal in calibrations.values():
            db.execute('INSERT INTO lab_calibration VALUES(?,?,?,?,?,?,?)',
                       (cal['id'], d['id'], cal['instrument'], cal['at'],
                        calibration_status(cal, reference),
                        json.dumps({'blank': cal['blank'], 'standard': cal['standard']}, sort_keys=True), sid))
        for run in runs.values():
            db.execute('INSERT INTO lab_run VALUES(?,?,?,?,?,?,?,?,?)',
                       (run['id'], d['id'], run['instrument'], run['slot'], run['at'], run['calibration_id'],
                        run_match(run, calibrations[run['calibration_id']], reference),
                        json.dumps(run['peaks'], sort_keys=True), sid))
        for frame in d['tray_frames']:
            for slot, seal in frame['slots'].items():
                db.execute('INSERT INTO lab_tray_frame VALUES(?,?,?,?,?)',
                           (d['id'], int(frame['seq']), frame['at'], slot, seal))
        for scan in scans:
            db.execute('INSERT INTO lab_seal_scan VALUES(?,?,?,?,?)',
                       (d['id'], scan['sample_id'], scan['seal'], scan['at'], sid))
        for claim in d.get('report_page', []):
            db.execute('INSERT INTO lab_report_claim VALUES(?,?,?,?)',
                       (d['id'], claim['sample_id'], claim['verdict'], sid))
        view = analyze(db)
        return {'id': d['id'], 'status': 'captured', 'interpretations': len(view['interpretations']),
                'certain': {s: v['certain'] for s, v in view['samples'].items()}}


def import_evidence(path, file):
    """Attach independent evidence (seal video or tray log) that constrains slot occupancy."""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'lab_evidence', file)
        if unique_event(db, 'lab_evidence', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if d['kind'] not in EVIDENCE_KINDS:
            raise ValueError('unknown_evidence_kind')
        moment = at(d['at'])
        db.execute('INSERT INTO lab_evidence VALUES(?,?,?,?,?)', (d['id'], d['kind'], 'active', moment, sid))
        for frame in d['frames']:
            at(frame['at'])
            for slot, seal in frame['slots'].items():
                db.execute('INSERT INTO lab_evidence_frame VALUES(?,?,?,?)', (d['id'], frame['at'], slot, seal))
        return {'id': d['id'], 'status': 'active', 'interpretations': len(analyze(db)['interpretations'])}


def issue_certificate(path, file):
    """Issue a route-bound authentic certificate for one concrete sample identity."""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'lab_certificate', file)
        if unique_event(db, 'lab_certificate', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if d['route'] not in ROUTES:
            raise ValueError('unknown_lab_route')
        sample = db.execute('SELECT * FROM lab_sample WHERE id=?', (d['sample_id'],)).fetchone()
        if not sample:
            raise ValueError('unknown_sample')
        moment = at(d['at'])
        check = _route_check(db, d['sample_id'], d['route'])
        if not check['ok']:
            raise ValueError(check['reason'])
        if db.execute("SELECT 1 FROM lab_certificate WHERE sample_id=? AND route=? AND status='active'",
                      (d['sample_id'], d['route'])).fetchone():
            raise ValueError('duplicate_certificate')
        analysts = sorted({row['analyst'] for row in db.execute('SELECT DISTINCT analyst FROM lab_capture')})
        basis = {**check['basis'], 'analysts': analysts, 'seal': sample['seal']}
        db.execute('INSERT INTO lab_certificate VALUES(?,?,?,?,?,?,?,?,?)',
                   (d['id'], d['sample_id'], sample['asset_id'], d['route'], 'authentic', 'active', moment,
                    json.dumps(basis, sort_keys=True), sid))
        return {'id': d['id'], 'status': 'active', 'asset_id': sample['asset_id'], 'route': d['route']}


def revoke_evidence(path, file):
    """Revoke independent evidence; withdraw certificates that lose their basis and
    move loans depending on them to risk review (status 'disputed')."""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'lab_evidence_revoke', file)
        if unique_event(db, 'lab_revocation', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        evidence = db.execute('SELECT * FROM lab_evidence WHERE id=?', (d['evidence_id'],)).fetchone()
        if not evidence:
            raise ValueError('unknown_evidence')
        if evidence['status'] != 'active':
            raise ValueError('evidence_not_active')
        moment = at(d['at'])
        db.execute('INSERT INTO lab_revocation VALUES(?,?,?,?,?)',
                   (d['id'], d['evidence_id'], d.get('reason', ''), moment, sid))
        db.execute("UPDATE lab_evidence SET status='revoked' WHERE id=?", (d['evidence_id'],))
        withdrawn, reviewed = [], []
        for cert in db.execute("SELECT * FROM lab_certificate WHERE status='active'").fetchall():
            if _route_check(db, cert['sample_id'], cert['route'])['ok']:
                continue
            db.execute("UPDATE lab_certificate SET status='withdrawn' WHERE id=?", (cert['id'],))
            withdrawn.append(cert['id'])
            for loan_row in db.execute("SELECT * FROM loan WHERE asset_id=? AND status IN ('active','extended')",
                                       (cert['asset_id'],)).fetchall():
                db.execute("UPDATE loan SET status='disputed' WHERE id=?", (loan_row['id'],))
                db.execute('INSERT INTO lab_review(loan_id,certificate_id,reason,at,source_id) VALUES(?,?,?,?,?)',
                           (loan_row['id'], cert['id'], 'lab_certificate_withdrawn', moment, sid))
                reviewed.append(loan_row['id'])
        return {'id': d['id'], 'revoked': d['evidence_id'],
                'withdrawn_certificates': withdrawn, 'loans_in_review': reviewed}


def _log_frames(db):
    groups = {}
    for row in db.execute('SELECT capture_id, seq, at, slot, seal FROM lab_tray_frame'):
        key = (row['capture_id'], row['seq'])
        groups.setdefault(key, {'at': row['at'], 'slots': {}})['slots'][row['slot']] = row['seal']
    return [(g['at'], g['slots']) for g in groups.values()]


def _evidence_frames(db):
    active = {row['id']: row['kind'] for row in db.execute("SELECT id, kind FROM lab_evidence WHERE status='active'")}
    groups = {}
    for row in db.execute('SELECT evidence_id, at, slot, seal FROM lab_evidence_frame'):
        if row['evidence_id'] in active:
            groups.setdefault((row['evidence_id'], row['at']), {})[row['slot']] = row['seal']
    return [(t, slots) for (_, t), slots in groups.items()]


def _regions(frame_maps):
    """Compress slot-occupancy observations into certain/ambiguous timeline regions.

    Between two consecutive observations with differing maps the swap moment is
    unknown, so both maps stay feasible for the whole open interval."""
    by_time = {}
    conflict = False
    for t, slots in frame_maps:
        merged = by_time.setdefault(t, {})
        for slot, seal in slots.items():
            if slot in merged and merged[slot] != seal:
                conflict = True
            merged[slot] = seal
    times = sorted(by_time, key=_moment)
    regions = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else None
        if nxt is not None and by_time[t] != by_time[nxt]:
            regions.append({'start': t, 'end': nxt, 'maps': [by_time[t], by_time[nxt]], 'certain': False})
        else:
            regions.append({'start': t, 'end': nxt, 'maps': [by_time[t]], 'certain': True})
    return regions, conflict


def _overlaps(interval, region):
    a, b = interval
    if b <= _moment(region['start']):
        return False
    return region['end'] is None or a < _moment(region['end'])


def _interpretations(runs, regions):
    """Enumerate globally consistent swap-time choices for every ambiguous region."""
    interpretations = [{}]
    for idx, region in enumerate(regions):
        if region['certain']:
            continue
        start, end = _moment(region['start']), _moment(region['end'])
        doms = []
        for run in runs:
            a, b = run['interval']
            if not (b > start and a < end):
                continue
            domain = []
            if b < end:
                domain.append(0)  # run finished before the swap: early map
            if a > start:
                domain.append(1)  # run started after the swap: late map
            doms.append((run['id'], domain or [0, 1], a, b))
        feasible = []
        for combo in itertools.product(*(d for _, d, _, _ in doms)):
            early_end = [b for (_, _, _, b), c in zip(doms, combo) if c == 0]
            late_start = [a for (_, _, a, _), c in zip(doms, combo) if c == 1]
            if max([start] + early_end) <= min([end] + late_start):
                feasible.append(dict(zip((x[0] for x in doms), combo)))
        grown = []
        for base in interpretations:
            for assign in feasible:
                nxt = dict(base)
                nxt[idx] = assign
                grown.append(nxt)
        interpretations = grown
    return interpretations


def _run_seals(run, regions, interpretation):
    if run['interval'] is None:
        return set()
    seals = set()
    for idx, region in enumerate(regions):
        if not _overlaps(run['interval'], region):
            continue
        if region['certain']:
            seals.add(region['maps'][0].get(run['slot']))
        else:
            choice = interpretation.get(idx, {}).get(run['id'])
            if choice is None:
                seals.update(m.get(run['slot']) for m in region['maps'])
            else:
                seals.add(region['maps'][choice].get(run['slot']))
    seals.discard(None)
    return seals


def analyze(db):
    """Recompute tray attribution, calibration dependency and per-sample certainty.

    Pure function of the stored capture/evidence tables: every certificate
    check and every report re-derives the same feasible interpretations."""
    configs = {row['id']: json.loads(row['config']) for row in db.execute('SELECT id, config FROM lab_capture')}
    instruments, primary = {}, set()
    for cfg in configs.values():
        for inst in cfg['instruments']:
            instruments[inst['id']] = inst
            if inst.get('role') == 'primary':
                primary.add(inst['id'])
    calibrations = {row['id']: dict(row) for row in db.execute('SELECT * FROM lab_calibration')}
    runs = []
    for row in db.execute('SELECT * FROM lab_run ORDER BY id'):
        inst = instruments.get(row['instrument'])
        interval = None
        if inst:
            lo, hi = inst['clock_offset_seconds']
            t = _moment(row['at'])
            interval = (t - timedelta(seconds=hi), t - timedelta(seconds=lo))
        status = calibrations[row['calibration_id']]['status']
        runs.append({'id': row['id'], 'instrument': row['instrument'], 'slot': row['slot'],
                     'calibration_id': row['calibration_id'], 'calibration_status': status,
                     'usable': status == 'valid', 'matches': row['matches'], 'interval': interval})
    log_regions, log_conflict = _regions(_log_frames(db))
    log_gaps = [[r['start'], r['end']] for r in log_regions if not r['certain']]
    regions, conflict = _regions(_log_frames(db) + _evidence_frames(db))
    interpretations = _interpretations(runs, regions)
    seal_to_sample = {row['seal']: row['sample_id']
                      for row in db.execute('SELECT DISTINCT seal, sample_id FROM lab_seal_scan')}
    sample_ids = sorted({row['sample_id'] for row in db.execute('SELECT sample_id FROM lab_seal_scan')})
    sample_rows = {row['id']: row for row in db.execute('SELECT * FROM lab_sample')}
    interpretations_view = []
    for interpretation in interpretations:
        attrib = {}
        for run in runs:
            seals = _run_seals(run, regions, interpretation)
            attrib[run['id']] = seal_to_sample.get(next(iter(seals))) if len(seals) == 1 else None
        verdicts = {}
        for sample_id in sample_ids:
            owned = [r for r in runs if attrib[r['id']] == sample_id and r['usable']]
            if any(r['matches'] == 'authentic' for r in owned):
                verdicts[sample_id] = 'authentic'
            elif owned:
                verdicts[sample_id] = 'inauthentic'
            else:
                verdicts[sample_id] = 'unknown'
        interpretations_view.append({'runs': attrib, 'verdicts': verdicts})
    samples = {}
    for sample_id in sample_ids:
        per = [iv['verdicts'][sample_id] for iv in interpretations_view]
        certain = per[0] if per and all(v == per[0] for v in per) else 'uncertain'
        row = sample_rows.get(sample_id)
        samples[sample_id] = {'asset_id': row['asset_id'] if row else None,
                              'verdicts': sorted(set(per)),
                              'certain': certain if certain != 'unknown' else 'uncertain'}
    claims = []
    for row in db.execute('SELECT sample_id, verdict FROM lab_report_claim ORDER BY sample_id'):
        derived = samples.get(row['sample_id'], {}).get('certain', 'uncertain')
        comparable = 'unknown' if derived == 'uncertain' else derived
        claims.append({'sample_id': row['sample_id'], 'claimed': row['verdict'],
                       'derived': derived, 'conflict': row['verdict'] != comparable})
    evidence_frames = {}
    for row in db.execute('SELECT evidence_id, at FROM lab_evidence_frame ORDER BY at'):
        evidence_frames.setdefault(row['evidence_id'], []).append(row['at'])
    runs_view = []
    for run in runs:
        attributed = sorted({iv['runs'][run['id']] for iv in interpretations_view if iv['runs'][run['id']]})
        runs_view.append({'id': run['id'], 'instrument': run['instrument'], 'slot': run['slot'],
                          'calibration_id': run['calibration_id'], 'calibration_status': run['calibration_status'],
                          'usable': run['usable'], 'matches': run['matches'],
                          'window': [_stamp(run['interval'][0]), _stamp(run['interval'][1])] if run['interval'] else None,
                          'attributions': attributed or ['unresolved']})
    return {'captures': sorted(configs), 'primary_instruments': sorted(primary),
            'calibrations': {cid: c['status'] for cid, c in sorted(calibrations.items())},
            'runs': runs_view, 'interpretations': interpretations_view, 'samples': samples,
            'claims': claims, 'log_gaps': log_gaps, 'conflict': conflict or log_conflict,
            'evidence': [dict(row) for row in db.execute('SELECT id, kind, status, at FROM lab_evidence ORDER BY id')],
            'evidence_frames': evidence_frames,
            'certificates': [dict(row) for row in db.execute(
                'SELECT id, sample_id, asset_id, route, verdict, status, at FROM lab_certificate ORDER BY id')]}


def _route_check(db, sample_id, route):
    """Evidence-sufficiency boundary for one certificate route on one sample."""
    view = analyze(db)
    fail = lambda reason: {'ok': False, 'reason': reason, 'view': view}
    if sample_id not in view['samples']:
        return fail('unknown_sample')
    if view['conflict']:
        return fail('lab_evidence_conflict')
    if view['samples'][sample_id]['certain'] != 'authentic':
        return fail('lab_attribution_ambiguous' if len(view['interpretations']) > 1
                    else 'lab_authenticity_not_proven')
    certifying = None
    for iv in view['interpretations']:
        ids = {rid for rid, s in iv['runs'].items() if s == sample_id}
        certifying = ids if certifying is None else certifying & ids
    good = [r for r in view['runs']
            if r['id'] in (certifying or set()) and r['usable'] and r['matches'] == 'authentic']
    basis = {'runs': sorted(r['id'] for r in good),
             'calibrations': sorted({r['calibration_id'] for r in good}),
             'evidence': sorted(e['id'] for e in view['evidence'] if e['status'] == 'active')}
    if route == 'video_primary':
        primary_runs = [r for r in good if r['instrument'] in set(view['primary_instruments'])]
        videos = [e for e in view['evidence'] if e['kind'] == 'seal_video' and e['status'] == 'active']
        if not primary_runs or not videos:
            return fail('lab_route_evidence_insufficient')
        for run in primary_runs:
            a, b = (_moment(run['window'][0]), _moment(run['window'][1]))
            for g0, g1 in view['log_gaps']:
                if _moment(g0) < a and b < _moment(g1):
                    frames = [t for v in videos for t in view['evidence_frames'].get(v['id'], [])]
                    if not any(_moment(g0) < _moment(t) < _moment(g1) for t in frames):
                        return fail('lab_route_evidence_insufficient')
    elif route == 'dual_instrument':
        if len({r['instrument'] for r in good}) < 2:
            return fail('lab_route_evidence_insufficient')
    else:
        return fail('unknown_lab_route')
    return {'ok': True, 'basis': basis, 'view': view}
