from urllib.parse import urlsplit

import pytest

from cn_telemetry import decode_status
from test_cn_adapter import raw, vehicle
from test_cn_language import cn_web  # noqa: F401
from web_in_a_browser import chromium


@pytest.mark.parametrize('path', ['/', '/commands'])
def test_mobile_command_feedback_survives_refresh_and_waits_for_confirmation(cn_web, monkeypatch, path):
    import main

    sync_api = pytest.importorskip('playwright.sync_api')
    client, database = cn_web
    car = vehicle()
    vehicle_id = database.ensure_vehicle(car.vin, 'B05', 2026)
    database.set_setting('setup_complete', '1')
    frame = decode_status(raw(**{'1298': 0, '1277': 1, '2188': 152}), car)
    database.save_position(vehicle_id, frame.data)
    monkeypatch.setattr(main.capability_profile, 'command_shown', lambda *args, **kwargs: True)
    monkeypatch.setattr(main, '_last_command_at', 0)
    monkeypatch.setattr(main, '_command_receipts', {})
    monkeypatch.setitem(main._COMMANDS, 'lock', lambda: (True, 'cloud accepted'))
    monkeypatch.setattr(main, '_post_command_refresh', lambda *args, **kwargs: None)

    pending = []

    def serve(route):
        request = route.request
        destination = urlsplit(request.url)
        if destination.hostname != 'mate-cn.test':
            return route.abort()
        if destination.path == '/api/command/lock':
            pending.append(route)
            return
        response = client.request(request.method, destination.path + ('?' + destination.query if destination.query else ''),
                                  content=request.post_data_buffer, headers=request.headers)
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    playwright, browser = chromium(sync_api)
    try:
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', serve)
        page.goto('http://mate-cn.test' + path)
        if path == '/':
            sync_api.expect(page.locator('#overview-soc')).to_be_visible()
            sync_api.expect(page.locator('#overview-range')).to_have_text('152km')
            sync_api.expect(page.locator('#overview-frame-age')).to_contain_text('最后上报')
            sync_api.expect(page.locator('#overview-vehicle-alerts')).to_contain_text('车门未关闭')
            sync_api.expect(page.locator('#overview-status-details')).not_to_have_attribute('open', '')
        control = page.locator('[hx-post="api/command/lock"]')
        control.click()
        page.locator('#mate-confirm-yes').click()
        feedback = page.locator('#command-receipt')
        sync_api.expect(feedback).to_have_attribute('data-state', 'sending')
        response = client.post('/api/command/lock')
        token = response.headers['X-Mate-Command-Receipt']
        pending.pop().fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        sync_api.expect(feedback).to_have_attribute('data-state', 'waiting')
        sync_api.expect(feedback).to_contain_text('云端已受理')
        assert client.get('/api/command-receipt/' + token).json() == {'status': 'waiting'}
        target = 'api/overview-hero' if path == '/' else 'api/cmd-grid'
        with page.expect_response('**/' + target):
            page.evaluate('refreshHero()' if path == '/' else 'refreshCmdGrid()')
        sync_api.expect(feedback).to_have_attribute('data-state', 'waiting')
        main._command_receipts[token]['confirmed'] = True
        sync_api.expect(feedback).to_have_text('车辆已确认')
        assert not errors
    finally:
        browser.close()
        playwright.stop()
