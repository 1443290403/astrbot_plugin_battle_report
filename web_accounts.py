"""Team-only web credentials; never issue or accept AstrBot dashboard JWTs."""
import asyncio
import base64
from collections import OrderedDict
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from uuid import uuid4

QQ = re.compile(r'[1-9][0-9]{0,19}')
USERNAME = re.compile(r'[A-Za-z0-9_.-]{3,32}')
ITERATIONS = 600_000
SESSION_TTL = 7 * 24 * 3600
CODE_TTL = 300
CODE_COOLDOWN = 30


class WebAuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise WebAuthError('密码需为8至256个字符。')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def valid_hash(value):
    return isinstance(value,str) and re.fullmatch(r'pbkdf2_sha256\$600000\$[0-9a-f]{32}\$[0-9a-f]{64}',value)


def password_matches(stored, password):
    if not valid_hash(stored) or not isinstance(password,str) or len(password)>256:
        return False
    _, rounds, salt, digest = stored.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(actual,digest)


def token_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


class WebAccounts:
    def __init__(self, path, teams, *, clock=time.time):
        self.path = Path(path)
        self.teams = teams
        self.clock = clock
        self.closed = False
        self.accounts = {}
        self.initial_key = secrets.token_hex(32)
        self.sessions = OrderedDict()
        self.codes = {}
        self.code_issued_at = {}
        self.limits = OrderedDict()
        self.lock = asyncio.Lock()
        self.hash_slots = asyncio.Semaphore(4)
        # A valid, non-secret hash keeps unknown-user checks on the same path.
        self.dummy = 'pbkdf2_sha256$600000$' + '00'*16 + '$' + '00'*32
        if self.path.exists():
            if self.path.stat().st_size > 2*1024*1024:
                raise ValueError('网页账号存档过大')
            data = json.loads(self.path.read_text('utf-8'))
            if not isinstance(data,dict) or type(data.get('version')) is not int or data['version']!=1 or not isinstance(data.get('accounts'),dict):
                raise ValueError('网页账号存档格式错误')
            key = data.get('initial_key')
            if key is not None:
                if not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key):
                    raise ValueError('网页初始凭据密钥格式错误')
                self.initial_key = key
            names,qqs = set(),set()
            for aid,a in data['accounts'].items():
                if (not re.fullmatch(r'[0-9a-f]{32}',aid) or not isinstance(a,dict)
                        or a.get('id')!=aid or not USERNAME.fullmatch(str(a.get('username','')))
                        or not QQ.fullmatch(str(a.get('qq',''))) or type(a.get('enabled')) is not bool
                        or not valid_hash(a.get('hash')) or not re.fullmatch(r'[0-9a-f]{32}',str(a.get('revision','')))
                        or type(a.get('initial_password',False)) is not bool
                        or (a.get('initial_password') and key is None)
                        or a['username'].lower() in names or a['qq'] in qqs):
                    raise ValueError('网页账号存档格式错误')
                names.add(a['username'].lower());qqs.add(a['qq'])
            self.accounts = data['accounts']
            sessions=data.get('sessions',{})
            if not isinstance(sessions,dict) or len(sessions)>2000:raise ValueError('网页会话存档格式错误')
            now=self.clock()
            for key,session in sessions.items():
                if (not re.fullmatch(r'[0-9a-f]{64}',key) or not isinstance(session,dict)
                        or not isinstance(session.get('qq'),str) or not QQ.fullmatch(session['qq'])
                        or type(session.get('expires')) not in (int,float) or not math.isfinite(session['expires'])
                        or (session.get('account_id') is None and session.get('revision') is not None)
                        or (session.get('account_id') is not None and
                            (not isinstance(session['account_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',session['account_id'])
                             or not isinstance(session.get('revision'),str) or not re.fullmatch(r'[0-9a-f]{32}',session['revision'])))):
                    raise ValueError('网页会话存档格式错误')
                account=self.accounts.get(session.get('account_id'))
                if session['expires']<=now or session['expires']>now+SESSION_TTL:continue
                if session.get('account_id') and (not account or not account['enabled'] or account['qq']!=session['qq'] or account['revision']!=session['revision']):continue
                self.sessions[key]={k:session.get(k) for k in ('qq','account_id','revision','expires')}

    def require_open(self):
        if self.closed:raise WebAuthError('插件正在停止，请稍后重新登录。',503)

    def save(self):
        self.require_open()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,name = tempfile.mkstemp(prefix='.web-accounts-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(dict(version=1,initial_key=self.initial_key,accounts=self.accounts,sessions=self.sessions),stream,ensure_ascii=False,allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
            os.chmod(name,0o600)
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)

    @staticmethod
    def public_account(a):
        return {**{k:a.get(k) for k in ('id','username','qq','enabled','created_at')},
                'initial_password':bool(a.get('initial_password'))}

    def list_accounts(self):
        self.require_open()
        return [self.public_account(a) for a in sorted(self.accounts.values(),key=lambda a:a['username'].lower())]

    def _initial_password(self, aid):
        # The installation key is a secret: it recovers only unchanged initial passwords.
        digest=hmac.new(bytes.fromhex(self.initial_key),('battle-report-initial:'+aid).encode(),hashlib.sha256).digest()
        return 'Br!'+base64.urlsafe_b64encode(digest[:18]).decode()

    async def ensure_accounts(self, qqs):
        """Provision missing administrators atomically, preserving all existing credentials."""
        self.require_open()
        qqs=list(dict.fromkeys(str(qq).strip() for qq in qqs))
        if any(not QQ.fullmatch(qq) for qq in qqs):raise WebAuthError('管理员QQ号无效。')
        async with self.lock:
            before=self.accounts
            accounts=copy.deepcopy(before)
            created=[]
            names={a['username'].lower() for a in accounts.values()}
            known={a['qq'] for a in accounts.values()}
            for qq in qqs:
                if qq in known:continue
                if not await self.teams(qq):continue
                if len(accounts)>=1000:raise WebAuthError('密码账号数量已达上限。')
                username=qq if len(qq)>=3 else 'qq_'+qq
                if username.lower() in names:
                    username='qq_'+qq
                    suffix=1
                    while username.lower() in names:
                        username=f'qq_{qq}_{suffix}';suffix+=1
                aid=uuid4().hex
                async with self.hash_slots:
                    hashed=await asyncio.to_thread(password_hash,self._initial_password(aid))
                account=dict(id=aid,username=username,qq=qq,enabled=True,hash=hashed,
                    revision=uuid4().hex,created_at=self.clock(),initial_password=True)
                accounts[aid]=account;known.add(qq);names.add(username.lower())
                created.append(self.public_account(account))
            self.require_open()
            if created:
                self.accounts=accounts
                try:self.save()
                except BaseException:
                    self.accounts=before;raise
            return created

    async def credentials(self, qq):
        """For a verified QQ owner or core administrator, never for public account lists."""
        self.require_open()
        await self.allowed_teams(qq)
        self.require_open()
        account=next((a for a in self.accounts.values() if a['qq']==qq),None)
        if account is None:raise WebAuthError('密码账号尚未同步，请稍后重试。',503)
        return dict(username=account['username'],enabled=account['enabled'],
                    password=self._initial_password(account['id']) if account.get('initial_password') else None)

    async def create(self, username, password, qq):
        self.require_open()
        username = str(username or '').strip()
        qq = str(qq or '').strip()
        if not USERNAME.fullmatch(username) or not QQ.fullmatch(qq):
            raise WebAuthError('账号使用3至32位字母、数字、下划线、点或短横线，QQ号需有效。')
        if not await self.teams(qq):
            raise WebAuthError('该QQ尚未被设置为战队管理员。')
        async with self.hash_slots:
            hashed = await asyncio.to_thread(password_hash,password)
        async with self.lock:
            self.require_open()
            if any(a['username'].lower()==username.lower() or a['qq']==qq for a in self.accounts.values()):
                raise WebAuthError('该账号名或管理员QQ已配置密码账号。')
            if len(self.accounts)>=1000:
                raise WebAuthError('密码账号数量已达上限。')
            aid=uuid4().hex
            account=dict(id=aid,username=username,qq=qq,enabled=True,hash=hashed,
                         revision=uuid4().hex,created_at=self.clock())
            self.accounts[aid]=account
            try:self.save()
            except BaseException:
                self.accounts.pop(aid,None);raise
            return self.public_account(account)

    async def update(self, aid, payload, *, revision=None):
        self.require_open()
        action=payload.get('action')
        if action not in ('reset','enable','disable','delete'):
            raise WebAuthError('账号操作无效。')
        hashed=None
        if action=='reset':
            async with self.hash_slots:
                hashed=await asyncio.to_thread(password_hash,payload.get('password'))
        async with self.lock:
            self.require_open()
            if aid not in self.accounts:raise WebAuthError('找不到该密码账号。',404)
            if revision is not None and self.accounts[aid]['revision']!=revision:
                raise WebAuthError('账号状态已变化，请重新登录。',401)
            before=copy.deepcopy(self.accounts)
            before_sessions=self.sessions.copy()
            if action=='delete':self.accounts.pop(aid)
            else:
                account=self.accounts[aid]
                account['revision']=uuid4().hex
                if action=='reset':
                    account['hash']=hashed
                    account['initial_password']=False
                else:account['enabled']=action=='enable'
            self.sessions=OrderedDict((k,s) for k,s in self.sessions.items() if s.get('account_id')!=aid)
            try:self.save()
            except BaseException:
                self.accounts=before;self.sessions=before_sessions;raise

    def prune(self):
        now=self.clock()
        self.sessions=OrderedDict((k,s) for k,s in self.sessions.items() if s['expires']>now)
        self.codes={k:c for k,c in self.codes.items() if c['expires']>now}
        self.code_issued_at={qq:issued for qq,issued in self.code_issued_at.items() if issued+CODE_COOLDOWN>now}
        self.limits=OrderedDict((k,v) for k,v in self.limits.items() if v[0]+300>now)

    def limit(self, client):
        self.require_open()
        self.prune()
        client=str(client or 'unknown')[:200]
        window,count=self.limits.get(client,(self.clock(),0))
        if count>=20:raise WebAuthError('登录尝试过于频繁，请5分钟后重试。',429)
        self.limits[client]=(window,count+1)
        if len(self.limits)>2000:self.limits.popitem(last=False)

    async def allowed_teams(self, qq):
        teams=await self.teams(qq)
        if not teams:raise WebAuthError('当前QQ没有战队管理员权限。',403)
        return teams

    def new_session(self, qq, account=None):
        self.require_open()
        self.prune()
        before=self.sessions.copy()
        token='brs_'+secrets.token_urlsafe(32)
        self.sessions[token_key(token)]=dict(qq=qq,account_id=account['id'] if account else None,
            revision=account['revision'] if account else None,expires=self.clock()+SESSION_TTL)
        if len(self.sessions)>2000:self.sessions.popitem(last=False)
        try:self.save()
        except BaseException:
            self.sessions=before;raise
        return token

    async def login(self, username, password, client):
        self.limit(client)
        account=next((a for a in self.accounts.values() if a['username'].lower()==str(username or '').lower().strip()),None)
        account=copy.deepcopy(account)
        async with self.hash_slots:
            matched=await asyncio.to_thread(password_matches,account['hash'] if account else self.dummy,password)
        if not account or not matched or not account['enabled']:
            raise WebAuthError('账号或密码错误。',401)
        await self.allowed_teams(account['qq'])
        current=self.accounts.get(account['id'])
        if not current or not current['enabled'] or current['revision']!=account['revision']:
            raise WebAuthError('账号或密码错误。',401)
        return self.new_session(account['qq'],current)

    async def issue_code(self, qq):
        self.require_open()
        qq=str(qq).strip()
        if not QQ.fullmatch(qq):raise WebAuthError('QQ号无效。')
        await self.allowed_teams(qq)
        self.require_open()
        self.prune()
        issued=self.code_issued_at.get(qq)
        if issued is not None and self.clock()-issued<CODE_COOLDOWN:
            raise WebAuthError('请稍后再获取登录码喵。',429)
        self.codes={k:c for k,c in self.codes.items() if c['qq']!=qq}
        code='brc_'+secrets.token_urlsafe(24)
        self.code_issued_at[qq]=self.clock()
        if len(self.code_issued_at)>2000:self.code_issued_at.pop(next(iter(self.code_issued_at)))
        self.codes[token_key(code)]=dict(qq=qq,issued=self.clock(),expires=self.clock()+CODE_TTL)
        if len(self.codes)>2000:self.codes.pop(next(iter(self.codes)))
        return code

    async def login_code(self, code, client):
        self.limit(client)
        if not isinstance(code,str) or len(code)>128:raise WebAuthError('登录码无效或已过期。',401)
        record=self.codes.pop(token_key(code.strip()),None)
        if record is None or record['expires']<=self.clock():raise WebAuthError('登录码无效或已过期。',401)
        await self.allowed_teams(record['qq'])
        return self.new_session(record['qq'])

    async def authenticate(self, token):
        self.require_open()
        self.prune()
        if not isinstance(token,str) or len(token)>200:raise WebAuthError('请先登录战报系统。',401)
        key=token_key(token)
        session=self.sessions.get(key)
        if session is None:raise WebAuthError('登录已失效，请重新登录。',401)
        account=self.accounts.get(session['account_id']) if session['account_id'] else None
        if session['account_id'] and (not account or not account['enabled'] or account['revision']!=session['revision']):
            self.sessions.pop(key,None)
            self.save()
            raise WebAuthError('登录已失效，请重新登录。',401)
        try:teams=await self.allowed_teams(session['qq'])
        except WebAuthError as exc:
            if exc.status==403:
                self.sessions.pop(key,None)
                self.save()
                raise WebAuthError('登录已失效或管理员授权已撤销。',401) from exc
            raise
        self.require_open()
        current=self.accounts.get(session['account_id']) if session['account_id'] else None
        if self.sessions.get(key) is not session or (session['account_id'] and
                (not current or not current['enabled'] or current['revision']!=session['revision'])):
            raise WebAuthError('登录已失效，请重新登录。',401)
        return dict(qq=session['qq'],teams=teams,owner='team-qq:'+session['qq'],role='team',
                    username=account['username'] if account else session['qq'],password_login=bool(account))

    def logout(self, token):
        self.require_open()
        if isinstance(token,str) and token_key(token) in self.sessions:
            before=self.sessions.copy();self.sessions.pop(token_key(token))
            try:self.save()
            except BaseException:
                self.sessions=before;raise

    async def change_password(self, token, old_password, new_password):
        await self.authenticate(token)
        session=self.sessions[token_key(token)]
        aid=session['account_id']
        if not aid:raise WebAuthError('请使用自动创建的账号密码登录后修改密码。')
        async with self.hash_slots:
            matched=await asyncio.to_thread(password_matches,self.accounts[aid]['hash'],old_password)
        if not matched:raise WebAuthError('原密码错误。',403)
        # Recheck the same credential version before changing it.
        await self.authenticate(token)
        qq=session['qq']
        await self.update(aid,dict(action='reset',password=new_password),revision=session['revision'])
        return self.new_session(qq,self.accounts[aid])

    def close(self):
        self.closed=True
        self.sessions.clear();self.codes.clear();self.code_issued_at.clear();self.limits.clear()
