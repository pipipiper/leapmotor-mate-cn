"""China cloud charging records: endpoints, GCJ02/QUICK migration, history sync, and web UI."""
import json
import sqlite3
from datetime import date, datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import mate_api  # noqa: F401
import db as mate_db
import db_reader
from cn_telemetry import decode_status
import history_worker as worker
import migrate_cloud_charges as charge_migrator
from test_cn_adapter import api, raw, vehicle  # noqa: F401
from test_cn_language import cn_web  # noqa: F401
from web_in_a_browser import chromium


@pytest.mark.parametrize('code,port,expected', [
    (1, {'1197': 1}, '充电中 · 直流'),
    (1, {'47': 1}, '充电中 · 交流'),
    (2, {}, '充电已停止'),
    (3, {}, '充电故障'),
    (4, {}, '等待预约充电'),
    (6, {}, '充电暂停'),
    (None, {}, '充电状态未知'),
])
def test_cn_live_charge_displays_reported_state_and_connector(cn_web, code, port, expected):
    client, database = cn_web
    car = vehicle()
    vehicle_id = database.ensure_vehicle(car.vin, 'B05', 2026)
    database.set_setting('setup_complete', '1')
    frame = decode_status(raw(**{'1149': code, '1177': None, **port}), car)
    database.save_position(vehicle_id, frame.data)

    response = client.get('/api/charging-live')
    assert response.status_code == 200
    assert expected in response.text
    assert '0.00' not in response.text


def test_cn_charge_view_restores_the_selected_date_and_ignores_old_periods(cn_web):
    sync_api = pytest.importorskip('playwright.sync_api')
    client, database = cn_web
    database.ensure_vehicle('SYNTHETIC-B05', 'B05', 2026)
    database.set_setting('setup_complete', '1')
    old_requests = []
    hold_week = False
    opened_dates = []

    def stats(period, energy, *dates):
        return {'today': '2026-11-20', 'valid_start': '2026-01-01', 'period': period,
                'total_energy_kwh': energy, 'total_count': 2, 'fast_charge_percent': 50,
                'series': [{'date': day, 'label': day.split('-')[-1], 'energy_kwh': 2} for day in dates]}

    def serve(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname != 'mate-cn.test':
            return route.abort()
        query = parse_qs(parsed.query)
        if parsed.path == '/api/cn/charges/statistics':
            mode = query['mode'][0]
            if mode == '7d' and hold_week:
                old_requests.append(route)
                return
            if mode == 'year':
                return route.fulfill(json=stats('2026', 48, '2026-10'))
            return route.fulfill(json=stats('2026-11', 12, '2026-11-13', '2026-11-19'))
        if parsed.path == '/api/cn/charges/details':
            opened_dates.append(query)
            return route.fulfill(json={'items': []})
        if parsed.path == '/api/cn/charges/sync':
            return route.fulfill(json={'inserted': 1})
        response = client.request(request.method, parsed.path + ('?' + parsed.query if parsed.query else ''))
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    playwright, browser = chromium(sync_api)
    try:
        page = browser.new_page(viewport={'width': 390, 'height': 900})
        page.route('**/*', serve)
        page.goto('http://mate-cn.test/charges?month=2026-10&official_mode=month'
                  '&official_period=2026-11&official_date=2026-11-13')
        sync_api.expect(page.locator('#cn-charge-details-target')).to_have_text('2026/11/13')
        with page.expect_navigation():
            page.locator('#cn-charge-sync-btn').click()
        sync_api.expect(page.locator('#cn-charge-details-target')).to_have_text('2026/11/13')
        assert opened_dates.count({'date': ['2026-11-13']}) == 2
        assert parse_qs(urlsplit(page.url).query)['month'] == ['2026-10']

        hold_week = True
        with page.expect_request('**/api/cn/charges/statistics?mode=7d'):
            page.locator('[data-cn-mode="7d"]').click()
        sync_api.expect(page.locator('#cn-cs-energy')).to_have_text('—')
        page.locator('[data-cn-mode="year"]').click()
        sync_api.expect(page.locator('#cn-cs-energy')).to_have_text('48.0')
        with page.expect_response('**/api/cn/charges/statistics?mode=7d') as late:
            old_requests.pop().fulfill(json=stats('2026-11-20', 999, '2026-11-20'))
        late.value.finished()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        sync_api.expect(page.locator('#cn-cs-energy')).to_have_text('48.0')
        sync_api.expect(page.locator('#cn-charge-details-target')).to_have_text('2026年10月')
    finally:
        browser.close()
        playwright.stop()


def test_cn_charge_endpoints_use_carownerservice_charge_json_post(api):
    car = vehicle()
    def transport(method, url, headers, body=None, form=False):
        api.calls.append((method, url, dict(headers), body, form))
        if '/getCarRoute' in url:
            return {'code': 0, 'data': {'appRegion': 'https://appgateway.leapmotor.com'}}
        if url.endswith('/carownerservice/charge/vail/startTime/query'):
            return {'code': 0, 'data': {'vailTime': 1767196800000, 'currentTimeMillis': 1795055788677}}
        if url.endswith('/carownerservice/charge/statistic/days'):
            return {'code': 0, 'data': {
                'totalChargeCounts': 5,
                'totalChargeInEnergy': 118.5,
                'fastChargePercent': 100.0,
                'chargeStatisList': [
                    {'statisDay': '2026-11-14 00:00:00', 'chargeInEnergy': 43.5},
                    {'statisDay': '2026-11-15 00:00:00', 'chargeInEnergy': 75.0},
                ],
            }}
        if url.endswith('/carownerservice/charge/statistic/year'):
            return {'code': 0, 'data': {
                'totalChargeCounts': 120,
                'totalChargeInEnergy': 2950.1,
                'fastChargePercent': 64.0,
                'chargeStatisList': None,
            }}
        if url.endswith('/carownerservice/charge/daily/detail/page'):
            page = int(body['pageNum'])
            if page == 1:
                return {'code': 0, 'data': {
                    'pageNum': 1, 'pageSize': 1, 'totalPage': 2, 'total': 2,
                    'list': [{
                        'chargeGunStartTs': 1794734760000,
                        'chargeGunEndTs': 1794735660000,
                        'chargeEarliestTs': 1794734760000,
                        'chargeLatestTs': 1794735660000,
                        'chargeType': 'QUICK',
                        'chargeInEnergy': 24.7,
                        'chargeStartLatitude': '31.2304',
                        'chargeStartLongitude': '121.4737',
                        'zone': 'UTC+8',
                    }],
                }}
            return {'code': 0, 'data': {
                'pageNum': 2, 'pageSize': 1, 'totalPage': 2, 'total': 2,
                'list': [{
                    'chargeGunStartTs': 1794733920000,
                    'chargeGunEndTs': 1794734580000,
                    'chargeEarliestTs': 1794733920000,
                    'chargeLatestTs': 1794734580000,
                    'chargeType': 'SLOW',
                    'chargeInEnergy': 11.1,
                    'chargeStartLatitude': '31.2304',
                    'chargeStartLongitude': '121.4737',
                    'zone': 'UTC+8',
                }],
            }}
        raise AssertionError(f'unexpected url: {url}')

    api._p.transport = transport
    api.calls.clear()

    vail = api.get_charge_valid_start(car)
    assert vail['vail_date'] == '2026-01-01'

    days = api.get_charge_statistics_days(car, '2026-11-13', '2026-11-19')
    assert days['total_count'] == 5
    assert days['total_energy_kwh'] == 118.5
    assert days['fast_charge_percent'] == 100.0
    assert len(days['items']) == 2

    year = api.get_charge_statistics_year(car, '2026')
    assert year['total_count'] == 120
    assert year['items'] == []

    records = api.get_charge_records(car, '2026-11-15', '2026-11-16', page_size=1)
    assert len(records) == 2
    assert all(r['vin'] == car.vin for r in records)
    detail_calls = [c for c in api.calls if c[1].endswith('/carownerservice/charge/daily/detail/page')]
    assert len(detail_calls) == 2
    assert detail_calls[0][0] == 'POST'
    assert detail_calls[0][4] is False
    assert detail_calls[0][3]['pageNum'] == '1'


def test_cn_migrate_cloud_charges_maps_quick_to_dc_and_converts_gcj02(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    path = str(tmp_path / 'cn-migrate.db')
    database = mate_db.Database(path)
    vid = database.ensure_vehicle('SYNTHETIC-B05', 'B05', 2026)
    database._conn.execute('''CREATE TABLE IF NOT EXISTS api_lab_cloud_history_records (
        kind TEXT NOT NULL, record_sha256 TEXT NOT NULL, source TEXT NOT NULL,
        imported_at TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(kind, record_sha256))''')
    rows = [
        ('h1', {
            'vin': 'SYNTHETIC-B05',
            'chargeGunStartTs': 1794734760000,
            'chargeGunEndTs': 1794735660000,
            'chargeEarliestTs': 1794734760000,
            'chargeLatestTs': 1794735660000,
            'chargeType': 'QUICK',
            'chargeInEnergy': 24.7,
            'chargeStartLatitude': '31.2304',
            'chargeStartLongitude': '121.4737',
            'zone': 'UTC+8',
        }),
        ('h2', {
            'vin': 'SYNTHETIC-B05',
            'chargeGunStartTs': 1794733920000,
            'chargeGunEndTs': 1794734580000,
            'chargeEarliestTs': 1794733920000,
            'chargeLatestTs': 1794734580000,
            'chargeType': 'SLOW',
            'chargeInEnergy': 11.1,
            'chargeStartLatitude': '31.2304',
            'chargeStartLongitude': '121.4737',
            'zone': 'UTC+8',
        }),
    ]
    for digest, payload in rows:
        database._conn.execute(
            'INSERT INTO api_lab_cloud_history_records VALUES (?,?,?,?,?)',
            ('charge', digest, 'test', '2026-11-19T00:00:00+00:00', json.dumps(payload)),
        )
    database._conn.commit()
    database._conn.close()

    with sqlite3.connect(path) as con:
        report = charge_migrator.migrate(con)
        assert report['inserted'] == 2
        second = charge_migrator.migrate(con)
        assert second['unchanged'] == 2 and second['inserted'] == 0
        saved = con.execute(
            'SELECT vehicle_id, charge_type, energy_added_kwh, latitude, longitude FROM charges ORDER BY id'
        ).fetchall()
    assert saved[0][0] == vid and saved[0][1] == 'DC' and saved[0][2] == pytest.approx(24.7)
    assert saved[1][0] == vid and saved[1][1] == 'AC' and saved[1][2] == pytest.approx(11.1)
    # GCJ-02 -> WGS-84 shifts Shanghai coordinates slightly
    assert abs(saved[0][3] - 31.2304) > 0.001
    assert abs(saved[0][4] - 121.4737) > 0.001


def test_cn_history_worker_syncs_current_and_past_months_once(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    path = str(tmp_path / 'cn-worker.db')
    monkeypatch.setenv('DB_PATH', path)
    database = mate_db.Database(path)
    database.ensure_vehicle('SYNTHETIC-B05', 'B05', 2026)
    database.set_setting('leapmotor_user', '13800000000')
    database._conn.close()

    monkeypatch.setattr(worker, 'DB', path)
    monkeypatch.setattr(worker, 'connect_db', lambda: sqlite3.connect(path))
    monkeypatch.setattr(worker.crypto, 'decrypt', lambda value: value)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
            return base.astimezone(tz) if tz else base

    monkeypatch.setattr(worker, 'datetime', _FixedDatetime)
    asked_windows = []

    class FakeCNClient:
        def __init__(self, **kwargs):
            pass

        def get_vehicle_list(self):
            return [SimpleNamespace(vin='SYNTHETIC-B05', is_shared=True)]

        def get_charge_valid_start(self, car):
            return {'vail_date': '2026-10-01'}

        def get_charge_records(self, car, start_date, end_date):
            asked_windows.append((start_date, end_date))
            if start_date == '2026-10-01':
                return [{
                    'vin': car.vin,
                    'chargeGunStartTs': 1791504000000,
                    'chargeGunEndTs': 1791505800000,
                    'chargeEarliestTs': 1791504000000,
                    'chargeLatestTs': 1791505800000,
                    'chargeType': 'QUICK',
                    'chargeInEnergy': 30.0,
                }]
            return [{
                'vin': car.vin,
                'chargeGunStartTs': 1794734760000,
                'chargeGunEndTs': 1794735660000,
                'chargeEarliestTs': 1794734760000,
                'chargeLatestTs': 1794735660000,
                'chargeType': 'SLOW',
                'chargeInEnergy': 12.5,
            }]

    monkeypatch.setattr(worker, 'ChinaAPIClient', FakeCNClient)
    first = worker.sync_once()
    assert first['charge_import']['inserted'] == 2
    assert asked_windows == [('2026-11-01', '2026-11-20'), ('2026-10-01', '2026-11-01')]

    asked_windows.clear()
    second = worker.sync_once()
    assert second['charge_import']['unchanged'] == 2
    assert asked_windows == [('2026-11-01', '2026-11-20')]

    with sqlite3.connect(path) as check_db:
        kinds = check_db.execute(
            "SELECT kind, month FROM api_lab_cloud_history_months WHERE vin='SYNTHETIC-B05'"
        ).fetchall()
    assert kinds == [('cn_charge', '2026-10')]


def test_cn_history_worker_ignores_legacy_charge_month_markers(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    path = str(tmp_path / 'cn-worker-legacy.db')
    monkeypatch.setenv('DB_PATH', path)
    database = mate_db.Database(path)
    database.ensure_vehicle('SYNTHETIC-B05', 'B05', 2026)
    database.set_setting('leapmotor_user', '13800000000')
    database._conn.execute(
        "CREATE TABLE IF NOT EXISTS api_lab_cloud_history_months "
        "(vin TEXT, kind TEXT, month TEXT, asked_at TEXT, PRIMARY KEY(vin, kind, month))"
    )
    database._conn.execute(
        "INSERT INTO api_lab_cloud_history_months VALUES ('SYNTHETIC-B05', 'charge', '2026-10', '2026-11-19T03:09:07')"
    )
    database._conn.commit()
    database._conn.close()

    monkeypatch.setattr(worker, 'DB', path)
    monkeypatch.setattr(worker, 'connect_db', lambda: sqlite3.connect(path))
    monkeypatch.setattr(worker.crypto, 'decrypt', lambda value: value)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
            return base.astimezone(tz) if tz else base

    monkeypatch.setattr(worker, 'datetime', _FixedDatetime)
    asked_windows = []

    class FakeCNClient:
        def __init__(self, **kwargs):
            pass

        def get_vehicle_list(self):
            return [SimpleNamespace(vin='SYNTHETIC-B05', is_shared=True)]

        def get_charge_valid_start(self, car):
            return {'vail_date': '2026-10-01'}

        def get_charge_records(self, car, start_date, end_date):
            asked_windows.append((start_date, end_date))
            return []

    monkeypatch.setattr(worker, 'ChinaAPIClient', FakeCNClient)
    worker.sync_once()
    assert ('2026-10-01', '2026-11-01') in asked_windows

    with sqlite3.connect(path) as check_db:
        rows = check_db.execute(
            "SELECT kind, month FROM api_lab_cloud_history_months WHERE vin='SYNTHETIC-B05' ORDER BY kind"
        ).fetchall()
    assert ('cn_charge', '2026-10') in rows


def test_cn_web_charge_statistics_and_sync_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import command_client

    path = str(tmp_path / 'cn-web-charges.db')
    monkeypatch.setenv('MATE_MARKET', 'CN')
    monkeypatch.setenv('DB_PATH', path)
    monkeypatch.setattr(db_reader, 'DB_PATH', path)
    monkeypatch.setattr(db_reader, '_lang_memo', [None])
    database = mate_db.Database(path)
    database.ensure_vehicle('SYNTHETIC-B05', 'B05', 2026)
    database.set_setting('setup_complete', '1')
    main._cn_charge_stats_cache.clear()
    main._cn_charge_details_cache.clear()
    command_client._session._cn_charge_valid_start_cache.clear()

    detail_calls = []

    class FakeAPI:
        def get_charge_valid_start(self, car):
            return {'vail_date': '2026-02-26'}

        def get_charge_statistics_days(self, car, start_date, end_date):
            if start_date == '2026-01-01' and end_date == '2026-12-31':
                return {
                    'total_count': 12,
                    'total_energy_kwh': 240.0,
                    'fast_charge_percent': 50.0,
                    'items': [{'day': '2026-11-15', 'month': '2026-11', 'energy_kwh': 55.2}],
                }
            return {
                'total_count': 3,
                'total_energy_kwh': 55.2,
                'fast_charge_percent': 66.7,
                'items': [{'day': start_date, 'month': start_date[:7], 'energy_kwh': 20.0}],
            }

        def get_charge_statistics_year(self, car, year):
            return {
                'total_count': 12,
                'total_energy_kwh': 240.0,
                'fast_charge_percent': 50.0,
                'items': [],
            }

        def get_charge_records(self, car, start_date, end_date):
            detail_calls.append((start_date, end_date))
            return [
                {
                    'chargeEarliestTs': 1794709500000,  # 2026-11-15 10:25 Asia/Shanghai
                    'chargeLatestTs': 1794712500000,    # 2026-11-15 11:15 Asia/Shanghai
                    'chargeType': 'SLOW',
                    'chargeInEnergy': 11.1,
                },
                {
                    'chargeEarliestTs': 1794734760000,  # 2026-11-15 17:26 Asia/Shanghai
                    'chargeLatestTs': 1794735660000,    # 2026-11-15 17:41 Asia/Shanghai
                    'chargeType': 'QUICK',
                    'chargeInEnergy': 24.7,
                },
            ]

    monkeypatch.setattr(command_client._session, '_connect', lambda: None)
    monkeypatch.setattr(command_client._session, '_target', lambda: 'SYNTHETIC-B05')
    monkeypatch.setattr(command_client._session, '_api', FakeAPI())
    monkeypatch.setattr(worker, 'sync_once', lambda: {'charge': 1, 'charge_import': {'inserted': 1, 'unchanged': 0, 'overlapping': 0}, 'at': '2026-11-19T02:00:00+00:00'})

    client = TestClient(main.app, base_url='http://mate-cn.test')
    html = client.get('/charges').text
    assert 'cn-charge-stats-card' in html
    assert 'cn-charge-sync-btn' in html
    assert 'cn-charge-details-section' in html
    assert 'csv-import-form' not in html

    assert client.get('/api/charges/import-template.csv').status_code in (404, 405)
    assert client.post('/api/charges/import').status_code in (404, 405)

    r7 = client.get('/api/cn/charges/statistics?mode=7d')
    assert r7.status_code == 200 and len(r7.json()['series']) == 7
    assert r7.json()['valid_start'] == '2026-02-26'
    assert 'today' in r7.json()
    rm = client.get('/api/cn/charges/statistics?mode=month&month=2026-11')
    assert rm.status_code == 200 and len(rm.json()['series']) == 30
    ry = client.get('/api/cn/charges/statistics?mode=year&year=2026')
    assert ry.status_code == 200 and len(ry.json()['series']) == 12
    nov_point = next(p for p in ry.json()['series'] if p['date'] == '2026-11')
    assert nov_point['energy_kwh'] == 55.2

    rd = client.get('/api/cn/charges/details?date=2026-11-15')
    assert rd.status_code == 200
    assert detail_calls == [('2026-11-15', '2026-11-16')]
    items = rd.json()['items']
    assert len(items) == 2
    assert items[0]['charge_type'] == 'fast' and items[0]['duration_min'] == 15
    assert items[1]['charge_type'] == 'slow' and items[1]['duration_min'] == 50
    assert items[0]['start_ms'] > items[1]['start_ms']

    rm_det = client.get('/api/cn/charges/details?month=2026-11')
    assert rm_det.status_code == 200
    assert detail_calls[-1] == ('2026-11-01', '2026-12-01')

    # Cached day details do not hit API again until /api/cn/charges/sync clears cache
    count_before = len(detail_calls)
    assert client.get('/api/cn/charges/details?date=2026-11-15').status_code == 200
    assert len(detail_calls) == count_before

    rs = client.post('/api/cn/charges/sync')
    assert rs.status_code == 200 and rs.json()['inserted'] == 1
    assert client.get('/api/cn/charges/details?date=2026-11-15').status_code == 200
    assert len(detail_calls) == count_before + 1
    database._conn.close()
