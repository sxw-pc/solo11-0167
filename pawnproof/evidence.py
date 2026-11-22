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
    for table in ('asset','ownership','appraisal','vault_slot','custody','loan','payment','extension','notice','decision'):
        out[table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
    return out


# ---------------------------------------------------------------------------
# Field-level retention dependency support (privacy disposal)
# ---------------------------------------------------------------------------

# Every table/column that can carry a customer's original identity.  These are
# the only locations the disposal job is allowed to pseudonymise; everything
# else is evidence that must survive the request.
IDENTITY_FIELDS=(
    ('asset','owner'),
    ('ownership','claimant'),
    ('loan','customer'),
    ('extension','customer_actor'),
    ('notice','recipient'),
    ('source_file','raw'),
)

# JSON keys that carry identity inside an original source document.
IDENTITY_JSON_KEYS=('owner','claimant','customer','customer_actor','recipient')

# Columns whose value must remain auditable after disposal: amounts, moments
# and the relations proving the same customer stood behind each event.
HISTORICAL_PROOF_COLUMNS={
    'loan':('asset_id','principal_cents','interest_cents','due_at','status','appraisal_id'),
    'payment':('amount_cents','at'),
    'decision':('kind','status','reason','at'),
    'ownership':('asset_id','valid_from','valid_to','status'),
    'asset':('asset_id','kind','serial','status'),
    'custody':('kind','slot_id','seal','at','status'),
    'extension':('new_due_at','at'),
    'notice':('delivered_at','grace_until'),
}

FIELD_BASIS={
    'active_pledge_required':'进行中质押仍需识别当事人，处置前不得撤下',
    'historical_proof':'结清后仅为历史证明，保留值但撤去可读身份',
    'no_retention_basis':'无保留依据，须从普通访问与原文字节中清除',
}


def derive_pid(customer,salt):
    """Irreversible pseudonym: HMAC-SHA256 keyed by a per-request salt."""
    import hmac
    return 'PID-'+hmac.new(salt.encode('utf8'),customer.encode('utf8'),'sha256').hexdigest()[:24]


def table_exists(db,name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def has_active_pledge(db,customer):
    return db.execute(
        "SELECT 1 FROM loan WHERE customer=? AND status IN ('active','extended','disputed') LIMIT 1",
        (customer,)).fetchone() is not None


def customer_sources(db,customer):
    """source_file rows whose raw JSON bytes name the customer."""
    hits=[]
    for row in db.execute('SELECT id,kind,sha,raw FROM source_file ORDER BY id'):
        try:
            doc=json.loads(row['raw'].decode('utf8'))
        except (UnicodeDecodeError,json.JSONDecodeError):
            continue
        if isinstance(doc,dict) and customer in _json_identity_values(doc):
            hits.append(dict(row))
    return hits


def _json_identity_values(doc):
    return [doc[k] for k in IDENTITY_JSON_KEYS if k in doc and isinstance(doc[k],str)]


def identity_field_hits(db,customer):
    """Concrete (table,column,row_key) occurrences of the identity string."""
    hits=[]
    for table,column in IDENTITY_FIELDS:
        if table=='source_file' or not table_exists(db,table):
            continue
        rows=db.execute(f'SELECT * FROM {table}').fetchall()
        for row in rows:
            if row[column]==customer:
                key='id' if 'id' in row.keys() else ('asset_id' if table=='asset' else row.keys()[0])
                hits.append((table,column,row[key]))
    hits.extend(('source_file','raw',s['id']) for s in customer_sources(db,customer))
    return hits


def redact_document(raw,customer,pid):
    """Rewrite one source document's JSON bytes, replacing identity only."""
    doc=json.loads(raw.decode('utf8'))
    for key in IDENTITY_JSON_KEYS:
        if isinstance(doc,dict) and doc.get(key)==customer:
            doc[key]=pid
    return json.dumps(doc,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode('utf8')


def scan_identity_bytes(db,customer):
    """True while any stored source blob still carries the identity as an
    actual identity *value* (a coincidental substring such as an event id
    named "C1" does not count)."""
    for row in db.execute('SELECT raw FROM source_file'):
        try:
            doc=json.loads(row['raw'].decode('utf8'))
        except (UnicodeDecodeError,json.JSONDecodeError):
            if customer.encode('utf8') in row['raw']:
                return True
            continue
        if isinstance(doc,dict) and customer in _json_identity_values(doc):
            return True
    return False


def fingerprint_customer(db,customer):
    """Stable hash of every row mentioning a customer, used to prove C2 is
    untouched across a backup refresh."""
    material=[]
    for table in ('asset','ownership','loan','extension','notice','custody','payment','decision'):
        if not table_exists(db,table):
            continue
        for row in db.execute(f'SELECT * FROM {table} ORDER BY 1'):
            data=dict(row)
            if any(v==customer for v in data.values()):
                material.append((table,data))
    blob=json.dumps(material,sort_keys=True,default=str).encode('utf8')
    return hashlib.sha256(blob).hexdigest()
