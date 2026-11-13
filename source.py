import sqlite3

SCHEMA = """
CREATE TABLE versions(
 id TEXT NOT NULL, rev INTEGER NOT NULL, entity TEXT NOT NULL,
 event_time INTEGER NOT NULL, available_at INTEGER NOT NULL,
 amount INTEGER NOT NULL, deleted INTEGER NOT NULL,
 PRIMARY KEY(id,rev));
CREATE TABLE samples(
 sample_id TEXT PRIMARY KEY, entity TEXT NOT NULL,
 prediction_time INTEGER NOT NULL, cutoff INTEGER NOT NULL);
CREATE TABLE features(
 sample_id TEXT PRIMARY KEY, total TEXT NOT NULL, n INTEGER NOT NULL);
"""

def integer(value):
    return type(value) is int and -(2**63) < value < 2**63

def validate_version(row):
    if not isinstance(row, list) or len(row) != 7:
        raise ValueError("BAD_VERSION")
    rid, rev, entity, event_time, available_at, amount, deleted = row
    if not all(type(x) is str and x for x in (rid, entity)):
        raise ValueError("BAD_VERSION")
    if not all(integer(x) for x in (rev, event_time, available_at, amount)):
        raise ValueError("BAD_VERSION")
    if rev <= 0 or type(deleted) is not bool:
        raise ValueError("BAD_VERSION")

def connect(samples):
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    seen = set()
    with db:
        for row in samples:
            if not isinstance(row, list) or len(row) != 4:
                raise ValueError("BAD_SAMPLE")
            sid, entity, t, c = row
            if not all(type(x) is str and x for x in (sid, entity)):
                raise ValueError("BAD_SAMPLE")
            if not integer(t) or not integer(c) or sid in seen:
                raise ValueError("BAD_SAMPLE")
            seen.add(sid)
            db.execute("INSERT INTO samples VALUES(?,?,?,?)", row)
            db.execute("INSERT INTO features VALUES(?,0,0)", (sid,))
    return db

def import_versions(db, rows, trace):
    if not isinstance(rows, list):
        raise ValueError("BAD_BATCH")
    for row in rows:
        validate_version(row)
    accepted = 0
    for row in rows:
        previous = db.execute(
            "SELECT * FROM versions WHERE id=? AND rev=?", row[:2]).fetchone()
        trace.append(["version_lookup", row[0], row[1]])
        if previous is not None:
            if tuple(previous) != tuple(row):
                raise ValueError("REVISION_CONFLICT")
            continue
        db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?)", row)
        accepted += 1
    return accepted

def all_versions(db, trace):
    rows = [list(r) for r in db.execute("SELECT * FROM versions ORDER BY id,rev")]
    trace.append(["versions_full_scan", len(rows)])
    return rows

def all_samples(db, trace):
    rows = [list(r) for r in db.execute("SELECT * FROM samples ORDER BY sample_id")]
    trace.append(["samples_full_scan", len(rows)])
    return rows
