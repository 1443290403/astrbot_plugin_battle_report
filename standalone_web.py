"""Automatically host public login assets on AstrBot's existing dashboard port.

Only static assets are public. Authentication and all data APIs remain owned by
AstrBot. The dashboard compatibility adapter is checked before installing routes.
"""
from pathlib import Path

try:
    from .scripts.export_report_webui import build_assets
except ImportError:
    from scripts.export_report_webui import build_assets

PREFIX = '/battle-report'
OWNER = 'astrbot_plugin_battle_report.standalone'
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
    def __init__(self, context, log, *, root=Path(__file__).resolve().parent):
        self.context=context
        self.log=log
        self.assets=build_assets(root, builtin=True)
        self.app=None
        self.routes=[]

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

        routes=[Route(PREFIX,redirect,methods=['GET']),Route(PREFIX+'/{asset:path}',serve,methods=['GET'])]
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
