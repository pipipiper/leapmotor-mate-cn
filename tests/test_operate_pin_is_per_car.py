"""The operation PIN belongs to the CAR (multi-car, #186): it is set on the car, and the cloud checks
it per VIN (`/operPwd/verify` takes `operatePassword` **and** `vin`).

@cookingeek found the hole before his second car had even arrived:

    "For performing actions on the car, I'd think you'd need to store two PINs. The T03 has a
     4-digit one, the C10 probably doesn't — or rather, I don't really know."

What holds:
  * each car answers with its own PIN, in both processes and on every command path alike (the page,
    Home Assistant, the Ready automation);
  * `LEAPMOTOR_PIN` is the PIN of every car without its own (an install configured from its
    environment);
  * the install-wide PIN an older install still has becomes the own PIN of each car that has none
    (on one car, its PIN: there it is the newer), once, and is then gone;
  * the wizard stores the PIN only for the cars it found, saying so when it found none, and no
    older PIN comes back to a car after it;
  * a car with no PIN gets none: never the one another car's command left in the session, nor that
    command's refusal.
"""
import importlib.util
import json
import pathlib
import sqlite3
import threading
from types import SimpleNamespace

import db as D                 # poller/db.py
import db_reader
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
T03, C10 = "LFZT03AAAAAAAAAA1", "LFZC10BBBBBBBBBB2"


def _locked(vin=""):
    raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def two_cars(tmp_path, monkeypatch):
    monkeypatch.delenv("LEAPMOTOR_PIN", raising=False)
    database = D.Database(str(tmp_path / "p.db"))
    database._conn.execute("INSERT INTO vehicles (id, vin, car_type) VALUES (1,?,'T03')", (T03,))
    database._conn.execute("INSERT INTO vehicles (id, vin, car_type) VALUES (2,?,'C10')", (C10,))
    database._conn.commit()
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "p.db"))
    return database


# ── the reader, on both sides ─────────────────────────────────────────────────

def test_each_car_gets_its_own_pin(two_cars):
    db_reader.set_operate_pin("1234", T03)
    db_reader.set_operate_pin("9876", C10)
    assert db_reader.get_operate_pin(T03) == "1234"
    assert db_reader.get_operate_pin(C10) == "9876"


def test_a_car_without_its_own_takes_leapmotor_pin(two_cars, monkeypatch):
    monkeypatch.setenv("LEAPMOTOR_PIN", "5555")
    db_reader.set_operate_pin("1234", T03)
    assert db_reader.get_operate_pin(T03) == "1234", "a car's own PIN beats the environment"
    assert db_reader.get_operate_pin(C10) == "5555"


def test_a_car_without_any_pin_has_none(two_cars):
    db_reader.set_operate_pin("1234", T03)
    db_reader.set_operate_pin("", T03)
    assert db_reader.get_operate_pin(T03) == ""


def test_no_vin_means_the_selected_car(two_cars):
    """The web asks without a vin — every page is already scoped to the picked car, and the PIN has
    to follow the same choice or a command goes to the car you are not looking at."""
    db_reader.set_operate_pin("1234", T03)
    db_reader.set_operate_pin("9876", C10)
    db_reader.set_active_vehicle(C10)
    assert db_reader.get_operate_pin() == "9876"
    db_reader.set_active_vehicle(T03)
    assert db_reader.get_operate_pin() == "1234"


@pytest.mark.parametrize("env", ["", "5555"])
def test_the_poller_reads_the_same_pins(two_cars, monkeypatch, env):
    """Two processes, two modules, one stored secret. If they disagreed, a command from Home
    Assistant and the same command from the page would authenticate differently."""
    monkeypatch.setenv("LEAPMOTOR_PIN", env)
    db_reader.set_operate_pin("1234", T03)
    for vin in (T03, C10):
        assert two_cars.get_operate_pin(vin) == db_reader.get_operate_pin(vin)


def test_the_pin_is_stored_encrypted(two_cars):
    db_reader.set_operate_pin("1234", T03)
    raw = db_reader.get_setting(f"leapmotor_pin_{T03.lower()}", "")
    assert raw and "1234" not in raw, "the stored value must not contain the PIN"


def test_a_lost_encryption_key_names_the_per_car_pins_too(two_cars, monkeypatch):
    """`check_decryption` tells people, in words, which secrets belong to a key they no longer have
    (#227). A per-car PIN that is not in that list would go missing in silence."""
    db_reader.set_operate_pin("1234", T03)
    monkeypatch.setattr(db_reader.crypto, "can_decrypt", lambda _v: False)
    lost = db_reader.check_decryption()
    assert f"leapmotor_pin_{T03.lower()}" in lost


# ── the install-wide PIN of an older install ──────────────────────────────────

def test_the_install_pin_becomes_each_car_s_own_and_is_gone(two_cars):
    two_cars.set_secret("leapmotor_pin", "5555")
    assert sorted(two_cars.migrate_install_pin()) == sorted([T03, C10])
    assert db_reader.get_operate_pin(T03) == db_reader.get_operate_pin(C10) == "5555"
    assert two_cars.get_setting("leapmotor_pin") == ""
    assert two_cars.migrate_install_pin() == [], "the second start finds nothing to do"


def test_a_car_s_own_pin_is_never_overwritten(two_cars):
    two_cars.set_secret("leapmotor_pin", "5555")
    db_reader.set_operate_pin("1234", T03)
    assert two_cars.migrate_install_pin() == [C10]
    assert db_reader.get_operate_pin(T03) == "1234"
    assert db_reader.get_operate_pin(C10) == "5555"


def test_on_one_car_the_pin_typed_in_settings_beats_the_wizard_s(tmp_path):
    """The wizard wrote the car's PIN and the install-wide one alike; on one car only the Settings
    form then wrote the install-wide one alone, so that one is the newer."""
    database = D.Database(str(tmp_path / "one.db"))
    database._conn.execute("INSERT INTO vehicles (id, vin, car_type) VALUES (1,?,'T03')", (T03,))
    database._conn.commit()
    database.set_secret(f"leapmotor_pin_{T03.lower()}", "1111")
    database.set_secret("leapmotor_pin", "2222")
    assert database.migrate_install_pin() == [T03]
    assert database.get_operate_pin(T03) == "2222"


def test_an_older_install_has_it_moved_when_its_database_opens(tmp_path):
    """Before the first login of the new version: the web's commands need the PIN even while the
    poller cannot reach the cloud."""
    path = str(tmp_path / "o.db")
    old = D.Database(path)
    old._conn.execute("INSERT INTO vehicles (id, vin, car_type) VALUES (1,?,'T03')", (T03,))
    old._conn.commit()
    old.set_secret("leapmotor_pin", "5555")
    old._conn.close()
    assert D.Database(path).get_operate_pin(T03) == "5555"


def test_with_no_car_yet_the_install_pin_waits(tmp_path):
    database = D.Database(str(tmp_path / "e.db"))
    database.set_secret("leapmotor_pin", "5555")
    assert database.migrate_install_pin() == []
    assert database.get_secret("leapmotor_pin") == "5555", "dropped with no car to give it to, it would be lost"


# ── every command path uses it ────────────────────────────────────────────────

class _Api:
    def __init__(self):
        self.operation_password = "INSTALL"
        self.seen = []

    def lock_vehicle(self, vin):
        self.seen.append((vin, self.operation_password))
        from types import SimpleNamespace
        self.last_new_command_receipt = SimpleNamespace(outcome='accepted')

    def windows(self, vin, value):
        self.seen.append((vin, self.operation_password))


def _web_session(monkeypatch, picked):
    import command_client as cc
    db_reader.set_active_vehicle(picked)
    session = cc.LeapmotorSession()
    session._api = _Api()
    session._vehicle = type("_V", (), {"vin": picked, "car_type": "C10" if picked == C10 else "T03"})()
    monkeypatch.setattr(session, "_connect", lambda: None)
    return session


@pytest.mark.parametrize("picked,expected", [(C10, "9876"), (T03, "1234")])
def test_the_web_command_uses_the_selected_cars_pin(two_cars, monkeypatch, picked, expected):
    """⚠️ Driven through the REAL session object, not a stand-in with an `execute` of its own."""
    db_reader.set_operate_pin("1234", T03)
    db_reader.set_operate_pin("9876", C10)
    session = _web_session(monkeypatch, picked)
    ok, _msg = session._execute_inner(lambda api, vin: api.lock_vehicle(vin))
    assert ok
    assert session._api.seen[-1] == (picked, expected), "the command carries the picked car's PIN"


def test_a_web_command_to_a_car_without_a_pin_says_which_car(two_cars, monkeypatch):
    """The session keeps the PIN the last command used; sending it with another car's command is a
    refusal from the cloud that names no car."""
    db_reader.set_operate_pin("1234", T03)
    session = _web_session(monkeypatch, C10)
    session._api.operation_password = "1234"
    ok, msg = session._execute_inner(lambda api, vin: api.lock_vehicle(vin))
    assert not ok and session._api.seen == []
    assert msg == "No operation PIN for C10 ·BBBBB2: set it in Settings → Vehicle"


def test_a_failed_pin_read_sends_no_other_car_s_pin(two_cars, monkeypatch):
    session = _web_session(monkeypatch, C10)
    session._api.operation_password = "1234"
    monkeypatch.setattr(db_reader, "get_operate_pin", _locked)
    ok, _msg = session._execute_inner(lambda api, vin: api.lock_vehicle(vin))
    assert not ok and session._api.seen == []


def test_a_car_without_a_pin_takes_no_other_car_s_refusal(two_cars, monkeypatch):
    """The page stores a refusal (code 40) against its car as "this car has not got that command"."""
    session = _web_session(monkeypatch, C10)
    session.last_refusal = (T03, 40)
    ok, _msg = session._execute_inner(lambda api, vin: api.lock_vehicle(vin))
    assert not ok and session.last_refusal is None


def _poller_main():
    spec = importlib.util.spec_from_file_location("poller_main_pin", ROOT / "poller" / "main.py")
    pmain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pmain)
    return pmain


def test_the_home_assistant_command_uses_the_pin_of_the_car_it_names(two_cars):
    """🔑 MQTT is not scoped to the picked car — the topic names the VIN, and a command can arrive
    for either. Using the *selected* car's PIN here would authenticate car A's command with car B's
    four digits, which fails only sometimes and never says why."""
    pmain = _poller_main()
    db_reader.set_operate_pin("1234", T03)
    db_reader.set_operate_pin("9876", C10)
    api = _Api()
    client = type("_C", (), {"_api": api, "_vehicle": None})()
    service = type("_S", (), {"last_climate_on": None})()
    pmain._handle_mqtt_command(client, service, two_cars, T03, "lock", None)
    assert api.seen[-1] == (T03, "1234")
    pmain._handle_mqtt_command(client, service, two_cars, C10, "lock", None)
    assert api.seen[-1] == (C10, "9876")


def test_a_home_assistant_command_to_a_car_without_a_pin_carries_none(two_cars):
    pmain = _poller_main()
    db_reader.set_operate_pin("1234", T03)
    api = _Api()
    client = type("_C", (), {"_api": api, "_vehicle": None})()
    service = type("_S", (), {"last_climate_on": None})()
    pmain._handle_mqtt_command(client, service, two_cars, T03, "lock", None)
    pmain._handle_mqtt_command(client, service, two_cars, C10, "lock", None)
    assert api.seen[-1] == (C10, ""), "not the T03's PIN left in the session"



def test_a_failed_pin_read_on_home_assistant_s_side_sends_none(two_cars, monkeypatch):
    pmain = _poller_main()
    api = _Api()
    api.operation_password = "1234"
    client = type("_C", (), {"_api": api, "_vehicle": None})()
    service = type("_S", (), {"last_climate_on": None})()
    monkeypatch.setattr(two_cars, "get_operate_pin", _locked)
    pmain._handle_mqtt_command(client, service, two_cars, C10, "lock", None)
    assert api.seen[-1] == (C10, ""), "not the PIN the last command left in the session"
