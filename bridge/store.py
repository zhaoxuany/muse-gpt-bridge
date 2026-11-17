"""Local single-process SQLite store. Completed rounds commit atomically."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import os

def now(): return int(time.time())

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS discussions (
 discussion_id TEXT PRIMARY KEY, openai_conversation_id TEXT, title TEXT NOT NULL,
 original_request TEXT NOT NULL, user_context TEXT NOT NULL, project_goal TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', round INTEGER NOT NULL DEFAULT 0,
 max_rounds INTEGER NOT NULL, budget_cap_cny REAL, cost_total_cny REAL NOT NULL DEFAULT 0,
 final_report TEXT, close_reason TEXT, idempotency_key TEXT UNIQUE,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, closed_at INTEGER);
CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, discussion_id TEXT NOT NULL REFERENCES discussions(discussion_id),
 seq_no INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, structured_json TEXT NOT NULL,
 model TEXT, requested_model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
 cost_cny REAL NOT NULL DEFAULT 0, funding TEXT, idempotency_key TEXT UNIQUE,
 created_at INTEGER NOT NULL, UNIQUE(discussion_id, seq_no));
CREATE TABLE IF NOT EXISTS cost_records (
 call_id TEXT PRIMARY KEY, discussion_id TEXT NOT NULL REFERENCES discussions(discussion_id),
 message_id TEXT REFERENCES messages(message_id), model TEXT NOT NULL, requested_model TEXT,
 prompt_tokens INTEGER, completion_tokens INTEGER, cost_cny REAL NOT NULL,
 funding TEXT NOT NULL, duration_ms INTEGER, is_retry INTEGER NOT NULL,
 status TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
 idempotency_key TEXT PRIMARY KEY, discussion_id TEXT NOT NULL REFERENCES discussions(discussion_id),
 fingerprint TEXT NOT NULL, state TEXT NOT NULL, http_status INTEGER,
 response_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
"""

class Store:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._owner = self.db_path.with_suffix(self.db_path.suffix+'.owner.lock').open('a+b')
        if self._owner.tell()==0: self._owner.write(b'0'); self._owner.flush()
        self._owner.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self._owner.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self._owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self._conn=sqlite3.connect(str(self.db_path),check_same_thread=False)
            self._conn.row_factory=sqlite3.Row
            columns=self._conn.execute('PRAGMA table_info(messages)').fetchall()
            if columns and 'funding' not in [r['name'] for r in columns]:
                raise RuntimeError('Legacy database needs a separate reviewed migration; select a new local DB')
            self._conn.executescript(SCHEMA)
            with self.transaction() as con:
                for row in con.execute("SELECT * FROM operations WHERE state='running'").fetchall():
                    response={'detail':{'code':'interrupted','detail':'Previous process stopped during this request; do not automatically replay it','discussion_id':row['discussion_id']}}
                    con.execute("UPDATE operations SET state='interrupted',http_status=409,response_json=?,updated_at=? WHERE idempotency_key=?",(json.dumps(response),now(),row['idempotency_key']))
        except Exception:
            if hasattr(self,'_conn'):self._conn.close()
            self._owner.close()
            raise

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _one(self,sql,params=()):
        with self._lock:
            row=self._conn.execute(sql,params).fetchone()
            return dict(row) if row else None

    def _all(self,sql,params=()):
        with self._lock:return [dict(r) for r in self._conn.execute(sql,params).fetchall()]

    def get_discussion(self,did):return self._one('SELECT * FROM discussions WHERE discussion_id=?',(did,))
    def get_operation(self,key):return self._one('SELECT * FROM operations WHERE idempotency_key=?',(key,))
    def list_messages(self,did):return self._all('SELECT * FROM messages WHERE discussion_id=? ORDER BY seq_no',(did,))
    def list_cost_records(self,did):return self._all('SELECT * FROM cost_records WHERE discussion_id=? ORDER BY created_at,rowid',(did,))

    def begin(self,key,did,fingerprint,discussion=None):
        with self.transaction() as con:
            if discussion:self._insert(con,'discussions',discussion)
            con.execute("INSERT INTO operations VALUES(?,?,?,'running',NULL,NULL,?,?)",(key,did,fingerprint,now(),now()))

    @staticmethod
    def _insert(con,table,row):
        fields=list(row)
        con.execute('INSERT INTO '+table+' ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(row.values()))

    def fail(self,key,response,status):
        with self.transaction() as con:
            con.execute("UPDATE operations SET state='failed',http_status=?,response_json=?,updated_at=? WHERE idempotency_key=?",(status,json.dumps(response,ensure_ascii=False),now(),key))

    def commit_round(self,did,key,user_row,gpt_row,cost_row,fields,response,status):
        with self.transaction() as con:
            self._insert(con,'messages',user_row)
            self._insert(con,'messages',gpt_row)
            self._insert(con,'cost_records',cost_row)
            con.execute('UPDATE discussions SET '+','.join(k+'=?' for k in fields)+' WHERE discussion_id=?',(*fields.values(),did))
            con.execute("UPDATE operations SET state='completed',http_status=?,response_json=?,updated_at=? WHERE idempotency_key=?",(status,json.dumps(response,ensure_ascii=False),now(),key))

    def update_discussion(self,did,**fields):
        if set(fields)-{'status','closed_at','final_report','close_reason','updated_at'}:raise ValueError('Invalid update fields')
        fields.setdefault('updated_at',now())
        with self.transaction() as con:
            con.execute('UPDATE discussions SET '+','.join(k+'=?' for k in fields)+' WHERE discussion_id=?',(*fields.values(),did))

    def close(self):
        self._conn.close()
        self._owner.close()
