import json
from .store import open_db
from .evidence import snapshot
from . import lab_capture

def report(path):
    with open_db(path) as db:
        state=snapshot(db)
        for row in state['loan']:
            paid=sum(x['amount_cents'] for x in state['payment'] if x['loan_id']==row['id'])
            row['outstanding_cents']=row['principal_cents']+row['interest_cents']-paid
        return {'state':state,'lab':lab_capture.analyze(db),'source_files':[{'id':x['id'],'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT id,kind,sha FROM source_file ORDER BY id')]}

def audit(path):
    from tempfile import TemporaryDirectory
    from pathlib import Path
    from . import asset,vault,loan,lab
    funcs={'asset':asset.import_asset,'ownership':asset.ownership,'appraisal':asset.appraise,
           'vault':vault.import_vault,'custody':vault.custody,'loan':loan.originate,
           'payment':loan.pay,'extension':loan.extend,'notice':loan.notice,'decision':loan.decide}
    funcs.update({'lab_sample':lab.sample,'lab_result':lab.certify,
                  'lab_capture':lab_capture.import_capture,'lab_evidence':lab_capture.import_evidence,
                  'lab_evidence_revoke':lab_capture.revoke_evidence,'lab_certificate':lab_capture.issue_certificate})
    original=report(path)
    with open_db(path) as db:
        files=[(x['kind'],x['raw']) for x in db.execute('SELECT kind,raw FROM source_file ORDER BY id')]
    with TemporaryDirectory() as tmp:
        testdb=Path(tmp)/'replay.sqlite'
        for i,(kind,raw) in enumerate(files):
            file=Path(tmp)/f'{i}.json';file.write_bytes(raw)
            funcs[kind](testdb,file)
        fresh=report(testdb)
    # SQLite row IDs for sources and decisions are deterministic in import order.
    return {'replay_ok':fresh==original,'events':len(files)}
