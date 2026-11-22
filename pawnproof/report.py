from .store import open_db
from .evidence import snapshot


def report(path):
    with open_db(path) as db:
        state = snapshot(db)
        for row in state['loan']:
            paid = sum(x['amount_cents'] for x in state['payment']
                       if x['loan_id'] == row['id'])
            row['outstanding_cents'] = row['principal_cents'] + row['interest_cents'] - paid
        result = {'state': state,
                  'source_files': [{'id': x['id'], 'kind': x['kind'], 'sha': x['sha']}
                                   for x in db.execute(
                                       'SELECT id,kind,sha FROM source_file ORDER BY id')]}
        # Ordinary counter view: completed disposal never exposes originals.
        disposals = _disposal_summary(db)
        if disposals:
            result['privacy_disposals'] = disposals
        return result


def _disposal_summary(db):
    if not _table(db, 'privacy_disposal'):
        return []
    out = []
    for row in db.execute('SELECT * FROM privacy_disposal ORDER BY request_id'):
        out.append({'request_id': row['request_id'], 'job_id': row['job_id'],
                    'state': row['state'], 'route': row['route'],
                    'identity_recoverable': bool(row['identity_recoverable']),
                    'completed_at': row['completed_at'], 'reason': row['reason']})
    return out


def _table(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def audit(path):
    """Tiered audit.

    On an undisposed ledger this is the original byte-for-byte replay: every
    ``source_file.raw`` is replayed into a fresh database and the full snapshot
    must match, yielding ``replay_ok=True``.

    After a privacy disposal the raw identity bytes are gone by design.
    Returning a bare ``replay_ok`` then would hide deleted fields, so the
    report degrades explicitly: ``replay_ok`` only covers what still exists,
    and a ``privacy_disposal`` block records which sources were redacted,
    whether originals are recoverable, whether evidence commitments verify,
    and whether a *full* original replay is even possible.
    """
    original = report(path)
    with open_db(path) as db:
        files = [(x['kind'], x['raw']) for x in
                 db.execute('SELECT kind,raw FROM source_file ORDER BY id')]
        has_disposal = _table(db, 'privacy_disposal') and \
            db.execute('SELECT count(*) FROM privacy_disposal').fetchone()[0]
        disposal = _audit_disposal(db) if has_disposal else None
    from tempfile import TemporaryDirectory
    from pathlib import Path
    from . import asset, vault, loan
    funcs = {'asset': asset.import_asset, 'ownership': asset.ownership,
             'appraisal': asset.appraise, 'vault': vault.import_vault,
             'custody': vault.custody, 'loan': loan.originate,
             'payment': loan.pay, 'extension': loan.extend,
             'notice': loan.notice, 'decision': loan.decide}
    replay_error = None
    with TemporaryDirectory() as tmp:
        testdb = Path(tmp) / 'replay.sqlite'
        try:
            for i, (kind, raw) in enumerate(files):
                if kind not in funcs:  # privacy artefacts are not replay inputs
                    continue
                file = Path(tmp) / f'{i}.json'
                file.write_bytes(raw)
                funcs[kind](testdb, file)
            fresh_full = report(testdb)
            # Disposal bookkeeping lives only in the disposed database; the
            # replay comparison covers the business ledger and source hashes.
            fresh = {k: v for k, v in fresh_full.items()
                     if k != 'privacy_disposals'}
            original_cmp = {k: v for k, v in original.items()
                            if k != 'privacy_disposals'}
            replay_ok = fresh == original_cmp
        except ValueError as exc:
            fresh = None
            replay_ok = False
            replay_error = str(exc)
    result = {'replay_ok': replay_ok, 'events': len(files)}
    if replay_error:
        result['replay_error'] = replay_error
    if disposal is not None:
        result['privacy_disposal'] = disposal
        # A full replay of the *original* ledger is only possible when every
        # redacted source still exists inside a sealed dual-authority package.
        result['full_original_replay_possible'] = (
            replay_ok and disposal['identity_recoverable']
            and not disposal['replay_mismatches'])
        result['evidence_commitments_verify'] = disposal['commitments_verify']
        # Explicitly refuse to mask deleted fields: if any redacted source
        # exists, replay alone can never be claimed as the full audit.
        if disposal['redacted_sources'] and not disposal['identity_recoverable']:
            result['replay_ok'] = False
    return result


def _audit_disposal(db):
    import hashlib
    items = [dict(x) for x in db.execute(
        'SELECT * FROM privacy_disposal ORDER BY request_id')]
    redacted_sources = []
    mismatches = []
    if _table(db, 'identity_commitment'):
        for com in db.execute('SELECT * FROM identity_commitment ORDER BY source_id'):
            live = db.execute('SELECT sha,raw FROM source_file WHERE id=?',
                              (com['source_id'],)).fetchone()
            redacted_sources.append(com['source_id'])
            if not live or live['sha'] != com['redacted_sha']:
                mismatches.append({'source_id': com['source_id'],
                                   'expected_sha': com['redacted_sha'],
                                   'live_sha': live['sha'] if live else None})
            elif hashlib.sha256(live['raw']).hexdigest() != live['sha']:
                mismatches.append({'source_id': com['source_id'],
                                   'reason': 'stored_sha_mismatch'})
    packages = []
    if _table(db, 'custody_package'):
        packages = [{'job_id': x['job_id'], 'status': x['status'],
                     'blob_sha': x['blob_sha'],
                     'custodian_a': x['custodian_a'],
                     'custodian_b': x['custodian_b']}
                    for x in db.execute('SELECT * FROM custody_package')]
    copy_ledger = []
    if _table(db, 'distribution_copy'):
        copy_ledger = [{'copy_id': x['copy_id'], 'medium': x['medium'],
                        'status': x['status'], 'attestation': x['attestation']}
                       for x in db.execute(
                           'SELECT copy_id,medium,status,attestation '
                           'FROM distribution_copy ORDER BY copy_id')]
    identity_recoverable = all(x['identity_recoverable'] for x in items)
    return {
        'jobs': [{'request_id': x['request_id'], 'state': x['state'],
                  'route': x['route'], 'reason': x['reason']} for x in items],
        'identity_recoverable': identity_recoverable,
        'redacted_sources': redacted_sources,
        'replay_mismatches': mismatches,
        'commitments_verify': not mismatches,
        'custody_packages': packages,
        'distribution_copies': copy_ledger,
        'plaintext_copies_outstanding': sum(
            1 for x in copy_ledger if x['status'] == 'plaintext'),
    }
