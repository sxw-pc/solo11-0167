"""A basic offline pledge dossier and authority receipt workflow."""
import hashlib
import json
from pathlib import Path
from .report import report
from .store import open_db


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
