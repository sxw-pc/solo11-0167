from pathlib import Path

from pawnproof import asset, vault, loan, privacy
from pawnproof.report import audit


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_retention_review_changes_after_redemption(tmp_path):
    db = tmp_path / 'pawn.sqlite'
    asset.import_asset(db, FIX / 'asset-ok.json')
    asset.ownership(db, FIX / 'ownership-ok.json')
    asset.appraise(db, FIX / 'appraisal-ok.json')
    vault.import_vault(db, FIX / 'vault-ok.json')
    vault.custody(db, FIX / 'custody-ok.json')
    loan.originate(db, FIX / 'loan-ok.json')
    privacy.request(db, FIX / 'privacy-ok.json')
    assert privacy.review(db, 'PR1')['status'] == 'held'
    loan.pay(db, FIX / 'payment-ok.json')
    loan.decide(db, FIX / 'decision-ok.json')
    assert privacy.review(db, 'PR1')['status'] == 'approved'
    assert audit(db)['replay_ok']
