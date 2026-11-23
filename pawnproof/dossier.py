"""Versioned pledge dossiers with point-in-time watermarks, selective disclosure
commitments, supersession and cross file/database publish recovery.

The original ``prepare``/``review`` workflow is kept byte-for-byte in behaviour so
the legacy regulator sign-off path stays runnable.  The enhanced workflow below
treats every dossier as an *immutable, time-point* sealed artifact:

* ``capture``      - seal a new dossier version against a re-computable watermark
* ``release``      - build an authorized selective-disclosure package for one
                     pre-locked review purpose
* ``verify``       - re-compute every commitment/digest from public material
* ``acknowledge``  - record an external regulator receipt against a dossier
* ``adjudicate``   - the two locked review paths (historical / current custody)
* ``recover``      - reconcile published files with the SQLite pointers
* ``dossier_audit``- historical/current/missing-or-conflict verdicts with reasons
"""
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .report import report
from .store import open_db

# The two regulator-authorized review paths.  Purpose and visible fields are
# locked before release and cannot be re-labelled later (see ``release`` /
# ``adjudicate``).
PURPOSE_HISTORICAL = 'historical_transaction_review'
PURPOSE_CURRENT = 'current_custody_review'
AUTHORIZED_PURPOSES = (PURPOSE_HISTORICAL, PURPOSE_CURRENT)

# Leaves whose paths end with one of these suffixes carry customer-sensitive
# identity.  A release profile that names any such leaf is rejected upstream.
SENSITIVE_SUFFIXES = (
    '.customer', '.owner', '.claimant',
    '.recipient', '.customer_actor', '.serial',
)

# Stable status vocabulary used by the current-qualification adjudication.
LIVE_ASSET_STATUSES = ('sealed',)
LIVE_LOAN_STATUSES = ('active', 'extended', 'disputed')

VERDICT_HISTORICAL_PASS = 'historical_archive_pass'
VERDICT_HISTORICAL_CONFLICT = 'historical_evidence_conflict'
VERDICT_CURRENT_PASS = 'current_custody_pass'
VERDICT_CURRENT_RELEASED = 'current_pledge_released'
VERDICT_STALE = 'current_claim_stale_version'
VERDICT_CONFLICT = 'evidence_missing_or_conflict'


# --------------------------------------------------------------------------- #
# Canonicalization and commitments
# --------------------------------------------------------------------------- #
def _canon(value):
    """Deterministic byte encoding shared by every digest / commitment."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf8')


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _flatten(value, prefix=''):
    """Flatten any JSON value into deterministic (path, value) leaves."""
    if isinstance(value, dict):
        leaves = []
        for key in sorted(value):
            leaves.extend(_flatten(value[key], f'{prefix}.{key}' if prefix else str(key)))
        return leaves
    if isinstance(value, list):
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(_flatten(item, f'{prefix}[{index}]'))
        return leaves
    return [(prefix, value)]


def _is_sensitive(path):
    # List indices (e.g. data.ownership[0].claimant) must not let a leaf
    # escape the suffix classification, so compare against the de-indexed path.
    normalized = path.replace('[', '.').replace(']', '')
    return any(normalized == suffix.lstrip('.') or normalized.endswith(suffix)
               for suffix in SENSITIVE_SUFFIXES)


def _sealed_leaf(path, value, salt):
    """Commitment leaf of the sealed dossier.  Sensitive leaves are salted so
    the commitment still covers them while the public material cannot reveal
    them; non-sensitive leaves commit to their plain value directly."""
    encoded = _canon(value)
    if _is_sensitive(path):
        digest = _sha(b'salted:' + _canon(path) + b'|' + _canon(salt) + b'|' + encoded)
        return {'path': path, 'sensitive': True, 'digest': digest}
    digest = _sha(b'plain:' + _canon(path) + b'|' + encoded)
    return {'path': path, 'sensitive': False, 'value': value, 'digest': digest}


def _root_commitment(entries):
    """Merkle-style root over *all* leaves, including redacted ones."""
    return _sha(b'root:' + b'|'.join(
        _canon([entry['path'], entry['digest']]) for entry in entries))


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _atomic_write(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}')
    tmp.write_bytes(raw)
    os.replace(tmp, path)


def _write_if_consistent(path, raw, expected_digest):
    """Publish ``raw`` only if the existing file (if any) is the same artifact.
    A different file at the registered path is a conflict, never overwritten."""
    path = Path(path)
    if path.exists():
        actual = _sha(path.read_bytes())
        if actual != expected_digest:
            raise ValueError('file_version_conflict')
        return
    _atomic_write(path, raw)


# --------------------------------------------------------------------------- #
# Legacy workflow (unchanged behaviour, kept runnable)
# --------------------------------------------------------------------------- #
def prepare(db_path, request_file, output_dir):
    request = json.loads(Path(request_file).read_text(encoding='utf8'))
    loan_id = request['loan_id']
    state = report(db_path)['state']
    loan = next((row for row in state['loan'] if row['id'] == loan_id), None)
    if not loan:
        raise ValueError('unknown_loan')
    data = {'dossier_id': request['id'], 'loan': loan,
            'asset': [row for row in state['asset'] if row['asset_id'] == loan['asset_id']],
            'custody': [row for row in state['custody'] if row['asset_id'] == loan['asset_id']],
            'decisions': [row for row in state['decision'] if row['loan_id'] == loan_id]}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf8')
    digest = hashlib.sha256(raw).hexdigest()
    target = Path(output_dir) / f"{request['id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as db, db:
        if db.execute('SELECT 1 FROM dossier WHERE id=?', (request['id'],)).fetchone():
            raise ValueError('dossier_id_conflict')
        target.write_bytes(raw)
        db.execute('INSERT INTO dossier VALUES(?,?,?,?,?,?)',
                   (request['id'], loan_id, request['authority'], digest, str(target), 'submitted'))
    return {'id': request['id'], 'digest': digest, 'status': 'submitted'}


def review(db_path, receipt_file):
    receipt = json.loads(Path(receipt_file).read_text(encoding='utf8'))
    with open_db(db_path) as db, db:
        dossier = db.execute('SELECT * FROM dossier WHERE id=?', (receipt['dossier_id'],)).fetchone()
        if not dossier:
            raise ValueError('unknown_dossier')
        matched = receipt['authority'] == dossier['authority'] and receipt['digest'] == dossier['digest']
        status = receipt['outcome'] if matched else 'held'
        db.execute('INSERT INTO dossier_review VALUES(?,?,?,?,?,?)',
                   (receipt['id'], receipt['dossier_id'], receipt['authority'], receipt['digest'],
                    receipt['outcome'], status))
        if matched:
            db.execute('UPDATE dossier SET status=? WHERE id=?', (status, receipt['dossier_id']))
    return {'id': receipt['id'], 'status': status}


# --------------------------------------------------------------------------- #
# Snapshot slices and watermarks
# --------------------------------------------------------------------------- #
def _loan_slice(db_path, loan_id):
    rep = report(db_path)
    state, files = rep['state'], rep['source_files']
    loan = next((row for row in state['loan'] if row['id'] == loan_id), None)
    if not loan:
        raise ValueError('unknown_loan')
    asset_id = loan['asset_id']
    return {
        'loan': loan,
        'asset': [row for row in state['asset'] if row['asset_id'] == asset_id],
        'ownership': [row for row in state['ownership'] if row['asset_id'] == asset_id],
        'appraisal': [row for row in state['appraisal'] if row['asset_id'] == asset_id],
        'custody': [row for row in state['custody'] if row['asset_id'] == asset_id],
        'payments': [row for row in state['payment'] if row['loan_id'] == loan_id],
        'decisions': [row for row in state['decision'] if row['loan_id'] == loan_id],
    }, files


def _watermark(db):
    """Re-computable ingestion watermark: every source file imported so far.
    A later payment/decision adds rows, so the watermark can never repeat."""
    rows = db.execute('SELECT id,kind,sha FROM source_file ORDER BY id').fetchall()
    return {
        'kind': 'source_file_sequence',
        'value': len(rows),
        'digest': _sha(b'watermark:' + b'|'.join(
            _canon([row['id'], row['kind'], row['sha']]) for row in rows)),
    }


def _capture_watermark_digest(db, dossier_id):
    """Recompute the watermark digest captured by a dossier from its registered
    source identities, so the claim can be checked without trusting the file."""
    rows = db.execute(
        'SELECT source_id,kind,sha FROM dossier_source WHERE dossier_id=? ORDER BY ord',
        (dossier_id,)).fetchall()
    return _sha(b'watermark:' + b'|'.join(
        _canon([row['source_id'], row['kind'], row['sha']]) for row in rows))


# --------------------------------------------------------------------------- #
# capture: immutable, versioned sealed dossier
# --------------------------------------------------------------------------- #
def capture(db_path, request_file, output_dir, captured_at=None):
    """Seal a new dossier version.

    The dossier records the ingestion watermark, every source-file identity,
    the point-in-time loan/asset status, the public field catalog and a
    withdrawal link.  A new version for a loan automatically revokes the
    previous current version (its file and receipt history are never touched).
    """
    request = json.loads(Path(request_file).read_text(encoding='utf8'))
    dossier_id = request['id']
    loan_id = request['loan_id']
    authority = request['authority']
    captured_at = captured_at or request.get('captured_at') or _now()

    with open_db(db_path) as db, db:
        if db.execute('SELECT 1 FROM dossier_version WHERE id=?', (dossier_id,)).fetchone():
            raise ValueError('dossier_id_conflict')
        previous = db.execute(
            "SELECT * FROM dossier_version WHERE loan_id=? AND current=1 ORDER BY version DESC LIMIT 1",
            (loan_id,)).fetchone()
        version = (previous['version'] + 1) if previous else 1
        watermark = _watermark(db)
        source_rows = db.execute('SELECT id,kind,sha FROM source_file ORDER BY id').fetchall()
        loan_row = db.execute('SELECT * FROM loan WHERE id=?', (loan_id,)).fetchone()
        if not loan_row:
            raise ValueError('unknown_loan')
        asset_row = db.execute('SELECT status FROM asset WHERE asset_id=?',
                               (loan_row['asset_id'],)).fetchone()
        loan_status = loan_row['status']
        asset_status = asset_row['status'] if asset_row else 'unknown'

    slice_, _ = _loan_slice(db_path, loan_id)
    salt = secrets.token_hex(32)
    sealed = {
        'dossier_id': dossier_id,
        'loan_id': loan_id,
        'authority': authority,
        'version': version,
        'supersedes': previous['id'] if previous else None,
        'captured_at': captured_at,
        'watermark': watermark,
        'loan_status_at_capture': loan_status,
        'asset_status_at_capture': asset_status,
        'source_files': [{'id': row['id'], 'kind': row['kind'], 'sha': row['sha']}
                         for row in source_rows],
        'data': slice_,
        'withdrawal_link': {
            'kind': 'current_pointer',
            'db_pointer': f'dossier_version(loan_id={loan_id},current=1)',
            'revocation_table': 'dossier_revocation',
            'supersedes': previous['id'] if previous else None,
        },
    }
    # Public field catalog: every leaf path, classified up front.  Paths are
    # rooted at "data." so release profiles can name them unambiguously.
    data_leaves = [('data.' + path if path else 'data', value)
                   for path, value in _flatten(sealed['data'])]
    sealed['field_catalog'] = sorted((
        {'path': path, 'public': not _is_sensitive(path)}
        for path, _ in data_leaves
    ), key=lambda item: item['path'])
    entries = [_sealed_leaf(path, value, salt) for path, value in data_leaves]
    sealed['commitment_entries'] = entries
    sealed['root_commitment'] = _root_commitment(entries)

    raw = _canon(sealed)
    file_digest = _sha(raw)
    target = Path(output_dir) / f'{dossier_id}.sealed.json'
    sealed_path = str(target)

    # File first, then the commit; a crash between the two leaves an orphan
    # file that ``recover`` reconciles instead of a dangling database pointer.
    _write_if_consistent(target, raw, file_digest)
    with open_db(db_path) as db, db:
        if db.execute('SELECT 1 FROM dossier_version WHERE id=?', (dossier_id,)).fetchone():
            raise ValueError('dossier_id_conflict')
        db.execute(
            'INSERT INTO dossier_version VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (dossier_id, loan_id, authority, version,
             previous['id'] if previous else None,
             sealed['root_commitment'], file_digest, sealed_path, raw,
             loan_status, asset_status, watermark['kind'], watermark['value'],
             len(source_rows), salt, captured_at, 0, 'pending'))
        for ord_, row in enumerate(source_rows):
            db.execute('INSERT INTO dossier_source VALUES (?,?,?,?,?)',
                       (dossier_id, row['id'], row['kind'], row['sha'], ord_))
        if previous:
            db.execute('UPDATE dossier_version SET current=0 WHERE id=?', (previous['id'],))
            db.execute('INSERT OR REPLACE INTO dossier_revocation VALUES (?,?,?,?)',
                       (previous['id'], dossier_id, 'superseded_by_new_capture', captured_at))
        db.execute("UPDATE dossier_version SET current=1,publish_state='published' WHERE id=?",
                   (dossier_id,))
    return {
        'id': dossier_id, 'version': version, 'loan_id': loan_id,
        'commitment': sealed['root_commitment'], 'digest': file_digest,
        'loan_status_at_capture': loan_status, 'asset_status_at_capture': asset_status,
        'watermark': watermark, 'supersedes': previous['id'] if previous else None,
        'file': sealed_path, 'status': 'published',
    }


# --------------------------------------------------------------------------- #
# release: selective disclosure for one locked review purpose
# --------------------------------------------------------------------------- #
def _select_fields(entries, allowed_prefixes):
    """Project sealing entries into a package leaf list.

    A disclosed leaf keeps its plain value; a redacted leaf keeps only its
    salted digest.  Path and digest order are identical to the sealing set,
    so the verifier reconstructs the very same Merkle root."""
    def covered(path, prefix):
        if path == prefix:
            return True
        # Match both dict children (prefix.path) and list elements
        # (prefix[0]) regardless of where an index appears.
        if path.startswith(prefix + '.') or path.startswith(prefix + '['):
            return True
        normalized = path.replace('[', '.').replace(']', '')
        return normalized.startswith(prefix + '.')

    leaves, visible_paths = [], set()
    for entry in entries:
        allowed = any(covered(entry['path'], prefix) for prefix in allowed_prefixes)
        if not allowed:
            leaves.append({'path': entry['path'], 'sensitive': entry['sensitive'],
                           'disclosed': False, 'digest': entry['digest'],
                           'reason': 'redacted_sensitive' if entry['sensitive']
                           else 'outside_authorized_profile'})
            continue
        if entry['sensitive']:
            # The profile must not name customer-sensitive leaves; failing
            # closed keeps redaction out of ad-hoc caller decisions.
            raise ValueError('profile_discloses_sensitive_field:' + entry['path'])
        leaves.append({'path': entry['path'], 'sensitive': False,
                       'disclosed': True, 'value': entry['value'],
                       'digest': entry['digest']})
        visible_paths.add(entry['path'])
    return leaves, visible_paths


def release(db_path, request_file, output_dir, created_at=None):
    """Build a selective-disclosure package.

    Purpose, authority and visible-field prefixes are locked into the package
    commitment; the sealed root commitment (covering the redacted fields too)
    is embedded, so a verifier confirms same-source/same-version without ever
    receiving the sensitive values.
    """
    request = json.loads(Path(request_file).read_text(encoding='utf8'))
    dossier_id = request['dossier_id']
    purpose = request['purpose']
    authority = request['authority']
    fields = list(request['fields'])
    created_at = created_at or request.get('created_at') or _now()
    if purpose not in AUTHORIZED_PURPOSES:
        raise ValueError('unknown_review_purpose')
    if not fields:
        raise ValueError('empty_field_profile')

    with open_db(db_path) as db:
        version = db.execute('SELECT * FROM dossier_version WHERE id=?', (dossier_id,)).fetchone()
        if not version:
            raise ValueError('unknown_dossier')
        sealed = json.loads(version['raw'])

    entries = sealed['commitment_entries']
    leaves, visible_paths = _select_fields(entries, fields)
    root = _root_commitment(entries)
    if not hmac.compare_digest(root, sealed['root_commitment']):
        raise ValueError('sealed_commitment_tampered')

    package_id = request.get('release_id') or f'REL-{dossier_id}-{secrets.token_hex(6)}'
    package = {
        'release_id': package_id,
        'dossier_id': dossier_id,
        'loan_id': sealed['loan_id'],
        'authority': authority,
        'purpose': purpose,
        'fields_locked': fields,
        'captured_at': sealed['captured_at'],
        'dossier_version': sealed['version'],
        'watermark': sealed['watermark'],
        'loan_status_at_capture': sealed['loan_status_at_capture'],
        'asset_status_at_capture': sealed['asset_status_at_capture'],
        'source_files': sealed['source_files'],
        'withdrawal_link': sealed['withdrawal_link'],
        # The full sealing entry set: disclosed leaves carry values, redacted
        # leaves carry only their salted commitment.  The root is therefore
        # recomputable from public material while sensitive values are absent.
        'leaves': leaves,
        'sealed_root_commitment': root,
        'sealed_file_digest': version['file_digest'],
        'supersedes': sealed['supersedes'],
        'created_at': created_at,
    }
    package['package_commitment'] = _sha(
        b'package:' + _canon({
            'release_id': package['release_id'],
            'dossier_id': dossier_id,
            'authority': authority,
            'purpose': purpose,
            'fields_locked': fields,
            'sealed_root_commitment': root,
        }))
    raw = _canon(package)
    # Defense in depth: every entry marked sensitive at sealing time must be
    # redacted in the package; only its salted digest may be public.
    sensitive_paths = {entry['path'] for entry in entries if entry['sensitive']}
    if sensitive_paths & visible_paths:
        raise ValueError('sensitive_value_leak_in_package')
    digest = _sha(raw)
    target = Path(output_dir) / f'{package_id}.disclosure.json'
    _write_if_consistent(target, raw, digest)
    with open_db(db_path) as db, db:
        if db.execute('SELECT 1 FROM dossier_release WHERE id=?', (package_id,)).fetchone():
            raise ValueError('release_id_conflict')
        db.execute('INSERT INTO dossier_release VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (package_id, dossier_id, authority, purpose, _canon(fields),
                    root, digest, str(target), raw, 1, created_at))
    return {'release_id': package_id, 'dossier_id': dossier_id, 'purpose': purpose,
            'commitment': root, 'package_digest': digest,
            'visible_fields': len(visible_paths),
            'hidden_fields': len(leaves) - len(visible_paths),
            'file': str(target)}


def verify_package(db_path, package_file):
    """Re-compute every commitment from the public package material alone."""
    package = json.loads(Path(package_file).read_text(encoding='utf8'))
    leaves = []
    for item in package['leaves']:
        if item['disclosed']:
            if 'value' not in item:
                return {'ok': False, 'reason': 'disclosed_leaf_missing_value',
                        'path': item['path']}
            digest = _sha(b'plain:' + _canon(item['path']) + b'|' + _canon(item['value']))
            if not hmac.compare_digest(digest, item['digest']):
                return {'ok': False, 'reason': 'visible_leaf_digest_mismatch',
                        'path': item['path']}
        else:
            # Redacted material must expose no value at all; the salted
            # digest is the only thing a verifier needs for the shared root.
            if 'value' in item:
                return {'ok': False, 'reason': 'redacted_leaf_carries_value',
                        'path': item['path']}
        leaves.append({'path': item['path'], 'digest': item['digest']})
    root = _root_commitment(leaves)
    if not hmac.compare_digest(root, package['sealed_root_commitment']):
        return {'ok': False, 'reason': 'root_commitment_mismatch'}
    package_commitment = _sha(b'package:' + _canon({
        'release_id': package['release_id'],
        'dossier_id': package['dossier_id'],
        'authority': package['authority'],
        'purpose': package['purpose'],
        'fields_locked': package['fields_locked'],
        'sealed_root_commitment': root,
    }))
    if not hmac.compare_digest(package_commitment, package['package_commitment']):
        return {'ok': False, 'reason': 'package_commitment_mismatch'}

    # Cross-check against the SQLite current pointer / registry when available.
    result = {'ok': True, 'commitment': root,
              'package_commitment': package_commitment, 'db_checks': {}}
    with open_db(db_path) as db:
        version = db.execute('SELECT * FROM dossier_version WHERE id=?',
                             (package['dossier_id'],)).fetchone()
        if not version:
            result['db_checks']['dossier_registered'] = False
            result['ok'] = False
            result['reason'] = 'dossier_not_in_registry'
            return result
        result['db_checks']['dossier_registered'] = True
        if not hmac.compare_digest(version['commitment'], root):
            result['ok'] = False
            result['reason'] = 'registry_commitment_diverges'
            return result
        if _sha(version['raw']) != version['file_digest']:
            result['ok'] = False
            result['reason'] = 'sealed_blob_tampered'
            return result
        sealed_path = Path(version['file_path'])
        if not sealed_path.exists():
            result['db_checks']['sealed_file'] = 'missing'
        elif _sha(sealed_path.read_bytes()) != version['file_digest']:
            result['db_checks']['sealed_file'] = 'conflict'
        else:
            result['db_checks']['sealed_file'] = 'ok'
        rel = db.execute('SELECT * FROM dossier_release WHERE id=?',
                         (package['release_id'],)).fetchone()
        result['db_checks']['release_registered'] = bool(rel)
        if rel and not hmac.compare_digest(rel['package_digest'],
                                           _sha(Path(package_file).read_bytes())):
            result['ok'] = False
            result['reason'] = 'release_pointer_diverges'
            return result
        current = db.execute('SELECT id,current FROM dossier_version WHERE loan_id=? ORDER BY version',
                             (package['loan_id'],)).fetchall()
        result['db_checks']['current_version_id'] = next(
            (row['id'] for row in current if row['current']), None)
        result['db_checks']['package_is_current'] = (
            result['db_checks']['current_version_id'] == package['dossier_id'])
        revocation = db.execute('SELECT * FROM dossier_revocation WHERE dossier_id=?',
                                (package['dossier_id'],)).fetchone()
        result['db_checks']['revoked'] = bool(revocation)
        if revocation:
            result['db_checks']['superseded_by'] = revocation['superseded_by']
    return result


# --------------------------------------------------------------------------- #
# External regulator receipts
# --------------------------------------------------------------------------- #
def acknowledge(db_path, receipt_file, at=None):
    """Record an external regulator sign-off.  The receipt only matches when
    authority and digest line up with the registered sealed dossier; history is
    append-only and a superseded dossier keeps its old receipt facts."""
    receipt = json.loads(Path(receipt_file).read_text(encoding='utf8'))
    at = at or receipt.get('at') or _now()
    with open_db(db_path) as db, db:
        version = db.execute('SELECT * FROM dossier_version WHERE id=?',
                             (receipt['dossier_id'],)).fetchone()
        if not version:
            raise ValueError('unknown_dossier')
        matched = (receipt['authority'] == version['authority']
                   and hmac.compare_digest(receipt['digest'], version['file_digest']))
        if db.execute('SELECT 1 FROM dossier_acknowledgment WHERE id=?',
                      (receipt['id'],)).fetchone():
            raise ValueError('acknowledgment_id_conflict')
        db.execute('INSERT INTO dossier_acknowledgment VALUES (?,?,?,?,?,?,?)',
                   (receipt['id'], receipt['dossier_id'],
                    receipt.get('release_id'), receipt['authority'],
                    receipt['digest'], at, 1 if matched else 0))
    return {'id': receipt['id'], 'dossier_id': receipt['dossier_id'],
            'matched': bool(matched)}


# --------------------------------------------------------------------------- #
# Adjudication: the two locked regulator review paths
# --------------------------------------------------------------------------- #
def adjudicate(db_path, request_file, at=None):
    request = json.loads(Path(request_file).read_text(encoding='utf8'))
    release_id = request['release_id']
    purpose = request['purpose']
    at = at or request.get('at') or _now()
    if purpose not in AUTHORIZED_PURPOSES:
        raise ValueError('unknown_review_purpose')

    with open_db(db_path) as db:
        rel = db.execute('SELECT * FROM dossier_release WHERE id=?', (release_id,)).fetchone()
        if not rel:
            raise ValueError('unknown_release')
        # Purpose is locked at release time; re-labelling is impossible.
        if rel['purpose'] != purpose:
            raise ValueError('purpose_relabel_denied')
        package = json.loads(rel['raw'])
        version = db.execute('SELECT * FROM dossier_version WHERE id=?',
                             (rel['dossier_id'],)).fetchone()
        loan_id = version['loan_id']
        loan = db.execute('SELECT * FROM loan WHERE id=?', (loan_id,)).fetchone()
        asset = db.execute('SELECT * FROM asset WHERE asset_id=?',
                           (loan['asset_id'],)).fetchone()
        current_row = db.execute(
            'SELECT id FROM dossier_version WHERE loan_id=? AND current=1',
            (loan_id,)).fetchone()
        current_id = current_row['id'] if current_row else None
        revocation = db.execute('SELECT * FROM dossier_revocation WHERE dossier_id=?',
                                (version['id'],)).fetchone()
        acknowledgments = db.execute(
            'SELECT COUNT(*) AS n FROM dossier_acknowledgment WHERE dossier_id=? AND matched=1',
            (version['id'],)).fetchone()['n']
        source_ids = {row['source_id'] for row in db.execute(
            'SELECT source_id FROM dossier_source WHERE dossier_id=?', (version['id'],))}
        live_sources = {row['id'] for row in db.execute('SELECT id FROM source_file')}
        sealed_path = Path(version['file_path'])

    reasons = []
    # Integrity / evidence gates shared by both paths.
    if not sealed_path.exists():
        reasons.append('sealed_file_missing')
    elif _sha(sealed_path.read_bytes()) != version['file_digest']:
        reasons.append('sealed_file_digest_conflict')
    if not source_ids.issubset(live_sources):
        reasons.append('source_file_missing_from_ledger')
    if acknowledgments == 0:
        reasons.append('no_matched_external_receipt')

    watermark_now = None
    with open_db(db_path) as db:
        watermark_now = _watermark(db)
        captured_digest = _capture_watermark_digest(db, version['id'])

    if package['watermark']['digest'] != captured_digest:
        reasons.append('watermark_unrecomputable')
    watermark_stale = watermark_now['value'] != version['watermark_value']
    if watermark_stale:
        reasons.append(
            f"watermark_stale:captured={version['watermark_value']},current={watermark_now['value']}")

    adjudication_id = request.get('id') or f'ADJ-{release_id}-{secrets.token_hex(6)}'

    if purpose == PURPOSE_HISTORICAL:
        # Only asks whether the pledge and loan were real at issuance.  The
        # snapshot is valid historical evidence even after redemption, but it
        # must never be presented as a current custody claim.
        if reasons and not all(r.startswith('watermark_stale') for r in reasons):
            verdict = VERDICT_HISTORICAL_CONFLICT
        else:
            verdict = VERDICT_HISTORICAL_PASS
            reasons = [r for r in reasons if r.startswith('watermark_stale')]
            reasons.append('historical_only_not_current_custody_claim')
            if version['loan_status_at_capture'] == 'active' \
                    and version['asset_status_at_capture'] == 'sealed':
                reasons.append('point_in_time_active_loan_sealed_asset_confirmed')
        is_current_claim = False
    else:
        is_current_claim = True
        blocking = [r for r in reasons if not r.startswith('watermark_stale')]
        if blocking:
            verdict = VERDICT_CONFLICT
        elif current_id != version['id'] or revocation:
            verdict = VERDICT_STALE
            reasons.append(f'current_version_is:{current_id}')
        elif loan['status'] not in LIVE_LOAN_STATUSES \
                or asset['status'] not in LIVE_ASSET_STATUSES:
            verdict = VERDICT_CURRENT_RELEASED
            reasons.append(f"loan_status={loan['status']}")
            reasons.append(f"asset_status={asset['status']}")
        else:
            verdict = VERDICT_CURRENT_PASS
            reasons.append('loan_active_and_asset_sealed_at_current_watermark')

    with open_db(db_path) as db, db:
        db.execute('INSERT INTO dossier_adjudication VALUES (?,?,?,?,?,?,?,?,?)',
                   (adjudication_id, release_id, version['id'], rel['authority'],
                    purpose, verdict, 1 if is_current_claim else 0,
                    _canon(reasons), at))
    return {'id': adjudication_id, 'release_id': release_id,
            'dossier_id': version['id'], 'purpose': purpose,
            'verdict': verdict, 'is_current_claim': is_current_claim,
            'reasons': reasons}


# --------------------------------------------------------------------------- #
# Recovery: reconcile published files, SQLite pointers and external receipts
# --------------------------------------------------------------------------- #
def recover(db_path):
    """Make the on-disk publication set and the SQLite registry one version
    again.  Registered blobs are the source of truth; nothing is overwritten
    silently and any divergent file is reported as a conflict."""
    report_ = {'restored': [], 'confirmed': [], 'conflicts': [], 'orphans': []}
    with open_db(db_path) as db, db:
        for version in db.execute('SELECT * FROM dossier_version ORDER BY version'):
            path = Path(version['file_path'])
            if not path.exists():
                _atomic_write(path, version['raw'])
                report_['restored'].append({'kind': 'sealed', 'id': version['id'],
                                            'path': str(path)})
            elif _sha(path.read_bytes()) != version['file_digest']:
                report_['conflicts'].append({'kind': 'sealed', 'id': version['id'],
                                             'path': str(path),
                                             'reason': 'digest_mismatch'})
            else:
                report_['confirmed'].append({'kind': 'sealed', 'id': version['id']})
            if version['publish_state'] != 'published':
                db.execute("UPDATE dossier_version SET publish_state='published' WHERE id=?",
                           (version['id'],))
                report_['restored'].append({'kind': 'pointer', 'id': version['id']})

        known = {Path(row['file_path']).resolve()
                 for row in db.execute('SELECT file_path FROM dossier_version')}
        known |= {Path(row['file_path']).resolve()
                  for row in db.execute('SELECT file_path FROM dossier_release')}
        for rel in db.execute('SELECT * FROM dossier_release'):
            path = Path(rel['file_path'])
            if not path.exists():
                _atomic_write(path, rel['raw'])
                report_['restored'].append({'kind': 'disclosure', 'id': rel['id'],
                                            'path': str(path)})
            elif _sha(path.read_bytes()) != rel['package_digest']:
                report_['conflicts'].append({'kind': 'disclosure', 'id': rel['id'],
                                             'path': str(path),
                                             'reason': 'digest_mismatch'})
            else:
                report_['confirmed'].append({'kind': 'disclosure', 'id': rel['id']})

        scan_dirs = {Path(row['file_path']).parent
                     for row in db.execute('SELECT file_path FROM dossier_version')}
        scan_dirs |= {Path(row['file_path']).parent
                      for row in db.execute('SELECT file_path FROM dossier_release')}
        for directory in scan_dirs:
            if not directory.exists():
                continue
            for candidate in directory.iterdir():
                if candidate.name.startswith('.') or not candidate.is_file():
                    continue
                if candidate.resolve() not in known and candidate.suffix == '.json':
                    report_['orphans'].append(str(candidate))
    return report_


# --------------------------------------------------------------------------- #
# Audit: historical / current / missing-or-conflict buckets, re-computable
# --------------------------------------------------------------------------- #
def dossier_audit(db_path):
    buckets = {'historical_archive_verifiable': [],
               'current_regulatory_clear': [],
               'evidence_missing_or_conflict': []}
    details = []
    with open_db(db_path) as db:
        versions = db.execute('SELECT * FROM dossier_version ORDER BY captured_at,version').fetchall()
        for version in versions:
            item = {'dossier_id': version['id'], 'loan_id': version['loan_id'],
                    'version': version['version'], 'current': bool(version['current']),
                    'reasons': []}
            path = Path(version['file_path'])
            integrity_ok = True
            if not path.exists():
                item['reasons'].append('sealed_file_missing')
                integrity_ok = False
            elif _sha(path.read_bytes()) != version['file_digest']:
                item['reasons'].append('sealed_file_digest_conflict')
                integrity_ok = False
            source_ids = {row['source_id'] for row in db.execute(
                'SELECT source_id FROM dossier_source WHERE dossier_id=?',
                (version['id'],))}
            live_sources = {row['id'] for row in db.execute('SELECT id FROM source_file')}
            if not source_ids.issubset(live_sources):
                item['reasons'].append('source_file_missing_from_ledger')
                integrity_ok = False
            salt_ok = True
            sealed = json.loads(version['raw'])
            data_leaves = dict(('data.' + path if path else 'data', value)
                               for path, value in _flatten(sealed['data']))
            for entry in sealed['commitment_entries']:
                if not entry.get('sensitive'):
                    continue
                leaf = data_leaves.get(entry['path'])
                if leaf is None:
                    salt_ok = False
                    continue
                recomputed = _sealed_leaf(entry['path'], leaf, version['blinding_salt'])
                if recomputed['digest'] != entry['digest']:
                    salt_ok = False
            if not salt_ok:
                item['reasons'].append('salted_commitment_recompute_failed')
                integrity_ok = False
            if _root_commitment(sealed['commitment_entries']) != version['commitment']:
                item['reasons'].append('root_commitment_recompute_failed')
                integrity_ok = False
            receipts = db.execute(
                'SELECT COUNT(*) AS n FROM dossier_acknowledgment WHERE dossier_id=? AND matched=1',
                (version['id'],)).fetchone()['n']
            item['matched_receipts'] = receipts
            if receipts == 0:
                item['reasons'].append('no_matched_external_receipt')

            loan = db.execute('SELECT * FROM loan WHERE id=?',
                              (version['loan_id'],)).fetchone()
            asset = db.execute('SELECT status FROM asset WHERE asset_id=?',
                               (loan['asset_id'],)).fetchone() if loan else None
            watermark = _watermark(db)
            stale = watermark['value'] != version['watermark_value']
            item['watermark_captured'] = version['watermark_value']
            item['watermark_current'] = watermark['value']
            item['loan_status_at_capture'] = version['loan_status_at_capture']
            item['asset_status_at_capture'] = version['asset_status_at_capture']
            item['loan_status_now'] = loan['status'] if loan else None
            item['asset_status_now'] = asset['status'] if asset else None

            if not integrity_ok or receipts == 0:
                bucket = 'evidence_missing_or_conflict'
            elif version['current']:
                if loan and loan['status'] in LIVE_LOAN_STATUSES \
                        and asset and asset['status'] in LIVE_ASSET_STATUSES:
                    bucket = 'current_regulatory_clear'
                    item['reasons'].append('current_pointer_active_and_sealed')
                else:
                    bucket = 'evidence_missing_or_conflict'
                    item['reasons'].append('current_pointer_but_pledge_not_live')
            else:
                bucket = 'historical_archive_verifiable'
                item['reasons'].append('superseded_time_point_evidence_retained')
                if stale:
                    item['reasons'].append(
                        f"watermark_stale:captured={version['watermark_value']},"
                        f"current={watermark['value']}")
            buckets[bucket].append(version['id'])
            details.append(item)
    return {'buckets': buckets, 'details': details}
