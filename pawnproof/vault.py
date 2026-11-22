from .store import open_db
from .evidence import at,read_source,unique_event

def import_vault(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'vault',file)
        if replay:return {'slot_id':d['slot_id'],'replayed':True}
        if int(d['capacity'])!=1:raise ValueError('one_asset_per_slot')
        db.execute('INSERT INTO vault_slot VALUES(?,?,NULL)',(d['slot_id'],1))
        return {'slot_id':d['slot_id'],'status':'available'}

def custody(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'custody',file)
        if unique_event(db,'custody',d['id'],sid):return {'id':d['id'],'replayed':True}
        asset=db.execute('SELECT * FROM asset WHERE asset_id=?',(d['asset_id'],)).fetchone()
        if not asset:raise ValueError('unknown_asset')
        if d['actor_a']==d['actor_b']:raise ValueError('independent_custodians_required')
        moment=at(d['at']);kind=d['kind'];slot=d['slot_id'];seal=d.get('seal','')
        active=db.execute("SELECT * FROM custody WHERE asset_id=? ORDER BY at DESC,id DESC LIMIT 1",(d['asset_id'],)).fetchone()
        if kind=='seal':
            if asset['status']!='candidate' or not seal:raise ValueError('asset_not_sealable')
            place=db.execute('SELECT * FROM vault_slot WHERE slot_id=?',(slot,)).fetchone()
            if not place or place['occupied_asset']:raise ValueError('slot_occupied')
            db.execute('UPDATE vault_slot SET occupied_asset=? WHERE slot_id=?',(d['asset_id'],slot))
            db.execute("UPDATE asset SET status='sealed' WHERE asset_id=?",(d['asset_id'],))
            status='sound'
        elif kind=='transfer':
            if not active or active['status']!='sound' or active['at']>=moment or active['seal']!=seal:
                raise ValueError('custody_chain_broken')
            place=db.execute('SELECT * FROM vault_slot WHERE slot_id=?',(slot,)).fetchone()
            if not place or place['occupied_asset']:raise ValueError('slot_occupied')
            db.execute('UPDATE vault_slot SET occupied_asset=NULL WHERE slot_id=?',(active['slot_id'],))
            db.execute('UPDATE vault_slot SET occupied_asset=? WHERE slot_id=?',(d['asset_id'],slot))
            status='sound'
        elif kind=='audit':
            if not active or active['at']>=moment:raise ValueError('audit_out_of_order')
            status='sound' if active['slot_id']==slot and active['seal']==seal and d.get('observed',True) else 'disputed'
            if status=='disputed':db.execute("UPDATE asset SET status='disputed' WHERE asset_id=?",(d['asset_id'],))
        else:raise ValueError('unknown_custody_kind')
        db.execute('INSERT INTO custody VALUES(?,?,?,?,?,?,?,?,?,?)',(d['id'],d['asset_id'],kind,slot,seal,d['actor_a'],d['actor_b'],moment,status,sid))
        return {'id':d['id'],'status':status,'asset_status':db.execute('SELECT status FROM asset WHERE asset_id=?',(d['asset_id'],)).fetchone()[0]}

def release_slot(db,asset_id):
    db.execute('UPDATE vault_slot SET occupied_asset=NULL WHERE occupied_asset=?',(asset_id,))
    db.execute("UPDATE asset SET status='released' WHERE asset_id=?",(asset_id,))
