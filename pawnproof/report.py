import json
from .store import open_db
from .evidence import snapshot

STATE_TABLES=('asset','ownership','appraisal','vault_slot','custody','loan','payment',
              'payment_payer','extension','notice','decision','customer','heir',
              'inheritance_document','document_body','counter_verification',
              'handoff_plan','dispatch_intent','physical_handover','position_log',
              'handoff_event','dispute','recourse')

def _three_lines(db,state):
    from . import redemption_handoff as rh
    lines=[]
    for row in state['loan']:
        lines.append(rh.loan_status(db,row))
    return lines

def report(path):
    with open_db(path) as db:
        state=snapshot(db)
        for row in state['loan']:
            paid=sum(x['amount_cents'] for x in state['payment'] if x['loan_id']==row['id'])
            row['outstanding_cents']=row['principal_cents']+row['interest_cents']-paid
        # 三条状态线：债务清偿 / 合法领取权 / 实际交付。互不替代。
        lines=_three_lines(db,state)
        return {'state':state,
                'three_line_status':lines,
                'source_files':[{'id':x['id'],'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT id,kind,sha FROM source_file ORDER BY id')]}

def audit(path):
    from tempfile import TemporaryDirectory
    from pathlib import Path
    from . import asset,vault,loan,redemption_handoff as rh
    funcs={'asset':asset.import_asset,'ownership':asset.ownership,'appraisal':asset.appraise,
           'vault':vault.import_vault,'custody':vault.custody,'loan':loan.originate,
           'payment':loan.pay,'extension':loan.extend,'notice':loan.notice,'decision':loan.decide,
           'customer':rh.customer_status,'heir':rh.heir_share,'inheritance_document':rh.document,
           'document_body':rh.document_body,'counter_verification':rh.counter_check,
           'custody_point':rh.custody_point,'handoff_plan':rh.fix_route,
           'dispatch':rh.dispatch,'physical_handover':rh.confirm_handover}
    original=report(path)
    with open_db(path) as db:
        files=[(x['kind'],x['raw']) for x in db.execute('SELECT kind,raw FROM source_file ORDER BY id')]
    with TemporaryDirectory() as tmp:
        testdb=Path(tmp)/'replay.sqlite'
        for i,(kind,raw) in enumerate(files):
            file=Path(tmp)/f'{i}.json';file.write_bytes(raw)
            funcs[kind](testdb,file)
        fresh=report(testdb)
    # SQLite row IDs for sources, decisions and position logs are deterministic in import order.
    ok=fresh==original
    return {'replay_ok':ok,'events':len(files),
            'three_line_status':fresh.get('three_line_status')}
