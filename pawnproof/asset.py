from .store import open_db
from .evidence import read_source,unique_event,money,at

def import_asset(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'asset',file)
        if replay:return {'asset_id':d['asset_id'],'replayed':True}
        if not d['asset_id'] or not d['owner'] or not d['serial']:raise ValueError('asset_identity_missing')
        if db.execute("SELECT 1 FROM asset WHERE serial=? AND status!='released'",(d['serial'],)).fetchone():raise ValueError('serial_already_pledged')
        db.execute('INSERT INTO asset VALUES(?,?,?,?,?,?)',(d['asset_id'],d['owner'],d['kind'],d['serial'],'candidate',sid))
        return {'asset_id':d['asset_id'],'status':'candidate'}

def ownership(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'ownership',file)
        if unique_event(db,'ownership',d['id'],sid):return {'id':d['id'],'replayed':True}
        asset=db.execute('SELECT * FROM asset WHERE asset_id=?',(d['asset_id'],)).fetchone()
        if not asset:raise ValueError('unknown_asset')
        if d['claimant']!=asset['owner']:raise ValueError('claimant_mismatch')
        start,end=at(d['valid_from']),at(d['valid_to'])
        if start>=end:raise ValueError('invalid_ownership_window')
        status='disputed' if d.get('dispute') else 'verified'
        db.execute('INSERT INTO ownership VALUES(?,?,?,?,?,?,?)',(d['id'],d['asset_id'],d['claimant'],start,end,status,sid))
        if status=='disputed':db.execute("UPDATE asset SET status='disputed' WHERE asset_id=?",(d['asset_id'],))
        return {'id':d['id'],'status':status}

def appraise(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'appraisal',file)
        if unique_event(db,'appraisal',d['id'],sid):return {'id':d['id'],'replayed':True}
        asset=db.execute('SELECT * FROM asset WHERE asset_id=?',(d['asset_id'],)).fetchone()
        if not asset:raise ValueError('unknown_asset')
        if d['assessor_a']==d['assessor_b']:raise ValueError('independent_assessors_required')
        percent=int(d['discount_percent'])
        if not 1<=percent<=100:raise ValueError('discount_out_of_range')
        valid_to=at(d['valid_to']);amount=money(d['amount'])
        db.execute('INSERT INTO appraisal VALUES(?,?,?,?,?,?,?,?,?)',(d['id'],d['asset_id'],amount,percent,d['assessor_a'],d['assessor_b'],valid_to,'valid',sid))
        return {'id':d['id'],'loan_ceiling_cents':amount*percent//100}
