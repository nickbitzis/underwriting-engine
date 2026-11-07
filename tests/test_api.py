import json
import sqlite3
from types import SimpleNamespace

import pytest
from conftest import ROOT
from fastapi.testclient import TestClient

from uwengine.app import create_app
from uwengine.llm import AnthropicProvider, MockProvider

SAMPLES = ROOT / "data" / "samples"


class FakeAnthropic:
    """Records calls and returns text that tries to overrule the engine."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Decision: DECLINE. I recommend declining this loan.")],
            usage=SimpleNamespace(input_tokens=321, output_tokens=45),
        )


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "d.db"), provider=MockProvider())
    with TestClient(app) as c:
        yield c


def sample_ids():
    return sorted(p.stem for p in SAMPLES.glob("*.json"))


def first_with(outcome_label):
    for p in sorted(SAMPLES.glob("*.json")):
        d = json.loads(p.read_text())
        if outcome_label in d["label"]:
            return d
    raise AssertionError(outcome_label)


def test_healthz_and_index(client):
    h = client.get("/healthz").json()
    assert h["status"] == "ok" and "2026.11.0" in h["rules_versions"]
    r = client.get("/")
    assert r.status_code == 200 and "Underwriting Engine" in r.text


def test_samples_are_listed_without_monitoring(client):
    listed = client.get("/api/samples").json()
    assert len(listed) == len(sample_ids()) >= 7
    detail = client.get(f"/api/samples/{listed[0]['id']}").json()
    assert "monitoring" not in detail


def test_decide_then_fetch_audit_record(client):
    sid = first_with("large deposit")["id"]
    d = client.post("/decide", json={"sample_id": sid}).json()
    assert d["outcome"] == "approve_with_conditions"
    assert any(c["code"] == "LOE_LARGE_DEPOSIT" for c in d["conditions"])
    rec = client.get(f"/decisions/{d['decision_id']}").json()
    assert rec["outcome"] == d["outcome"]
    assert rec["rules_version"] == "2026.11.0"
    assert rec["input"]["id"] == sid
    assert "monitoring" not in rec["input"]
    assert rec["income_worksheet"]["qualifying_monthly_income"] > 0


def test_posted_loan_file_monitoring_is_stored_apart(client):
    lf = first_with("DSCR investor, approve")
    d = client.post("/decide", json={"loan_file": lf}).json()
    store = client.app.state.store
    rec = store.get(d["decision_id"])
    assert "monitoring" not in rec["input"]
    row = store.conn.execute("SELECT protected_group FROM monitoring WHERE application_id = ?", (lf["id"],)).fetchone()
    assert row[0] == lf["monitoring"]["protected_group"]


def test_rules_version_override_is_recorded(client):
    sid = sample_ids()[0]
    d = client.post("/decide", json={"sample_id": sid, "rules_version": "2026.07.0"}).json()
    assert d["rules_version"] == "2026.07.0"
    assert client.post("/decide", json={"sample_id": sid, "rules_version": "1999.1"}).status_code == 404


def test_bad_requests(client):
    assert client.post("/decide", json={}).status_code == 422
    assert client.post("/decide", json={"sample_id": "nope"}).status_code == 404
    assert client.post("/decide", json={"loan_file": {"id": "x"}}).status_code == 422
    assert client.get("/decisions/D-missing").status_code == 404


def test_what_if_variations_and_range(client):
    sid = first_with("clean approve")["id"]
    r = client.post(
        "/what-if", json={"sample_id": sid, "variations": [{"lock_days": 30}, {"lock_days": 60}, {"points": 2}]}
    ).json()
    rates = [s["note_rate"] for s in r["scenarios"]]
    assert rates[1] - rates[0] == pytest.approx(0.25)
    assert rates[0] - rates[2] == pytest.approx(0.5)
    r = client.post(
        "/what-if", json={"sample_id": sid, "down_payment_range": {"from": 0.05, "to": 0.5, "step": 0.05}}
    ).json()
    assert len(r["scenarios"]) == 10
    ltvs = [s["ltv"] for s in r["scenarios"]]
    assert ltvs == sorted(ltvs, reverse=True)


def test_what_if_limits(client):
    sid = sample_ids()[0]
    assert client.post("/what-if", json={"sample_id": sid}).status_code == 422
    bad = client.post("/what-if", json={"sample_id": sid, "variations": [{"fico": 800}]})
    assert bad.status_code == 422


def test_metrics_and_request_id(client):
    r = client.post("/decide", json={"sample_id": sample_ids()[0]})
    assert r.headers["x-request-id"]
    m = client.get("/metrics").text
    assert "uw_decisions_total{" in m
    assert 'uw_http_requests_total{route="/decide",status="200"} 1' in m
    assert "uw_request_duration_seconds_count" in m


def test_store_is_append_only(client):
    d = client.post("/decide", json={"sample_id": sample_ids()[0]}).json()
    conn = client.app.state.store.conn
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decisions SET outcome = 'approve' WHERE decision_id = ?", (d["decision_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM decisions")


def test_assist_with_mock_provider(client):
    d = client.post("/decide", json={"sample_id": first_with("large deposit")["id"]}).json()
    a = client.post(f"/decisions/{d['decision_id']}/assist").json()
    assert a["advisory"] is True and a["provider"] == "mock"
    assert d["application_id"] in a["summary"]
    assert len(a["condition_letters"]) == len(d["conditions"])


def test_model_cannot_change_the_decision(tmp_path):
    fake = FakeAnthropic()
    app = create_app(db_path=str(tmp_path / "d.db"), provider=AnthropicProvider(client=fake))
    with TestClient(app) as c:
        d = c.post("/decide", json={"sample_id": first_with("clean approve")["id"]}).json()
        a = c.post(f"/decisions/{d['decision_id']}/assist").json()
        assert "DECLINE" in a["summary"]
        assert a["decision_outcome"] == "approve"
        assert c.get(f"/decisions/{d['decision_id']}").json()["outcome"] == "approve"
        assert a["usage"]["input_tokens"] == 321
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert "final" in call["system"]
    assert "protected_group" not in call["messages"][0]["content"]


def test_fair_lending_endpoint(client):
    r = client.get("/fair-lending").json()
    assert r["applications"] == 300
    assert "Not a legal conclusion" in r["note"]
    assert all("air" in g for g in r["groups"].values())
