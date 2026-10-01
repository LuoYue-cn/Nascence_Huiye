"""Administrator sessions; no public customer accounts or identity binding."""
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from fastapi import HTTPException, Request
from utils.paths import DATA_DIR

COOKIE='huiye_session'

def password_hash(password, salt=None):
    salt=salt or secrets.token_hex(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),260000)
    return salt+':'+digest.hex()

def password_valid(password, encoded):
    try:
        salt,_=encoded.split(':',1)
        return hmac.compare_digest(password_hash(password,salt),encoded)
    except (ValueError,TypeError): return False

def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()

class Auth:
    def __init__(self, repo):
        self.repo=repo; self.failures={}; self.dummy_hash=password_hash('dummy-password')
        if not repo.query('SELECT name FROM admin_accounts LIMIT 1'):
            password=os.environ.get('HUIYE_ADMIN_PASSWORD')
            if not password:
                password=secrets.token_urlsafe(24)
                path=DATA_DIR/'initial_admin_password.txt'
                if path.exists(): password=path.read_text(encoding='utf-8').strip()
                else:
                    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(password+'\n')
            if len(password)<12: raise ValueError('Administrator password must be at least 12 characters')
            repo.execute('INSERT INTO admin_accounts VALUES(?,?)',('admin',password_hash(password)))

    async def login(self, request, name, password):
        peer=request.client.host if request.client else 'local'
        now=time.monotonic()
        attempts=[t for t in self.failures.get(peer,[]) if now-t<300]
        if len(attempts)>=10: raise HTTPException(429,'登录尝试过多，请稍后再试')
        if len(self.failures)>1000:
            self.failures={key:times for key,times in self.failures.items() if times and now-times[-1]<300}
            if len(self.failures)>1000: raise HTTPException(429,'登录服务繁忙，请稍后再试')
        self.origin(request)
        rows=self.repo.query('SELECT password_hash FROM admin_accounts WHERE name=?',(name,))
        encoded=rows[0]['password_hash'] if rows else self.dummy_hash
        valid=await asyncio.to_thread(password_valid,password,encoded)
        if not rows or not valid:
            self.failures[peer]=attempts+[now]
            raise HTTPException(401,'用户名或密码错误')
        self.failures.pop(peer,None)
        token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
        self.repo.execute('DELETE FROM admin_sessions WHERE expires_at<?',(time.time(),))
        self.repo.execute('INSERT INTO admin_sessions VALUES(?,?,?,?)',
                          (token_hash(token),name,csrf,time.time()+12*3600))
        self.repo.audit('auth.login',{'name':name})
        return token,csrf

    def session(self, request, write=False):
        token=request.cookies.get(COOKIE,'')
        rows=self.repo.query('SELECT * FROM admin_sessions WHERE token_hash=? AND expires_at>?',
                             (token_hash(token),time.time())) if token else []
        if not rows: raise HTTPException(401,'请先登录管理面板')
        row=rows[0]
        if write:
            self.origin(request)
            if not secrets.compare_digest(row['csrf'],request.headers.get('x-csrf-token','')):
                raise HTTPException(403,'管理操作缺少有效 CSRF 验证')
        return row

    def origin(self, request):
        origin=request.headers.get('origin')
        if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
            raise HTTPException(403,'请求来源不匹配')

    async def change_password(self, request, old, new):
        session=self.session(request,True)
        if len(new)<12: raise HTTPException(422,'新密码至少 12 个字符')
        encoded=self.repo.query('SELECT password_hash FROM admin_accounts WHERE name=?',(session['name'],))[0]['password_hash']
        if not await asyncio.to_thread(password_valid,old,encoded): raise HTTPException(403,'原密码错误')
        updated=await asyncio.to_thread(password_hash,new)
        self.repo.execute('UPDATE admin_accounts SET password_hash=? WHERE name=?',(updated,session['name']))
        self.repo.execute('DELETE FROM admin_sessions')
        (DATA_DIR/'initial_admin_password.txt').unlink(missing_ok=True)
        self.repo.audit('auth.password_changed',{'name':session['name']})
