"""Executable privacy disposal jobs.

A privacy *request* (:mod:`pawnproof.privacy`) only decides whether the
customer may withdraw ordinary access.  This module performs the actual,
partly irreversible field-level disposal and proves what remains auditable.

Two pre-locked routes, both removing ordinary counter access:

* ``controlled_retention`` – identity originals are sealed into an offline
  package protected by a 2-of-2 custodian split; ordinary reports only show an
  irreversible pseudonym; a regulator can recover the original owner only with
  both custodians present.
* ``minimal_retention`` – the decryption material is destroyed; only
  irreversible commitments and verification data survive.  Settlement can be
  verified, the customer's name cannot be reconstructed.

The job is a crash-recoverable state machine.  The main-database rewrite is a
single SQLite transaction; every other step is idempotent and recorded in
``disposal_step`` so a restart never produces "report says deleted, raw still
public" or "raw deleted, audit pretends the full replay exists".
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import crypto
from .evidence import (
    FIELD_BASIS,
    HISTORICAL_PROOF_COLUMNS,
    at,
    customer_sources,
    derive_pid,
    fingerprint_customer,
    has_active_pledge,
    identity_field_hits,
    redact_document,
    scan_identity_bytes,
    table_exists,
)
from .store import open_db

ROUTES = ('controlled_retention', 'minimal_retention')
DEFAULT_PURPOSE = {
    'controlled_retention': 'dual_authority_regulatory_verification',
    'minimal_retention': 'irreversible_settlement_evidence',
}
# Ordered state machine.  Each name is a crash barrier.
STEPS = ('preconditions', 'distribution_boundary', 'seal_package',
         'commitments', 'rewrite_main_db', 'disposal_state', 'post_verify')
FAILPOINT_ENV = 'PAWNPROOF_FAILPOINT'


class DisposalError(ValueError):
    pass


def _job_id(request_id):
    return 'J-' + request_id


# ---------------------------------------------------------------------------
# Planning: lock route and purpose, build the field dependency inventory
# ---------------------------------------------------------------------------

def plan(path, request_id, route, purpose=None, manifest_file=None, media_root=None):
    if route not in ROUTES:
        raise DisposalError('unknown_route')
    purpose = purpose or DEFAULT_PURPOSE[route]
    with open_db(path) as db, db:
        req = db.execute('SELECT * FROM privacy_request WHERE id=?',
                         (request_id,)).fetchone()
        if not req:
            raise DisposalError('unknown_privacy_request')
        if req['status'] != 'approved':
            raise DisposalError('request_not_approved')
        customer = req['customer']
        existing = db.execute('SELECT status,route FROM disposal_job WHERE job_id=?',
                              (_job_id(request_id),)).fetchone()
        if existing:
            if existing['status'] == 'completed':
                raise DisposalError('disposal_already_completed')
            if existing['route'] != route:
                raise DisposalError('route_locked_cannot_change')
            return _job_view(db, request_id)
        salt = os.urandom(16).hex()
        pid = derive_pid(customer, salt)
        blocked = has_active_pledge(db, customer)
        db.execute(
            'INSERT INTO disposal_job VALUES(?,?,?,?,?,?,?,?,?)',
            (_job_id(request_id), request_id, pid, route, purpose,
             'blocked' if blocked else 'planned',
             at(_now()), salt, pid))
        db.execute('INSERT INTO pseudonym VALUES(?,?,?)', (pid, salt, at(_now())))
        _record_event(db, request_id, 'plan_locked', actor='disposal-desk',
                      detail=json.dumps({'route': route, 'purpose': purpose},
                                        ensure_ascii=False))
        _build_inventory(db, _job_id(request_id), customer, blocked)
        if manifest_file:
            _load_manifest(db, request_id, Path(manifest_file), media_root)
        view = _job_view(db, request_id)
        if blocked:
            db.commit()  # keep the blocked job and its inventory on record
            raise DisposalError('active_pledge_blocks_disposal')
    return view


def _build_inventory(db, job_id, customer, active):
    rows = []
    for table, column, key in identity_field_hits(db, customer):
        if table == 'source_file':
            classification = 'no_retention_basis'
            basis = '明文字节中的身份无普通库保留依据，须改写；受控路线另封离线包'
        elif active:
            classification = 'active_pledge_required'
            basis = FIELD_BASIS['active_pledge_required']
        else:
            classification = 'historical_proof'
            basis = FIELD_BASIS['historical_proof']
        source_id = key if table == 'source_file' else None
        rows.append((job_id, table, column, source_id, classification, basis))
    # Amounts, moments and relations are historical proof that must survive.
    for table, columns in HISTORICAL_PROOF_COLUMNS.items():
        if not table_exists(db, table):
            continue
        if not db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
            continue
        for column in columns:
            rows.append((job_id, table, column, None, 'historical_proof',
                         '金额、时间或同一客户关系的历史证明，保留数值并以代号关联'))
    db.executemany(
        'INSERT INTO disposal_field(job_id,table_name,column_name,source_id,'
        'classification,basis) VALUES(?,?,?,?,?,?)', rows)


def _load_manifest(db, request_id, manifest_path, media_root):
    doc = json.loads(manifest_path.read_text(encoding='utf8'))
    root = Path(media_root) if media_root else manifest_path.parent
    for item in doc['copies']:
        location = str(root / item['location']) if item.get('location') else None
        original = _hash_medium(item['medium'], location) if location else None
        db.execute(
            'INSERT OR REPLACE INTO distribution_copy VALUES(?,?,?,?,?,?,?,?,?)',
            (item['copy_id'], _job_id(request_id), item['medium'], location,
             'plaintext', original, None, None, None))


def register_copy(path, request_id, copy_id, medium, location):
    """Programmatic equivalent of a manifest entry (used by tests/CLI)."""
    with open_db(path) as db, db:
        original = _hash_medium(medium, location)
        db.execute(
            'INSERT OR REPLACE INTO distribution_copy VALUES(?,?,?,?,?,?,?,?,?)',
            (copy_id, _job_id(request_id), medium, location, 'plaintext',
             original, None, None))
    return {'copy_id': copy_id, 'original_sha': original}


def _hash_medium(medium, location):
    if not location or not Path(location).exists():
        return None
    if medium == 'sqlite_backup':
        return crypto.sha256_file(location)
    if medium == 'export_dossier':
        h = []
        for file in sorted(Path(location).rglob('*')):
            if file.is_file():
                h.append(crypto.sha256_file(file))
        return crypto.commitment(b'dossier', *h) if h else None
    raise DisposalError('unknown_medium')


# ---------------------------------------------------------------------------
# Timeline events: withdrawal, legal hold, later disputes
# ---------------------------------------------------------------------------

def record_event(path, request_id, kind, at_value, actor, detail=None):
    if kind not in ('withdrawal', 'legal_hold', 'dispute', 'plan_locked',
                    'regulator_access'):
        raise DisposalError('unknown_event_kind')
    at(at_value)
    with open_db(path) as db, db:
        if not db.execute('SELECT 1 FROM privacy_request WHERE id=?',
                          (request_id,)).fetchone():
            raise DisposalError('unknown_privacy_request')
        _record_event(db, request_id, kind, at_value, actor, detail)
    return {'request_id': request_id, 'kind': kind, 'at': at_value}


def _record_event(db, request_id, kind, at_value=None, actor='disposal-desk',
                  detail=None):
    db.execute(
        'INSERT INTO disposal_event(job_id,request_id,kind,at,actor,detail) '
        'VALUES(?,?,?,?,?,?)',
        (_job_id(request_id), request_id, kind,
         at_value or _now(), actor, detail))


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _has_blocking_event(db, request_id):
    row = db.execute(
        "SELECT kind FROM disposal_event WHERE request_id=? AND kind IN "
        "('withdrawal','legal_hold') ORDER BY id DESC LIMIT 1",
        (request_id,)).fetchone()
    if not row:
        return None
    completed = db.execute(
        "SELECT 1 FROM privacy_disposal WHERE request_id=? AND state='completed'",
        (request_id,)).fetchone()
    if completed:
        return None  # history is recorded; nothing can resurrect the original
    return row['kind']


# ---------------------------------------------------------------------------
# Execution: crash-recoverable state machine
# ---------------------------------------------------------------------------

def execute(path, request_id, vault_dir=None, failpoint=None):
    """Run (or resume) the disposal job.

    A failpoint name from :data:`STEPS` makes the process raise after the
    barrier is opened, simulating a crash; the next call resumes safely.
    """
    if failpoint:
        os.environ[FAILPOINT_ENV] = failpoint
    try:
        with open_db(path) as db:
            job = db.execute('SELECT * FROM disposal_job WHERE job_id=?',
                             (_job_id(request_id),)).fetchone()
            if not job:
                raise DisposalError('unknown_disposal_job')
            if job['status'] == 'completed':
                return verify(path, request_id)
            # On resume the main DB may already be rewritten; the pseudonym
            # then stands in for the customer in every idempotent check.
            try:
                customer = _load_customer(db, request_id)
            except DisposalError:
                if job['status'] not in ('completing', 'executing'):
                    raise
                customer = job['pid']
            pid, salt, route = job['pid'], job['salt'], job['route']
            vdir = Path(vault_dir) if vault_dir else \
                Path(path).resolve().parent / 'privacy-vault'

            _step(db, job, request_id, 'preconditions',
                  lambda: _check_preconditions(db, request_id, customer))
            _step(db, job, request_id, 'distribution_boundary',
                  lambda: _close_distribution(db, request_id, customer, pid, salt))
            if route == 'controlled_retention':
                _step(db, job, request_id, 'seal_package',
                      lambda: _seal_identity_package(db, job, request_id,
                                                     customer, vdir))
            else:
                _crash_hook('seal_package')
                _mark_done(db, job['job_id'], 'seal_package',
                           'not_applicable_minimal_route')
            _step(db, job, request_id, 'commitments',
                  lambda: _write_commitments(db, job, customer, pid, salt))
            _step(db, job, request_id, 'rewrite_main_db',
                  lambda: _rewrite_db(db, customer, pid, salt, job['job_id'],
                                      include_request=False))
            _step(db, job, request_id, 'disposal_state',
                  lambda: _finalize_state(db, job, request_id))
            _step(db, job, request_id, 'post_verify',
                  lambda: _post_verify(db, request_id, customer, pid,
                                       original_known=not customer.startswith('PID-')))
            return _job_view(db, request_id)
    finally:
        os.environ.pop(FAILPOINT_ENV, None)


def _crash_hook(name):
    if os.environ.get(FAILPOINT_ENV) == name:
        raise RuntimeError('crash_injected_' + name)


def _step(db, job, request_id, name, action):
    marker = db.execute('SELECT state,detail FROM disposal_step WHERE job_id=? AND step=?',
                        (job['job_id'], name)).fetchone()
    if marker and marker['state'] == 'done':
        return
    # A previous run already proved this step blocks: keep the recorded
    # reason and stop instead of overwriting it with 'executing'.
    if marker and marker['state'] == 'failed':
        raise DisposalError(marker['detail'] or ('blocked_at_' + name))
    db.execute(
        'INSERT OR REPLACE INTO disposal_step VALUES(?,?,?,?)',
        (job['job_id'], name, 'executing', None))
    db.commit()
    _crash_hook(name)
    try:
        detail = action()
    except DisposalError as exc:
        db.execute(
            'INSERT OR REPLACE INTO disposal_step VALUES(?,?,?,?)',
            (job['job_id'], name, 'failed', str(exc)))
        db.commit()
        raise
    db.commit()
    _mark_done(db, job['job_id'], name, detail)
    db.commit()


def _mark_done(db, job_id, name, detail=None):
    db.execute('INSERT OR REPLACE INTO disposal_step VALUES(?,?,?,?)',
               (job_id, name, 'done', detail))


def _load_customer(db, request_id):
    req = db.execute('SELECT customer FROM privacy_request WHERE id=?',
                     (request_id,)).fetchone()
    if req and req['customer'] and not str(req['customer']).startswith('PID-'):
        return req['customer']
    # Main DB already rewritten: recover the name from the sealed package or
    # fail loudly rather than guessing.
    pkg = db.execute('SELECT 1 FROM custody_package WHERE job_id=?',
                     (_job_id(request_id),)).fetchone()
    raise DisposalError('customer_rewritten_reopen_package' if pkg
                        else 'customer_irrecoverable_minimal_route')


def _check_preconditions(db, request_id, customer):
    req = db.execute('SELECT status,customer FROM privacy_request WHERE id=?',
                     (request_id,)).fetchone()
    if req['status'] != 'approved':
        raise DisposalError('request_not_approved')
    if not customer.startswith('PID-') and has_active_pledge(db, customer):
        _set_blocked(db, request_id, 'active_pledge_blocks_disposal')
        raise DisposalError('active_pledge_blocks_disposal')
    blocking = _has_blocking_event(db, request_id)
    if blocking:
        _set_blocked(db, request_id, blocking + '_blocks_disposal')
        raise DisposalError(blocking + '_blocks_disposal')
    return 'request_approved_no_active_pledge_no_hold'


def _set_blocked(db, request_id, reason):
    job_id = _job_id(request_id)
    db.execute("UPDATE disposal_job SET status='blocked' WHERE job_id=?",
               (job_id,))
    route = db.execute('SELECT route FROM disposal_job WHERE job_id=?',
                       (job_id,)).fetchone()[0]
    db.execute(
        'INSERT OR REPLACE INTO privacy_disposal VALUES(?,?,?,?,?,?,?)',
        (request_id, job_id, 'incomplete', route, 0, None, reason))


# ---------------------------------------------------------------------------
# Distribution boundary: backups and external dossiers
# ---------------------------------------------------------------------------

def attest_unrecoverable(path, request_id, copy_id, attestation_file):
    item = json.loads(Path(attestation_file).read_text(encoding='utf8'))
    at(item['at'])
    with open_db(path) as db, db:
        row = db.execute('SELECT * FROM distribution_copy WHERE copy_id=? AND job_id=?',
                         (copy_id, _job_id(request_id))).fetchone()
        if not row:
            raise DisposalError('unknown_copy')
        if row['status'] != 'plaintext':
            raise DisposalError('copy_already_handled')
        db.execute(
            'UPDATE distribution_copy SET status=?,attestation=?,updated_at=? '
            'WHERE copy_id=?',
            ('unrecoverable', json.dumps(item, ensure_ascii=False),
             item['at'], copy_id))
        _record_event(db, request_id, 'copy_exception',
                      at_value=item['at'], actor=item.get('actor', 'custodian'),
                      detail=json.dumps({'copy_id': copy_id,
                                         'attestation': item.get('statement')},
                                        ensure_ascii=False))
    return {'copy_id': copy_id, 'status': 'unrecoverable'}


def _close_distribution(db, request_id, customer, pid, salt):
    job_id = _job_id(request_id)
    copies = db.execute('SELECT * FROM distribution_copy WHERE job_id=?',
                        (job_id,)).fetchall()
    pending = [c for c in copies if c['status'] == 'plaintext']
    missing = [c['copy_id'] for c in pending if not Path(c['location']).exists()]
    if missing:
        reason = 'distribution_copy_missing:' + ','.join(missing)
        _set_blocked(db, request_id, reason)
        raise DisposalError(reason)
    for copy in pending:
        if copy['medium'] == 'sqlite_backup':
            _refresh_sqlite_copy(db, copy, customer, pid, salt)
        elif copy['medium'] == 'export_dossier':
            _refresh_dossier_copy(db, copy, customer, pid)
    outstanding = db.execute(
        "SELECT copy_id FROM distribution_copy WHERE job_id=? AND status='plaintext'",
        (job_id,)).fetchall()
    if outstanding:
        reason = 'plaintext_copies_remain:' + ','.join(x['copy_id'] for x in outstanding)
        _set_blocked(db, request_id, reason)
        raise DisposalError(reason)
    return 'all_copies_redacted_or_attested_unrecoverable'


def _refresh_sqlite_copy(db, copy, customer, pid, salt):
    # C2 fingerprint before the rewrite proves the shared batch is not
    # collateral damage.
    with open_db(copy['location']) as bdb:
        others = _other_customers(bdb, customer)
        before = {c: fingerprint_customer(bdb, c) for c in others}
        _rewrite_db(bdb, customer, pid, salt, copy['job_id'], include_request=True)
        bdb.commit()
        after = {c: fingerprint_customer(bdb, c) for c in others}
        if before != after:
            raise DisposalError('backup_refresh_touched_other_customer')
        if scan_identity_bytes(bdb, customer):
            raise DisposalError('backup_refresh_incomplete')
    updated = crypto.sha256_file(copy['location'])
    db.execute(
        'UPDATE distribution_copy SET status=?,updated_sha=?,updated_at=? '
        'WHERE copy_id=?',
        ('redacted', updated, _now(), copy['copy_id']))


def _refresh_dossier_copy(db, copy, customer, pid):
    from .evidence import IDENTITY_JSON_KEYS
    root = Path(copy['location'])
    changed = []
    for file in sorted(root.rglob('*.json')):
        raw = file.read_bytes()
        try:
            doc = json.loads(raw.decode('utf8'))
        except (ValueError, UnicodeDecodeError):
            doc = {}
        if not (isinstance(doc, dict)
                and customer in [doc.get(k) for k in IDENTITY_JSON_KEYS
                                 if isinstance(doc.get(k), str)]):
            continue
        redacted = redact_document(raw, customer, pid)
        file.write_bytes(redacted)
        changed.append({'file': file.name,
                        'sha': crypto.sha256_file(file)})
    if not changed:
        raise DisposalError('dossier_without_target_customer')
    db.execute(
        'UPDATE distribution_copy SET status=?,updated_sha=?,updated_at=? '
        'WHERE copy_id=?',
        ('redacted', crypto.commitment(b'dossier',
                                       *[x['sha'] for x in changed]),
         _now(), copy['copy_id']))


def _other_customers(db, customer):
    names = set()
    for table, column in (('loan', 'customer'), ('asset', 'owner'),
                          ('ownership', 'claimant')):
        if not table_exists(db, table):
            continue
        for row in db.execute(f'SELECT DISTINCT {column} AS v FROM {table}'):
            if row['v'] and row['v'] != customer and not row['v'].startswith('PID-'):
                names.add(row['v'])
    return sorted(names)


# ---------------------------------------------------------------------------
# Controlled route: offline dual-authority package
# ---------------------------------------------------------------------------

def _seal_identity_package(db, job, request_id, customer, vault_dir):
    vault_dir.mkdir(parents=True, exist_ok=True)
    originals = []
    for src in customer_sources(db, customer):
        originals.append({'source_id': src['id'], 'kind': src['kind'],
                          'sha': src['sha'], 'raw': src['raw'].decode('utf8')})
    payload = json.dumps(
        {'request_id': request_id, 'customer': customer, 'pid': job['pid'],
         'route': job['route'], 'purpose': job['purpose'],
         'sealed_at': _now(), 'original_sources': originals},
        ensure_ascii=False).encode('utf8')
    blob, key = crypto.seal_envelope(payload)
    share_a, share_b = crypto.split_secret(key)
    base = vault_dir / request_id
    pkg_path = base.with_suffix('.package')
    sa_path = base.with_suffix('.share-a')
    sb_path = base.with_suffix('.share-b')
    pkg_path.write_bytes(blob)
    sa_path.write_bytes(share_a)
    sb_path.write_bytes(share_b)
    db.execute(
        'INSERT OR REPLACE INTO custody_package VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (job['job_id'], str(pkg_path), crypto.sha256_file(pkg_path),
         'hmac-sha256-keystream', 'custodian-a', 'custodian-b',
         str(sa_path), str(sb_path),
         crypto.sha256_file(sa_path), crypto.sha256_file(sb_path),
         _now(), 'sealed'))
    # The unsealed key never touches the database.
    return 'package_sealed_2of2'


def regulator_access(path, request_id, share_a_file=None, share_b_file=None):
    """Tiered regulator entry point.

    Controlled route: both custodian shares are required; the original owner
    is reconstructed and the access is logged on the timeline.
    Minimal route: identity is not recoverable; commitments and settlement
    verification data are returned instead.
    """
    with open_db(path) as db, db:
        job = db.execute('SELECT * FROM disposal_job WHERE job_id=?',
                         (_job_id(request_id),)).fetchone()
        if not job:
            raise DisposalError('unknown_disposal_job')
        if job['route'] == 'minimal_retention':
            _record_event(db, request_id, 'regulator_access',
                          actor='regulator',
                          detail='identity_not_recoverable_commitments_only')
            result = {'identity_recoverable': False,
                      'reason': 'key_material_destroyed',
                      'pid': job['pid'],
                      'commitments': _commitment_view(db, request_id),
                      'verification': _settlement_verification(db, job['pid'])}
            db.commit()
            return result
        pkg = db.execute('SELECT * FROM custody_package WHERE job_id=?',
                         (job['job_id'],)).fetchone()
        if not pkg:
            raise DisposalError('custody_package_missing')
        if not share_a_file or not share_b_file:
            raise DisposalError('dual_authorization_required')
        share_a = Path(share_a_file).read_bytes()
        share_b = Path(share_b_file).read_bytes()
        if crypto.sha256_file(share_a_file) != pkg['share_a_sha'] or \
                crypto.sha256_file(share_b_file) != pkg['share_b_sha']:
            raise DisposalError('custodian_share_mismatch')
        key = crypto.combine_secret(share_a, share_b)
        try:
            payload = json.loads(crypto.open_envelope(
                Path(pkg['package_path']).read_bytes(), key).decode('utf8'))
        except ValueError:
            raise DisposalError('package_open_failed')
        _record_event(db, request_id, 'regulator_access', actor='regulator',
                      detail='original_identity_disclosed_dual_authorization')
        db.commit()
        return {'identity_recoverable': True,
                'authorization': 'dual_custodian',
                'customer': payload['customer'],
                'pid': payload['pid'],
                'purpose': payload['purpose'],
                'original_sources': payload['original_sources']}


# ---------------------------------------------------------------------------
# Minimal route: irreversible commitments over retained evidence
# ---------------------------------------------------------------------------

def _write_commitments(db, job, customer, pid, salt):
    import hashlib
    existing = db.execute('SELECT 1 FROM identity_commitment WHERE job_id=? LIMIT 1',
                          (job['job_id'],)).fetchone()
    if existing:
        return 'commitments_resumed'
    for src in customer_sources(db, customer):
        redacted = redact_document(src['raw'], customer, pid)
        redacted_sha = hashlib.sha256(redacted).hexdigest()
        evidence_digest = crypto.commitment(
            salt.encode(), pid, src['kind'], redacted_sha)
        db.execute(
            'INSERT INTO identity_commitment VALUES(?,?,?,?,?,?,?)',
            (job['job_id'], src['id'], src['sha'], redacted_sha, pid,
             src['kind'], evidence_digest))
    return 'commitments_sealed'


def _commitment_view(db, request_id):
    return [dict(x) for x in db.execute(
        'SELECT source_id,original_sha,redacted_sha,pid,kind,evidence_digest '
        'FROM identity_commitment WHERE job_id=? ORDER BY source_id',
        (_job_id(request_id),))]


def _settlement_verification(db, pid):
    loans = db.execute('SELECT * FROM loan WHERE customer=?', (pid,)).fetchall()
    out = []
    for loan in loans:
        paid = db.execute(
            'SELECT COALESCE(SUM(amount_cents),0) AS v FROM payment WHERE loan_id=?',
            (loan['id'],)).fetchone()['v']
        due = loan['principal_cents'] + loan['interest_cents']
        decision = db.execute(
            "SELECT kind,status,at FROM decision WHERE loan_id=? "
            "ORDER BY id DESC LIMIT 1", (loan['id'],)).fetchone()
        out.append({'loan_id': loan['id'], 'pid': pid,
                    'principal_cents': loan['principal_cents'],
                    'interest_cents': loan['interest_cents'],
                    'paid_cents': paid, 'due_cents': due,
                    'settled': paid == due,
                    'loan_status': loan['status'],
                    'decision': dict(decision) if decision else None})
    return out


# ---------------------------------------------------------------------------
# Shared SQLite rewrite (main DB and offline backup copies)
# ---------------------------------------------------------------------------

def _rewrite_db(db, customer, pid, salt, job_id, include_request=True):
    if not scan_identity_bytes(db, customer) and not _column_hits(db, customer):
        return 'already_rewritten'
    for src in customer_sources(db, customer):
        redacted = redact_document(src['raw'], customer, pid)
        import hashlib
        db.execute('UPDATE source_file SET raw=?,sha=? WHERE id=?',
                   (redacted, hashlib.sha256(redacted).hexdigest(), src['id']))
    for table, column in (('asset', 'owner'), ('ownership', 'claimant'),
                          ('loan', 'customer'), ('extension', 'customer_actor'),
                          ('notice', 'recipient')):
        if table_exists(db, table):
            db.execute(f'UPDATE {table} SET {column}=? WHERE {column}=?',
                       (pid, customer))
    if include_request and table_exists(db, 'privacy_request'):
        db.execute('UPDATE privacy_request SET customer=? WHERE customer=?',
                   (pid, customer))


def _column_hits(db, customer):
    for table, column in (('asset', 'owner'), ('ownership', 'claimant'),
                          ('loan', 'customer'), ('extension', 'customer_actor'),
                          ('notice', 'recipient')):
        if table_exists(db, table) and \
                db.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1',
                           (customer,)).fetchone():
            return True
    return False


def _finalize_state(db, job, request_id):
    db.execute('UPDATE privacy_request SET customer=? WHERE id=?',
               (job['pid'], request_id))
    recoverable = 1 if job['route'] == 'controlled_retention' else 0
    # 'completing' becomes 'completed' only after post_verify succeeds, so a
    # crash in between can never advertise a finished disposal.
    db.execute(
        'INSERT OR REPLACE INTO privacy_disposal VALUES(?,?,?,?,?,?,?)',
        (request_id, job['job_id'], 'completing', job['route'],
         recoverable, None, None))
    db.execute("UPDATE disposal_job SET status='completing' WHERE job_id=?",
               (job['job_id'],))
    return 'state_recorded_pending_verification'


def _post_verify(db, request_id, customer, pid, original_known):
    if original_known:
        if scan_identity_bytes(db, customer):
            raise DisposalError('post_verify_plaintext_raw_remains')
        if _column_hits(db, customer):
            raise DisposalError('post_verify_plaintext_column_remains')
    else:
        # Resume after the rewrite: the original name is gone, so verify the
        # identity columns this job touched now carry this pid.  Other
        # customers (e.g. an active C2 sharing the tables) must remain
        # untouched and are deliberately outside this check.
        identity_columns = {
            'asset': ('owner',), 'ownership': ('claimant',),
            'loan': ('customer',), 'extension': ('customer_actor',),
            'notice': ('recipient',)}
        for table, columns in identity_columns.items():
            if not table_exists(db, table):
                continue
            for column in columns:
                owned = db.execute(
                    'SELECT count(*) AS v FROM disposal_field WHERE job_id=? '
                    'AND table_name=? AND column_name=?',
                    (_job_id(request_id), table, column)).fetchone()['v']
                if not owned:
                    continue
                values = [r[0] for r in db.execute(
                    f'SELECT DISTINCT {column} FROM {table} '
                    f'WHERE {column} IS NOT NULL')]
                if pid not in values:
                    raise DisposalError(
                        'post_verify_pseudonym_missing:' + table + '.' + column)
    copies = db.execute(
        "SELECT count(*) AS v FROM distribution_copy WHERE job_id=? AND status='plaintext'",
        (_job_id(request_id),)).fetchone()['v']
    if copies:
        raise DisposalError('post_verify_plaintext_copy_remains')
    db.execute(
        "UPDATE privacy_disposal SET state='completed',completed_at=? "
        'WHERE request_id=?', (_now(), request_id))
    db.execute("UPDATE disposal_job SET status='completed' WHERE job_id=?",
               (_job_id(request_id),))
    _record_event(db, request_id, 'disposal_completed',
                  detail=json.dumps({
                      'route': db.execute(
                          'SELECT route FROM disposal_job WHERE job_id=?',
                          (_job_id(request_id),)).fetchone()[0],
                      'identity_recoverable': bool(db.execute(
                          'SELECT identity_recoverable FROM privacy_disposal '
                          'WHERE request_id=?', (request_id,)).fetchone()[0])},
                      ensure_ascii=False))
    return 'ordinary_access_removed_all_media'


# ---------------------------------------------------------------------------
# Verification view
# ---------------------------------------------------------------------------

def verify(path, request_id):
    with open_db(path) as db:
        job = db.execute('SELECT * FROM disposal_job WHERE job_id=?',
                         (_job_id(request_id),)).fetchone()
        if not job:
            raise DisposalError('unknown_disposal_job')
        state = db.execute('SELECT * FROM privacy_disposal WHERE request_id=?',
                           (request_id,)).fetchone()
        reason = state['reason'] if state else None
        if not reason:
            failed = db.execute(
                "SELECT detail FROM disposal_step WHERE job_id=? AND state='failed' "
                'ORDER BY rowid DESC LIMIT 1', (job['job_id'],)).fetchone()
            reason = failed['detail'] if failed else None
        inventory = [dict(x) for x in db.execute(
            'SELECT table_name,column_name,source_id,classification,basis '
            'FROM disposal_field WHERE job_id=? ORDER BY id', (job['job_id'],))]
        copies = [dict(x) for x in db.execute(
            'SELECT copy_id,medium,location,status,original_sha,updated_sha,'
            'attestation FROM distribution_copy WHERE job_id=? ORDER BY copy_id',
            (job['job_id'],))]
        steps = [dict(x) for x in db.execute(
            'SELECT step,state,detail FROM disposal_step WHERE job_id=? ORDER BY rowid',
            (job['job_id'],))]
        events = [dict(x) for x in db.execute(
            'SELECT kind,at,actor,detail FROM disposal_event WHERE request_id=? '
            'ORDER BY id', (request_id,))]
        customer = None
        req = db.execute('SELECT customer FROM privacy_request WHERE id=?',
                         (request_id,)).fetchone()
        if req:
            customer = req['customer']
        ordinary_removed = not (customer and not str(customer).startswith('PID-'))
        return {
            'request_id': request_id,
            'job_id': job['job_id'],
            'route': job['route'],
            'purpose': job['purpose'],
            'status': job['status'],
            'state': state['state'] if state else 'incomplete',
            'reason': state['reason'] if state else None,
            'pid': job['pid'],
            'identity_recoverable': bool(state['identity_recoverable']) if state else False,
            'ordinary_access_removed': ordinary_removed,
            'field_inventory': inventory,
            'distribution_copies': copies,
            'steps': steps,
            'timeline': events,
            'commitments': _commitment_view(db, request_id),
        }


def _job_view(db, request_id):
    job = db.execute('SELECT * FROM disposal_job WHERE job_id=?',
                     (_job_id(request_id),)).fetchone()
    fields = db.execute(
        'SELECT count(*) AS v FROM disposal_field WHERE job_id=?',
        (job['job_id'],)).fetchone()['v']
    copies = db.execute(
        'SELECT status,count(*) AS v FROM distribution_copy WHERE job_id=? '
        'GROUP BY status', (job['job_id'],)).fetchall()
    return {'job_id': job['job_id'], 'request_id': request_id,
            'route': job['route'], 'purpose': job['purpose'],
            'status': job['status'], 'pid': job['pid'],
            'field_entries': fields,
            'copy_statuses': {x['status']: x['v'] for x in copies}}


# ---------------------------------------------------------------------------
# The three conventional approaches, demonstrated on disposable copies
# ---------------------------------------------------------------------------

def clone_db(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def demonstrate_report_mask_only(db_path, customer, backup_locations=()):
    """Approach 1: mask C1 only inside report output.

    Nothing is rewritten, so ``source_file.raw`` and every offline/exported
    copy keeps the plaintext identity bytes.  Entries may be bare paths or
    ``(medium, location)`` pairs matching the distribution manifest.
    """
    from .evidence import IDENTITY_JSON_KEYS
    leaks = []
    with open_db(db_path) as db:
        for src in customer_sources(db, customer):
            leaks.append({'medium': 'main_db', 'source_id': src['id'],
                          'sha': src['sha']})
    for entry in backup_locations:
        if isinstance(entry, (tuple, list)):
            medium, raw_location = entry
        else:
            raw_location = entry
            medium = 'export_dossier' if Path(raw_location).is_dir() \
                else 'offline_backup'
        location = Path(raw_location)
        if medium == 'sqlite_backup':
            with open_db(location) as bdb:
                leaks.extend({'medium': 'sqlite_backup', 'source_id': s['id']}
                             for s in customer_sources(bdb, customer))
        elif location.is_dir():
            for file in sorted(location.rglob('*.json')):
                try:
                    doc = json.loads(file.read_text(encoding='utf8'))
                except (ValueError, UnicodeDecodeError):
                    doc = {}
                if isinstance(doc, dict) and customer in \
                        [doc.get(k) for k in IDENTITY_JSON_KEYS
                         if isinstance(doc.get(k), str)]:
                    leaks.append({'medium': 'export_dossier', 'file': str(file)})
        elif customer.encode('utf8') in location.read_bytes():
            leaks.append({'medium': medium, 'file': str(location)})
    return {'approach': 'report_mask_only',
            'public_report_clean': True,
            'source_raw_leaks': leaks,
            'failure': '遮盖只发生在输出层，source_file.raw 与分发副本继续明文凭据'}


def demonstrate_update_only(db_path, customer, pid):
    """Approach 2: UPDATE identity columns but leave original raw bytes.

    The non-null columns now disagree with the raw sources; replaying those
    sources restores C1, so the audit cannot confirm a coherent ledger.
    """
    with open_db(db_path) as db, db:
        for table, column in (('asset', 'owner'), ('ownership', 'claimant'),
                              ('loan', 'customer'), ('extension', 'customer_actor'),
                              ('notice', 'recipient')):
            if table_exists(db, table):
                db.execute(f'UPDATE {table} SET {column}=? WHERE {column}=?',
                           (pid, customer))
        raw_still_plain = [s['id'] for s in customer_sources(db, customer)]
    from .report import audit
    audit_result = audit(db_path)
    return {'approach': 'column_update_only',
            'columns_rewritten': True,
            'raw_sources_still_plaintext': raw_still_plain,
            'replay_restores_identity': bool(raw_still_plain),
            'audit_replay_ok': audit_result['replay_ok'],
            'failure': '非空字段与原始重放不一致，重放即还原 C1，审计无法确认账证一致'}


def demonstrate_delete_raw_only(db_path, customer):
    """Approach 3: delete C1 source rows outright.

    Foreign keys force the operator to detach the evidence references first;
    the replay then loses the payment/decision evidence while live columns
    still name C1, so the audit can no longer reproduce the ledger at all.
    """
    with open_db(db_path) as db, db:
        db.execute('PRAGMA foreign_keys=OFF')
        ids = [s['id'] for s in customer_sources(db, customer)]
        db.execute('DELETE FROM source_file WHERE id IN (%s)' %
                   ','.join('?' * len(ids)), ids)
        remaining_column_hits = identity_field_hits(db, customer)
        payments = db.execute(
            'SELECT count(*) AS v FROM payment').fetchone()['v']
    from .report import audit
    audit_result = audit(db_path)
    return {'approach': 'delete_raw_only',
            'raw_sources_deleted': ids,
            'identity_still_in_tables': remaining_column_hits,
            'payment_rows_orphaned': payments,
            'audit_replay_ok': audit_result['replay_ok'],
            'replay_events': audit_result['events'],
            'replay_error': audit_result.get('replay_error'),
            'failure': '重放链断裂，付款与赎回依据被一并销毁，表内身份仍在'}
