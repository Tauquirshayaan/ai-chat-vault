import base64
import sqlite3

from chatvault import antigravity_index as fix

INDEX_KEY = "antigravityUnifiedStateSync.trajectorySummaries"


def workspace_field(uri, repo_root, git_info=b"\n\x0buser/remote"):
    """Field 9 shaped like real Antigravity data: URI, repo root, git info."""
    sub = (fix.encode_string_field(1, uri) + fix.encode_string_field(2, repo_root)
           + fix.encode_length_delimited(3, git_info))
    return fix.encode_length_delimited(9, sub)


def inner_blob(title, workspace=None, updated=100):
    blob = fix.encode_string_field(1, title) + fix.build_timestamp_fields(updated)
    if workspace:
        blob += workspace
    blob += fix.encode_string_field(15, "opaque-state")
    return blob


def encode_index(entries):
    """entries: list of (conversation_id, inner_blob)."""
    raw = b""
    for cid, inner in entries:
        info = fix.encode_string_field(1, base64.b64encode(inner).decode())
        entry = fix.encode_string_field(1, cid) + fix.encode_length_delimited(2, info)
        raw += fix.encode_length_delimited(1, entry)
    return base64.b64encode(raw).decode()


def make_state_db(path, value):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    if value is not None:
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)", (INDEX_KEY, value))
    conn.commit()
    conn.close()


def read_state_value(path):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM ItemTable WHERE key=?", (INDEX_KEY,)).fetchone()
    conn.close()
    return row[0] if row else None


def decode_index(value):
    """Return {cid: inner_blob} from a stored index value."""
    result = {}
    for entry in fix.iter_length_delimited_fields(base64.b64decode(value), 1):
        cid = next(fix.iter_length_delimited_fields(entry, 1)).decode()
        info = next(fix.iter_length_delimited_fields(entry, 2))
        b64 = next(fix.iter_length_delimited_fields(info, 1))
        result[cid] = base64.b64decode(b64)
    return result
