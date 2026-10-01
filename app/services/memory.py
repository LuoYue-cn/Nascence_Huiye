"""All administrative and QQ memory changes share validation and persistence."""
import json
import numpy as np
from core import memory_engine as me
from utils.session_context import group_context

class MemoryService:
    def list(self, query='', group_id=None, before=None, limit=50):
        conditions,params = [],[]
        if query:
            conditions.append('content LIKE ?'); params.append('%'+query+'%')
        if group_id:
            conditions.append('group_id=?'); params.append(str(group_id))
        if before:
            conditions.append('rowid<?'); params.append(before)
        sql = 'SELECT rowid,* FROM memories' + (' WHERE '+ ' AND '.join(conditions) if conditions else '')
        rows = me._get_db().execute(sql+' ORDER BY rowid DESC LIMIT ?', (*params,limit)).fetchall()
        return [{**{k:v for k,v in me._row_to_memory(row[1:]).items() if k!='vector'}, '_cursor':row[0]} for row in rows]

    def detail(self, mid):
        row = me._get_db().execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
        if not row: raise KeyError(mid)
        result = me._row_to_memory(row); result.pop('vector')
        result['links'] = [dict(zip(('src','tgt','weight','type'),r)) for r in me._get_db().execute(
            'SELECT src,tgt,weight,type FROM links WHERE src=? OR tgt=?',(mid,mid))]
        result['concepts'] = []
        for cid,name,raw in me._get_db().execute('SELECT c.id,c.name,e.member_ids FROM concepts c JOIN concept_events e ON e.concept_id=c.id'):
            if mid in json.loads(raw): result['concepts'].append({'id':cid,'name':name})
        return result

    def create(self, content, half_life, scope, group_id=None):
        vector = me.validate_vector(me.text_to_vector(content))
        with group_context(group_id,source='admin'), me.atomic_memories():
            return me.create_memory(content, half_life, vector=vector, scope=scope, group_id=group_id, source='admin')

    def edit(self, mid, content, half_life, scope, group_id=None):
        vector = me.validate_vector(me.text_to_vector(content))
        with group_context(group_id,source='admin'), me.atomic_memories() as db:
            old = db.execute('SELECT id FROM memories WHERE id=?',(mid,)).fetchone()
            if not old: raise KeyError(mid)
            db.execute('UPDATE memories SET content=?,vector=?,half_life=?,scope=?,group_id=? WHERE id=?',
                       (content,vector.tobytes(),half_life,scope,group_id,mid))
            # Links and concepts may describe the old text or scope. Remove them
            # rather than moving another group's concept/edges across a boundary.
            db.execute('DELETE FROM links WHERE src=? OR tgt=?',(mid,mid))
            for key in [k for k in me.links if mid in k]: me.links.pop(key)
            me._dirty_links = {k for k in me._dirty_links if mid not in k}
            me._deleted_links = {k for k in me._deleted_links if mid not in k}
            from core.concept_store import remove_members
            remove_members([mid])
            db.execute("UPDATE memories SET concept_tag_ids='[]' WHERE id=?",(mid,))
            me.memories.pop(mid,None); me.hot_ids.discard(mid)
            me._load_memory_from_db(mid)
            me._rebuild_faiss_index(); me._build_word_to_memories()
            me.build_initial_links(mid)
        return self.detail(mid)

    def delete(self, mid):
        with me.atomic_memories() as db:
            if not db.execute('SELECT id FROM memories WHERE id=?',(mid,)).fetchone(): raise KeyError(mid)
            db.execute('DELETE FROM memories WHERE id=?',(mid,))
            db.execute('DELETE FROM links WHERE src=? OR tgt=?',(mid,mid))
            for key in [k for k in me.links if mid in k]: me.links.pop(key)
            me._dirty_links = {k for k in me._dirty_links if mid not in k}
            me._deleted_links = {k for k in me._deleted_links if mid not in k}
            me.memories.pop(mid,None); me.hot_ids.discard(mid)
            from core.concept_store import remove_members
            remove_members([mid])
            me._rebuild_faiss_index(); me._build_word_to_memories()
            me._count_mem_deleted += 1

    def import_records(self, records, apply=False):
        existing = set(me._get_db().execute('SELECT scope,group_id,content FROM memories'))
        seen=set(); unique=[]
        for item in records:
            key=(item['scope'],item.get('group_id'),item['content'])
            if key not in existing and key not in seen:
                unique.append(item); seen.add(key)
        if not apply: return {'total':len(records),'new':len(unique),'duplicates':len(records)-len(unique)}
        # Prepare every vector before opening the all-or-nothing transaction.
        vectors = [me.validate_vector(me.text_to_vector(item['content'])) for item in unique]
        ids=[]
        with me.atomic_memories():
            for item,vec in zip(unique,vectors):
                with group_context(item.get('group_id'),source='admin'):
                    ids.append(me.create_memory(item['content'],item['half_life'],vector=vec,
                                               scope=item['scope'],group_id=item.get('group_id'),source='admin'))
        return {'created':len(ids),'ids':ids}
