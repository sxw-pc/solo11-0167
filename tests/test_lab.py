import json
from pathlib import Path

import pytest
from pawnproof import asset, vault, loan, lab
from pawnproof.report import audit


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_gem_certificate_enables_loan(tmp_path):
    db = tmp_path / 'gem.sqlite'
    def feed(name, body):
        path = tmp_path / name
        path.write_text(json.dumps(body))
        return path
    asset.import_asset(db, feed('asset.json', {'asset_id':'G2','owner':'C2','kind':'gemstone','serial':'GEM-2'}))
    asset.ownership(db, feed('ownership.json', {'id':'O2','asset_id':'G2','claimant':'C2',
        'valid_from':'2026-01-01T00:00:00Z','valid_to':'2027-01-01T00:00:00Z'}))
    asset.appraise(db, feed('appraisal.json', {'id':'A2','asset_id':'G2','amount':'1000.00',
        'discount_percent':60,'assessor_a':'X','assessor_b':'Y','valid_to':'2026-12-01T00:00:00Z'}))
    vault.import_vault(db, feed('vault.json', {'slot_id':'V2','capacity':1}))
    vault.custody(db, feed('custody.json', {'id':'C2','asset_id':'G2','kind':'seal',
        'slot_id':'V2','seal':'gem-seal','actor_a':'guard-a','actor_b':'guard-b','at':'2026-03-01T07:00:00Z'}))
    loan_file = feed('loan.json', {'id':'L2','asset_id':'G2','appraisal_id':'A2','customer':'C2',
        'principal':'500.00','interest':'25.00','issued_at':'2026-03-01T10:00:00Z',
        'due_at':'2026-04-01T10:00:00Z'})
    with pytest.raises(ValueError, match='lab_authenticity_missing'):
        loan.originate(db, loan_file)
    lab.sample(db, FIX / 'gem-sample.json')
    lab.certify(db, FIX / 'gem-result.json')
    assert loan.originate(db, loan_file)['status'] == 'active'
    assert audit(db)['replay_ok']
