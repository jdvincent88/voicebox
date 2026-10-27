import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main, twilio


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def test_incoming_returns_conversation_relay(client):
    r = client.post("/voice/incoming", data={"CallSid": "CA123", "From": "+15551234567", "To": "+15559990000"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    body = r.text
    assert '<ConversationRelay url="wss://agent.example.com/voice/relay"' in body
    assert 'action="https://agent.example.com/voice/after-relay"' in body
    assert 'welcomeGreeting="Thanks for calling Test Cafe!"' in body
    token = twilio.call_token("test-token", "CA123")
    assert f'<Parameter name="token" value="{token}"/>' in body


def test_after_relay_transfer_dials_destination(client):
    handoff = json.dumps({"action": "transfer", "to": "manager", "summary": "refund"})
    r = client.post("/voice/after-relay", data={"HandoffData": handoff, "From": "+15551234567", "To": "+15559990000"})
    assert "<Number>+15550001111</Number>" in r.text
    assert 'callerId="+15559990000"' in r.text
    assert "/voice/after-dial?dest=manager" in r.text


def test_after_relay_without_handoff_hangs_up(client):
    r = client.post("/voice/after-relay", data={"From": "+15551234567"})
    assert "<Hangup/>" in r.text


def test_missed_transfer_apologises(client):
    r = client.post("/voice/after-dial?dest=manager", data={"DialCallStatus": "no-answer", "From": "+15551234567"})
    assert "call you back" in r.text


def test_relay_rejects_bad_token(client):
    with client.websocket_connect("/voice/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA1", "from": "+1555", "customParameters": {"token": "nope"}}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 4403


def test_signature_round_trip():
    params = {"CallSid": "CA1", "From": "+15551234567"}
    sig = twilio.twilio_signature("tok", "https://a.example.com/voice/incoming", params)
    assert twilio.valid_signature("tok", "https://a.example.com/voice/incoming", params, sig)
    assert not twilio.valid_signature("tok", "https://a.example.com/voice/incoming", {**params, "From": "x"}, sig)


def test_signature_matches_twilio_docs_example():
    # Example from Twilio's "Validating requests" docs.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    assert twilio.twilio_signature("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
