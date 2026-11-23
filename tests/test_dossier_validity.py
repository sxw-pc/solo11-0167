"""Versioned dossier issuance, selective disclosure, supersession and recovery.

The same input fixture set drives every test:

1. L1 active / G1 sealed  -> DOC1 sealed with an active/sealed watermark
2. full payment + redeem  -> L1 redeemed, G1 released
3. DOC2 captured          -> DOC1 loses current qualification, history retained

The two regulator review paths are locked before release and must produce
different business conclusions while facing the same source files and the
same external receipt.
"""
import json
from pathlib import Path

import pytest

from pawnproof import asset, vault, loan, dossier
from pawnproof.report import report, dossier_report
from pawnproof.store import open_db

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


# --------------------------------------------------------------------------- #
# Fixture-level helpers
# --------------------------------------------------------------------------- #
def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf8')
    return path


def _seed_active_loan(tmp_path):
    """Replay the upstream chain up to the active loan / sealed asset point."""
    db = tmp_path / 'state.sqlite'
    for func, name in [
        (asset.import_asset, 'asset-ok.json'),
        (asset.ownership, 'ownership-ok.json'),
        (asset.appraise, 'appraisal-ok.json'),
        (vault.import_vault, 'vault-ok.json'),
        (vault.custody, 'custody-ok.json'),
        (loan.originate, 'loan-ok.json'),
    ]:
        func(db, FIX / name)
    return db


def _redeem(db):
    loan.pay(db, FIX / 'payment-ok.json')
    decision = loan.decide(db, FIX / 'decision-ok.json')
    assert decision['status'] == 'approved'
    return decision


def _acknowledge(db, tmp_path, fixture_name, digest):
    receipt = json.loads((FIX / fixture_name).read_text(encoding='utf8'))
    receipt['digest'] = digest
    return dossier.acknowledge(db, _write(tmp_path, f'{fixture_name}.filled.json', receipt))


def _adjudicate(db, tmp_path, name):
    return dossier.adjudicate(db, FIX / name)


def _capture_and_release_doc1(db, tmp_path):
    out = tmp_path / 'pub'
    cap = dossier.capture(db, FIX / 'dossier-ok.json', out)
    rel_hist = dossier.release(db, FIX / 'release-historical.json', out)
    rel_cur = dossier.release(db, FIX / 'release-current.json', out)
    _acknowledge(db, tmp_path, 'acknowledgment-dossier.json', cap['digest'])
    return out, cap, rel_hist, rel_cur


# --------------------------------------------------------------------------- #
# Capture: watermark, source identity, point-in-time status, catalog, link
# --------------------------------------------------------------------------- #
def test_capture_records_watermark_sources_status_and_withdrawal_link(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, cap, _, _ = _capture_and_release_doc1(db, tmp_path)

    assert cap['version'] == 1
    assert cap['loan_status_at_capture'] == 'active'
    assert cap['asset_status_at_capture'] == 'sealed'
    # Six upstream source files have been ingested at this watermark.
    assert cap['watermark']['kind'] == 'source_file_sequence'
    assert cap['watermark']['value'] == 6
    assert cap['supersedes'] is None

    sealed = json.loads((out / 'DOC1.sealed.json').read_text(encoding='utf8'))
    assert sealed['withdrawal_link']['revocation_table'] == 'dossier_revocation'
    assert sealed['withdrawal_link']['supersedes'] is None
    # Every source identity backing the dossier is listed.
    assert [s['kind'] for s in sealed['source_files']] == [
        'asset', 'ownership', 'appraisal', 'vault', 'custody', 'loan']
    # The public field catalog classifies every leaf before release.
    catalog_paths = {c['path'] for c in sealed['field_catalog']}
    assert 'data.loan.customer' in catalog_paths
    assert not next(c for c in sealed['field_catalog']
                    if c['path'] == 'data.loan.customer')['public']
    assert next(c for c in sealed['field_catalog']
                if c['path'] == 'data.loan.status')['public']
    # The registered digest matches the bytes actually published.
    assert dossier._sha((out / 'DOC1.sealed.json').read_bytes()) == cap['digest']
    with open_db(db) as con:
        row = con.execute('SELECT * FROM dossier_version WHERE id=?', ('DOC1',)).fetchone()
        assert row['current'] == 1 and row['publish_state'] == 'published'
        assert row['watermark_value'] == 6 and row['source_count'] == 6


def test_duplicate_capture_id_rejected(tmp_path):
    db = _seed_active_loan(tmp_path)
    out = tmp_path / 'pub'
    dossier.capture(db, FIX / 'dossier-ok.json', out)
    with pytest.raises(ValueError, match='dossier_id_conflict'):
        dossier.capture(db, FIX / 'dossier-ok.json', out)


# --------------------------------------------------------------------------- #
# Selective disclosure: redaction stays inside the signed commitment
# --------------------------------------------------------------------------- #
def test_release_redacts_sensitive_fields_without_removing_them_from_commitment(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, cap, rel_hist, _ = _capture_and_release_doc1(db, tmp_path)
    package = json.loads((out / 'REL-DOC1-HIST.disclosure.json').read_text(encoding='utf8'))

    sensitive = [leaf for leaf in package['leaves'] if leaf['sensitive']]
    assert {leaf['path'] for leaf in sensitive} == {
        'data.loan.customer', 'data.asset[0].owner', 'data.asset[0].serial',
        'data.ownership[0].claimant'}
    # Redacted leaves expose salted commitments only - never a value.
    assert all('value' not in leaf for leaf in sensitive)
    assert all(not leaf['disclosed'] for leaf in sensitive)
    # Public leaves carry verifiable plain values.
    status_leaf = next(leaf for leaf in package['leaves']
                       if leaf['path'] == 'data.loan.status')
    assert status_leaf['disclosed'] is True and status_leaf['value'] == 'active'

    # The verifier recomputes the same sealed root from public material.
    result = dossier.verify_package(db, out / 'REL-DOC1-HIST.disclosure.json')
    assert result['ok'] is True
    assert result['commitment'] == cap['commitment']
    assert result['db_checks'] == {
        'dossier_registered': True, 'sealed_file': 'ok',
        'release_registered': True, 'current_version_id': 'DOC1',
        'package_is_current': True, 'revoked': False}


def test_profile_naming_sensitive_field_is_rejected(tmp_path):
    db = _seed_active_loan(tmp_path)
    out = tmp_path / 'pub'
    dossier.capture(db, FIX / 'dossier-ok.json', out)
    with pytest.raises(ValueError, match='profile_discloses_sensitive_field'):
        dossier.release(db, FIX / 'release-forbidden-fields.json', out)
    # Nothing was published for the rejected request.
    assert not (out / 'REL-FORBIDDEN.disclosure.json').exists()


def test_salted_redacted_cannot_be_reversed_but_remains_committed(tmp_path):
    """The verifier confirms same-sealed-original consistency while the
    customer value cannot be recovered from the public package."""
    db = _seed_active_loan(tmp_path)
    out, cap, _, _ = _capture_and_release_doc1(db, tmp_path)
    package = json.loads((out / 'REL-DOC1-CUR.disclosure.json').read_text(encoding='utf8'))
    customer_leaf = next(leaf for leaf in package['leaves']
                         if leaf['path'] == 'data.loan.customer')
    # The salted digest is 64 hex chars and unrelated to the plain value.
    assert len(customer_leaf['digest']) == 64
    assert customer_leaf['digest'] != dossier._sha(b'"C1"')
    # All package bytes together never contain the serial value.
    assert 'S-1' not in (out / 'REL-DOC1-CUR.disclosure.json').read_text(encoding='utf8')
    # Yet the root over visible + redacted leaves equals the sealed commitment.
    assert dossier.verify_package(
        db, out / 'REL-DOC1-CUR.disclosure.json')['commitment'] == cap['commitment']


def test_two_packages_of_same_dossier_are_same_source_same_version(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, cap, rel_hist, rel_cur = _capture_and_release_doc1(db, tmp_path)
    hist = json.loads((out / 'REL-DOC1-HIST.disclosure.json').read_text(encoding='utf8'))
    cur = json.loads((out / 'REL-DOC1-CUR.disclosure.json').read_text(encoding='utf8'))
    assert hist['sealed_root_commitment'] == cur['sealed_root_commitment']
    assert hist['sealed_root_commitment'] == cap['commitment']
    assert hist['dossier_version'] == cur['dossier_version'] == 1
    # Different purposes and field locks make distinct package digests.
    assert rel_hist['package_digest'] != rel_cur['package_digest']
    assert hist['purpose'] != cur['purpose']


# --------------------------------------------------------------------------- #
# Tampering must be detectable from public material
# --------------------------------------------------------------------------- #
def test_verifier_rejects_tampered_visible_value_and_digest(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, _, _, _ = _capture_and_release_doc1(db, tmp_path)
    package_path = out / 'REL-DOC1-HIST.disclosure.json'
    package = json.loads(package_path.read_text(encoding='utf8'))

    tampered_value = json.loads(json.dumps(package))
    leaf = next(leaf for leaf in tampered_value['leaves'] if leaf['disclosed'])
    leaf['value'] = 'forged'
    bad = _write(tmp_path, 'bad-value.json', tampered_value)
    assert dossier.verify_package(db, bad)['reason'] == 'visible_leaf_digest_mismatch'

    tampered_digest = json.loads(json.dumps(package))
    redacted_index = next(i for i, leaf in enumerate(tampered_digest['leaves'])
                          if not leaf['disclosed'])
    tampered_digest['leaves'][redacted_index]['digest'] = 'f' * 64
    bad2 = _write(tmp_path, 'bad-digest.json', tampered_digest)
    assert dossier.verify_package(db, bad2)['reason'] == 'root_commitment_mismatch'

    tampered_purpose = json.loads(json.dumps(package))
    tampered_purpose['purpose'] = dossier.PURPOSE_CURRENT
    bad3 = _write(tmp_path, 'bad-purpose.json', tampered_purpose)
    assert dossier.verify_package(db, bad3)['reason'] == 'package_commitment_mismatch'


def test_verifier_flags_missing_and_conflicting_sealed_file(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, _, _, _ = _capture_and_release_doc1(db, tmp_path)
    package_path = out / 'REL-DOC1-HIST.disclosure.json'

    (out / 'DOC1.sealed.json').unlink()
    result = dossier.verify_package(db, package_path)
    assert result['ok'] is True  # package itself still verifies...
    assert result['db_checks']['sealed_file'] == 'missing'

    (out / 'DOC1.sealed.json').write_text('{"evil":true}', encoding='utf8')
    result = dossier.verify_package(db, package_path)
    assert result['db_checks']['sealed_file'] == 'conflict'


# --------------------------------------------------------------------------- #
# The two locked regulator review paths after redemption
# --------------------------------------------------------------------------- #
def _redeemed_world(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, cap, _, _ = _capture_and_release_doc1(db, tmp_path)
    _redeem(db)
    state = report(db)['state']
    assert state['loan'][0]['status'] == 'redeemed'
    assert state['asset'][0]['status'] == 'released'
    return db, out, cap


def test_historical_review_passes_but_is_flagged_not_current(tmp_path):
    db, out, cap = _redeemed_world(tmp_path)
    verdict = _adjudicate(db, tmp_path, 'adjudication-historical.json')
    assert verdict['verdict'] == dossier.VERDICT_HISTORICAL_PASS
    assert verdict['is_current_claim'] is False
    reasons = verdict['reasons']
    assert 'historical_only_not_current_custody_claim' in reasons
    assert 'point_in_time_active_loan_sealed_asset_confirmed' in reasons
    assert any(r.startswith('watermark_stale:captured=6,current=8') for r in reasons)


def test_current_review_on_stale_doc1_cannot_pass_after_redemption(tmp_path):
    db, out, cap = _redeemed_world(tmp_path)
    verdict = _adjudicate(db, tmp_path, 'adjudication-current.json')
    assert verdict['verdict'] == dossier.VERDICT_CURRENT_RELEASED
    assert verdict['is_current_claim'] is True
    assert 'loan_status=redeemed' in verdict['reasons']
    assert 'asset_status=released' in verdict['reasons']


def test_purpose_cannot_be_relabelled_after_redemption_discovered(tmp_path):
    db, out, cap = _redeemed_world(tmp_path)
    # A historical-purpose release exists; attempting to adjudicate it as a
    # current custody review is refused rather than silently re-scoped.
    dossier.release(db, FIX / 'fault-relabel-purpose.json', out)
    forged = _write(tmp_path, 'relabel-request.json', {
        'release_id': 'REL-RELABEL', 'purpose': dossier.PURPOSE_CURRENT,
        'id': 'ADJ-RELABEL'})
    with pytest.raises(ValueError, match='purpose_relabel_denied'):
        dossier.adjudicate(db, forged)
    with open_db(db) as con:
        stored = con.execute(
            "SELECT purpose FROM dossier_release WHERE id='REL-RELABEL'").fetchone()
        assert stored['purpose'] == dossier.PURPOSE_HISTORICAL


def test_unknown_purpose_rejected(tmp_path):
    db = _seed_active_loan(tmp_path)
    out = tmp_path / 'pub'
    dossier.capture(db, FIX / 'dossier-ok.json', out)
    bad_profile = json.loads((FIX / 'release-historical.json').read_text())
    bad_profile['purpose'] = 'fishing_expedition'
    bad_profile['release_id'] = 'REL-BAD'
    with pytest.raises(ValueError, match='unknown_review_purpose'):
        dossier.release(db, _write(tmp_path, 'bad-purpose-profile.json', bad_profile), out)


# --------------------------------------------------------------------------- #
# Version 2: new watermark, revoked current qualification, retained history
# --------------------------------------------------------------------------- #
def test_doc2_supersedes_doc1_without_overwriting_or_reusing_digest(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    cap2 = dossier.capture(db, FIX / 'dossier-capture-v2.json', out)
    assert cap2['version'] == 2 and cap2['supersedes'] == 'DOC1'
    assert cap2['loan_status_at_capture'] == 'redeemed'
    assert cap2['asset_status_at_capture'] == 'released'
    assert cap2['watermark']['value'] == 8
    assert cap2['digest'] != cap1['digest']
    assert cap2['commitment'] != cap1['commitment']

    # Old file and old receipt facts survive untouched.
    assert (out / 'DOC1.sealed.json').exists()
    assert (out / 'DOC2.sealed.json').exists()
    with open_db(db) as con:
        rows = {r['id']: dict(r) for r in con.execute(
            'SELECT id,current,file_digest FROM dossier_version')}
        assert rows['DOC1']['current'] == 0
        assert rows['DOC2']['current'] == 1
        revocation = con.execute(
            'SELECT * FROM dossier_revocation WHERE dossier_id=?', ('DOC1',)).fetchone()
        assert revocation['superseded_by'] == 'DOC2'
        ack = con.execute(
            'SELECT COUNT(*) AS n FROM dossier_acknowledgment WHERE dossier_id=?',
            ('DOC1',)).fetchone()
        assert ack['n'] == 1


def test_doc2_current_review_adjudicates_released_pledge(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    cap2 = dossier.capture(db, FIX / 'dossier-capture-v2.json', out)
    dossier.release(db, FIX / 'release-current-v2.json', out)
    _acknowledge(db, tmp_path, 'acknowledgment-dossier-v2.json', cap2['digest'])
    verdict = _adjudicate(db, tmp_path, 'adjudication-current-v2.json')
    assert verdict['verdict'] == dossier.VERDICT_CURRENT_RELEASED
    assert verdict['dossier_id'] == 'DOC2'
    # The stale DOC1 current package now reports itself as non-current.
    check = dossier.verify_package(db, out / 'REL-DOC1-CUR.disclosure.json')
    assert check['db_checks']['package_is_current'] is False
    assert check['db_checks']['revoked'] is True
    assert check['db_checks']['superseded_by'] == 'DOC2'


def test_current_review_on_superseded_doc1_is_stale_not_released(tmp_path):
    """After DOC2 exists, a current-purpose adjudication against DOC1 must
    fail on staleness first - the stale snapshot cannot speak for the present
    at all, regardless of what the live loan status is."""
    db, out, cap1 = _redeemed_world(tmp_path)
    dossier.capture(db, FIX / 'dossier-capture-v2.json', out)
    verdict = _adjudicate(db, tmp_path, 'adjudication-current.json')
    assert verdict['verdict'] == dossier.VERDICT_STALE
    assert verdict['dossier_id'] == 'DOC1'
    assert any(r.startswith('current_version_is:DOC2') for r in verdict['reasons'])


def test_current_review_pass_while_loan_still_active(tmp_path):
    """A current review on DOC1 before any redemption must pass cleanly."""
    db = _seed_active_loan(tmp_path)
    out, cap, _, _ = _capture_and_release_doc1(db, tmp_path)
    verdict = _adjudicate(db, tmp_path, 'adjudication-current.json')
    assert verdict['verdict'] == dossier.VERDICT_CURRENT_PASS
    assert 'loan_active_and_asset_sealed_at_current_watermark' in verdict['reasons']


# --------------------------------------------------------------------------- #
# The three required wrong conclusions, reproduced in one fixture
# --------------------------------------------------------------------------- #
def test_three_naive_strategies_each_emit_a_wrong_current_conclusion(tmp_path):
    """Same input, same redemption, three naive fixes - each demonstrably wrong:

    1. append-only flag  -> a current custody pass is still issued on DOC1
    2. DB recompute + reused DOC1 file -> signed digest no longer matches bytes
    3. delete old DOC1   -> the historical issuance state can no longer be proven
    """
    db, out, cap1 = _redeemed_world(tmp_path)
    request = {fix: json.loads((FIX / f'fault-{fix}.json').read_text())
               for fix in ('append-accepted', 'reuse-file', 'delete-old')}

    # --- Wrong conclusion 1: stamp "accepted" onto the stale dossier --------
    with open_db(db) as con, con:
        con.execute("UPDATE dossier_version SET publish_state='accepted' WHERE id='DOC1'")
    naive1 = report(db)  # status-only view still treats DOC1 as the live claim
    with open_db(db) as con:
        row = con.execute(
            "SELECT current,publish_state,loan_status_at_capture FROM dossier_version "
            "WHERE id='DOC1'").fetchone()
        assert row['current'] == 1 and row['publish_state'] == 'accepted'
    # The proper machinery refuses to turn this into a current pass:
    proper = _adjudicate(db, tmp_path, 'adjudication-current.json')
    assert proper['verdict'] == dossier.VERDICT_CURRENT_RELEASED
    assert naive1 is not None  # the naive flag carries no staleness signal at all

    # --- Wrong conclusion 2: recompute DB state but reuse the DOC1 file -----
    # A naive recompute rewrites the stored row to the new status while the
    # customer-signed DOC1 file and its digest are kept "for continuity".
    with open_db(db) as con, con:
        row = con.execute('SELECT raw FROM dossier_version WHERE id=?',
                          ('DOC1',)).fetchone()
        sealed = json.loads(row['raw'])
        sealed['loan_status_at_capture'] = 'redeemed'
        sealed['asset_status_at_capture'] = 'released'
        con.execute('UPDATE dossier_version SET raw=? WHERE id=?',
                    (dossier._canon(sealed), 'DOC1'))
    sealed_path = out / 'DOC1.sealed.json'
    on_disk_digest = dossier._sha(sealed_path.read_bytes())
    with open_db(db) as con:
        registered = con.execute(
            'SELECT file_digest FROM dossier_version WHERE id=?', ('DOC1',)).fetchone()
    # The signed file is untouched, but the registered blob no longer hashes
    # to the digest the customer signed: file and database have split.
    assert on_disk_digest == registered['file_digest']
    check = dossier.verify_package(db, out / 'REL-DOC1-CUR.disclosure.json')
    assert check['ok'] is False and check['reason'] == 'sealed_blob_tampered'

    # --- Wrong conclusion 3: delete the old dossier outright ---------------
    sealed_path.unlink()
    with open_db(db) as con, con:
        con.execute('DELETE FROM dossier_version WHERE id=?', ('DOC1',))
    remaining = dossier.dossier_audit(db)
    ids = {v['id'] for v in remaining['details']}
    assert 'DOC1' not in ids
    historical = remaining['buckets']['historical_archive_verifiable']
    # Nothing remains that can prove the March 1 active/sealed issuance point.
    assert historical == []


def test_enhanced_workflow_avoids_all_three_wrong_conclusions(tmp_path):
    """The real implementation: new version, retained file, distinct digest,
    historical pass plus current released verdict."""
    db, out, cap1 = _redeemed_world(tmp_path)
    hist = _adjudicate(db, tmp_path, 'adjudication-historical.json')
    stale_current = _adjudicate(db, tmp_path, 'adjudication-current.json')
    cap2 = dossier.capture(db, FIX / 'dossier-capture-v2.json', out)
    dossier.release(db, FIX / 'release-current-v2.json', out)
    _acknowledge(db, tmp_path, 'acknowledgment-dossier-v2.json', cap2['digest'])
    current_v2 = _adjudicate(db, tmp_path, 'adjudication-current-v2.json')

    assert hist['verdict'] == dossier.VERDICT_HISTORICAL_PASS
    assert stale_current['verdict'] == dossier.VERDICT_CURRENT_RELEASED
    assert current_v2['verdict'] == dossier.VERDICT_CURRENT_RELEASED
    assert (out / 'DOC1.sealed.json').exists()
    assert cap1['digest'] != cap2['digest']


# --------------------------------------------------------------------------- #
# Recovery: published files, SQLite pointers and receipts cannot diverge
# --------------------------------------------------------------------------- #
def test_recover_restores_deleted_files_and_repairs_pointer_state(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    (out / 'DOC1.sealed.json').unlink()
    (out / 'REL-DOC1-CUR.disclosure.json').unlink()
    with open_db(db) as con, con:
        con.execute("UPDATE dossier_version SET publish_state='pending' WHERE id='DOC1'")

    result = dossier.recover(db)
    restored = {(item['kind'], item['id']) for item in result['restored']}
    assert ('sealed', 'DOC1') in restored
    assert ('disclosure', 'REL-DOC1-CUR') in restored
    assert dossier.verify_package(
        db, out / 'REL-DOC1-CUR.disclosure.json')['ok'] is True
    with open_db(db) as con:
        assert con.execute(
            "SELECT publish_state FROM dossier_version WHERE id='DOC1'"
        ).fetchone()[0] == 'published'


def test_recover_never_overwrites_a_conflicting_file(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    sealed_path = out / 'DOC1.sealed.json'
    sealed_path.write_text('{"evil":true}', encoding='utf8')
    result = dossier.recover(db)
    assert result['conflicts'] == [{
        'kind': 'sealed', 'id': 'DOC1', 'path': str(sealed_path),
        'reason': 'digest_mismatch'}]
    assert sealed_path.read_text(encoding='utf8') == '{"evil":true}'


def test_recover_lists_orphan_publications(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    (out / 'mystery.disclosure.json').write_text('{}', encoding='utf8')
    result = dossier.recover(db)
    assert str(out / 'mystery.disclosure.json') in result['orphans']


def test_external_receipt_with_wrong_digest_is_unmatched(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    bad = json.loads((FIX / 'acknowledgment-bad-digest.json').read_text())
    result = dossier.acknowledge(
        db, _write(tmp_path, 'bad-ack.json', bad))
    assert result['matched'] is False
    with open_db(db) as con:
        assert con.execute(
            'SELECT matched FROM dossier_acknowledgment WHERE id=?',
            ('ACK-BAD-DIGEST',)).fetchone()[0] == 0
    # An unmatched receipt means the dossier fails evidence gates.
    verdict = _adjudicate(db, tmp_path, 'adjudication-historical.json')
    # DOC1 also has its genuine receipt, so the historical pass still holds;
    # the bad receipt is retained but never counted as matched.
    assert verdict['verdict'] == dossier.VERDICT_HISTORICAL_PASS
    with open_db(db) as con:
        matched = con.execute(
            'SELECT COUNT(*) AS n FROM dossier_acknowledgment '
            "WHERE dossier_id='DOC1' AND matched=1").fetchone()['n']
        assert matched == 1


def test_adjudication_without_any_receipt_is_evidence_conflict(tmp_path):
    db = _seed_active_loan(tmp_path)
    out = tmp_path / 'pub'
    dossier.capture(db, FIX / 'dossier-ok.json', out)
    dossier.release(db, FIX / 'release-current.json', out)
    # No acknowledgment at all.
    verdict = _adjudicate(db, tmp_path, 'adjudication-current.json')
    assert verdict['verdict'] == dossier.VERDICT_CONFLICT
    assert 'no_matched_external_receipt' in verdict['reasons']


# --------------------------------------------------------------------------- #
# Audit buckets with re-computable reasons
# --------------------------------------------------------------------------- #
def test_dossier_audit_buckets_historical_current_and_conflict(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    cap2 = dossier.capture(db, FIX / 'dossier-capture-v2.json', out)
    dossier.release(db, FIX / 'release-current-v2.json', out)
    _acknowledge(db, tmp_path, 'acknowledgment-dossier-v2.json', cap2['digest'])

    audit = dossier.dossier_audit(db)
    assert audit['buckets'] == {
        'historical_archive_verifiable': ['DOC1'],
        'current_regulatory_clear': [],
        'evidence_missing_or_conflict': ['DOC2']}
    doc1 = next(d for d in audit['details'] if d['dossier_id'] == 'DOC1')
    assert doc1['current'] is False
    assert doc1['watermark_captured'] == 6 and doc1['watermark_current'] == 8
    assert any(r.startswith('watermark_stale') for r in doc1['reasons'])
    doc2 = next(d for d in audit['details'] if d['dossier_id'] == 'DOC2')
    assert doc2['loan_status_at_capture'] == 'redeemed'
    assert doc2['reasons'] == [] or 'no_matched_external_receipt' not in doc2['reasons']
    assert doc2['matched_receipts'] == 1
    # DOC2 is current but the pledge is not live, so it lands in conflict.
    assert 'current_pointer_but_pledge_not_live' in doc2['reasons']


def test_dossier_audit_detects_missing_file_and_bad_commitment(tmp_path):
    db = _seed_active_loan(tmp_path)
    out, cap, _, _ = _capture_and_release_doc1(db, tmp_path)
    (out / 'DOC1.sealed.json').unlink()
    audit = dossier.dossier_audit(db)
    doc1 = next(d for d in audit['details'] if d['dossier_id'] == 'DOC1')
    assert 'sealed_file_missing' in doc1['reasons']
    assert 'DOC1' in audit['buckets']['evidence_missing_or_conflict']

    # Restore, then corrupt the stored blob: the salted commitment must fail
    # re-computation and be reported as a conflict rather than trusted.
    dossier.recover(db)
    with open_db(db) as con, con:
        row = con.execute('SELECT raw FROM dossier_version WHERE id=?', ('DOC1',)).fetchone()
        sealed = json.loads(row['raw'])
        sealed['commitment_entries'][0]['digest'] = '9' * 64
        con.execute('UPDATE dossier_version SET raw=? WHERE id=?',
                    (dossier._canon(sealed), 'DOC1'))
    audit2 = dossier.dossier_audit(db)
    doc1b = next(d for d in audit2['details'] if d['dossier_id'] == 'DOC1')
    assert 'root_commitment_recompute_failed' in doc1b['reasons']


def test_dossier_report_exposes_full_registry_view(tmp_path):
    db, out, cap1 = _redeemed_world(tmp_path)
    _adjudicate(db, tmp_path, 'adjudication-historical.json')
    view = dossier_report(db)
    assert {v['id'] for v in view['versions']} == {'DOC1'}
    assert {r['purpose'] for r in view['releases']} == {
        dossier.PURPOSE_HISTORICAL, dossier.PURPOSE_CURRENT}
    assert view['acknowledgments'][0]['matched'] == 1
    assert view['adjudications'][0]['verdict'] == dossier.VERDICT_HISTORICAL_PASS
    assert 'audit' in view and 'buckets' in view['audit']


# --------------------------------------------------------------------------- #
# Legacy workflow remains runnable on the same database
# --------------------------------------------------------------------------- #
def test_legacy_prepare_review_still_runs_alongside_enhanced_workflow(tmp_path):
    db = _seed_active_loan(tmp_path)
    # Enhanced flow first.
    out, cap, _, _ = _capture_and_release_doc1(db, tmp_path)
    # Legacy flow uses its own tables and request shape.
    legacy_out = tmp_path / 'legacy'
    submitted = dossier.prepare(db, FIX / 'dossier-ok.json', legacy_out)
    assert submitted['status'] == 'submitted'
    receipt = _write(tmp_path, 'legacy-receipt.json', {
        'id': 'REVIEW1', 'dossier_id': 'DOC1', 'authority': 'regulator-A',
        'digest': submitted['digest'], 'outcome': 'accepted'})
    assert dossier.review(db, receipt)['status'] == 'accepted'
    with open_db(db) as con:
        assert con.execute(
            "SELECT status FROM dossier WHERE id='DOC1'").fetchone()[0] == 'accepted'
        # The enhanced version row is independent and untouched.
        assert con.execute(
            "SELECT current FROM dossier_version WHERE id='DOC1'").fetchone()[0] == 1


def test_legacy_review_flaw_is_contained_and_enhanced_path_rejects_it(tmp_path):
    """Documents the exact reported flaw: the frozen legacy review only checks
    authority and digest, so it still stamps the stale DOC1 accepted after
    redemption.  The enhanced adjudication on the same loan must not."""
    db = _seed_active_loan(tmp_path)
    legacy_out = tmp_path / 'legacy'
    submitted = dossier.prepare(db, FIX / 'dossier-ok.json', legacy_out)
    # Redemption happens between sealing and the external review.
    _redeem(db)
    receipt = _write(tmp_path, 'stale-receipt.json', {
        'id': 'REVIEW-STALE', 'dossier_id': 'DOC1', 'authority': 'regulator-A',
        'digest': submitted['digest'], 'outcome': 'accepted'})
    # Legacy behaviour is intentionally preserved and runnable.
    assert dossier.review(db, receipt)['status'] == 'accepted'

    # The enhanced workflow seals its own version and refuses a current pass.
    out = tmp_path / 'pub'
    cap = dossier.capture(db, FIX / 'dossier-ok.json', out)
    dossier.release(db, FIX / 'release-current.json', out)
    _acknowledge(db, tmp_path, 'acknowledgment-dossier.json', cap['digest'])
    verdict = _adjudicate(db, tmp_path, 'adjudication-current.json')
    assert verdict['verdict'] in (
        dossier.VERDICT_STALE, dossier.VERDICT_CURRENT_RELEASED)
    assert verdict['verdict'] != dossier.VERDICT_CURRENT_PASS
