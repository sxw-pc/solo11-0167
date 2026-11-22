"""Baseline customer data retention request review."""
import json
from pathlib import Path
from .store import open_db
from .evidence import at


def request(path, file):
    item = json.loads(Path(file).read_text(encoding='utf8'))
    if not item['customer']:
        raise ValueError('customer_required')
    with open_db(path) as db, db:
        db.execute('INSERT INTO privacy_request VALUES(?,?,?,?)',
                   (item['id'], item['customer'], at(item['at']), 'pending'))
    return {'id': item['id'], 'status': 'pending'}


def review(path, request_id):
    with open_db(path) as db, db:
        item = db.execute('SELECT * FROM privacy_request WHERE id=?', (request_id,)).fetchone()
        if not item:
            raise ValueError('unknown_privacy_request')
        active = db.execute("SELECT 1 FROM loan WHERE customer=? AND status IN ('active','extended','disputed') LIMIT 1",
                            (item['customer'],)).fetchone()
        status = 'held' if active else 'approved'
        db.execute('UPDATE privacy_request SET status=? WHERE id=?', (status, request_id))
    return {'id': request_id, 'status': status}
