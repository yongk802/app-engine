import json
import socket
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('managed', [False, True], ids=['static-legacy-save', 'managed-control-name'])
def test_launcher_save_survives_engine_restart(engine_server, browser_page, managed):
    engine = engine_server
    app_id = 'players' if managed else 'browser-notes'
    directory = engine.add_app(app_id, managed=managed)
    legacy = engine.state / f'{app_id}.json'
    original = b'{"engine_canary":"private account data"}' if managed else b'{"note":"before upgrade"}'
    legacy.write_bytes(original)
    expected_note = '' if managed else 'before upgrade'
    approvals = []

    def approve(dialog):
        assert dialog.type == 'confirm'
        assert 'Approve this local process plan?' in dialog.message
        approvals.append(dialog.message)
        dialog.accept()

    def open_notes(page):
        page.on('dialog', approve)
        page.goto(engine.url)
        page.locator(f'#applist > button[data-app-id="{app_id}"]').click()
        notes = page.frame_locator('iframe[title="Browser Notes"]')
        expect(notes.get_by_role('status')).to_have_text('Loaded')
        return notes

    engine.start()
    with browser_page(engine) as page:
        notes = open_notes(page)
        expect(notes.get_by_label('Note', exact=True)).to_have_value(expected_note)
        notes.get_by_label('Note', exact=True).fill('Saved through the real app iframe')
        notes.get_by_role('button', name='Save note').click()
        expect(notes.get_by_role('status')).to_have_text('Saved')
    save = engine.state / 'saves' / 'owner' / f'{app_id}.json'
    assert json.loads(save.read_text()) == {'note': 'Saved through the real app iframe'}
    assert legacy.read_bytes() == original
    backend = json.loads((directory / 'backend.json').read_text()) if managed else None
    engine.stop()
    if backend:
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', backend['port'])) != 0, 'Managed backend was orphaned'

    engine.start()
    with browser_page(engine) as page:
        notes = open_notes(page)
        expect(notes.get_by_label('Note', exact=True)).to_have_value('Saved through the real app iframe')
        notes.get_by_label('Note', exact=True).fill('Updated after engine restart')
        notes.get_by_role('button', name='Save note').click()
        expect(notes.get_by_role('status')).to_have_text('Saved')
        page.reload()
        page.locator(f'#applist > button[data-app-id="{app_id}"]').click()
        notes = page.frame_locator('iframe[title="Browser Notes"]')
        expect(notes.get_by_role('status')).to_have_text('Loaded')
        expect(notes.get_by_label('Note', exact=True)).to_have_value('Updated after engine restart')
    assert legacy.read_bytes() == original
    # Local-owner approvals are per open: initial, after restart, after reload.
    assert len(approvals) == (3 if managed else 0)
    if managed:
        assert json.loads((directory / 'backend.json').read_text())['pid'] != backend['pid']


@pytest.mark.parametrize('destination', ['launcher', 'sibling'])
def test_managed_app_content_cannot_execute_on_other_origins(engine_server, browser_page, destination):
    engine = engine_server
    engine.add_app('browser-notes', managed=True)
    engine.add_app('other')
    engine.start()
    with browser_page(engine) as page:
        page.on('dialog', lambda dialog: dialog.accept())
        page.goto(engine.url)
        page.locator('#applist > button[data-app-id="browser-notes"]').click()
        notes = page.frame_locator('iframe[title="Browser Notes"]')
        expect(notes.get_by_role('status')).to_have_text('Loaded')
        source = page.locator('iframe[title="Browser Notes"]').get_attribute('src')
        target = engine.url if destination == 'launcher' else f'http://other.localhost:{engine.port}'
        response = page.goto(target + urlsplit(source).path)
        assert response.status == 403
        assert page.evaluate('Boolean(window.browserFlowApp)') is False


def test_public_app_cannot_impersonate_owner_chat_with_shared_cookie(engine_server, browser_page):
    engine = engine_server
    engine.add_app('browser-notes')
    engine.url = f'http://play.localhost:{engine.port}'
    engine.env.update({'APP_ENGINE_PUBLIC_ORIGIN': engine.url,
                       'APP_ENGINE_ADMIN_SECRET': 'browser-test-owner'})
    engine.start()
    with browser_page(engine) as page:
        page.goto(engine.url)
        page.get_by_placeholder('Admin secret').fill('browser-test-owner')
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.locator('#applist > button[data-app-id="browser-notes"]').click()
        notes = page.frame_locator('iframe[title="Browser Notes"]')
        expect(notes.get_by_role('status')).to_have_text('Loaded')
        cookies = page.context.cookies()
        session = next(cookie for cookie in cookies if cookie['name'] == 'app_engine_session')
        assert session['httpOnly'] and session['domain'] == '.play.localhost'
        result = notes.locator('body').evaluate('''async () => {
          const read = await window.fetch('/api/chat');
          const write = await window.fetch('/api/chat', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: 'app must not send this as owner'}),
          });
          return [read.status, write.status];
        }''')
        assert result == [403, 403]
        assert page.evaluate("async () => (await (await window.fetch('/api/chat')).json()).messages") == []


def test_launcher_approves_multiplayer_before_rule_execution(engine_server, browser_page):
    engine = engine_server
    engine.add_app('browser-notes', multiplayer=True)
    engine.start()
    dialogs = []
    with browser_page(engine) as page:
        def approve(dialog):
            dialogs.append(dialog.message)
            assert dialog.type == 'confirm'
            assert 'Approve multiplayer code' in dialog.message
            assert 'host account privileges' in dialog.message
            dialog.accept()
        page.on('dialog', approve)
        page.goto(engine.url)
        page.locator('#applist > button[data-app-id="browser-notes"]').click()
        notes = page.frame_locator('iframe[title="Browser Notes"]')
        expect(notes.get_by_role('status')).to_have_text('Loaded')
        result = notes.locator('body').evaluate('''async () => {
          const token = new window.URLSearchParams(window.location.hash.slice(1)).get('atrium_state_token');
          const response = await window.fetch('/api/app-multiplayer/browser-notes/health', {
            headers: {'X-App-State-Token': token},
          });
          return {status: response.status, body: await response.json()};
        }''')
        assert result['status'] == 200
        assert result['body']['rulesVersion'] == 'fake-rules-1'
    assert len(dialogs) == 1


def test_launcher_approves_legacy_backend_then_app_can_save(engine_server, browser_page, legacy_backend):
    engine = engine_server
    directory = engine.add_app('browser-notes')
    dialogs = []
    with legacy_backend(directory) as url:
        manifest = directory / 'app.json'
        value = json.loads(manifest.read_text())
        value['entry_point'] = url
        manifest.write_text(json.dumps(value))
        engine.start()
        with browser_page(engine) as page:
            def approve(dialog):
                dialogs.append(dialog.message)
                assert dialog.type == 'confirm'
                assert 'to proxy the local service ' + url in dialog.message
                dialog.accept()
            page.on('dialog', approve)
            page.goto(engine.url)
            page.locator('#applist > button[data-app-id="browser-notes"]').click()
            notes = page.frame_locator('iframe[title="Browser Notes"]')
            expect(notes.get_by_role('status')).to_have_text('Loaded')
            notes.get_by_label('Note', exact=True).fill('Approved legacy backend works')
            notes.get_by_role('button', name='Save note').click()
            expect(notes.get_by_role('status')).to_have_text('Saved')
    assert len(dialogs) == 1
