import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pawnproof import asset, vault, loan, lab, lab_capture
from pawnproof.report import report, audit

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def feed(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return path


def seed_gems(tmp_path):
    """Two sealed gemstone pledges with valid ownership/appraisal and samples SMP1/SMP2."""
    db = tmp_path / 'lab.sqlite'
    for asset_id, owner, serial, seal, slot in [('G2', 'C2', 'GEM-2', 'gem-seal', 'V2'),
                                                ('G3', 'C3', 'GEM-3', 'gem-seal-2', 'V3')]:
        asset.import_asset(db, feed(tmp_path, f'{asset_id}-asset.json',
            {'asset_id': asset_id, 'owner': owner, 'kind': 'gemstone', 'serial': serial}))
        asset.ownership(db, feed(tmp_path, f'{asset_id}-own.json',
            {'id': f'O-{asset_id}', 'asset_id': asset_id, 'claimant': owner,
             'valid_from': '2026-01-01T00:00:00Z', 'valid_to': '2027-01-01T00:00:00Z'}))
        asset.appraise(db, feed(tmp_path, f'{asset_id}-app.json',
            {'id': f'A-{asset_id}', 'asset_id': asset_id, 'amount': '1000.00',
             'discount_percent': 60, 'assessor_a': 'X', 'assessor_b': 'Y',
             'valid_to': '2026-12-01T00:00:00Z'}))
        vault.import_vault(db, feed(tmp_path, f'{asset_id}-vault.json', {'slot_id': slot, 'capacity': 1}))
        vault.custody(db, feed(tmp_path, f'{asset_id}-cust.json',
            {'id': f'C-{asset_id}', 'asset_id': asset_id, 'kind': 'seal', 'slot_id': slot,
             'seal': seal, 'actor_a': 'guard-a', 'actor_b': 'guard-b', 'at': '2026-03-01T07:00:00Z'}))
    lab.sample(db, FIX / 'gem-sample.json')
    lab.sample(db, FIX / 'gem-sample-2.json')
    return db


def loan_doc(loan_id='L2', asset_id='G2', customer='C2', route='video_primary'):
    doc = {'id': loan_id, 'asset_id': asset_id, 'appraisal_id': f'A-{asset_id}', 'customer': customer,
           'principal': '500.00', 'interest': '25.00',
           'issued_at': '2026-03-01T10:00:00Z', 'due_at': '2026-04-01T10:00:00Z'}
    if route:
        doc['lab_route'] = route
    return doc


def cert_doc(sample_id='SMP1', route='video_primary', cert_id='CERT1', at='2026-03-01T09:40:00Z'):
    return {'id': cert_id, 'sample_id': sample_id, 'route': route, 'at': at}


def test_missing_tray_frame_blocks_certificate_and_exposes_naive_report_page(tmp_path):
    db = seed_gems(tmp_path)
    outcome = lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    assert outcome['interpretations'] == 2
    view = report(db)['lab']
    # two feasible global explanations swap the authentic conclusion between G2 and G3
    verdicts = [iv['verdicts'] for iv in view['interpretations']]
    assert {'SMP1': 'authentic', 'SMP2': 'unknown'} in verdicts
    assert {'SMP1': 'unknown', 'SMP2': 'authentic'} in verdicts
    assert view['samples']['SMP1']['certain'] == 'uncertain'
    assert view['samples']['SMP2']['certain'] == 'uncertain'
    # the run itself carries both candidate identities; neither may be certified
    runs = {r['id']: r for r in view['runs']}
    assert runs['RA']['attributions'] == ['SMP1', 'SMP2']
    with pytest.raises(ValueError, match='lab_attribution_ambiguous'):
        lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    with pytest.raises(ValueError, match='lab_authenticity_missing'):
        loan.originate(db, feed(tmp_path, 'loan.json', loan_doc()))
    # naive scheme: trust the report page and import its claims as manual verdicts
    claims = {c['sample_id']: c for c in view['claims']}
    assert claims['SMP1']['claimed'] == 'authentic'
    assert claims['SMP1']['conflict']  # claim is not substantiated by any feasible attribution
    lab.certify(db, feed(tmp_path, 'naive.json',
        {'id': 'NAIVE1', 'sample_id': 'SMP1', 'analyst': 'lab-B',
         'at': '2026-03-01T09:05:00Z', 'verdict': claims['SMP1']['claimed']}))
    # ...which would release G2's loan even though a feasible global attribution denies it
    assert loan.originate(db, feed(tmp_path, 'naive-loan.json', loan_doc(route=None)))['status'] == 'active'


def test_seal_video_resolves_attribution_and_releases_route_one_loan(tmp_path):
    db = seed_gems(tmp_path)
    lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    lab_capture.import_evidence(db, FIX / 'lab-video-independent.json')
    view = report(db)['lab']
    assert len(view['interpretations']) == 1
    assert view['samples']['SMP1']['certain'] == 'authentic'
    assert view['samples']['SMP2']['certain'] == 'uncertain'
    cert = lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    assert cert == {'id': 'CERT1', 'status': 'active', 'asset_id': 'G2', 'route': 'video_primary'}
    # the certificate traces to seal, tray slot, calibration batch, run and authorized analyst
    basis = json.loads(report(db)['state']['lab_certificate'][0]['basis'])
    assert basis['runs'] == ['RA'] and basis['calibrations'] == ['CB-A']
    assert basis['evidence'] == ['VID1'] and basis['analysts'] == ['lab-B']
    assert basis['seal'] == 'gem-seal'
    assert loan.originate(db, feed(tmp_path, 'loan.json', loan_doc()))['status'] == 'active'
    # the old manual verdict and its source stay on record next to the grounded certificate
    lab.certify(db, FIX / 'gem-result.json')
    state = report(db)['state']
    assert [x['id'] for x in state['lab_result']] == ['RES1']
    assert any(c['sample_id'] == 'SMP1' and not c['conflict'] for c in report(db)['lab']['claims'])
    assert audit(db)['replay_ok']


def test_contaminated_blank_isolates_only_dependent_runs_and_blocks_dual_route(tmp_path):
    db = seed_gems(tmp_path)
    lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    lab_capture.import_evidence(db, FIX / 'lab-video-independent.json')
    view = report(db)['lab']
    # the bad batch only isolates its own runs; the primary instrument stays valid
    assert view['calibrations'] == {'CB-A': 'valid', 'CB-B': 'contaminated'}
    runs = {r['id']: r for r in view['runs']}
    assert runs['RA']['usable'] and runs['RA']['matches'] == 'authentic'
    assert not runs['RB']['usable']
    # dual-instrument route needs two independent valid results: SPEC-B cannot back one
    with pytest.raises(ValueError, match='lab_route_evidence_insufficient'):
        lab_capture.issue_certificate(db, feed(tmp_path, 'certd.json',
            cert_doc(route='dual_instrument', cert_id='CERT-D')))
    with pytest.raises(ValueError, match='lab_authenticity_missing'):
        loan.originate(db, feed(tmp_path, 'loand.json', loan_doc(loan_id='L3', route='dual_instrument')))
    # route one on the same raw files still certifies G2 and releases L2
    lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    assert loan.originate(db, feed(tmp_path, 'loan.json', loan_doc()))['status'] == 'active'
    assert audit(db)['replay_ok']


def test_naive_peak_averaging_dilutes_valid_primary_evidence(tmp_path):
    db = seed_gems(tmp_path)
    lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    doc = json.loads((FIX / 'lab-capture-main.json').read_text())
    reference = doc['reference']
    segment = reference['valid_segments'][0]

    def calibrated_ratio(run):
        cal = next(c for c in doc['calibrations'] if c['id'] == run['calibration_id'])
        blank = max((p['height'] for p in cal['blank'] if segment[0] <= p['position'] <= segment[1]), default=0)
        standard = max(p['height'] for p in cal['standard'] if segment[0] <= p['position'] <= segment[1])
        peak = max(p['height'] for p in run['peaks']
                   if abs(p['position'] - 410) <= reference['position_tolerance'])
        return (peak - blank) / standard

    ratios = [calibrated_ratio(run) for run in doc['runs']]
    # naive scheme: average both instruments; SPEC-B's residue-laden blank drags 0.97 down to ~0.77
    assert abs(ratios[0] - 1.0) <= reference['height_tolerance']
    assert abs(sum(ratios) / len(ratios) - 1.0) > reference['height_tolerance']
    # the capture engine isolates the contaminated batch instead and keeps SPEC-A decisive
    lab_capture.import_evidence(db, FIX / 'lab-video-independent.json')
    cert = lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    assert cert['status'] == 'active'
    runs = {r['id']: r for r in report(db)['lab']['runs']}
    assert runs['RA']['usable'] and not runs['RB']['usable']


def test_certificate_binds_concrete_sample_identity(tmp_path):
    db = seed_gems(tmp_path)
    lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    lab_capture.import_evidence(db, FIX / 'lab-video-independent.json')
    lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    # G2's certificate cannot be lent to G3's loan
    with pytest.raises(ValueError, match='lab_authenticity_missing'):
        loan.originate(db, feed(tmp_path, 'loan3.json',
            loan_doc(loan_id='L3', asset_id='G3', customer='C3')))
    # SMP2 cannot earn a certificate from the same evidence: its only candidate run is contaminated
    with pytest.raises(ValueError, match='lab_authenticity_not_proven'):
        lab_capture.issue_certificate(db, feed(tmp_path, 'cert2.json',
            cert_doc(sample_id='SMP2', cert_id='CERT2')))
    # a manual authentic verdict on SMP2 (G3) does not unlock G2 either
    lab.certify(db, feed(tmp_path, 'res2.json',
        {'id': 'RES2', 'sample_id': 'SMP2', 'analyst': 'lab-B',
         'at': '2026-03-01T09:05:00Z', 'verdict': 'authentic'}))
    with pytest.raises(ValueError, match='lab_authenticity_missing'):
        loan.originate(db, feed(tmp_path, 'loan2.json', loan_doc(route=None)))


def test_video_revocation_withdraws_certificate_and_moves_loan_to_risk_review(tmp_path):
    db = seed_gems(tmp_path)
    lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    lab_capture.import_evidence(db, FIX / 'lab-video-independent.json')
    lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    loan.originate(db, feed(tmp_path, 'loan.json', loan_doc()))
    outcome = lab_capture.revoke_evidence(db, FIX / 'lab-video-revoke.json')
    assert outcome['withdrawn_certificates'] == ['CERT1']
    assert outcome['loans_in_review'] == ['L2']
    state = report(db)['state']
    loan_row = [x for x in state['loan'] if x['id'] == 'L2'][0]
    # risk review, not erasure: the original loan decision and terms stay on record
    assert loan_row['status'] == 'disputed'
    assert loan_row['principal_cents'] == 50000
    assert state['lab_review'][0]['reason'] == 'lab_certificate_withdrawn'
    assert state['lab_review'][0]['loan_id'] == 'L2'
    cert = [x for x in state['lab_certificate'] if x['id'] == 'CERT1'][0]
    assert cert['status'] == 'withdrawn'
    # certainty is gone again: re-issuing under the same route is refused
    with pytest.raises(ValueError, match='lab_attribution_ambiguous'):
        lab_capture.issue_certificate(db, feed(tmp_path, 'cert3.json',
            cert_doc(cert_id='CERT3', at='2026-03-11T09:00:00Z')))
    assert audit(db)['replay_ok']


def test_independent_tray_records_resolve_uncertainty_without_satisfying_routes(tmp_path):
    db = seed_gems(tmp_path)
    lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    lab_capture.import_evidence(db, FIX / 'lab-tray-supplement.json')
    view = report(db)['lab']
    # independent tray records alone do resolve the global attribution
    assert len(view['interpretations']) == 1
    assert view['samples']['SMP1']['certain'] == 'authentic'
    # but the pre-authorized routes keep their own evidence boundaries
    with pytest.raises(ValueError, match='lab_route_evidence_insufficient'):
        lab_capture.issue_certificate(db, feed(tmp_path, 'cert.json', cert_doc()))
    with pytest.raises(ValueError, match='lab_route_evidence_insufficient'):
        lab_capture.issue_certificate(db, feed(tmp_path, 'certd.json',
            cert_doc(route='dual_instrument', cert_id='CERT-D')))


def test_capture_intake_validates_analyst_seal_chain_and_replay(tmp_path):
    db = seed_gems(tmp_path)
    doc = json.loads((FIX / 'lab-capture-main.json').read_text())

    def variant(capture_id, **changes):
        body = json.loads(json.dumps(doc))
        body['id'] = capture_id
        body.update(changes)
        return body

    with pytest.raises(ValueError, match='unauthorized_analyst'):
        lab_capture.import_capture(db, feed(tmp_path, 'bad-analyst.json',
            variant('CAP-X', analyst='stranger')))
    bad_seal = variant('CAP-Y')
    bad_seal['seal_scans'][0]['seal'] = 'tampered-seal'
    with pytest.raises(ValueError, match='seal_chain_broken'):
        lab_capture.import_capture(db, feed(tmp_path, 'bad-seal.json', bad_seal))
    with pytest.raises(ValueError, match='analyst_not_independent'):
        lab_capture.import_capture(db, feed(tmp_path, 'collector.json',
            variant('CAP-Z', analyst='clerk-A', authorized_analysts=['clerk-A'])))
    unknown = variant('CAP-W')
    unknown['seal_scans'][0]['sample_id'] = 'SMP9'
    with pytest.raises(ValueError, match='unknown_sample'):
        lab_capture.import_capture(db, feed(tmp_path, 'unknown.json', unknown))
    first = lab_capture.import_capture(db, FIX / 'lab-capture-main.json')
    assert first['status'] == 'captured' and first['interpretations'] == 2
    assert lab_capture.import_capture(db, FIX / 'lab-capture-main.json')['replayed']


def test_cli_registers_capture_commands(tmp_path):
    db = seed_gems(tmp_path)
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parent.parent)}
    p = subprocess.run([sys.executable, '-m', 'pawnproof', 'lab-capture-import',
                        str(FIX / 'lab-capture-main.json'), '--db', str(db)],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['interpretations'] == 2
    p = subprocess.run([sys.executable, '-m', 'pawnproof', 'lab-evidence-import',
                        str(FIX / 'lab-video-independent.json'), '--db', str(db)],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    p = subprocess.run([sys.executable, '-m', 'pawnproof', 'audit', '--db', str(db)],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['replay_ok']
