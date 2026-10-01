"""SQLite history with unbounded durable sequence numbers and group context."""
import json
from config.constants import BOT_NAME
from utils.session_context import current_group, current_message
from utils.paths import data_path
from app.repositories.sqlite import history_repository

_HISTORY_FILE = data_path('message_history.log')
_STATE_FILE = data_path('message_state.json')

def add_message(sender, content, source, quote=None):
    role = 'bot' if sender == BOT_NAME else 'user'
    if source not in ('QQ', 'qq'):
        role = 'internal'
    return history_repository().history_add(sender, content, source, current_group.get(),
                                            quote, current_message.get(), role)

def quote_suffix(msg):
    quote = msg.get('quote') or {}
    return f"，这是在回应{quote.get('sender') or '某人'}之前说的：“{quote['text']}”" if quote.get('text') else ''

def render_message(msg):
    return f"{msg['sender']}说：{msg['content']}{quote_suffix(msg)}"

def get_all():
    group = current_group.get()
    # No default group means admin context, never a merged dialogue prompt.
    return history_repository().history(group) if group is not None else []

def get_recent(n=10):
    return '\n'.join(render_message(m) for m in get_all()[-n:])

def save_state():
    return None  # Every message is already committed.

def load_state():
    return None  # Legacy import is an explicit offline migration.

def flush_to_file():
    repo = history_repository()
    with repo.lock:
        rows = repo.query("SELECT value FROM settings WHERE key='history_log_cursor'")
        cursor = int(rows[0]['value']) if rows else 0
        pending = repo.query('SELECT * FROM messages WHERE seq>? ORDER BY seq', (cursor,))
        if not pending:
            return
        from pathlib import Path
        Path(_HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(_HISTORY_FILE, 'a', encoding='utf-8') as f:
            for row in pending:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
        repo.execute("INSERT OR REPLACE INTO settings VALUES('history_log_cursor',?)", (str(pending[-1]['seq']),))
