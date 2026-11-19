"""Text that comes from outside Mate does not become markup by another door.

The Costs page put its bands and modes into a <script> as json.dumps, which leaves "</script>" as
it is. Found in the 4.24.0 security review.
"""
import json

import pytest

pytest.importorskip("fastapi", reason="web.main needs the production web dependencies")
pytest.importorskip("httpx", reason="Starlette TestClient needs httpx")

import main  # noqa: E402


def test_json_in_a_script_cannot_close_the_script():
    value = [{"name": "</script><script>alert(1)</script>", "price": 0.2}]
    out = main._script_json(value)
    assert "</script>" not in out and "<" not in out
    assert json.loads(out) == value
