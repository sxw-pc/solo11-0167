import hashlib,json
from pathlib import Path

def read_source(db,kind,file):
    raw=Path(file).read_bytes();digest=hashlib.sha256(raw).hexdigest();doc=json.loads(raw)
    old=db.execute('SELECT id,kind FROM source_file WHERE sha=?',(digest,)).fetchone()
    if old:
        if old['kind']!=kind:raise ValueError('source_kind_conflict')
        return old['id'],doc,True
    cur=db.execute('INSERT INTO source_file(kind,sha,raw) VALUES(?,?,?)',(kind,digest,raw))
    return cur.lastrowid,doc,False

def unique_event(db,table,event_id,source_id):
    old=db.execute(f'SELECT source_id FROM {table} WHERE id=?',(event_id,)).fetchone()
    if old:
        if old['source_id']!=source_id:raise ValueError('event_id_conflict')
        return True
    return False

def money(value):
    from decimal import Decimal
    amount=Decimal(str(value))*100
    if amount!=amount.to_integral_value() or amount<=0:raise ValueError('invalid_amount')
    return int(amount)

def at(value):
    from datetime import datetime
    if not isinstance(value,str) or not value.endswith('Z'):raise ValueError('utc_time_required')
    datetime.fromisoformat(value[:-1]+'+00:00')
    return value

def snapshot(db):
    out={}
    for table in ('asset','ownership','appraisal','vault_slot','custody','loan','payment','extension','notice','decision','lab_sample','lab_result'):
        out[table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
    return out
