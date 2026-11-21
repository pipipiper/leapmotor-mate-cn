from urllib.parse import parse_qs, urlsplit

import db_reader
import geocode
import place_lookup
from leapmotor_cn.protocol import wgs84_to_gcj02
from test_cn_language import cn_web  # noqa: F401


def test_amap_key_is_separate_and_manual_lookup_replaces_osm_address(cn_web, monkeypatch):
    client, database = cn_web
    database.set_setting('setup_complete', '1')
    db_reader.set_secret('geocoder_key', 'legacy-key')
    monkeypatch.setattr(place_lookup, '_asked_at', float('-inf'))
    point = (39.9165, 116.3971)
    monkeypatch.setattr(geocode, '_get', lambda url: {
        'display_name': 'Old address', 'address': {'road': 'Old Road'},
    })
    assert place_lookup.look_up_now([point], pause=lambda seconds: None)['found'] == 1

    missing = client.post('/api/settings/geocoder', data={'geocoder_provider': 'amap'})
    assert '请填写高德' in missing.text
    assert db_reader.get_setting('geocoder_provider', '') == ''
    assert 'data-mate-map-provider="osm"' in client.get('/').text
    client.post('/api/settings/geocoder', data={'geocoder_provider': 'amap', 'geocoder_key': 'synthetic-amap-key'})
    assert db_reader.get_secret('geocoder_key') == 'legacy-key'
    assert db_reader.get_secret('amap_geocoder_key') == 'synthetic-amap-key'
    assert database.get_setting('amap_geocoder_key') != 'synthetic-amap-key'
    settings = client.get('/settings').text
    assert '高德地图 (AMap)' in settings
    assert 'data-mate-map-provider="amap"' in settings
    assert 'synthetic-amap-key' not in settings

    calls = []
    def reply(url):
        parts = urlsplit(url)
        calls.append(parts.hostname)
        assert parts.path == '/v3/geocode/regeo'
        latitude, longitude = wgs84_to_gcj02(*point)
        assert parse_qs(parts.query) == {
            'key': ['synthetic-amap-key'], 'extensions': ['base'],
            'location': [f'{longitude:.6f},{latitude:.6f}'],
        }
        return {'status': '1', 'regeocode': {
            'formatted_address': '北京市东城区东长安街1号',
            'addressComponent': {'province': '北京市', 'city': [], 'district': '东城区',
                                 'streetNumber': {'street': '东长安街', 'number': '1号'}},
        }}

    monkeypatch.setattr(geocode, '_get', reply)
    result = place_lookup.look_up_now([point], pause=lambda seconds: None)
    assert result['found'] == 1
    assert calls == ['restapi.amap.com']
    address = database._conn.execute('SELECT provider, latitude, longitude, road, house_number, locality FROM addresses').fetchone()
    assert tuple(address) == ('amap', *point, '东长安街', '1号', '北京市')

    client.post('/api/settings/geocoder', data={'geocoder_provider': ''})
    assert 'data-mate-map-provider="osm"' in client.get('/map').text
    client.post('/api/settings/geocoder', data={'geocoder_provider': 'amap'})
    map_page = client.get('/map').text
    assert 'data-mate-map-provider="amap"' in map_page
    assert 'synthetic-amap-key' not in map_page


def test_b05_is_shown_as_lafa5_without_changing_model_code(cn_web):
    client, database = cn_web
    database.ensure_vehicle('SYNTHETIC-B05', 'B05', 2026)
    database.set_setting('setup_complete', '1')
    overview = client.get('/').text
    settings = client.get('/settings').text
    assert 'Lafa5 实时车况' in overview
    assert 'Leapmotor Lafa5' in settings
    assert db_reader.get_vehicle()[0]['car_type'] == 'B05'


def test_vector_credentials_are_separate_and_only_the_js_key_reaches_the_page(cn_web, monkeypatch):
    import main

    client, database = cn_web
    database.set_setting('setup_complete', '1')
    db_reader.set_secret('amap_geocoder_key', 'address-key')
    form = {'geocoder_provider': 'amap', 'amap_vector_present': '1', 'amap_vector_enabled': '1'}
    assert '配套安全密钥' in client.post('/api/settings/geocoder', data=form).text
    form.update(amap_js_key='js-key', amap_js_security_code='private-code')
    assert '已保存' in client.post('/api/settings/geocoder', data=form).text
    assert db_reader.get_secret('amap_geocoder_key') == 'address-key'
    assert database.get_setting('amap_js_security_code') != 'private-code'
    settings = client.get('/settings').text
    assert 'data-mate-amap-js-key="js-key"' in settings
    assert 'private-code' not in settings and 'address-key' not in settings

    endpoint = '/api/_AMapService/v4/map/styles'

    class Upstream:
        status = 200

        def __init__(self, host, timeout):
            assert host == 'webapi.amap.com'

        def request(self, method, url):
            parts = urlsplit(url)
            assert method == 'GET' and parts.path == '/v4/map/styles'
            assert parse_qs(parts.query) == {'id': ['dark'], 'key': ['js-key'], 'jscode': ['private-code']}

        def getresponse(self):
            return self

        def read(self, limit):
            return b'{}'

        def close(self):
            pass

    monkeypatch.setattr(main, 'HTTPSConnection', Upstream)
    response = client.get(endpoint, params={'id': 'dark', 'jscode': 'override', 'callback': 'override'})
    assert response.status_code == 200 and response.json() == {}
    assert client.get(endpoint, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    client.post('/api/settings/geocoder', data={'geocoder_provider': ''})
    assert client.get(endpoint).status_code == 404
    assert 'data-mate-amap-js-key' not in client.get('/map').text
    client.post('/api/settings/geocoder', data={'geocoder_provider': 'amap'})
    assert 'data-mate-amap-js-key="js-key"' in client.get('/map').text
