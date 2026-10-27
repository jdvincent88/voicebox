import asyncio
import json
from types import SimpleNamespace

import pytest

from app import agent as agent_mod
from app.agent import CallSession, build_system_prompt
from app.config import get_business, get_settings
from app.tools import CallState, ToolDeps


class FakeStream:
    def __init__(self, texts, final):
        self.texts = texts
        self.final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for t in self.texts:
                yield SimpleNamespace(type="text", text=t)

        return gen()

    async def get_final_message(self):
        return self.final


class FakeClient:
    """Plays back scripted responses and records each request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        texts, final = self.script.pop(0)
        return FakeStream(texts, final)


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(name, args, id_="tu1"):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


async def run_call(script, prompts):
    sent = []

    async def send(m):
        sent.append(m)

    client = FakeClient(script)
    business = get_business()
    deps = ToolDeps(business=business, square=None, parlor=None, sms=None)
    state = CallState(call_sid="CA1", caller="+15551234567")
    session = CallSession(get_settings(), business, deps, client, build_system_prompt(business, "(menu)"), state, send)
    session.start()
    for p in prompts:
        session.on_prompt(p)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if session.closed or (not client.script and session.queue.empty()):
            break
    await asyncio.sleep(0.05)
    await session.stop()
    return sent, client, state


@pytest.fixture(autouse=True)
def fast_hangup(monkeypatch):
    monkeypatch.setattr(agent_mod, "END_DELAY_SECONDS", {})


async def test_streams_tokens_then_marks_last():
    script = [(["Hi there! ", "How can I help?"], SimpleNamespace(stop_reason="end_turn", content=[text_block("Hi")]))]
    sent, client, _ = await run_call(script, ["hello"])
    assert [m["token"] for m in sent] == ["Hi there! ", "How can I help?", ""]
    assert sent[-1]["last"] is True
    first_user = client.requests[0]["messages"][0]["content"]
    assert "Caller ID: +15551234567" in first_user and first_user.endswith("Caller: hello")
    assert client.requests[0]["fallbacks"] == "default"
    assert client.requests[0]["output_config"] == {"effort": get_settings().anthropic_effort}


async def test_tool_round_then_hangup_sends_end():
    script = [
        (["Goodbye now!"], SimpleNamespace(stop_reason="tool_use", content=[text_block("Goodbye now!"), tool_block("end_call", {})])),
        ([], SimpleNamespace(stop_reason="end_turn", content=[])),
    ]
    sent, client, state = await run_call(script, ["that's all, bye"])
    assert sent[-1]["type"] == "end"
    assert json.loads(sent[-1]["handoffData"]) == {"action": "hangup"}
    # tool_result went back to the model in the second request
    second = client.requests[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "tu1"


async def test_interrupt_mutes_rest_of_turn_and_tells_model():
    sent = []

    async def send(m):
        sent.append(m)

    script = [
        (["one ", "two"], SimpleNamespace(stop_reason="end_turn", content=[text_block("one two")])),
        (["ok"], SimpleNamespace(stop_reason="end_turn", content=[text_block("ok")])),
    ]
    client = FakeClient(script)
    business = get_business()
    session = CallSession(
        get_settings(), business, ToolDeps(business, None, None, None), client, "sys",
        CallState("CA1", "+15551234567"), send,
    )
    session.on_interrupt("one")
    await session._turn("wait")  # muted at start is reset per turn
    session.on_interrupt("one")
    await session._turn("actually, two people")
    last_user = client.requests[1]["messages"][-1]["content"]
    assert 'The caller only heard: "one"' in last_user


async def test_model_without_fallback_support_omits_fallbacks(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(agent_mod, "FALLBACK_MODELS", set())
    script = [(["hi"], SimpleNamespace(stop_reason="end_turn", content=[text_block("hi")]))]
    _, client, _ = await run_call(script, ["hello"])
    assert "fallbacks" not in client.requests[0] and "betas" not in client.requests[0]
    assert client.requests[0]["model"] == settings.anthropic_model
