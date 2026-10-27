"""Twilio helpers: SMS, webhook signatures, and TwiML.

Written against Twilio's REST API and TwiML spec directly so the service
does not depend on the Twilio SDK.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from xml.sax.saxutils import quoteattr, escape

import httpx

logger = logging.getLogger(__name__)


class SmsClient:
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.from_number = from_number
        self._url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        self._http = httpx.AsyncClient(timeout=15.0, auth=(account_sid, auth_token))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, to: str, body: str) -> bool:
        if not to or not self.from_number:
            return False
        try:
            r = await self._http.post(self._url, data={"To": to, "From": self.from_number, "Body": body})
            if r.status_code >= 400:
                logger.error("SMS to %s failed: %s %s", to, r.status_code, r.text[:300])
                return False
            return True
        except httpx.HTTPError:
            logger.exception("SMS to %s failed", to)
            return False

    async def send_many(self, numbers: list[str], body: str) -> None:
        for n in numbers:
            await self.send(n, body)


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: HMAC-SHA1 over url + sorted key/values."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature or "")


def call_token(secret: str, call_sid: str) -> str:
    """Per-call token so only Twilio's relay for this call can open the socket."""
    return hmac.new(secret.encode(), call_sid.encode(), hashlib.sha256).hexdigest()


def _attrs(**kw) -> str:
    return "".join(f" {k}={quoteattr(str(v))}" for k, v in kw.items() if v not in (None, ""))


def relay_twiml(
    ws_url: str,
    action_url: str,
    token: str,
    greeting_audio_url: str | None,
    greeting_text: str,
    tts_provider: str,
    tts_voice: str,
    hints: str,
) -> str:
    play = f"<Play>{escape(greeting_audio_url)}</Play>" if greeting_audio_url else ""
    relay = _attrs(
        url=ws_url,
        welcomeGreeting=None if greeting_audio_url else greeting_text,
        ttsProvider=tts_provider,
        voice=tts_voice,
        transcriptionProvider="Deepgram",
        language="en-US",
        interruptible="any",
        dtmfDetection="true",
        hints=hints,
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"{play}<Connect{_attrs(action=action_url)}>"
        f'<ConversationRelay{relay}><Parameter name="token" value={quoteattr(token)}/>'
        "</ConversationRelay></Connect></Response>"
    )


def dial_twiml(number: str, caller_id: str, action_url: str, hold_audio_url: str | None) -> str:
    play = f"<Play>{escape(hold_audio_url)}</Play>" if hold_audio_url else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"{play}<Dial{_attrs(timeout=25, callerId=caller_id, action=action_url)}>"
        f"<Number>{escape(number)}</Number></Dial></Response>"
    )


def say_twiml(text: str, hangup: bool = True) -> str:
    end = "<Hangup/>" if hangup else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(text)}</Say>{end}</Response>'


def hangup_twiml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
