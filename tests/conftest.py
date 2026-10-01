import hashlib
import os
import tempfile
from pathlib import Path
import numpy as np
import pytest

TEST_ROOT=Path(tempfile.mkdtemp(prefix='huiye-tests-'))
os.environ['HUIYE_DATA_DIR']=str(TEST_ROOT/'data')
os.environ['HUIYE_CONFIG_DIR']=str(TEST_ROOT/'config')
os.environ['HUIYE_ADMIN_PASSWORD']='test-admin-password-12345'
os.environ['HUIYE_NAPCAT_TOKEN']='test-napcat-token-123456'


def embedding(text):
    from core import memory_engine as me
    rng=np.random.default_rng(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8],'big'))
    vec=rng.normal(size=me.EMBED_DIM).astype('float32');return vec/np.linalg.norm(vec)

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    import requests,httpx
    def forbidden(*args,**kwargs):raise AssertionError('External network is forbidden in regression tests')
    monkeypatch.setattr(requests.sessions.Session,'request',forbidden)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    from core import memory_engine as me,concept_store as cs
    from config.api_config import config,DEFAULT_CONFIG
    from app.repositories import sqlite as histories
    from qq_bot import get_manifest
    from core.biorhythm import BIORHYTHM
    db=me._get_db();cs._get_db()
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0]!='sqlite_sequence']
    for table in tables: db.execute(f'DELETE FROM {table}')
    db.commit()
    if histories._history_repository: histories._history_repository.close();histories._history_repository=None
    me.memories.clear();me.hot_ids.clear();me.links.clear();me.wordweb.clear();me.pending_deletion.clear();me._dirty_links.clear();me._deleted_links.clear();me._last_created_ids.clear()
    me._import_metrics_counters({k:0 for k in me._export_metrics_counters()})
    me._init_faiss_index();me._build_word_to_memories();cs._init_concept_faiss()
    config.clear();config.update(DEFAULT_CONFIG);config['action_enabled']=False
    (TEST_ROOT/'config'/'api_config.json').unlink(missing_ok=True)
    me.OLLAMA_BASE_URL=config['ollama_base_url'];me.OLLAMA_EMBED_MODEL=config['ollama_embed_model']
    get_manifest().save(['111','222'],{})
    BIORHYTHM.wake('test')
    monkeypatch.setattr(me,'text_to_vector',embedding)
    from utils import persistence
    persistence._last_save_time=0
    from utils.dialogue_state import import_states
    import_states({})
    for name in ('memory.json','clock_state.txt','offline_operation.json','dialogue_state.json','metrics_counters.json','biorhythm.json','message_history.log','metrics_baseline.json','metrics_daily.jsonl'):
        (TEST_ROOT/'data'/name).unlink(missing_ok=True)
    yield
