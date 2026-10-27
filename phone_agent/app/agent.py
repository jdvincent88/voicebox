"""One phone call: Claude streaming + tool loop over a ConversationRelay socket.

ConversationRelay handles speech-to-text, text-to-speech and barge-in. This
module receives the caller's words as text, streams Claude's reply back
token by token (so TTS starts speaking immediately), and runs tools.

History is append-only (every assistant turn is appended exactly as
returned) so prompt caching and thinking blocks stay valid across turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import anthropic

from .config import Business, Settings
from .tools import TOOLS, CallState, ToolDeps, ToolRunner

logger = logging.getLogger(__name__)

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Send = Callable[[dict], Awaitable[None]]

# Let TTS finish the goodbye / "connecting you" line before the relay
# session closes; ending immediately can clip the last words.
END_DELAY_SECONDS = {"hangup": 3.5, "transfer": 2.0}

INSTRUCTIONS = """\
You are the phone host for {name}. You are on a live phone call: everything \
you write is spoken aloud by text-to-speech the moment you write it.

How to talk
- Warm, Southern-hospitable, efficient. One or two short sentences per turn, \
then let the caller talk. Ask one question at a time.
- Plain spoken words only: no lists, markdown, emoji, symbols or abbreviations. \
Say prices like "fourteen ninety-five" and times like "six thirty".
- Never read out menu aliases (V12, M4), ids or internal notes.
- Only state facts from Business info, the Menu, or tool results. If you don't \
know, say so and offer to take a message or transfer. Never guess hours, \
prices, availability or allergen safety.
- If you need a moment for a tool, say a few words first ("Let me check that").

Takeout orders
1. Take items one at a time. Ask about every required choice on an item \
(the menu says "choose 1"). Mention optional add-ons only if natural.
2. Call quote_takeout_order, then read the order and subtotal back and ask \
"Did I get that right?".
3. Get the name for the order and pickup time (ASAP or a time today). Confirm \
the payment link goes to the number they're calling from, or take another mobile number.
4. Call place_takeout_order. Tell them the total with tax, that the link is on its \
way by text, and that the kitchen starts cooking once it's paid.
- Callers can't pay by card over the phone. If they can't use a text link, offer \
to transfer them to the manager.

Reservations
- Get party size, date and time, and the name. Resolve "tomorrow", "Friday" \
etc. from the call context date. Then call request_reservation. If it refuses, \
explain the reason simply and offer another time.
- Phone bookings are requests: tell callers the team will confirm by text or call.
- Larger parties, private events, the Magnolia Room and catering: collect \
details and call create_event_lead.

Transfers and messages
- Transfer when the caller asks for a person, is upset, has a complaint or \
refund, or needs something you can't do. Transfer keys:
{transfers}
- If the caller presses 0, transfer to manager.
- Otherwise offer take_message.
- When the caller is done, say a short goodbye, then call end_call.

# Business info
{info}

# Menu (live from the POS; only these items can be ordered)
{menu}
"""


def build_system_prompt(business: Business, menu_text: str) -> str:
    transfers = "\n".join(
        f"  - {key}: {t.get('when', '')}" for key, t in business.transfers.items()
    ) or "  (none configured; take a message instead)"
    return INSTRUCTIONS.format(
        name=business.name,
        transfers=transfers,
        info=business.info.strip(),
        menu=menu_text,
    )


class CallSession:
    def __init__(
        self,
        settings: Settings,
        business: Business,
        deps: ToolDeps,
        client: anthropic.AsyncAnthropic,
        system_prompt: str,
        state: CallState,
        send: Send,
    ):
        self.settings = settings
        self.business = business
        self.client = client
        self.system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        self.state = state
        self.tools = ToolRunner(deps, state)
        self.send = send
        self.messages: list[dict] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.muted = False
        self.heard_before_interrupt: str | None = None
        self._worker: asyncio.Task | None = None
        self.closed = False

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.closed = True
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass

    # -- inbound events from ConversationRelay ----------------------------

    def on_prompt(self, text: str) -> None:
        self.queue.put_nowait(text)

    def on_interrupt(self, heard: str) -> None:
        # Stop speaking the rest of this turn. The turn itself runs to
        # completion so tools that already started finish and history stays
        # consistent; the model is told what the caller actually heard.
        self.muted = True
        self.heard_before_interrupt = heard

    def on_dtmf(self, digit: str) -> None:
        self.queue.put_nowait(f"(The caller pressed {digit} on their keypad.)")

    # -- turn processing ---------------------------------------------------

    async def _run(self) -> None:
        while not self.closed:
            text = await self.queue.get()
            # Coalesce anything else the caller said while we were busy.
            while not self.queue.empty():
                text += " " + self.queue.get_nowait()
            try:
                await self._turn(text)
            except Exception:
                logger.exception("Turn failed on call %s", self.state.call_sid)
                await self._say("Sorry, I'm having trouble on my end. Let me connect you with someone.")
                self.state.pending_end = {"action": "transfer", "to": "manager", "summary": "agent error"}
            if self.state.pending_end:
                await self._finish_call()
                return

    def _user_content(self, text: str) -> str:
        parts = []
        if not self.messages:
            now = datetime.now(ZoneInfo(self.business.timezone))
            parts.append(
                f"[Call context: it is {now.strftime('%A, %B %-d, %Y, %-I:%M %p')} local time. "
                f"Caller ID: {self.state.caller or 'unknown'}.]"
            )
        if self.heard_before_interrupt is not None:
            parts.append(
                f"[You were interrupted. The caller only heard: \"{self.heard_before_interrupt}\"]"
            )
            self.heard_before_interrupt = None
        parts.append(f"Caller: {text}")
        return "\n".join(parts)

    async def _turn(self, text: str) -> None:
        self.muted = False
        self.messages.append({"role": "user", "content": self._user_content(text)})
        json_retries = 0
        for _ in range(8):  # tool rounds per caller turn
            try:
                response = await self._stream_once()
            except ValueError:
                # Tool input JSON the SDK could not parse; re-issue the turn.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                await self._say("I'm not able to help with that one, but I can take a message for the team.")
                break
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break

            results = []
            for block in tool_uses:
                args = block.input if isinstance(block.input, dict) else {}
                output, is_error = await self.tools.run(block.name, args)
                logger.info("call %s tool %s -> %s", self.state.call_sid, block.name, output[:200])
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
                )
            self.messages.append({"role": "user", "content": results})
        await self.send({"type": "text", "token": "", "last": True})

    async def _stream_once(self):
        kwargs: dict = dict(
            model=self.settings.anthropic_model,
            max_tokens=4096,
            system=self.system,
            tools=TOOLS,
            messages=self.messages,
            output_config={"effort": self.settings.anthropic_effort},
        )
        if self.settings.anthropic_model in FALLBACK_MODELS:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        async with self.client.beta.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "text" and event.text and not self.muted:
                    await self.send({"type": "text", "token": event.text, "last": False})
            return await stream.get_final_message()

    async def _say(self, text: str) -> None:
        await self.send({"type": "text", "token": text, "last": True})

    async def _finish_call(self) -> None:
        end = self.state.pending_end or {"action": "hangup"}
        await asyncio.sleep(END_DELAY_SECONDS.get(end["action"], 0))
        await self.send({"type": "end", "handoffData": json.dumps(end)})
        self.closed = True
