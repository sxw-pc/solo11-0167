"""Privacy disposal: field-level erasure, locked retention routes,
cross-media recovery and tiered audit behaviour."""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pawnproof import asset, vault, loan, privacy, disposal
from pawnproof.crypto import seal_envelope, open_envelope, split_secret, combine_secret
from pawnproof.evidence import (
    FIELD_BASIS,
    derive_pid,
    fingerprint_customer,
    scan_identity_bytes,
)
from pawnproof.report import audit, report
from pawnproof.store import open_db

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
DF = FIX / 'disposal'


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _import_c1_ledger(db):
    for func, name in [
        (asset.import_asset, 'asset-ok.json'),
        (asset.ownership, 'ownership-ok.json'),
        (asset.appraise, 'appraisal-ok.json'),
        (vault.import_vault, 'vault-ok.json'),
        (vault.custody, 'custody-ok.json'),
        (loan.originate, 'loan-ok.json'),
    ]:
        func(db, FIX / name)
    privacy.request(db, FIX / 'privacy-ok.json')
    loan.pay(db, FIX / 'payment-ok.json')
    loan.decide(db, FIX / 'decision-ok.json')
    assert privacy.review(db, 'PR1')['status'] == 'approved'


def _import_c2_active(db):
    for func, name in [
        (asset.import_asset, 'asset-c2.json'),
        (asset.ownership, 'ownership-c2.json'),
        (asset.appraise, 'appraisal-c2.json'),
        (vault.import_vault, 'vault-c2.json'),
        (vault.custody, 'custody-c2.json'),
        (loan.originate, 'loan-c2.json'),
    ]:
        func(db, DF / name)


def _media(tmp_path, db):
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()
    backup = backup_dir / 'batch-a.sqlite'
    shutil.copy2(db, backup)
    dossier = tmp_path / 'exports' / 'dossier-7'
    dossier.mkdir(parents=True)
    for name in ('asset-ok.json', 'loan-ok.json', 'payment-ok.json'):
        shutil.copy2(FIX / name, dossier / name)
    return backup, dossier


def _plan_and_run(tmp_path, route, with_c2=False, vault_dir=None):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    if with_c2:
        _import_c2_active(db)
    with open_db(db) as con:
        c2_before = fingerprint_customer(con, 'C2') if with_c2 else None
    backup, dossier = _media(tmp_path, db)
    plan = disposal.plan(
        db, 'PR1', route,
        manifest_file=DF / 'backup-manifest.json', media_root=tmp_path)
    assert plan['status'] == 'planned'
    result = disposal.execute(db, 'PR1', vault_dir=vault_dir)
    assert result['status'] == 'completed'
    return db, backup, dossier, c2_before


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------

def test_envelope_and_secret_split_round_trip():
    blob, key = seal_envelope(b'identity:C1')
    assert open_envelope(blob, key) == b'identity:C1'
    share_a, share_b = split_secret(key)
    assert share_a != key and share_b != key
    assert combine_secret(share_a, share_b) == key
    with pytest.raises(ValueError, match='package_tampered_or_wrong_key'):
        open_envelope(blob, bytes(32))
    with pytest.raises(ValueError, match='share_length_mismatch'):
        combine_secret(share_a, share_b[:-1])


def test_pseudonym_is_irreversible_and_stable():
    pid = derive_pid('C1', 'salt-1234')
    assert pid.startswith('PID-') and pid != 'C1'
    assert derive_pid('C1', 'salt-1234') == pid
    assert derive_pid('C1', 'other-salt') != pid
    assert derive_pid('C2', 'salt-1234') != pid


# ---------------------------------------------------------------------------
# Planning: field-level dependency inventory
# ---------------------------------------------------------------------------

def test_plan_builds_three_class_field_inventory(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    view = disposal.plan(db, 'PR1', 'minimal_retention')
    assert view['status'] == 'planned'
    detail = disposal.verify(db, 'PR1')
    classes = {row['classification'] for row in detail['field_inventory']}
    assert classes == {'historical_proof', 'no_retention_basis'}
    # Identity-bearing raw bytes have no ordinary retention basis.
    raw_rows = [r for r in detail['field_inventory']
                if r['table_name'] == 'source_file']
    assert raw_rows and all(r['classification'] == 'no_retention_basis'
                            for r in raw_rows)
    # Amounts and moments survive as historical proof.
    amount_rows = [r for r in detail['field_inventory']
                   if r['table_name'] == 'payment' and r['column_name'] == 'amount_cents']
    assert amount_rows and all(r['classification'] == 'historical_proof'
                               for r in amount_rows)
    assert all(r['basis'] for r in detail['field_inventory'])


def test_plan_requires_approved_request_and_locks_route(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    for func, name in [
        (asset.import_asset, 'asset-ok.json'),
        (asset.ownership, 'ownership-ok.json'),
        (asset.appraise, 'appraisal-ok.json'),
        (vault.import_vault, 'vault-ok.json'),
        (vault.custody, 'custody-ok.json'),
        (loan.originate, 'loan-ok.json'),
    ]:
        func(db, FIX / name)
    privacy.request(db, FIX / 'privacy-ok.json')
    # Still active: review is held and planning must refuse execution.
    assert privacy.review(db, 'PR1')['status'] == 'held'
    with pytest.raises(ValueError, match='request_not_approved'):
        disposal.plan(db, 'PR1', 'minimal_retention')
    loan.pay(db, FIX / 'payment-ok.json')
    loan.decide(db, FIX / 'decision-ok.json')
    assert privacy.review(db, 'PR1')['status'] == 'approved'
    disposal.plan(db, 'PR1', 'minimal_retention')
    with pytest.raises(ValueError, match='route_locked_cannot_change'):
        disposal.plan(db, 'PR1', 'controlled_retention')


def test_active_pledge_blocks_with_active_required_classification(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    # C1 ledger without redemption: loan stays active.
    for func, name in [
        (asset.import_asset, 'asset-ok.json'),
        (asset.ownership, 'ownership-ok.json'),
        (asset.appraise, 'appraisal-ok.json'),
        (vault.import_vault, 'vault-ok.json'),
        (vault.custody, 'custody-ok.json'),
        (loan.originate, 'loan-ok.json'),
    ]:
        func(db, FIX / name)
    privacy.request(db, FIX / 'privacy-ok.json')
    privacy.review(db, 'PR1')
    with open_db(db) as con, con:
        con.execute("UPDATE privacy_request SET status='approved' WHERE id='PR1'")
    with pytest.raises(ValueError, match='active_pledge_blocks_disposal'):
        disposal.plan(db, 'PR1', 'minimal_retention')
    detail = disposal.verify(db, 'PR1')
    assert detail['status'] == 'blocked'
    active_rows = [r for r in detail['field_inventory']
                   if r['classification'] == 'active_pledge_required']
    assert active_rows
    assert all(r['basis'] == FIELD_BASIS['active_pledge_required']
               for r in active_rows)


# ---------------------------------------------------------------------------
# Minimal retention route
# ---------------------------------------------------------------------------

def test_minimal_route_removes_ordinary_access_keeps_verification(tmp_path):
    db, backup, dossier, _ = _plan_and_run(tmp_path, 'minimal_retention')
    detail = disposal.verify(db, 'PR1')
    assert detail['state'] == 'completed'
    assert detail['identity_recoverable'] is False
    assert detail['ordinary_access_removed'] is True
    # No identity value anywhere in the main database.
    with open_db(db) as con:
        assert not scan_identity_bytes(con, 'C1')
        loan_row = con.execute("SELECT * FROM loan WHERE id='L1'").fetchone()
        assert loan_row['customer'].startswith('PID-')
        owner = con.execute("SELECT owner FROM asset WHERE asset_id='G1'").fetchone()[0]
        assert owner == loan_row['customer']
    # Ordinary report shows pseudonyms only in every identity-bearing column.
    rendered_state = report(db)['state']
    identity_values = set()
    for table, column in (('asset', 'owner'), ('ownership', 'claimant'),
                          ('loan', 'customer'), ('extension', 'customer_actor'),
                          ('notice', 'recipient')):
        identity_values.update(row[column] for row in rendered_state[table])
    assert identity_values and all(v.startswith('PID-') for v in identity_values)
    # Amounts, moments and status remain auditable.
    settlement = disposal.regulator_access(db, 'PR1')['verification'][0]
    assert settlement['principal_cents'] == 50000
    assert settlement['interest_cents'] == 2500
    assert settlement['paid_cents'] == 52500
    assert settlement['settled'] is True
    assert settlement['loan_status'] == 'redeemed'
    # Minimal route cannot reconstruct the name under any circumstances.
    access = disposal.regulator_access(db, 'PR1')
    assert access['identity_recoverable'] is False
    assert access['reason'] == 'key_material_destroyed'
    assert access['commitments']


def test_minimal_route_tiered_audit_does_not_fake_full_replay(tmp_path):
    db, *_ = _plan_and_run(tmp_path, 'minimal_retention')
    result = audit(db)
    # Deleted identity fields must not be hidden behind an unconditional ok.
    assert result['replay_ok'] is False
    assert result['full_original_replay_possible'] is False
    block = result['privacy_disposal']
    assert block['identity_recoverable'] is False
    assert block['redacted_sources']
    assert block['commitments_verify'] is True
    assert block['plaintext_copies_outstanding'] == 0


# ---------------------------------------------------------------------------
# Controlled retention route
# ---------------------------------------------------------------------------

def test_controlled_route_seals_identity_behind_dual_custody(tmp_path):
    vault_dir = tmp_path / 'vault'
    db, *_ = _plan_and_run(tmp_path, 'controlled_retention', vault_dir=vault_dir)
    detail = disposal.verify(db, 'PR1')
    assert detail['identity_recoverable'] is True
    assert detail['ordinary_access_removed'] is True
    shares = sorted(p.name for p in vault_dir.iterdir())
    assert shares == ['PR1.package', 'PR1.share-a', 'PR1.share-b']
    # No share alone opens the package.
    with pytest.raises(ValueError, match='dual_authorization_required'):
        disposal.regulator_access(db, 'PR1', share_a_file=vault_dir / 'PR1.share-a')
    with pytest.raises(ValueError, match='custodian_share_mismatch'):
        disposal.regulator_access(
            db, 'PR1', share_a_file=vault_dir / 'PR1.share-a',
            share_b_file=vault_dir / 'PR1.share-a')
    access = disposal.regulator_access(
        db, 'PR1', share_a_file=vault_dir / 'PR1.share-a',
        share_b_file=vault_dir / 'PR1.share-b')
    assert access['identity_recoverable'] is True
    assert access['customer'] == 'C1'
    assert access['authorization'] == 'dual_custodian'
    assert len(access['original_sources']) == 3
    # The access itself lands on the timeline.
    timeline = [e['kind'] for e in disposal.verify(db, 'PR1')['timeline']]
    assert 'regulator_access' in timeline


def test_controlled_route_audit_allows_full_replay_only_via_package(tmp_path):
    vault_dir = tmp_path / 'vault'
    db, *_ = _plan_and_run(tmp_path, 'controlled_retention', vault_dir=vault_dir)
    result = audit(db)
    # Redacted bytes replay consistently into the redacted ledger.
    assert result['replay_ok'] is True
    # Full original replay is possible only because the sealed package exists.
    assert result['full_original_replay_possible'] is True
    assert result['privacy_disposal']['custody_packages'][0]['status'] == 'sealed'


# ---------------------------------------------------------------------------
# Cross-media: SQLite backups and external dossiers
# ---------------------------------------------------------------------------

def test_disposal_refreshes_backup_and_dossier_without_c2_collateral(tmp_path):
    vault_dir = tmp_path / 'vault'
    db, backup, dossier, c2_before = _plan_and_run(
        tmp_path, 'minimal_retention', with_c2=True, vault_dir=vault_dir)
    # Main DB: C1 gone, C2 intact and still active.
    with open_db(db) as con:
        assert not scan_identity_bytes(con, 'C1')
        assert fingerprint_customer(con, 'C2') == c2_before
        c2_loan = dict(con.execute("SELECT * FROM loan WHERE id='L2'").fetchone())
        assert c2_loan['customer'] == 'C2' and c2_loan['status'] == 'active'
    # Shared offline backup batch: same pseudonym, C2 rows byte-semantics kept.
    bcon = sqlite3.connect(backup)
    try:
        assert bcon.execute(
            "SELECT count(*) FROM loan WHERE customer='C2' AND status='active'"
        ).fetchone()[0] == 1
        assert bcon.execute(
            "SELECT count(*) FROM loan WHERE customer LIKE 'PID-%'"
        ).fetchone()[0] == 1
        c2_assets = bcon.execute(
            "SELECT count(*) FROM asset WHERE owner='C2'").fetchone()[0]
        assert c2_assets == 1
    finally:
        bcon.close()
    # External dossier files were rewritten, not deleted.
    for file in sorted(dossier.glob('*.json')):
        doc = json.loads(file.read_text())
        for key in ('owner', 'customer'):
            assert doc.get(key) != 'C1'
    detail = disposal.verify(db, 'PR1')
    assert {c['status'] for c in detail['distribution_copies']} == {'redacted'}
    assert all(c['updated_sha'] and c['updated_sha'] != c['original_sha']
               for c in detail['distribution_copies'])


def test_plaintext_or_missing_distribution_copy_blocks_completion(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    backup, dossier = _media(tmp_path, db)
    disposal.plan(db, 'PR1', 'minimal_retention',
                  manifest_file=DF / 'backup-manifest.json', media_root=tmp_path)
    # Simulate an offsite batch that cannot be refreshed or located.
    backup.unlink()
    with pytest.raises(ValueError, match='distribution_copy_missing'):
        disposal.execute(db, 'PR1')
    detail = disposal.verify(db, 'PR1')
    assert detail['state'] == 'incomplete'
    assert 'distribution_copy_missing' in detail['reason']
    # Main DB untouched while the boundary is open.
    with open_db(db) as con:
        assert scan_identity_bytes(con, 'C1')


def test_unrecoverable_attestation_is_an_explicit_exception(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    backup, dossier = _media(tmp_path, db)
    disposal.plan(db, 'PR1', 'minimal_retention',
                  manifest_file=DF / 'backup-manifest.json', media_root=tmp_path)
    backup.unlink()  # physically destroyed offsite
    disposal.attest_unrecoverable(
        db, 'PR1', 'BATCH-2026-03-A', DF / 'unrecoverable-attestation.json')
    disposal.execute(db, 'PR1', vault_dir=tmp_path / 'vault')
    detail = disposal.verify(db, 'PR1')
    statuses = {c['copy_id']: c['status'] for c in detail['distribution_copies']}
    assert statuses['BATCH-2026-03-A'] == 'unrecoverable'
    assert statuses['EXPORT-DOSSIER-7'] == 'redacted'
    assert detail['status'] == 'completed'
    assert any(e['kind'] == 'copy_exception' for e in detail['timeline'])


# ---------------------------------------------------------------------------
# Crash recovery invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('failpoint', list(disposal.STEPS))
def test_crash_at_every_barrier_resumes_without_split_brain(tmp_path, failpoint):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    _import_c2_active(db)
    with open_db(db) as con:
        c2_before = fingerprint_customer(con, 'C2')
    _media(tmp_path, db)
    disposal.plan(db, 'PR1', 'minimal_retention',
                  manifest_file=DF / 'backup-manifest.json', media_root=tmp_path)
    with pytest.raises(RuntimeError, match='crash_injected_' + failpoint):
        disposal.execute(db, 'PR1', vault_dir=tmp_path / 'vault',
                         failpoint=failpoint)
    # Mid-flight: completion must never be claimed.
    mid = disposal.verify(db, 'PR1')
    assert mid['state'] != 'completed'
    mid_audit = audit(db)
    assert not mid_audit.get('full_original_replay_possible', False)
    # Resume from the recorded barrier.
    disposal.execute(db, 'PR1', vault_dir=tmp_path / 'vault')
    done = disposal.verify(db, 'PR1')
    assert done['state'] == 'completed'
    assert done['identity_recoverable'] is False
    with open_db(db) as con:
        assert not scan_identity_bytes(con, 'C1')
        assert fingerprint_customer(con, 'C2') == c2_before
    # Resuming again is idempotent rather than re-running destructive steps.
    again = disposal.execute(db, 'PR1', vault_dir=tmp_path / 'vault')
    assert again['status'] == 'completed'


def test_crash_after_main_rewrite_cannot_be_claimed_as_full_replay(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    _media(tmp_path, db)
    disposal.plan(db, 'PR1', 'minimal_retention',
                  manifest_file=DF / 'backup-manifest.json', media_root=tmp_path)
    with pytest.raises(RuntimeError):
        disposal.execute(db, 'PR1', vault_dir=tmp_path / 'vault',
                         failpoint='post_verify')
    # The raw identity is already gone but completion is not recorded.
    with open_db(db) as con:
        assert not scan_identity_bytes(con, 'C1')
    detail = disposal.verify(db, 'PR1')
    assert detail['state'] in ('incomplete', 'completing')
    assert detail['status'] != 'completed'
    result = audit(db)
    assert result['replay_ok'] is False
    assert result['full_original_replay_possible'] is False
    disposal.execute(db, 'PR1', vault_dir=tmp_path / 'vault')
    assert disposal.verify(db, 'PR1')['state'] == 'completed'


# ---------------------------------------------------------------------------
# Timeline events and irreversibility
# ---------------------------------------------------------------------------

def test_withdrawal_and_legal_hold_block_but_dispute_is_recorded(tmp_path):
    for event_file, expected in [
        ('withdrawal.json', 'withdrawal_blocks_disposal'),
        ('legal-hold.json', 'legal_hold_blocks_disposal'),
    ]:
        tmp = tmp_path / event_file
        tmp.mkdir()
        db = tmp / 'pawn.sqlite'
        _import_c1_ledger(db)
        _media(tmp, db)
        disposal.plan(db, 'PR1', 'minimal_retention',
                      manifest_file=DF / 'backup-manifest.json', media_root=tmp)
        event = json.loads((DF / event_file).read_text())
        disposal.record_event(db, 'PR1', event['kind'], event['at'],
                              event['actor'], event['statement'])
        with pytest.raises(ValueError, match=expected):
            disposal.execute(db, 'PR1')
        detail = disposal.verify(db, 'PR1')
        assert detail['state'] == 'incomplete'
        assert any(e['kind'] == event['kind'] for e in detail['timeline'])
    # A dispute after settlement is recorded at disposal time but not a block.
    tmp = tmp_path / 'dispute'
    tmp.mkdir()
    db = tmp / 'pawn.sqlite'
    _import_c1_ledger(db)
    _media(tmp, db)
    disposal.plan(db, 'PR1', 'minimal_retention',
                  manifest_file=DF / 'backup-manifest.json', media_root=tmp)
    event = json.loads((DF / 'dispute.json').read_text())
    disposal.record_event(db, 'PR1', event['kind'], event['at'],
                          event['actor'], event['statement'])
    disposal.execute(db, 'PR1', vault_dir=tmp / 'vault')
    detail = disposal.verify(db, 'PR1')
    assert detail['status'] == 'completed'
    assert any(e['kind'] == 'dispute' for e in detail['timeline'])


def test_completed_minimal_disposal_cannot_resurrect_identity(tmp_path):
    db, *_ = _plan_and_run(tmp_path, 'minimal_retention')
    # A later legal hold is appended to the timeline but cannot bring C1 back.
    disposal.record_event(db, 'PR1', 'legal_hold',
                          '2026-05-01T00:00:00Z', 'regulator',
                          'late hold after erasure')
    detail = disposal.verify(db, 'PR1')
    assert detail['state'] == 'completed'
    assert detail['identity_recoverable'] is False
    with open_db(db) as con:
        assert not scan_identity_bytes(con, 'C1')
    with pytest.raises(ValueError, match='customer_irrecoverable_minimal_route'):
        with open_db(db) as con:
            disposal._load_customer(con, 'PR1')


# ---------------------------------------------------------------------------
# The three conventional approaches fail on the real DB/backup fixtures
# ---------------------------------------------------------------------------

def test_naive_report_mask_only_leaves_raw_and_copies_plaintext(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    backup, dossier = _media(tmp_path, db)
    result = disposal.demonstrate_report_mask_only(
        db, 'C1', [('sqlite_backup', backup), ('export_dossier', dossier)])
    assert result['public_report_clean'] is True
    media = {item['medium'] for item in result['source_raw_leaks']}
    assert media == {'main_db', 'sqlite_backup', 'export_dossier'}
    # Original ledger and its audit are untouched by the demonstration.
    assert audit(db)['replay_ok'] is True


def test_naive_column_update_diverges_from_raw_replay(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    clone = disposal.clone_db(db, tmp_path / 'column-only.sqlite')
    pid = derive_pid('C1', 'naive-salt-1234')
    result = disposal.demonstrate_update_only(clone, 'C1', pid)
    assert result['columns_rewritten'] is True
    assert result['raw_sources_still_plaintext']
    assert result['replay_restores_identity'] is True
    assert result['audit_replay_ok'] is False
    # Replaying an original raw source restores C1 into a fresh table.
    from pawnproof.evidence import customer_sources
    with open_db(clone) as con:
        plain = customer_sources(con, 'C1')
        assert {s['kind'] for s in plain} == {'asset', 'ownership', 'loan'}


def test_naive_delete_raw_breaks_replay_and_orphans_payment_evidence(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    _import_c1_ledger(db)
    clone = disposal.clone_db(db, tmp_path / 'delete-only.sqlite')
    result = disposal.demonstrate_delete_raw_only(clone, 'C1')
    assert result['raw_sources_deleted']
    assert result['payment_rows_orphaned'] == 1
    assert result['identity_still_in_tables']  # columns still name C1
    assert result['audit_replay_ok'] is False
    assert result['replay_error']  # replay cannot rebuild the ledger
    # The genuine database still replays fully.
    assert audit(db)['replay_ok'] is True


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def test_cli_privacy_disposal_flow(tmp_path):
    env = {**__import__('os').environ,
           'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}
    db = tmp_path / 'cli.sqlite'
    code = (
        "from pawnproof import asset,vault,loan,privacy,disposal;"
        "from pathlib import Path;"
        "import shutil;"
        f"FIX=Path(r'{FIX}');DF=FIX/'disposal';db=Path(r'{db}');root=Path(r'{tmp_path}');"
        "imports=[('asset-ok.json',asset.import_asset),('ownership-ok.json',asset.ownership),"
        "('appraisal-ok.json',asset.appraise),('vault-ok.json',vault.import_vault),"
        "('custody-ok.json',vault.custody),('loan-ok.json',loan.originate)];"
        "[fn(db,FIX/n) for n,fn in imports];"
        "privacy.request(db,FIX/'privacy-ok.json');loan.pay(db,FIX/'payment-ok.json');"
        "loan.decide(db,FIX/'decision-ok.json');privacy.review(db,'PR1');"
        "(root/'backups').mkdir();shutil.copy2(db,root/'backups'/'batch-a.sqlite');"
        "d=root/'exports'/'dossier-7';d.mkdir(parents=True);"
        "[shutil.copy2(FIX/n,d/n) for n in ('asset-ok.json','loan-ok.json','payment-ok.json')]"
    )
    setup = subprocess.run([sys.executable, '-c', code], env=env,
                           capture_output=True, text=True)
    assert setup.returncode == 0, setup.stderr

    def run(*args):
        return subprocess.run(
            [sys.executable, '-B', '-m', 'pawnproof', *args],
            env=env, capture_output=True, text=True)

    plan = run('privacy-plan', 'PR1', '--db', str(db),
               '--route', 'minimal_retention',
               '--manifest', str(DF / 'backup-manifest.json'),
               '--media-root', str(tmp_path))
    assert plan.returncode == 0, plan.stderr
    executed = run('privacy-execute', 'PR1', '--db', str(db),
                   '--vault-dir', str(tmp_path / 'vault'))
    assert executed.returncode == 0, executed.stderr
    verified = run('privacy-verify', 'PR1', '--db', str(db))
    body = json.loads(verified.stdout)
    assert body['state'] == 'completed'
    assert body['identity_recoverable'] is False
    audited = run('audit', '--db', str(db))
    audit_body = json.loads(audited.stdout)
    assert audit_body['full_original_replay_possible'] is False
    # Original commands still work on a fresh demo database.
    demo = run('demo', '--db', str(tmp_path / 'demo.sqlite'))
    assert demo.returncode == 0, demo.stderr
    assert json.loads(demo.stdout)['audit']['replay_ok'] is True
