"""Durable QQ jobs, delivery state, management events and authenticated sessions."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from utils.paths import DATA_DIR

class Repository:
    def __init__(self, path=None):
        self.path = Path(path or DATA_DIR / 'memory.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS messages (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
          group_id TEXT, sender TEXT NOT NULL, sender_id TEXT, role TEXT NOT NULL,
          content TEXT NOT NULL, quote TEXT, received_at REAL NOT NULL,
          source TEXT NOT NULL DEFAULT 'qq', time_basis TEXT NOT NULL DEFAULT 'unix_utc',
          external_id TEXT, bot_id TEXT,
          UNIQUE(bot_id, group_id, external_id, role));
        CREATE TABLE IF NOT EXISTS turn_jobs (
          id TEXT PRIMARY KEY, message_id TEXT, group_id TEXT NOT NULL,
          raw TEXT, stage TEXT NOT NULL, fragments TEXT, memory_ids TEXT,
          decision TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS delivery_outbox (
          id TEXT PRIMARY KEY, job_id TEXT, group_id TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, echo TEXT,
          reply_vector BLOB, error TEXT, channel_message_id TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS management_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          payload TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
          details TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_accounts (name TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_sessions (
          token_hash TEXT PRIMARY KEY, name TEXT NOT NULL, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_group_seq ON messages(group_id, seq);
        CREATE INDEX IF NOT EXISTS jobs_stage ON turn_jobs(stage, created_at);
        ''')
        self.db.commit()

    def query(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self.lock, self.db:
            return self.db.execute(sql, params).rowcount

    def accept(self, data, max_pending=100):
        group = str(data['group_id'])
        bot = str(data['self_id'])
        ext = str(data['message_id'])
        now = time.time()
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT id FROM messages WHERE bot_id=? AND group_id=? AND external_id=? AND role='user'",
                (bot, group, ext)).fetchone()
            if existing:
                row = self.db.execute('SELECT id FROM turn_jobs WHERE message_id=?', (existing['id'],)).fetchone()
                return (row['id'] if row else None), False
            count = self.db.execute("SELECT COUNT(*) FROM turn_jobs WHERE stage IN ('queued','processing','prepared','stored','generated')").fetchone()[0]
            if count >= max_pending:
                raise OverflowError('QQ queue is full; event was not accepted')
            mid, jid = uuid.uuid4().hex, uuid.uuid4().hex
            sender = data.get('sender') or {}
            self.db.execute('''INSERT INTO messages
                (id,group_id,sender,sender_id,role,content,received_at,external_id,bot_id)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (mid, group, sender.get('card') or sender.get('nickname') or str(data['user_id']),
                 str(data['user_id']), 'user', str(data.get('raw_message') or ''), now, ext, bot))
            self.db.execute('''INSERT INTO turn_jobs(id,message_id,group_id,raw,stage,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)''', (jid, mid, group, json.dumps(data, ensure_ascii=False), 'queued', now, now))
            return jid, True

    def job(self, jid):
        rows = self.query('SELECT * FROM turn_jobs WHERE id=?', (jid,))
        return rows[0] if rows else None

    def update_job(self, jid, stage, **fields):
        allowed = {'fragments','memory_ids','decision','error','attempts'}
        if not set(fields) <= allowed:
            raise ValueError('Unknown job field')
        assignments = ['stage=?', 'updated_at=?'] + [f'{key}=?' for key in fields]
        self.execute('UPDATE turn_jobs SET ' + ','.join(assignments) + ' WHERE id=?',
                     (stage, time.time(), *fields.values(), jid))
        self.event('job', {'id': jid, 'stage': stage})

    def event(self, kind, payload):
        with self.lock, self.db:
            cur = self.db.execute('INSERT INTO management_events(kind,payload,created_at) VALUES(?,?,?)',
                                  (kind, json.dumps(payload, ensure_ascii=False), time.time()))
            self.db.execute('DELETE FROM management_events WHERE seq < ?', (cur.lastrowid - 2000,))
            return cur.lastrowid

    def events(self, after):
        return self.query('SELECT * FROM management_events WHERE seq>? ORDER BY seq LIMIT 100', (after,))

    def audit(self, action, details):
        self.execute('INSERT INTO audit_events(action,details,created_at) VALUES(?,?,?)',
                     (action, json.dumps(details, ensure_ascii=False), time.time()))

    def history_add(self, sender, content, source, group_id, quote=None, message_id=None, role=None):
        role = role or ('internal' if source != 'QQ' else 'user')
        with self.lock, self.db:
            if message_id and role == 'user':
                self.db.execute('UPDATE messages SET sender=?, content=?, quote=? WHERE id=?',
                                (sender or '', content, json.dumps(quote) if quote else None, message_id))
                return message_id
            mid = uuid.uuid4().hex
            self.db.execute('''INSERT INTO messages(id,group_id,sender,role,content,quote,received_at,source)
                VALUES(?,?,?,?,?,?,?,?)''',
                (mid, group_id, sender or '', role, content, json.dumps(quote) if quote else None, time.time(), source))
            return mid

    def history(self, group_id=None, before=None, limit=200, include_internal=False):
        where, params = [], []
        if group_id is not None:
            where.append('group_id=?'); params.append(str(group_id))
        if before is not None:
            where.append('seq<?'); params.append(before)
        if not include_internal:
            where.append("role IN ('user','bot')")
        sql = 'SELECT * FROM messages' + (' WHERE ' + ' AND '.join(where) if where else '')
        rows = self.query(sql + ' ORDER BY seq DESC LIMIT ?', (*params, min(max(limit,1),500)))
        for row in rows:
            row['time'] = row['received_at']
            row['quote'] = json.loads(row['quote']) if row['quote'] else None
        return list(reversed(rows))

    def recover(self):
        # A process may die after sending but before receiving its ACK. Never blindly resend.
        self.execute("UPDATE delivery_outbox SET status='unknown',error='Service restarted before ACK' WHERE status='sending'")
        self.execute("UPDATE turn_jobs SET stage='queued' WHERE stage='processing'")
        return self.query("SELECT id FROM turn_jobs WHERE stage IN ('queued','prepared','stored','generated') ORDER BY created_at")

    def close(self):
        with self.lock:
            self.db.close()

_history_repository = None

def history_repository():
    global _history_repository
    if _history_repository is None:
        _history_repository = Repository()
    return _history_repository
