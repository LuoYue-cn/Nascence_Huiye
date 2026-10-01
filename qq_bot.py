"""QQ-only OneBot adapter. The application owns its lifetime and job queue."""
import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import aiohttp
from config.api_config import config
from config.constants import BOT_NAME
from utils.paths import CONFIG_DIR, atomic_json

logger = logging.getLogger('QQBot')
CONFIG_PATH = str(CONFIG_DIR / 'qq_manifest.json')

class QQManifest:
    def __init__(self):
        self.reload()

    def reload(self):
        self.whitelist, self.name_map = set(), {}
        path = Path(CONFIG_PATH)
        if not path.exists():
            atomic_json(path, {'whitelist_groups': [], 'name_mapping': {}})
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data,dict) or not isinstance(data.get('whitelist_groups',[]),list) or not isinstance(data.get('name_mapping',{}),dict): raise ValueError('Invalid manifest structure')
            names=data.get('name_mapping') or {}
            if any(not isinstance(members,dict) or any(not isinstance(name,str) for name in members.values()) for members in names.values()): raise ValueError('Invalid display-name mapping')
            self.whitelist = {str(g) for g in data.get('whitelist_groups', []) if str(g).isdigit() and len(str(g))<=20}
            self.name_map = names
        except (ValueError, OSError):
            logger.error('QQ manifest is invalid; all groups are disabled')

    def save(self, groups, names):
        if any(not str(g).isdigit() for g in groups) or not isinstance(names, dict):
            raise ValueError('Invalid QQ manifest')
        atomic_json(CONFIG_PATH, {'whitelist_groups': sorted({str(g) for g in groups}), 'name_mapping': names})
        self.reload()

manifest = None

def get_manifest():
    global manifest
    if manifest is None:
        manifest = QQManifest()
    return manifest

def get_bot_qq():
    return str(config.get('bot_qq') or '')

def get_active_group_id():
    return str(config.get('active_group_id') or '')

def get_napcat_token():
    return os.environ.get('HUIYE_NAPCAT_TOKEN') or str(config.get('napcat_token') or '')

def parse_cq_code(text):
    mentions = re.findall(r'\[CQ:at,[^\]]*?qq=(\w+)', str(text))
    clean = re.sub(r'\[CQ:[^\]]+\]', '', str(text))
    for a,b in (('&#91;','['),('&#93;',']'),('&#44;',','),('&amp;','&')):
        clean = clean.replace(a,b)
    return clean.strip(), mentions

def message_text(data):
    segments = data.get('message')
    if isinstance(segments, list):
        text = ''.join(str(s.get('data',{}).get('text','')) for s in segments if isinstance(s,dict) and s.get('type') == 'text')
        mentions = [str(s.get('data',{}).get('qq','')) for s in segments if isinstance(s,dict) and s.get('type') == 'at']
        return text.strip(), mentions
    return parse_cq_code(data.get('raw_message') or segments or '')

def build_augmented_input(sender_name, raw_text, mentions, group_id, is_mentioned_me):
    target = BOT_NAME if is_mentioned_me else '、'.join(get_manifest().name_map.get(group_id,{}).get(q,q) for q in mentions)
    return f'{sender_name}对{target}说：“{raw_text}”' if target else f'{sender_name}说：“{raw_text}”'

class DeliveryError(Exception):
    def __init__(self, message, unknown=False):
        super().__init__(message)
        self.unknown = unknown

class QQAdapter:
    def __init__(self, runtime):
        self.runtime = runtime
        self.websocket = None
        self.pending = {}
        self.action_lock = asyncio.Lock()
        self.media_semaphore = asyncio.Semaphore(2)

    @property
    def connected(self):
        return self.websocket is not None

    async def handle(self, websocket):
        token = get_napcat_token()
        supplied = websocket.headers.get('authorization','').removeprefix('Bearer ').strip()
        if not token or not secrets.compare_digest(token, supplied):
            await websocket.close(code=1008, reason='Authentication required')
            return
        if self.connected or self.runtime.stopping:
            await websocket.close(code=1013, reason='Connection already active or service stopping')
            return
        await websocket.accept()
        self.websocket = websocket
        self.runtime.repo.event('qq', {'connected': True})
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode()) > 1024 * 1024:
                    await websocket.close(code=1009, reason='Event too large'); break
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        continue
                    echo = data.get('echo')
                    if isinstance(echo, str) and echo in self.pending:
                        future = self.pending.pop(echo)
                        if not future.done():
                            future.set_result(data)
                        continue
                    if data.get('post_type') == 'message' and data.get('message_type') == 'group':
                        await self.runtime.accept_message(data)
                except (ValueError, KeyError, OverflowError) as exc:
                    logger.warning('QQ event rejected: %s', exc)
                    self.runtime.repo.event('error', {'message': str(exc)})
        except Exception:
            logger.info('NapCat disconnected')
        finally:
            if self.websocket is websocket:
                self.websocket = None
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(DeliveryError('NapCat disconnected before ACK', unknown=True))
            self.pending.clear()
            self.runtime.repo.event('qq', {'connected': False})

    async def action(self, name, params, timeout=10, outbox_id=None):
        ws = self.websocket
        if ws is None:
            raise DeliveryError('NapCat is not connected')
        echo = 'huiye-' + secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        sent = False
        try:
            async with self.action_lock:
                if outbox_id:
                    self.runtime.repo.execute("UPDATE delivery_outbox SET status='sending',echo=?,updated_at=? WHERE id=?",
                                              (echo, __import__('time').time(), outbox_id))
                sent = True  # Transport failure may occur after bytes reached NapCat.
                await ws.send_text(json.dumps({'action':name,'params':params,'echo':echo},ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout)
            if response.get('status') != 'ok' or response.get('retcode',0) != 0:
                raise DeliveryError('NapCat rejected action: ' + str(response.get('retcode')))
            return response.get('data') or {}
        except asyncio.TimeoutError as exc:
            raise DeliveryError('QQ ACK timed out', unknown=True) from exc
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError('QQ transport failed', unknown=sent) from exc
        finally:
            self.pending.pop(echo, None)
            if not future.done():
                future.cancel()

    async def send(self, group_id, payload, outbox_id=None):
        from core.biorhythm import BIORHYTHM
        group_id = str(group_id)
        if self.runtime.stopping or self.runtime.paused or group_id not in get_manifest().whitelist:
            raise DeliveryError('QQ target is paused or not whitelisted')
        if BIORHYTHM.is_asleep():
            raise DeliveryError('QQ bot is sleeping')
        return await self.action('send_group_msg', {'group_id':int(group_id), 'message':payload}, outbox_id=outbox_id)

    async def quote(self, message_id, group_id):
        try:
            data = await self.action('get_msg', {'message_id':int(message_id)})
            if str(data.get('group_id','')) != str(group_id):
                return None
            text,_ = message_text(data)
            sender = data.get('sender') or {}
            return {'sender': sender.get('card') or sender.get('nickname') or str(data.get('user_id','')), 'text':text}
        except (DeliveryError, ValueError):
            return None

    async def describe_media(self, data):
        from core.llm_interface import describe_image_from_path, describe_audio_from_path, describe_video_from_path
        descriptions, files = [], []
        segments = [s for s in data.get('message',[]) if isinstance(s,dict) and s.get('type') in ('image','record','video','mface')] if isinstance(data.get('message'),list) else []
        if len(segments) > int(config.get('max_media_count',4)):
            raise ValueError('Too many QQ attachments')
        try:
            for segment in segments:
                kind, values = segment['type'], segment.get('data') or {}
                if kind == 'mface':
                    descriptions.append(('sticker', str(values.get('summary') or '商城表情包'), None)); continue
                limit = int(config.get('max_media_bytes',8*1024*1024))
                location = values.get('url') or values.get('path') or values.get('file')
                if kind == 'record' and location and not str(location).startswith(('http://','https://')) and not Path(str(location)).is_file():
                    record = await self.action('get_record', {'file':str(location),'out_format':'wav'})
                    location = record.get('file')
                if not location:
                    raise ValueError('QQ attachment has no accessible location')
                owned = False
                async with self.media_semaphore:
                    if str(location).startswith(('http://','https://')):
                        timeout = aiohttp.ClientTimeout(total=30)
                        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                            async with session.get(location) as resp:
                                resp.raise_for_status()
                                if resp.content_length and resp.content_length > limit:
                                    raise ValueError('QQ attachment exceeds size limit')
                                suffix = Path(urlparse(location).path).suffix[:10] or '.bin'
                                fd,path = tempfile.mkstemp(suffix=suffix)
                                files.append(path); owned=True
                                size=0
                                with os.fdopen(fd,'wb') as f:
                                    async for chunk in resp.content.iter_chunked(65536):
                                        size += len(chunk)
                                        if size > limit: raise ValueError('QQ attachment exceeds size limit')
                                        f.write(chunk)
                    else:
                        path = str(location)
                        if not Path(path).is_file() or Path(path).stat().st_size > limit:
                            raise ValueError('QQ attachment unavailable or too large')
                    if kind == 'image':
                        description = await describe_image_from_path(path)
                        is_sticker = str(values.get('sub_type')) == '1' or Path(path).suffix.lower()=='.gif' or '表情' in str(values.get('summary',''))
                        kind = 'sticker' if is_sticker else 'image'
                    elif kind == 'record':
                        description = await describe_audio_from_path(path)
                    else:
                        description = await describe_video_from_path(path)
                if description.startswith('[') and '失败' in description:
                    raise ValueError('QQ media model failed')
                descriptions.append((kind,description,path if kind in ('image','sticker') else None))
            return descriptions, files
        except BaseException:
            for path in files:
                Path(path).unlink(missing_ok=True)
            raise

    async def close(self):
        if self.websocket:
            await self.websocket.close(code=1000, reason='Service stopping')
