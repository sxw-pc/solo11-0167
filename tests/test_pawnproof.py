import json,os,subprocess,sys
from pathlib import Path
import pytest
from pawnproof import asset,vault,loan
from pawnproof.report import report,audit
from pawnproof.store import open_db

FIX=Path(__file__).parent.parent/'fixtures'

def fixture(tmp_path,name,doc):
    p=tmp_path/name;p.write_text(json.dumps(doc),encoding='utf-8');return p

def seed(tmp_path):
    db=tmp_path/'state.sqlite'
    for func,name in [(asset.import_asset,'asset-ok.json'),(asset.ownership,'ownership-ok.json'),
                      (asset.appraise,'appraisal-ok.json'),(vault.import_vault,'vault-ok.json'),
                      (vault.custody,'custody-ok.json')]:func(db,FIX/name)
    return db

def loaned(tmp_path):
    db=seed(tmp_path);loan.originate(db,FIX/'loan-ok.json');return db

def test_normal_redemption_and_replay(tmp_path):
    print(__import__('pawnproof').__file__)
    db=loaned(tmp_path);loan.pay(db,FIX/'payment-ok.json')
    assert loan.decide(db,FIX/'decision-ok.json')['status']=='approved'
    r=report(db)['state'];assert r['loan'][0]['status']=='redeemed'
    assert r['vault_slot'][0]['occupied_asset'] is None
    assert audit(db)['replay_ok']

def test_wrong_owner_does_not_create_claim(tmp_path):
    db=tmp_path/'state.sqlite';asset.import_asset(db,FIX/'asset-ok.json')
    with pytest.raises(ValueError,match='claimant_mismatch'):asset.ownership(db,FIX/'ownership-bad.json')
    with open_db(db) as con:assert con.execute('SELECT count(*) FROM ownership').fetchone()[0]==0

def test_same_serial_cannot_back_two_active_pledges(tmp_path):
    db=tmp_path/'state.sqlite';asset.import_asset(db,FIX/'asset-ok.json')
    second={'asset_id':'G2','owner':'C2','kind':'watch','serial':'S-1'}
    with pytest.raises(ValueError,match='serial_already_pledged'):
        asset.import_asset(db,fixture(tmp_path,'second.json',second))
    assert len(report(db)['state']['asset'])==1

def test_seal_needs_independent_custodians_and_free_slot(tmp_path):
    db=tmp_path/'state.sqlite';asset.import_asset(db,FIX/'asset-ok.json')
    vault.import_vault(db,FIX/'vault-ok.json')
    bad=json.loads((FIX/'custody-ok.json').read_text());bad['actor_b']=bad['actor_a']
    with pytest.raises(ValueError,match='independent_custodians_required'):
        vault.custody(db,fixture(tmp_path,'oneguard.json',bad))
    assert report(db)['state']['vault_slot'][0]['occupied_asset'] is None
    vault.custody(db,FIX/'custody-ok.json')
    another={'asset_id':'G2','owner':'C2','kind':'watch','serial':'S-2'}
    asset.import_asset(db,fixture(tmp_path,'another.json',another))
    bad2={**json.loads((FIX/'custody-ok.json').read_text()),'id':'C-other','asset_id':'G2'}
    with pytest.raises(ValueError,match='slot_occupied'):
        vault.custody(db,fixture(tmp_path,'occupied.json',bad2))

def test_appraisal_ceiling_and_independent_assessors(tmp_path):
    db=seed(tmp_path)
    high=json.loads((FIX/'loan-ok.json').read_text());high['principal']='700.00'
    with pytest.raises(ValueError,match='loan_ceiling_exceeded'):loan.originate(db,fixture(tmp_path,'high.json',high))
    assert report(db)['state']['loan']==[]
    same=json.loads((FIX/'appraisal-ok.json').read_text());same.update(id='A2',assessor_b='X')
    with pytest.raises(ValueError,match='independent_assessors_required'):asset.appraise(db,fixture(tmp_path,'same.json',same))

def test_custody_dispute_blocks_redeem_despite_full_payment(tmp_path):
    db=loaned(tmp_path);loan.pay(db,FIX/'payment-ok.json')
    assert vault.custody(db,FIX/'custody-bad.json')['status']=='disputed'
    assert loan.decide(db,FIX/'decision-ok.json')['reason']=='custody_disputed'
    assert report(db)['state']['loan'][0]['status']=='active'
    assert audit(db)['replay_ok']

def test_transfer_requires_chain_and_releases_old_slot(tmp_path):
    db=seed(tmp_path);vault.import_vault(db,fixture(tmp_path,'slot2.json',{'slot_id':'V2','capacity':1}))
    move={'id':'C2','asset_id':'G1','kind':'transfer','slot_id':'V2','seal':'seal-1','actor_a':'g1','actor_b':'g2','at':'2026-03-02T09:00:00Z'}
    assert vault.custody(db,fixture(tmp_path,'move.json',move))['status']=='sound'
    slots={x['slot_id']:x['occupied_asset'] for x in report(db)['state']['vault_slot']}
    assert slots=={'V1':None,'V2':'G1'}
    with pytest.raises(ValueError,match='custody_chain_broken'):
        vault.custody(db,fixture(tmp_path,'badmove.json',{**move,'id':'C3','seal':'wrong','at':'2026-03-03T09:00:00Z'}))

def test_partial_payment_and_no_notice_block_forfeit(tmp_path):
    db=loaned(tmp_path)
    part={'id':'P2','loan_id':'L1','amount':'100.00','at':'2026-03-20T00:00:00Z'}
    assert loan.pay(db,fixture(tmp_path,'part.json',part))['outstanding_cents']==42500
    assert loan.decide(db,FIX/'decision-bad.json')['reason']=='notice_or_grace_missing'
    assert report(db)['state']['loan'][0]['status']=='active'

def test_notice_grace_and_extension_change_disposition(tmp_path):
    db=loaned(tmp_path)
    extension={'id':'E1','loan_id':'L1','at':'2026-03-20T00:00:00Z','new_due_at':'2026-05-01T10:00:00Z','customer_actor':'C1','shop_actor':'shop'}
    loan.extend(db,fixture(tmp_path,'extension.json',extension))
    old_notice={'id':'N1','loan_id':'L1','delivered_at':'2026-04-02T00:00:00Z','grace_until':'2026-04-10T00:00:00Z','recipient':'C1'}
    with pytest.raises(ValueError,match='invalid_notice'):loan.notice(db,fixture(tmp_path,'oldnotice.json',old_notice))
    notice={**old_notice,'delivered_at':'2026-05-02T00:00:00Z','grace_until':'2026-05-10T00:00:00Z'}
    loan.notice(db,fixture(tmp_path,'notice.json',notice))
    early={'loan_id':'L1','kind':'forfeit','at':'2026-05-05T00:00:00Z'}
    assert loan.decide(db,fixture(tmp_path,'early.json',early))['status']=='blocked'
    late={**early,'at':'2026-05-11T00:00:00Z'}
    assert loan.decide(db,fixture(tmp_path,'late.json',late))['status']=='approved'
    assert report(db)['state']['loan'][0]['status']=='forfeited'

def test_source_replay_and_event_collision(tmp_path):
    db=seed(tmp_path);first=loan.originate(db,FIX/'loan-ok.json')
    assert loan.originate(db,FIX/'loan-ok.json')['replayed']
    altered=json.loads((FIX/'loan-ok.json').read_text());altered['principal']='400.00'
    with pytest.raises(ValueError,match='event_id_conflict'):
        loan.originate(db,fixture(tmp_path,'altered.json',altered))
    assert report(db)['state']['loan'][0]['principal_cents']==50000

def test_public_cli_demo_and_module_origin(tmp_path):
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parent.parent)}
    p=subprocess.run([sys.executable,'-m','pawnproof','demo','--db',str(tmp_path/'cli.sqlite')],env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert json.loads(p.stdout)['audit']['replay_ok']
    assert str(Path(__import__('pawnproof').__file__).resolve()).startswith(str(Path(__file__).parent.parent.resolve()))
