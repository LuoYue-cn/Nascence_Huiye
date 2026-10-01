"""One serialized cognition coordinator, with separate state for each QQ group."""
import json
import time
from collections import Counter, deque
from dataclasses import dataclass, field
import numpy as np
from core import memory_engine as me, cognition as cog, asset_library as assets
from core.biorhythm import BIORHYTHM
from core.llm_interface import decompose_input, verbalize
from core.concept_store import record_concept, _embed
from config.constants import BOT_NAME
from utils.dialogue_state import set_state, get_state
from utils.message_history import get_all, quote_suffix
from utils.persistence import save_state
from utils.session_context import group_context
from qq_bot import get_manifest, get_bot_qq, build_augmented_input, message_text

@dataclass
class GroupState:
    previous: str = ''
    previous_said: bool = False
    pending_note: str = ''
    shallow: deque = field(default_factory=lambda: deque(maxlen=20))
    continuity: dict = field(default_factory=dict)
    inhibited_seeds: dict = field(default_factory=dict)
    inhibited_edges: dict = field(default_factory=dict)
    inhibited_keywords: dict = field(default_factory=dict)

class QQConversationService:
    def __init__(self, repo):
        self.repo=repo
        self.groups={}

    def state(self, group):
        return self.groups.setdefault(str(group), GroupState())

    def prepare(self, job, quote=None, media=None):
        data=json.loads(job['raw'])
        group=job['group_id']
        text,mentions=message_text(data)
        sender=data.get('sender') or {}
        name=get_manifest().name_map.get(group,{}).get(str(data['user_id'])) or sender.get('card') or sender.get('nickname') or str(data['user_id'])
        directed=get_bot_qq() in mentions or '@'+BOT_NAME in text
        text=text.replace('@'+BOT_NAME,'').strip()
        if text.startswith('#'):
            self.repo.update_job(job['id'],'ignored'); return
        BIORHYTHM.note_activity()
        if BIORHYTHM.is_asleep():
            if not directed:
                self.repo.update_job(job['id'],'ignored'); return
            BIORHYTHM.wake('qq@')
        full=build_augmented_input(name,text,[q for q in mentions if q!=get_bot_qq()],group,directed)
        if media:
            full+='，同时发送了'+ '、'.join(f'{kind}，内容是“{desc}”' for kind,desc,_ in media)
        if quote and quote.get('text'):
            full=f"{name}引用了{quote['sender']}之前的话：“{quote['text']}”\n"+full
        with group_context(group,'qq',job['message_id']), assets.group_storage(group):
            fragments,mode,state,keywords,concept,intent=decompose_input(full)
            vectors=[me.validate_vector(me.text_to_vector(frag)).tolist() for frag in fragments if frag.strip()]
            fragments=[frag for frag in fragments if frag.strip()]
            cvec=_embed(concept) if concept else None
            if concept and cvec is None: raise ValueError('Concept embedding failed')
            cvec=cvec.reshape(-1).tolist() if cvec is not None else None
            for kind,desc,path in media or []:
                if path: assets.register_pending(kind,path,desc)
        prepared={'fragments':fragments,'vectors':vectors,'mode':mode,'state':state,
                  'keywords':cog.extract_keywords_jieba(text) or keywords,'concept':concept,
                  'concept_vector':cvec,'intent':intent,'full':full,'text':text or full,
                  'sender':name,'quote':quote,'directed':directed}
        self.repo.update_job(job['id'],'prepared',fragments=json.dumps(prepared,ensure_ascii=False))

    def commit(self, job):
        p=json.loads(job['fragments'])
        half=cog.MODE_HALF_LIFE.get(p['mode'],me.DEFAULT_HALF_LIFE)
        with group_context(job['group_id'],'qq',job['message_id']), me.atomic_memories() as db:
            ids=[me.create_memory(text,half,vector=vec) for text,vec in zip(p['fragments'],p['vectors'])]
            if p['concept'] and ids:
                cid=record_concept(p['concept'],ids,vector=p['concept_vector'])
                for mid in ids:
                    db.execute('UPDATE memories SET concept_tag_ids=? WHERE id=?',(json.dumps([cid]),mid))
                    me.memories[mid]['concept_tag_ids']=[cid]
            db.execute('UPDATE messages SET sender=?,content=?,quote=? WHERE id=?',
                       (p['sender'],p['text'],json.dumps(p['quote']) if p['quote'] else None,job['message_id']))
            db.execute("UPDATE turn_jobs SET stage='stored',memory_ids=?,updated_at=? WHERE id=?",
                       (json.dumps(ids),time.time(),job['id']))
        with group_context(job['group_id']):
            if p['state']: set_state(p['state'])
            save_state()
        self.repo.event('job',{'id':job['id'],'stage':'stored'})

    def think(self, job):
        group=job['group_id']; state=self.state(group)
        p=json.loads(job['fragments']) if job.get('fragments') else {}
        with group_context(group,'internal',job['id']), assets.group_storage(group):
            kws=list(p.get('keywords') or [])
            if not kws and state.previous: kws=cog.extract_keywords_jieba(state.previous)
            if not kws:
                seed=me.get_random_memory_id()
                if seed: kws=[me.get_memory_content(seed)[:80]]
            kws=[k for k in kws if k not in state.inhibited_keywords]
            for kw in kws: state.continuity[kw]=state.continuity.get(kw,0)+1
            state.continuity={k:v for k,v in state.continuity.items() if k in kws}
            triggered=[k for k,v in state.continuity.items() if v>=cog.RUMINATION_THRESHOLD]
            if triggered:
                state.inhibited_keywords.update({k:cog.KEYWORD_INHIBIT_ROUNDS for k in triggered})
                kws=[k for k in kws if k not in triggered]
            concept,_,_=cog.retrieve_by_concept(kws,p.get('intent','none'))
            related=cog.retrieve_and_diffuse(kws,3 if concept else 10,
                                             set(state.inhibited_seeds),set(state.inhibited_edges))
            if triggered:
                state.inhibited_seeds.update({mid:cog.SEED_INHIBIT_ROUNDS for mid in cog._current_round_seeds})
                state.inhibited_edges.update({edge:cog.EDGE_INHIBIT_ROUNDS for edge in cog._current_round_edges})
            history=get_all()[-12:]
            pinned=[]; times=[]
            for msg in history:
                prefix='我' if msg['sender']==BOT_NAME else msg['sender']
                pinned.append(f"{prefix}说：{msg['content']}{quote_suffix(msg)}")
                times.append(msg['time'] if msg['time_basis']=='unix_utc' else None)
            for ts,text in concept: pinned.append(text); times.append(ts)
            if state.previous:
                pinned.append(('我说：' if state.previous_said else '我想：')+state.previous); times.append(None)
            if state.pending_note:
                pinned.append(state.pending_note); times.append(None); state.pending_note=''
            feeling=BIORHYTHM.feeling_text()
            if feeling: pinned.append(feeling); times.append(time.time())
            result=verbalize([text for _,text in related],kws,get_state(),
                             user_input=p.get('full'),pinned=pinned,
                             timestamps=[ts for ts,_ in related],pinned_timestamps=times)
            result={'say':result.get('say') is True,'text':cog._strip_trailing_period(result.get('text','').strip())}
            if len(result['text'])>20000: raise ValueError('Generated response too long')
            state.previous=result['text']; state.previous_said=False
            for values in (state.inhibited_keywords,state.inhibited_seeds,state.inhibited_edges):
                for key in list(values):
                    values[key]-=1
                    if values[key]<=0: del values[key]
            vector=me.validate_vector(me.text_to_vector('我想：'+result['text'])) if result['text'] and not result['say'] else None
            with me.atomic_memories() as db:
                if vector is not None: me.create_memory('我想：'+result['text'],vector=vector)
                db.execute("UPDATE turn_jobs SET stage='generated',decision=?,updated_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),time.time(),job['id']))
        self.repo.event('job',{'id':job['id'],'stage':'generated'})
        return result

    def create_outbox(self, job, result, payload=None, memory_text=None):
        oid=job['id']+'-reply' if payload is None else job['id']+'-media'
        found=self.repo.query('SELECT * FROM delivery_outbox WHERE id=?',(oid,))
        if found: return found[0]
        text=memory_text or '我说：'+result['text']
        vector=me.validate_vector(me.text_to_vector(text))
        self.repo.execute('''INSERT INTO delivery_outbox(id,job_id,group_id,payload,status,reply_vector,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)''',
            (oid,job['id'],job['group_id'],json.dumps(payload if payload is not None else result['text'],ensure_ascii=False),
             'pending',vector.tobytes(),time.time(),time.time()))
        return self.repo.query('SELECT * FROM delivery_outbox WHERE id=?',(oid,))[0]

    def delivered(self, outbox, ack):
        group=outbox['group_id']; payload=json.loads(outbox['payload'])
        job=self.repo.job(outbox['job_id'])
        result=json.loads(job['decision'])
        media=not isinstance(payload,str)
        content=result.get('media_text','') if media else result['text']
        with group_context(group,'qq_bot',outbox['id']), me.atomic_memories() as db:
            row=db.execute('SELECT status FROM delivery_outbox WHERE id=?',(outbox['id'],)).fetchone()
            if not row or row[0]=='delivered': return
            me.create_memory(content if media else '我说：'+content,
                             vector=np.frombuffer(outbox['reply_vector'],dtype=np.float32))
            db.execute('''INSERT INTO messages(id,group_id,sender,role,content,received_at,source)
                VALUES(?,?,?,?,?,?,?)''',(outbox['id'],group,BOT_NAME,'bot',content,time.time(),'qq'))
            db.execute("UPDATE delivery_outbox SET status='delivered',channel_message_id=?,updated_at=? WHERE id=?",
                       (str(ack.get('message_id','')),time.time(),outbox['id']))
            me._bump_message_sent()
            if not media and not job.get('raw'): me._count_active_success+=1
            if '我' in content: me._bump_self_ref()
            if media:
                assets_kind=result.get('media_kind'); detail=result.get('media_detail')
                with assets.group_storage(group): assets.mark_sent(assets_kind,detail)
            self.state(group).previous_said=not media
        self.repo.event('delivery',{'id':outbox['id'],'status':'delivered'})

    def action(self, job, talk_sent):
        from core.action_layer import decide_action
        group=job['group_id']; result=json.loads(job.get('decision') or '{}')
        p=json.loads(job.get('fragments') or '{}')
        with group_context(group,'internal',job['id']), assets.group_storage(group):
            action=decide_action(result.get('text',''),result.get('say') is True,
                                 result.get('text','') if talk_sent else '',p.get('keywords',[]),'QQ 群内')
            kind=action.get('action')
            if kind in ('image','sticker'):
                entry=action['entry']; detail=action.get('detail','')
                text=f"我发了{'图片' if kind=='image' else '表情包'}，内容是：{detail}"
                result.update(media_text=text,media_kind=kind,media_detail=detail)
                self.repo.update_job(job['id'],'finished',decision=json.dumps(result,ensure_ascii=False))
                payload=[{'type':'image','data':{'file':__import__('pathlib').Path(entry['path']).resolve().as_uri(),
                                                 **({'sub_type':1} if kind=='sticker' else {})}}]
                return self.create_outbox(job,result,payload,text)
            trace=''
            if kind in ('save_image','save_sticker') and action.get('added'):
                trace='我收藏了素材，内容是：'+action.get('detail','')
            if kind=='write_txt': trace=f"我在记事本「{action['note_name']}」里写下：{action.get('written','')}"
            if kind=='edit_txt':
                trace=f"我把记事本「{action['note_name']}」里的「{action.get('old_text','')}」"+(
                    '划掉了' if action.get('deleted') else '改成了「'+action.get('new_text','')+'」')
            if trace:
                # The file operation has happened. Preserve its durable trace
                # even if embedding the trace fails afterwards.
                self.repo.history_add(BOT_NAME,trace,'internal',group,role='internal')
                self.repo.audit('qq.action',{'job':job['id'],'group':group,'action':kind,'trace':trace})
                me.create_memory(trace)
            if kind in ('write_txt','edit_txt','read_txt'):
                # An empty note after deleting its final line still has a trace.
                self.state(group).pending_note=f"我打开了记事本「{action.get('note_name','')}」，内容是：{action.get('note_text','') or '（空）'}"
            return None
