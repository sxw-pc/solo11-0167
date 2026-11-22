import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,kind TEXT,sha TEXT UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS asset(asset_id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,serial TEXT NOT NULL UNIQUE,status TEXT NOT NULL,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS ownership(id TEXT PRIMARY KEY,asset_id TEXT REFERENCES asset(asset_id),claimant TEXT,valid_from TEXT,valid_to TEXT,status TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS appraisal(id TEXT PRIMARY KEY,asset_id TEXT REFERENCES asset(asset_id),amount_cents INTEGER,discount_percent INTEGER,assessor_a TEXT,assessor_b TEXT,valid_to TEXT,status TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS vault_slot(slot_id TEXT PRIMARY KEY,capacity INTEGER NOT NULL,occupied_asset TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS custody(id TEXT PRIMARY KEY,asset_id TEXT REFERENCES asset(asset_id),kind TEXT,slot_id TEXT,seal TEXT,actor_a TEXT,actor_b TEXT,at TEXT,status TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS loan(id TEXT PRIMARY KEY,asset_id TEXT REFERENCES asset(asset_id),principal_cents INTEGER,interest_cents INTEGER,due_at TEXT,customer TEXT,status TEXT,appraisal_id TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS payment(id TEXT PRIMARY KEY,loan_id TEXT REFERENCES loan(id),amount_cents INTEGER,at TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS extension(id TEXT PRIMARY KEY,loan_id TEXT REFERENCES loan(id),new_due_at TEXT,customer_actor TEXT,shop_actor TEXT,at TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS notice(id TEXT PRIMARY KEY,loan_id TEXT REFERENCES loan(id),delivered_at TEXT,grace_until TEXT,recipient TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,loan_id TEXT,kind TEXT,status TEXT,reason TEXT,at TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS privacy_request(id TEXT PRIMARY KEY,customer TEXT,at TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS disposal_job(
 job_id TEXT PRIMARY KEY,request_id TEXT,customer TEXT,route TEXT NOT NULL,
 purpose TEXT NOT NULL,status TEXT NOT NULL,locked_at TEXT,salt TEXT,pid TEXT,
 CHECK(route IN ('controlled_retention','minimal_retention')),
 CHECK(status IN ('planned','executing','completing','blocked','completed','failed')));
CREATE TABLE IF NOT EXISTS disposal_field(
 id INTEGER PRIMARY KEY,job_id TEXT,table_name TEXT,column_name TEXT,
 source_id INTEGER,classification TEXT NOT NULL,basis TEXT,
 CHECK(classification IN ('active_pledge_required','historical_proof','no_retention_basis')));
CREATE TABLE IF NOT EXISTS disposal_step(job_id TEXT,step TEXT,state TEXT,detail TEXT,PRIMARY KEY(job_id,step));
CREATE TABLE IF NOT EXISTS disposal_event(
 id INTEGER PRIMARY KEY,job_id TEXT,request_id TEXT,kind TEXT,at TEXT,actor TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS identity_commitment(
 job_id TEXT,source_id INTEGER,original_sha TEXT,redacted_sha TEXT,pid TEXT,
 kind TEXT,evidence_digest TEXT,PRIMARY KEY(job_id,source_id));
CREATE TABLE IF NOT EXISTS custody_package(
 job_id TEXT PRIMARY KEY,package_path TEXT,blob_sha TEXT,nonce TEXT,
 custodian_a TEXT,custodian_b TEXT,share_a_path TEXT,share_b_path TEXT,
 share_a_sha TEXT,share_b_sha TEXT,sealed_at TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS distribution_copy(
 copy_id TEXT PRIMARY KEY,job_id TEXT,medium TEXT,location TEXT,status TEXT,
 original_sha TEXT,updated_sha TEXT,attestation TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS pseudonym(pid TEXT PRIMARY KEY,salt TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS privacy_disposal(
 request_id TEXT PRIMARY KEY,job_id TEXT,state TEXT,route TEXT,
 identity_recoverable INTEGER,completed_at TEXT,reason TEXT);
'''
@contextmanager
def open_db(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    try:
        db.executescript(SCHEMA)
        yield db
    finally:db.close()
