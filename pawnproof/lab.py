"""Baseline local specimen intake and authenticity result ledger."""
from .store import open_db
from .evidence import at, read_source, unique_event


def sample(path, file):
    with open_db(path) as db, db:
        sid, data, replay = read_source(db, 'lab_sample', file)
        if unique_event(db, 'lab_sample', data['id'], sid):
            return {'id': data['id'], 'replayed': True}
        asset = db.execute('SELECT 1 FROM asset WHERE asset_id=?', (data['asset_id'],)).fetchone()
        if not asset:
            raise ValueError('unknown_asset')
        db.execute('INSERT INTO lab_sample VALUES(?,?,?,?,?,?)',
                   (data['id'], data['asset_id'], data['seal'], at(data['at']), data['collector'], sid))
        return {'id': data['id'], 'status': 'collected'}


def certify(path, file):
    with open_db(path) as db, db:
        sid, data, replay = read_source(db, 'lab_result', file)
        if unique_event(db, 'lab_result', data['id'], sid):
            return {'id': data['id'], 'replayed': True}
        specimen = db.execute('SELECT * FROM lab_sample WHERE id=?', (data['sample_id'],)).fetchone()
        if not specimen:
            raise ValueError('unknown_sample')
        if data['analyst'] == specimen['collector'] or data['verdict'] not in ('authentic', 'inauthentic'):
            raise ValueError('invalid_lab_result')
        db.execute('INSERT INTO lab_result VALUES(?,?,?,?,?,?)',
                   (data['id'], data['sample_id'], data['analyst'], at(data['at']), data['verdict'], sid))
        return {'id': data['id'], 'verdict': data['verdict']}
