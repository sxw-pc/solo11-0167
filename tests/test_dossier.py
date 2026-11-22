import json
from pathlib import Path

from pawnproof import asset, vault, loan, dossier
from pawnproof.store import open_db


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_dossier_submit_and_review(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    asset.import_asset(db, FIX / 'asset-ok.json')
    asset.ownership(db, FIX / 'ownership-ok.json')
    asset.appraise(db, FIX / 'appraisal-ok.json')
    vault.import_vault(db, FIX / 'vault-ok.json')
    vault.custody(db, FIX / 'custody-ok.json')
    loan.originate(db, FIX / 'loan-ok.json')
    submitted = dossier.prepare(db, FIX / 'dossier-ok.json', tmp_path / 'out')
    assert submitted['status'] == 'submitted'
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'id': 'REVIEW1', 'dossier_id': 'DOC1',
                                   'authority': 'regulator-A', 'digest': submitted['digest'],
                                   'outcome': 'accepted'}))
    assert dossier.review(db, receipt)['status'] == 'accepted'
    with open_db(db) as connection:
        assert connection.execute('SELECT status FROM dossier WHERE id=?', ('DOC1',)).fetchone()[0] == 'accepted'
