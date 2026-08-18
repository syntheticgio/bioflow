"""Test the sidecar API (requires running instance)."""
import requests


def test_health():
    resp = requests.get("http://localhost:21235/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_predict_invalid_sequence():
    resp = requests.post(
        "http://localhost:21235/predict",
        json={"sequence": "INVALID_B"},
    )
    assert resp.status_code == 400


def test_predict_short_sequence():
    resp = requests.post(
        "http://localhost:21235/predict",
        json={"sequence": "ACD"},
    )
    assert resp.status_code == 400
