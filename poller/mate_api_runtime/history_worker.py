"""Read-only cloud synchronization + local lab import; no vehicle controls."""
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import crypto
from pathlib import Path
from api_v2_bridge import DB, NewAPIClient, LeapmotorApiError, connect_db, setting, set_setting
from cn_backend import ChinaAPIClient, ChinaAuthRequired, ChinaError, db_path
from migrate_cloud_charges import migrate as migrate_charges
from process_lock import exclusive

_cn_sync_lock = threading.Lock()


def _client(username, password, device, on_login=None):
    """The installation's client, holding its live session: it signs its own reads and knows each
    vehicle's route. A login it does spend is told to `on_login`, like the poller's own."""
    certs = Path(DB).parent / 'certs'
    api = NewAPIClient(username=username, password=password, device_id=device,
                       app_cert_path=str(certs / 'app.crt'),
                       app_key_path=str(certs / 'app.key'), language='en-US')
    api.on_login = on_login
    return api


def _read_window(api, route, vin, kind, start, end):
    """Every record the cloud holds for one window, or ValueError: a batch is staged whole or not at all."""
    records=[];seen=set();baseline=None;complete=False
    for page in range(1,101):
        size=50 if kind=='charge' else 20
        body=dict(vin=vin,pageNum=str(page) if kind=='charge' else page,
                  pageSize=str(size) if kind=='charge' else size,
                  startTime=str(int(start.timestamp())),endTime=str(int(end.timestamp())))
        data=api.read('/carownerservice/'+kind+'/daily/detail/page',body,origin=route['appCenter'])['data']
        if any(type(data.get(k))is not int for k in ('pageNum','pageSize','totalPage','total')):
            raise ValueError('Invalid history pagination')
        if data['pageNum']!=page or data['pageSize']!=size:raise ValueError('Unexpected history page')
        totals=(data['total'],data['totalPage'])
        if baseline is not None and totals!=baseline:raise ValueError('History changed during paging')
        baseline=totals;rows=data.get('list')
        if not isinstance(rows,list):raise ValueError('Invalid history records')
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Invalid history record')
            if kind=='charge':
                if row.get('vin',vin)!=vin:raise ValueError('History vehicle mismatch')
                # Charge DTO has no VIN; bind to the authenticated request.
                row=dict(row,vin=vin)
            elif row.get('vin')!=vin:raise ValueError('History vehicle mismatch')
            key=json.dumps(row,sort_keys=True,separators=(',',':'))
            if key in seen:raise ValueError('Duplicate history record')
            seen.add(key);records.append(row)
        if len(records)>=data['total']:
            complete=len(records)==data['total'];break
        if not rows:break
    if not complete:raise ValueError('Incomplete history batch; not imported')
    return records


def _stage(kind, records, now, month=None, month_kind=None):
    """Stage one window's records; an earlier month, `(vin, 'YYYY-MM')`, is marked asked with them."""
    with connect_db() as db:
        for row in records:
            payload=json.dumps(row,sort_keys=True,separators=(',',':'))
            db.execute('INSERT OR IGNORE INTO api_lab_cloud_history_records VALUES (?,?,?,?,?)',
                (kind,hashlib.sha256(payload.encode()).hexdigest(),'api-v2-worker',now.isoformat(),payload))
        if month:
            db.execute('INSERT OR REPLACE INTO api_lab_cloud_history_months VALUES (?,?,?,?)',
                (month[0],month_kind or kind,month[1],now.isoformat()))


def sync_once(on_login=None):
    return _sync_cn(on_login)


def _cn_past_months(start_ym, today_date):
    year, month = start_ym
    min_year = today_date.year - 3
    if (year, month) < (min_year, today_date.month):
        year, month = min_year, today_date.month
    months = []
    while (year, month) < (today_date.year, today_date.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _sync_cn_body(on_login=None):
    with connect_db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            return {'state': 'uninitialized'}
        db.execute('''CREATE TABLE IF NOT EXISTS api_lab_cloud_history_records (
            kind TEXT NOT NULL, record_sha256 TEXT NOT NULL, source TEXT NOT NULL,
            imported_at TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(kind,record_sha256))''')
        db.execute('''CREATE TABLE IF NOT EXISTS api_lab_cloud_history_months (
            vin TEXT NOT NULL, kind TEXT NOT NULL, month TEXT NOT NULL, asked_at TEXT NOT NULL,
            PRIMARY KEY(vin,kind,month))''')
        if setting(db, 'api_v2_import_cloud_charges', '1') == '0':
            return {'state': 'disabled'}
        username = crypto.decrypt(setting(db, 'leapmotor_user')) or ''
        local_map = {row[0].lower(): row[0] for row in db.execute('SELECT vin FROM vehicles')}
        done = {tuple(row) for row in db.execute("SELECT vin,kind,month FROM api_lab_cloud_history_months WHERE kind='cn_charge'")}
    if not local_map:
        return {'state': 'no_vehicles'}
    api = ChinaAPIClient(username=username, on_login=on_login)
    try:
        bindings = api.get_vehicle_list()
    except ChinaAuthRequired:
        return {'state': 'auth_required'}
    cars = [(car, local_map[car.vin.lower()]) for car in bindings if car.vin.lower() in local_map]
    if not cars:
        return {'state': 'no_matching_vehicles'}
    zone = ZoneInfo('Asia/Shanghai')
    now = datetime.now(timezone.utc)
    local = now.astimezone(zone)
    local_date = local.date()
    current_start = date(local_date.year, local_date.month, 1).isoformat()
    current_end = (local_date + timedelta(days=1)).isoformat()
    summary = {'market': 'CN', 'charge': 0}
    for car, db_vin in cars:
        try:
            try:
                valid = api.get_charge_valid_start(car)
            except ChinaError:
                valid = {}
            records = api.get_charge_records(car, current_start, current_end)
            records = [dict(row, vin=db_vin) for row in records]
            _stage('charge', records, now)
            summary['charge'] += len(records)
            earliest = (local_date.year, local_date.month)
            if valid.get('vail_date'):
                try:
                    vail_d = date.fromisoformat(valid['vail_date'])
                    earliest = (vail_d.year, vail_d.month)
                except ValueError:
                    pass
            for year, month in _cn_past_months(earliest, local_date):
                month_key = f'{year:04d}-{month:02d}'
                if (db_vin, 'cn_charge', month_key) in done:
                    continue
                m_start = date(year, month, 1).isoformat()
                m_end = date(year + month // 12, month % 12 + 1, 1).isoformat()
                try:
                    month_records = api.get_charge_records(car, m_start, m_end)
                except (ChinaError, ValueError):
                    summary['months_incomplete'] = summary.get('months_incomplete', 0) + 1
                    continue
                month_records = [dict(row, vin=db_vin) for row in month_records]
                _stage('charge', month_records, now, (db_vin, month_key), month_kind='cn_charge')
                summary['charge'] += len(month_records)
        except (ChinaError, ValueError):
            summary['unavailable_vehicles'] = summary.get('unavailable_vehicles', 0) + 1
            continue
    with connect_db() as db:
        summary['charge_import'] = migrate_charges(db)
        summary['at'] = now.isoformat()
        set_setting(db, 'api_v2_history_sync', json.dumps(summary))
    print(json.dumps({'api_v2_history_sync': summary}), flush=True)
    return summary


def _sync_cn(on_login=None):
    target = os.environ.get('DB_PATH') or DB
    if target == ':memory:':
        return _sync_cn_body(on_login)
    lock_path = Path(target).resolve().with_suffix('.cn-history.lock')
    with _cn_sync_lock, exclusive(lock_path):
        return _sync_cn_body(on_login)


if __name__=='__main__':
    while True:
        try:sync_once()
        except Exception as exc:
            print(json.dumps({'api_v2_history_error':type(exc).__name__}),flush=True)
        time.sleep(300)
