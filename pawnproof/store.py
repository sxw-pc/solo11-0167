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
CREATE TABLE IF NOT EXISTS payment_payer(payment_id TEXT PRIMARY KEY,loan_id TEXT,payer TEXT,relationship TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS extension(id TEXT PRIMARY KEY,loan_id TEXT REFERENCES loan(id),new_due_at TEXT,customer_actor TEXT,shop_actor TEXT,at TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS notice(id TEXT PRIMARY KEY,loan_id TEXT REFERENCES loan(id),delivered_at TEXT,grace_until TEXT,recipient TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,loan_id TEXT,kind TEXT,status TEXT,reason TEXT,at TEXT,source_id INTEGER REFERENCES source_file(id));
-- Inheritance intake.  Rows are append-only observations.  A share assertion
-- is never edited; a corrected assertion is a new heir record with another id.
CREATE TABLE IF NOT EXISTS heir(id TEXT PRIMARY KEY,customer TEXT,heir_id TEXT,share_num INTEGER,share_den INTEGER,deceased_at TEXT,asserted_at TEXT,status TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS customer(customer TEXT,status TEXT,verified_at TEXT,
    authority TEXT,deceased_at TEXT,source_id INTEGER REFERENCES source_file(id),
    PRIMARY KEY(customer,verified_at));
CREATE TABLE IF NOT EXISTS inheritance_document(
    id TEXT PRIMARY KEY,kind TEXT,customer TEXT,asset_id TEXT,loan_id TEXT,
    grantor TEXT,representative TEXT,scope TEXT,
    effective_from TEXT,effective_to TEXT,received_at TEXT,
    revokes TEXT,court_effect TEXT,court_priority INTEGER,authority TEXT,
    custody_point TEXT,body_sha TEXT,body_present INTEGER,state TEXT,detail TEXT,
    source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS document_body(document_id TEXT PRIMARY KEY,body_sha TEXT,received_at TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS counter_verification(id TEXT PRIMARY KEY,subject TEXT,verifier TEXT,at TEXT,matched INTEGER,method TEXT,source_id INTEGER);
-- A route is fixed before any physical movement and cannot be changed later.
CREATE TABLE IF NOT EXISTS handoff_plan(id TEXT PRIMARY KEY,loan_id TEXT,asset_id TEXT,route TEXT,
    recipient TEXT,recipient_kind TEXT,court_order_id TEXT,slot_id TEXT,
    planned_at TEXT,fixed_at TEXT,status TEXT,source_id INTEGER);
-- Write-ahead dispatch intent: durable before the slot is opened.
CREATE TABLE IF NOT EXISTS dispatch_intent(id TEXT PRIMARY KEY,plan_id TEXT,loan_id TEXT,asset_id TEXT,
    slot_id TEXT,actor_a TEXT,actor_b TEXT,recipient TEXT,recipient_kind TEXT,route TEXT,
    at TEXT,status TEXT,source_id INTEGER);
-- Append-only physical observations.  A row is never deleted or overwritten by
-- a later delivery attempt; corrections are new rows/events.
CREATE TABLE IF NOT EXISTS physical_handover(id TEXT PRIMARY KEY,plan_id TEXT,dispatch_id TEXT,
    loan_id TEXT,asset_id TEXT,route TEXT,recipient TEXT,recipient_kind TEXT,
    accepted INTEGER,status TEXT,reason TEXT,actual_custodian TEXT,holding_party TEXT,
    vault_actor_a TEXT,vault_actor_b TEXT,counter_verification_id TEXT,
    court_registration_id TEXT,receipt_sha TEXT,lifted_from_slot INTEGER,
    finalized INTEGER NOT NULL DEFAULT 0,at TEXT,detail TEXT,
    log_sha TEXT UNIQUE,source_id INTEGER);
CREATE TABLE IF NOT EXISTS position_log(id INTEGER PRIMARY KEY,asset_id TEXT,at TEXT,location TEXT,
    holder TEXT,reason TEXT,handover_id TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS handoff_event(id INTEGER PRIMARY KEY,plan_id TEXT,phase TEXT,state TEXT,
    at TEXT,detail TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS dispute(id INTEGER PRIMARY KEY,loan_id TEXT,handover_id TEXT,reason TEXT,
    opened_at TEXT,status TEXT,detail TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS recourse(id INTEGER PRIMARY KEY,dispute_id INTEGER,action TEXT,against TEXT,
    opened_at TEXT,status TEXT,detail TEXT);
'''
@contextmanager
def open_db(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    try:
        db.executescript(SCHEMA)
        yield db
    finally:db.close()
