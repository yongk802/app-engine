"""Owner-only store management for the standalone engine host."""
from dataclasses import asdict
from importlib.resources import files
import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from .store_contracts import StoreError


def create_store_router(bridge, runtime, *, require_owner, require_owner_action,
                        is_launcher_host, admin_capability, available) -> APIRouter:
    router = APIRouter(prefix='/api/app-engine', tags=['stores'])
    headers = {'Cache-Control': 'no-store'}
    limits = {'url': 2048, 'name': 200, 'store_id': 64, 'app_id': 64,
              'version': 64, 'fingerprint': 100}

    def store_error(exc):
        status = {'install_conflict': 409, 'release_changed': 409,
                  'store_unavailable': 502, 'store_not_found': 404,
                  'release_not_found': 404}.get(exc.code, 400)
        return HTTPException(status, detail={'code': exc.code, 'message': str(exc)})

    def require_read(request):
        require_owner(request)
        require_available()
        if not is_launcher_host(request):
            raise HTTPException(403, 'launcher origin required')

    def require_available():
        if not available():
            raise HTTPException(403, 'Store management requires isolated app origins; disable APP_ENGINE_APP_ORIGINS=same.')

    def require_action(request):
        require_owner_action(request)
        require_available()

    async def payload(request, fields, optional=()):
        require_action(request)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 16 * 1024:
                raise HTTPException(413, detail={'code': 'invalid_request', 'message': 'Request body exceeds 16 KiB.'})
            body.extend(chunk)
        try:
            value = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(400, detail={'code': 'invalid_request', 'message': 'Provide a JSON object.'}) from None
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in fields):
            raise HTTPException(400, detail={'code': 'invalid_request', 'message': 'Required fields must be strings.'})
        if any(key in value and not isinstance(value[key], str) for key in optional):
            raise HTTPException(400, detail={'code': 'invalid_request', 'message': 'Optional fields must be strings.'})
        if any(key in value and len(value[key]) > limits[key] for key in (*fields, *optional)):
            raise HTTPException(400, detail={'code': 'invalid_request', 'message': 'A request field exceeds its length limit.'})
        return value

    async def perform(operation, status=200):
        try:
            result = await operation
        except StoreError as exc:
            raise store_error(exc) from exc
        return JSONResponse(asdict(result), status_code=status, headers=headers)

    @router.get('/stores')
    async def stores(request: Request):
        require_read(request)
        return JSONResponse([asdict(store) for store in bridge.stores()], headers=headers)

    @router.post('/stores', status_code=201)
    async def add_store(request: Request):
        value = await payload(request, ('url',), ('name',))
        return await perform(bridge.add_store(value['url'], value.get('name', '')), 201)

    @router.delete('/stores/{store_id}', status_code=204)
    async def remove_store(store_id: str, request: Request):
        require_action(request)
        try:
            await bridge.remove_store(store_id)
        except StoreError as exc:
            raise store_error(exc) from exc
        return Response(status_code=204, headers=headers)

    @router.get('/store-catalog')
    async def catalog(request: Request, q: str = ''):
        require_read(request)
        value = asdict(await bridge.catalog())
        terms = q.casefold().split()
        if terms:
            value['apps'] = [item for item in value['apps'] if all(
                term in ' '.join((item['release']['id'], item['release']['label'],
                                 item['release']['description'], item['release']['author'],
                                 *item['release']['categories'], item['store_url'])).casefold()
                for term in terms)]
        return JSONResponse(value, headers=headers)

    @router.post('/store-installs/preview')
    async def preview(request: Request):
        value = await payload(request, ('store_id', 'app_id', 'version'))
        return await perform(bridge.preview(value['store_id'], value['app_id'], value['version']))

    @router.post('/store-installs', status_code=201)
    async def install(request: Request):
        value = await payload(request, ('fingerprint',))
        try:
            installed = await bridge.install(value['fingerprint'])
        except StoreError as exc:
            raise store_error(exc) from exc
        await runtime.catalog.configure(runtime.catalog_key, await runtime.host.sources(runtime.subject))
        return JSONResponse(asdict(installed), status_code=201, headers=headers)

    @router.get('/stores/ui', response_class=HTMLResponse)
    async def ui(request: Request):
        require_read(request)
        html = files('app_engine').joinpath('store_ui/index.html').read_text('utf-8')
        html = html.replace('__APP_ENGINE_ADMIN_CAPABILITY__', admin_capability)
        return HTMLResponse(html, headers={**headers, 'Content-Security-Policy': "frame-ancestors 'none'"})

    return router
