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
CREATE TABLE IF NOT EXISTS dossier(id TEXT PRIMARY KEY,loan_id TEXT,authority TEXT,digest TEXT,file_path TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS dossier_review(id TEXT PRIMARY KEY,dossier_id TEXT,authority TEXT,digest TEXT,outcome TEXT,status TEXT);
-- Versioned, time-point sealed dossier registry (the independent enhanced workflow).
CREATE TABLE IF NOT EXISTS dossier_version(
    id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL,
    authority TEXT NOT NULL,
    version INTEGER NOT NULL,
    supersedes TEXT,
    commitment TEXT NOT NULL,
    file_digest TEXT NOT NULL,
    file_path TEXT NOT NULL,
    raw BLOB NOT NULL,
    loan_status_at_capture TEXT NOT NULL,
    asset_status_at_capture TEXT NOT NULL,
    watermark_kind TEXT NOT NULL,
    watermark_value INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    blinding_salt TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    current INTEGER NOT NULL,
    publish_state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dossier_source(
    dossier_id TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    sha TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY(dossier_id,source_id)
);
CREATE TABLE IF NOT EXISTS dossier_release(
    id TEXT PRIMARY KEY,
    dossier_id TEXT NOT NULL,
    authority TEXT NOT NULL,
    purpose TEXT NOT NULL,
    fields TEXT NOT NULL,
    commitment TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    file_path TEXT NOT NULL,
    raw BLOB NOT NULL,
    locked INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dossier_acknowledgment(
    id TEXT PRIMARY KEY,
    dossier_id TEXT NOT NULL,
    release_id TEXT,
    authority TEXT NOT NULL,
    digest TEXT NOT NULL,
    at TEXT NOT NULL,
    matched INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dossier_revocation(
    dossier_id TEXT PRIMARY KEY,
    superseded_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dossier_adjudication(
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    dossier_id TEXT NOT NULL,
    authority TEXT NOT NULL,
    purpose TEXT NOT NULL,
    verdict TEXT NOT NULL,
    is_current_claim INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    at TEXT NOT NULL
);
'''
@contextmanager
def open_db(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    try:
        db.executescript(SCHEMA)
        yield db
    finally:db.close()
