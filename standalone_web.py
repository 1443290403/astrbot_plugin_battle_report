"""Automatically host public login assets on AstrBot's existing dashboard port.

Only static assets are public. Authentication and all data APIs remain owned by
AstrBot. The dashboard compatibility adapter is checked before installing routes.
"""
from pathlib import Path
import json
import re
import ipaddress

try:
    from .scripts.export_report_webui import build_assets
    from .web_accounts import WebAuthError,SESSION_TTL
except ImportError:
    from scripts.export_report_webui import build_assets
    from web_accounts import WebAuthError,SESSION_TTL

PREFIX = '/battle-report'
OWNER = 'astrbot_plugin_battle_report.standalone'
COOKIE = 'battle_report_session'
HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
}
MEDIA = {'index.html':'text/html','app.js':'application/javascript',
         'standalone.js':'application/javascript','style.css':'text/css',
         'standalone.css':'text/css','VERSION':'text/plain'}


class StandaloneFrontend:
    def __init__(self, context, log, *, root=Path(__file__).resolve().parent, webui=None):
        self.context=context
        self.log=log
        self.assets=build_assets(root, builtin=True)
        self.app=None
        self.routes=[]
        self.webui=webui

    def auth(self):
        if self.webui is None or self.webui.auth is None:
            raise WebAuthError('战队管理员登录暂不可用，请查看 AstrBot 日志。',503)
        return self.webui.auth

    @staticmethod
    def token(request):
        header=request.headers.get('authorization','')
        if header:return header.removeprefix('Bearer ').strip() if header.startswith('Bearer ') else ''
        return request.cookies.get(COOKIE,'')

    @staticmethod
    def encrypted(request):
        if request.url.scheme=='https':return True
        try:peer=ipaddress.ip_address(request.client.host)
        except (ValueError,AttributeError):return False
        # HTTPS terminates at the local reverse proxy; do not trust public peers' headers.
        forwarded=request.headers.get('x-forwarded-proto','').split(',')[0].strip().lower()
        return forwarded=='https' and (peer.is_loopback or peer.is_private)

    @classmethod
    def require_secure(cls, request):
        if cls.encrypted(request):return
        try:
            local=ipaddress.ip_address(request.client.host).is_loopback and request.url.hostname in ('localhost','127.0.0.1','::1')
        except (ValueError,AttributeError):local=False
        if not local:raise WebAuthError('请通过HTTPS访问战报页面，避免明文传输登录信息。',403)

    @staticmethod
    async def payload(request,limit=8192):
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>limit:raise WebAuthError('请求内容过大。',413)
        request._body=bytes(body)
        try:value=json.loads(body)
        except (ValueError,UnicodeError):raise WebAuthError('请求格式无效。')
        if not isinstance(value,dict):raise WebAuthError('请求格式无效。')
        return value

    async def auth_endpoint(self, request):
        from starlette.responses import JSONResponse
        try:
            self.require_secure(request)
            auth=self.auth()
            action=request.path_params['action']
            expected='GET' if action=='session' else 'POST'
            if action not in ('session','login','code-login','logout','password'):
                raise WebAuthError('接口不存在。',404)
            if request.method!=expected:raise WebAuthError('请求方法无效。',405)
            if request.headers.get('sec-fetch-site')=='cross-site':raise WebAuthError('请从本页面操作。',403)
            token=self.token(request)
            if action=='logout':
                auth.logout(token)
                response=JSONResponse(dict(status='ok',data=dict(logged_out=True)),headers=HEADERS)
                response.delete_cookie(COOKIE,path=PREFIX,httponly=True,samesite='strict')
                return response
            if action=='session':
                actor=await auth.authenticate(token)
                return JSONResponse(dict(status='ok',data=actor),headers=HEADERS)
            payload=await self.payload(request)
            client=request.client.host if request.client else 'unknown'
            if action=='login':token=await auth.login(payload.get('username'),payload.get('password'),client)
            elif action=='code-login':token=await auth.login_code(payload.get('code'),client)
            else:token=await auth.change_password(token,payload.get('old_password'),payload.get('new_password'))
            actor=await auth.authenticate(token)
            response=JSONResponse(dict(status='ok',data=dict(token=token,actor=actor)),headers=HEADERS)
            response.set_cookie(COOKIE,token,max_age=SESSION_TTL,path=PREFIX,
                                secure=self.encrypted(request),httponly=True,samesite='strict')
            return response
        except WebAuthError as exc:
            return JSONResponse(dict(status='error',message=str(exc)),status_code=exc.status,headers=HEADERS)
        except Exception:
            self.log.exception('战报登录请求失败')
            return JSONResponse(dict(status='error',message='请求未完成，请查看 AstrBot 日志。'),status_code=500,headers=HEADERS)

    async def team_api(self, request):
        from starlette.responses import JSONResponse
        try:
            self.require_secure(request)
            actor=await self.auth().authenticate(self.token(request))
            endpoint=request.path_params['endpoint']
            method='GET' if request.method=='HEAD' else request.method
            matched=False
            for pattern,handler,methods in self.webui.endpoints():
                expression=re.sub(r'<([a-z_]+)>',r'(?P<\1>[^/]+)',pattern)
                match=re.fullmatch(expression,endpoint)
                if not match:continue
                matched=True
                if method not in methods:continue
                if method=='POST':await self.payload(request,1024*1024)
                from astrbot.api.web import PluginRequest,bind_request_context
                with self.webui.bind_team(actor),bind_request_context(PluginRequest(request,username=actor['owner'])):
                    response=await handler(**match.groupdict())
                    response.headers.update(HEADERS)
                    return response
            raise WebAuthError('请求方法无效。' if matched else '接口不存在。',405 if matched else 404)
        except WebAuthError as exc:
            return JSONResponse(dict(status='error',message=str(exc)),status_code=exc.status,headers=HEADERS)
        except Exception:
            self.log.exception('战队管理员战报请求失败')
            return JSONResponse(dict(status='error',message='请求未完成，请查看 AstrBot 日志。'),status_code=500,headers=HEADERS)

    def register(self, adapter=None):
        if adapter is None:
            from astrbot.dashboard import server
            adapter=server.APP
        app=getattr(adapter, '_app', None)
        if app is None or not isinstance(getattr(getattr(app,'router',None),'routes',None),list):
            return False
        # Never attach to an earlier core's global APP during a restart.
        lifecycle=getattr(app.state,'core_lifecycle',None)
        if getattr(lifecycle,'star_context',None) is not self.context:
            return False
        if self.app is app and all(route in app.router.routes for route in self.routes) and self.routes:
            return True
        from starlette.responses import RedirectResponse, Response
        from starlette.routing import Route

        async def redirect(request):
            return RedirectResponse(PREFIX+'/', status_code=307, headers=HEADERS)

        async def serve(request):
            name=request.path_params['asset'] or 'index.html'
            content=self.assets.get(name)
            if content is None:return Response('Not found',status_code=404,headers=HEADERS)
            headers={**HEADERS,'Content-Length':str(len(content))}
            return Response(b'' if request.method=='HEAD' else content,media_type=MEDIA[name],headers=headers)

        routes=[Route(PREFIX,redirect,methods=['GET']),
                Route(PREFIX+'/auth/{action}',self.auth_endpoint,methods=['GET','POST']),
                Route(PREFIX+'/api/comparison/{endpoint:path}',self.team_api,methods=['GET','POST']),
                Route(PREFIX+'/{asset:path}',serve,methods=['GET'])]
        for route in routes:route._battle_report_owner=OWNER
        self.close()
        # Static SPA catch-alls must stay behind this exact prefix. Other routes
        # retain their order; only earlier instances of our own routes are replaced.
        retained=[r for r in app.router.routes if getattr(r,'_battle_report_owner',None)!=OWNER]
        app.router.routes[:]=routes+retained
        self.app=app
        self.routes=routes
        self.log.info('独立战报入口已自动注册：在 AstrBot 访问地址后添加 /battle-report/')
        return True

    def close(self):
        if self.app is not None:
            self.app.router.routes[:]=[r for r in self.app.router.routes if not any(r is owned for owned in self.routes)]
        self.app=None
        self.routes=[]
