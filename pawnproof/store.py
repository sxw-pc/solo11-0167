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
CREATE TABLE IF NOT EXISTS lab_sample(id TEXT PRIMARY KEY,asset_id TEXT,seal TEXT,at TEXT,collector TEXT,source_id INTEGER REFERENCES source_file(id));
CREATE TABLE IF NOT EXISTS lab_result(id TEXT PRIMARY KEY,sample_id TEXT,analyst TEXT,at TEXT,verdict TEXT,source_id INTEGER REFERENCES source_file(id));
'''
@contextmanager
def open_db(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    try:
        db.executescript(SCHEMA)
        yield db
    finally:db.close()
