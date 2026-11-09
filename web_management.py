"""Core-dashboard-only team and member management; team web sessions are never managers."""
import re

try:
    from .report_site import ComparisonError
except ImportError:
    from report_site import ComparisonError


def text(value, label, *, empty=False):
    if not isinstance(value,str):raise ComparisonError(label+'无效。')
    value=value.strip()
    if (not empty and not value) or len(value)>64 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ComparisonError(label+'需为1至64个字符。')
    return value


def qq(value, *, empty=False):
    if empty and value=='':return ''
    if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',value):
        raise ComparisonError('QQ号须为不以0开头的纯数字。')
    return value


def identity(value, *, optional=False):
    if optional and value is None:return None
    if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',value) or int(value)>=2**63:
        raise ComparisonError('角色编号无效。')
    return int(value)


def qqs(value):
    if not isinstance(value,list) or len(value)>100:raise ComparisonError('管理员名单最多100个QQ。')
    return list(dict.fromkeys(qq(item) for item in value))


class WebManagement:
    def __init__(self, ui):self.ui=ui

    def db(self):
        self.ui.require_master()
        self.ui.require_db()
        return self.ui.plugin.db

    @staticmethod
    def page(request):
        try:
            raw_offset=str(request.query.get('offset','0'));raw_limit=str(request.query.get('limit','30'))
            if not raw_offset.isascii() or not raw_offset.isdecimal() or not raw_limit.isascii() or not raw_limit.isdecimal():raise ValueError
            offset,limit=int(raw_offset),int(raw_limit)
            if not 0<=offset<=1000000 or not 1<=limit<=100:raise ValueError
        except (ValueError,TypeError):raise ComparisonError('分页参数无效。')
        return text(request.query.get('keyword',''),'搜索内容',empty=True),offset,limit

    @staticmethod
    async def payload(request):
        data=await request.json(default={})
        if not isinstance(data,dict):raise ComparisonError('管理参数无效。')
        return data

    async def teams(self, request):
        db=self.db();keyword,offset,limit=self.page(request)
        return dict(await db.get_webui_teams(keyword,offset,limit),offset=offset,limit=limit)

    async def team_save(self, request):
        db=self.db();data=await self.payload(request)
        team=text(data.get('team'),'战队名').upper()
        admins=qqs(data.get('qqs'));expected=qqs(data.get('expected'))
        try:await db.set_team_admins(team,admins,expected=expected)
        except ValueError as exc:raise ComparisonError(str(exc)) from exc
        try:await self.ui.sync_accounts(admins)
        except Exception:
            self.ui.log.exception('网页管理员授权已保存，密码账号同步失败')
            return dict(updated=True,accounts_ready=False,message='管理员名单已保存，网页账号暂未同步，请稍后重试喵。')
        return dict(updated=True,accounts_ready=True,message='管理员名单已保存，网页账号已同步喵。')

    async def members(self, request):
        db=self.db();keyword,offset,limit=self.page(request)
        team=text(request.query.get('team'),'战队名').upper()
        include=request.query.get('include_admins','0')
        if include not in ('0','1'):raise ComparisonError('管理员候选参数无效。')
        result=dict(await db.get_webui_members(team,keyword,offset,limit),offset=offset,limit=limit)
        if include=='1':result['admins']=await db.get_team_admins(team)
        return result

    async def member_save(self, request):
        db=self.db();data=await self.payload(request)
        team=text(data.get('team'),'战队名').upper();name=text(data.get('name'),'角色名')
        owner=qq(data.get('qq',''),empty=True)
        action=data.get('action')
        if action not in ('create','edit'):raise ComparisonError('队员操作无效。')
        uid=identity(data.get('id')) if action=='edit' else None
        expected=None
        if action=='edit':
            old=data.get('expected')
            if not isinstance(old,dict):raise ComparisonError('请刷新后重新编辑队员。')
            expected=dict(name=text(old.get('name'),'原角色名'),qq_id=qq(old.get('qq_id'),empty=True))
        try:uid=await db.save_webui_member(team,name,owner,user_id=uid,expected=expected)
        except ValueError as exc:raise ComparisonError(str(exc)) from exc
        return dict(updated=True,id=uid,message='队员资料已保存喵。')

    async def players(self, request):
        db=self.db();keyword,offset,limit=self.page(request)
        team=text(request.query.get('team'),'战队名').upper()
        flag=request.query.get('unbound','0')
        if flag not in ('0','1'):raise ComparisonError('参赛ID筛选无效。')
        return dict(await db.get_webui_players(team,keyword,offset,limit,unbound=flag=='1'),offset=offset,limit=limit)

    async def player_save(self, request):
        db=self.db();data=await self.payload(request)
        team=text(data.get('team'),'战队名').upper();player=text(data.get('player'),'参赛ID')
        action=data.get('action')
        if action not in ('bind','unbind') or 'expected_owner' not in data:raise ComparisonError('参赛ID操作无效，请刷新后重试。')
        uid=identity(data.get('id')) if action=='bind' else None
        expected=identity(data['expected_owner'],optional=True)
        try:await db.set_webui_player_owner(team,player,uid,expected_owner=expected)
        except ValueError as exc:raise ComparisonError(str(exc)) from exc
        return dict(updated=True,message='参赛ID归属已更新喵。')
