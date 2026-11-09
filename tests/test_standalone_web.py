"""自动入口的静态白名单、路由优先级及卸载／重载所有权边界。"""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('fastapi')
httpx = pytest.importorskip('httpx')
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from standalone_web import StandaloneFrontend

ROOT = Path(__file__).resolve().parents[1]


def dashboard():
    app = FastAPI()
    context = object()
    app.state.core_lifecycle = SimpleNamespace(star_context=context)

    @app.get('/api/plug/astrbot_plugin_battle_report/comparison/options')
    async def guarded():
        return JSONResponse({'status':'error','message':'未授权'},status_code=401)

    @app.get('/{rest:path}')
    async def fallback(rest):
        return {'original_spa':rest}

    return app, context, SimpleNamespace(_app=app)


def test_auto_entry_precedes_spa_and_exposes_only_static_assets():
    async def run():
        app, context, adapter = dashboard()
        original_routes = list(app.router.routes)
        before = (ROOT/'pages/report-comparison/index.html').read_bytes()
        frontend = StandaloneFrontend(context, logging.getLogger('test'))
        assert frontend.register(adapter)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response = await client.get('/battle-report')
            assert response.status_code == 307 and response.headers['location'] == '/battle-report/'
            response = await client.get('/battle-report/')
            assert response.status_code == 200
            assert 'name="battle-report-entry" content="builtin"' in response.text
            assert response.text.index('./standalone.js') < response.text.index('./app.js')
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-frame-options'] == 'DENY'
            for filename, content in frontend.assets.items():
                response = await client.get('/battle-report/'+filename)
                assert response.content == content
                head = await client.head('/battle-report/'+filename)
                assert head.status_code == 200 and not head.content
                assert int(head.headers['content-length']) == len(content)
            for filename in ['main.py','metadata.yaml','request_reviews.json','%2e%2e%2fmetadata.yaml','app.js/extra','//app.js']:
                assert (await client.get('/battle-report/'+filename)).status_code == 404
            assert (await client.post('/battle-report/index.html')).status_code == 405
            assert (await client.get('/api/plug/astrbot_plugin_battle_report/comparison/options')).status_code == 401
            assert (await client.get('/other-plugin')).json() == {'original_spa':'other-plugin'}
            frontend.close()
            assert (await client.get('/battle-report/')).json() == {'original_spa':'battle-report/'}
        assert app.router.routes == original_routes
        assert (ROOT/'pages/report-comparison/index.html').read_bytes() == before
    asyncio.run(run())


def test_reload_replaces_only_own_routes_and_old_unload_keeps_new_entry():
    app, context, adapter = dashboard()
    original_routes = list(app.router.routes)
    old = StandaloneFrontend(context, logging.getLogger('test'))
    assert old.register(adapter)
    registered = list(app.router.routes)
    assert old.register(adapter) and app.router.routes == registered
    new = StandaloneFrontend(context, logging.getLogger('test'))
    assert new.register(adapter)
    assert len(app.router.routes) == len(original_routes)+2
    old.close()
    assert all(route in app.router.routes for route in new.routes)
    assert app.router.routes[2:] == original_routes
    new.close()
    new.close()
    assert app.router.routes == original_routes


def test_unavailable_or_previous_core_adapter_is_not_modified():
    app, context, adapter = dashboard()
    frontend = StandaloneFrontend(context, logging.getLogger('test'))
    before = list(app.router.routes)
    assert not frontend.register(SimpleNamespace())
    assert not StandaloneFrontend(object(), logging.getLogger('test')).register(adapter)
    assert app.router.routes == before
