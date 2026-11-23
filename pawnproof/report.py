from .store import open_db
from .evidence import snapshot

def report(path):
    with open_db(path) as db:
        state=snapshot(db)
        for row in state['loan']:
            paid=sum(x['amount_cents'] for x in state['payment'] if x['loan_id']==row['id'])
            row['outstanding_cents']=row['principal_cents']+row['interest_cents']-paid
        return {'state':state,'source_files':[{'id':x['id'],'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT id,kind,sha FROM source_file ORDER BY id')]}

def dossier_report(path):
    """Versioned-dossier view: immutable sealed versions, current pointer,
    revocations, selective-disclosure releases, receipts and adjudications."""
    from .dossier import dossier_audit
    with open_db(path) as db:
        versions=[dict(x) for x in db.execute(
            'SELECT id,loan_id,authority,version,supersedes,commitment,file_digest,'
            'loan_status_at_capture,asset_status_at_capture,watermark_kind,'
            'watermark_value,source_count,captured_at,current,publish_state '
            'FROM dossier_version ORDER BY version')]
        releases=[dict(x) for x in db.execute(
            'SELECT id,dossier_id,authority,purpose,fields,commitment,package_digest,'
            'locked,created_at FROM dossier_release ORDER BY created_at')]
        revocations=[dict(x) for x in db.execute('SELECT * FROM dossier_revocation')]
        acknowledgments=[dict(x) for x in db.execute(
            'SELECT id,dossier_id,release_id,authority,digest,at,matched '
            'FROM dossier_acknowledgment ORDER BY at')]
        adjudications=[dict(x) for x in db.execute(
            'SELECT id,release_id,dossier_id,authority,purpose,verdict,'
            'is_current_claim,reasons,at FROM dossier_adjudication ORDER BY at')]
    return {'versions':versions,'releases':releases,'revocations':revocations,
            'acknowledgments':acknowledgments,'adjudications':adjudications,
            'audit':dossier_audit(path)}

def audit(path):
    from tempfile import TemporaryDirectory
    from pathlib import Path
    from . import asset,vault,loan
    funcs={'asset':asset.import_asset,'ownership':asset.ownership,'appraisal':asset.appraise,
           'vault':vault.import_vault,'custody':vault.custody,'loan':loan.originate,
           'payment':loan.pay,'extension':loan.extend,'notice':loan.notice,'decision':loan.decide}
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
