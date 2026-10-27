"""FastAPI app: Twilio voice webhooks + the ConversationRelay WebSocket.

Call flow
  1. Twilio POSTs /voice/incoming  -> TwiML: play greeting, <Connect><ConversationRelay>
  2. Twilio opens  /voice/relay     -> CallSession streams Claude replies back as text
  3. Agent ends the session with handoff data (transfer or hang up)
  4. Twilio POSTs /voice/after-relay -> TwiML: <Dial> the right person, or hang up
  5. Twilio POSTs /voice/after-dial  -> if nobody answered, apologise and text staff
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from urllib.parse import urlencode

import anthropic
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from . import twilio
from .agent import CallSession, build_system_prompt
from .config import get_business, get_settings
from .parlor import ParlorClient
from .square import SquareClient
from .tools import CallState, ToolDeps, normalize_phone
from .voicebox_audio import render_clips

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("phone_agent")

MENU_REFRESH_SECONDS = 15 * 60


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    business = get_business()
    if not settings.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL is required (the https URL Twilio reaches this server on).")

    square = (
        SquareClient(settings.square_access_token, settings.square_location_id, settings.square_base_url)
        if settings.square_access_token and settings.square_location_id
        else None
    )
    parlor = ParlorClient(settings.parlor_base_url, settings.parlor_api_key)
    sms = (
        twilio.SmsClient(settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_sms_from)
        if settings.twilio_account_sid and settings.twilio_sms_from
        else None
    )
    app.state.deps = ToolDeps(business=business, square=square, parlor=parlor, sms=sms)
    app.state.anthropic = anthropic.AsyncAnthropic()
    app.state.clips = {}
    app.state.system_prompt = build_system_prompt(business, "(The menu is still loading.)")

    async def refresh_menu_forever():
        while True:
            if square:
                try:
                    menu = await square.refresh_menu(business.takeout.get("hidden_categories"))
                    app.state.system_prompt = build_system_prompt(business, menu.as_prompt())
                except Exception:
                    logger.exception("Menu refresh failed; keeping the previous menu")
            else:
                app.state.system_prompt = build_system_prompt(
                    business, "(Takeout ordering is not connected. Do not take orders.)"
                )
            await asyncio.sleep(MENU_REFRESH_SECONDS)

    refresher = asyncio.create_task(refresh_menu_forever())
    if settings.voicebox_url and settings.voicebox_profile:
        app.state.clips = await render_clips(
            settings.voicebox_url, settings.voicebox_profile, business.greeting, settings.data_dir / "audio"
        )
    yield
    refresher.cancel()
    for client in (square, parlor, sms):
        if client:
            await client.aclose()


app = FastAPI(title="Phone agent", lifespan=lifespan)


# -- helpers ---------------------------------------------------------------


async def twilio_form(request: Request) -> dict[str, str]:
    """Read a Twilio webhook form and reject it unless Twilio signed it."""
    settings = get_settings()
    params = {k: str(v) for k, v in (await request.form()).items()}
    if settings.validate_twilio_signature:
        url = settings.public_base_url + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        if not twilio.valid_signature(
            settings.twilio_auth_token, url, params, request.headers.get("X-Twilio-Signature", "")
        ):
            raise HTTPException(status_code=403, detail="Bad Twilio signature")
    return params


def twiml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def clip_url(name: str) -> str | None:
    if name in app.state.clips:
        return f"{get_settings().public_base_url}/audio/{name}.wav"
    return None


def transcription_hints() -> str:
    deps: ToolDeps = app.state.deps
    names = [deps.business.name]
    if deps.square:
        names += sorted({v.item_name for v in deps.square.menu.variations.values()})
    return ",".join(n.replace(",", " ") for n in names)[:1000]


# -- routes ----------------------------------------------------------------


@app.get("/health")
async def health():
    deps: ToolDeps = app.state.deps
    return {
        "ok": True,
        "menu_items": len(deps.square.menu.variations) if deps.square else 0,
        "sms": deps.sms is not None,
        "voicebox_clips": sorted(app.state.clips),
    }


@app.get("/audio/{name}.wav")
async def audio(name: str):
    path = app.state.clips.get(name)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/wav")


@app.post("/voice/incoming")
async def incoming(request: Request):
    params = await twilio_form(request)
    settings = get_settings()
    business = get_business()
    call_sid = params.get("CallSid", "")
    return twiml(
        twilio.relay_twiml(
            ws_url=f"{settings.wss_base_url}/voice/relay",
            action_url=f"{settings.public_base_url}/voice/after-relay",
            token=twilio.call_token(settings.twilio_auth_token, call_sid),
            greeting_audio_url=clip_url("greeting"),
            greeting_text=business.greeting,
            tts_provider=settings.tts_provider,
            tts_voice=settings.tts_voice,
            hints=transcription_hints(),
        )
    )


@app.websocket("/voice/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    settings = get_settings()
    session: CallSession | None = None
    send_lock = asyncio.Lock()

    async def send(message: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(message))

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "setup":
                call_sid = msg.get("callSid", "")
                token = (msg.get("customParameters") or {}).get("token", "")
                if token != twilio.call_token(settings.twilio_auth_token, call_sid):
                    logger.warning("Rejected relay socket with bad token for %s", call_sid)
                    await ws.close(code=4403)
                    return
                state = CallState(call_sid=call_sid, caller=normalize_phone(msg.get("from")))
                session = CallSession(
                    settings,
                    get_business(),
                    app.state.deps,
                    app.state.anthropic,
                    app.state.system_prompt,
                    state,
                    send,
                )
                session.start()
                logger.info("Call %s from %s connected", call_sid, state.caller)
            elif session is None:
                await ws.close(code=4400)
                return
            elif kind == "prompt":
                session.on_prompt(msg.get("voicePrompt", ""))
            elif kind == "interrupt":
                session.on_interrupt(msg.get("utteranceUntilInterrupt", ""))
            elif kind == "dtmf":
                session.on_dtmf(msg.get("digit", ""))
            elif kind == "error":
                logger.error("ConversationRelay error: %s", msg.get("description"))
    except WebSocketDisconnect:
        pass
    finally:
        if session:
            await session.stop()


@app.post("/voice/after-relay")
async def after_relay(request: Request):
    params = await twilio_form(request)
    settings = get_settings()
    business = get_business()
    deps: ToolDeps = app.state.deps
    try:
        handoff = json.loads(params.get("HandoffData") or "{}")
    except json.JSONDecodeError:
        handoff = {}

    if handoff.get("action") != "transfer":
        return twiml(twilio.hangup_twiml())

    caller = params.get("From", "")
    dest_key = handoff.get("to", "manager")
    number = normalize_phone((business.transfers.get(dest_key) or {}).get("number"))
    if not number:
        if deps.sms:
            await deps.sms.send_many(
                business.staff_sms, f"CALLBACK NEEDED: {caller} asked for {dest_key}. {handoff.get('summary', '')}"
            )
        return twiml(twilio.say_twiml("Sorry, no one is available right now. We'll call you back shortly."))

    if deps.sms:
        await deps.sms.send(number, f"Incoming transfer from {caller}: {handoff.get('summary', '')}"[:300])
    after_dial = f"{settings.public_base_url}/voice/after-dial?" + urlencode(
        {"dest": dest_key, "summary": handoff.get("summary", "")[:200]}
    )
    return twiml(twilio.dial_twiml(number, params.get("To", ""), after_dial, clip_url("hold")))


@app.post("/voice/after-dial")
async def after_dial(request: Request):
    params = await twilio_form(request)
    if params.get("DialCallStatus") == "completed":
        return twiml(twilio.hangup_twiml())
    deps: ToolDeps = app.state.deps
    if deps.sms:
        await deps.sms.send_many(
            deps.business.staff_sms,
            f"MISSED TRANSFER to {request.query_params.get('dest')}: call back {params.get('From', '')}. "
            f"{request.query_params.get('summary', '')}",
        )
    return twiml(
        twilio.say_twiml(
            "Sorry, no one could get to the phone. I've let the team know, and someone will call you back soon. Goodbye!"
        )
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    if request.url.path.startswith("/voice/"):
        return twiml(twilio.say_twiml("Sorry, we're having trouble with our phones. Please call back in a few minutes."))
    return JSONResponse({"error": "internal"}, status_code=500)
