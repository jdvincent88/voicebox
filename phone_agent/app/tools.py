"""Tools the phone agent can call, and their handlers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Business
from .parlor import ParlorClient, ParlorRefused
from .square import OrderError, SquareClient, price_order
from .twilio import SmsClient

logger = logging.getLogger(__name__)

_ITEMS_SCHEMA = {
    "type": "array",
    "description": "Every line in the order.",
    "items": {
        "type": "object",
        "properties": {
            "variation": {"type": "string", "description": "Menu alias such as V12."},
            "quantity": {"type": "integer", "minimum": 1},
            "modifiers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Modifier aliases such as M4, for required and optional choices.",
            },
            "note": {"type": "string", "description": "Special request for the kitchen, e.g. 'no onions'."},
        },
        "required": ["variation", "quantity"],
    },
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


TOOLS = [
    _tool(
        "quote_takeout_order",
        "Check a takeout cart against the live menu and get the pre-tax subtotal. Call this before "
        "reading the order back to the caller. Fix any error it returns with the caller.",
        {"items": _ITEMS_SCHEMA},
        ["items"],
    ),
    _tool(
        "place_takeout_order",
        "Create the takeout order in Square and text the caller a payment link. Only call after the "
        "caller has confirmed the read-back, their name, the pickup time and the number to text.",
        {
            "items": _ITEMS_SCHEMA,
            "customer_name": {"type": "string"},
            "pickup_time": {
                "type": "string",
                "description": "'ASAP', or a time today in 24-hour HH:MM local time.",
            },
            "text_to": {
                "type": "string",
                "description": "Mobile number for the payment link. Omit to use the caller ID.",
            },
        },
        ["items", "customer_name", "pickup_time"],
    ),
    _tool(
        "request_reservation",
        "Request a table. The reservation system checks hours, closures and capacity and returns "
        "either the booking or the reason it can't be taken. Bookings are requests the team confirms.",
        {
            "guest_name": {"type": "string"},
            "party_size": {"type": "integer", "minimum": 1},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "24-hour HH:MM local time"},
            "notes": {"type": "string", "description": "Occasion, high chair, accessibility, allergies."},
            "phone": {"type": "string", "description": "Omit to use the caller ID."},
        },
        ["guest_name", "party_size", "date", "time"],
    ),
    _tool(
        "find_my_reservations",
        "Look up reservations under the caller's phone number (or another number they give).",
        {"phone": {"type": "string", "description": "Omit to use the caller ID."}},
        [],
    ),
    _tool(
        "create_event_lead",
        "Record a private event, Magnolia Room, catering or large-party enquiry for the events team.",
        {
            "name": {"type": "string"},
            "event_date": {"type": "string", "description": "YYYY-MM-DD if known"},
            "guest_count": {"type": "string"},
            "occasion": {"type": "string"},
            "notes": {"type": "string"},
            "phone": {"type": "string", "description": "Omit to use the caller ID."},
            "email": {"type": "string"},
        },
        ["name"],
    ),
    _tool(
        "take_message",
        "Leave a message for staff to call the caller back.",
        {
            "caller_name": {"type": "string"},
            "message": {"type": "string"},
            "callback_number": {"type": "string", "description": "Omit to use the caller ID."},
            "urgent": {"type": "boolean"},
        },
        ["caller_name", "message"],
    ),
    _tool(
        "transfer_call",
        "Transfer the caller to a person. Tell the caller you are connecting them first.",
        {
            "destination": {"type": "string", "description": "One of the transfer keys in your instructions."},
            "summary": {"type": "string", "description": "One line for staff on why they're calling."},
        },
        ["destination", "summary"],
    ),
    _tool(
        "end_call",
        "Hang up after you have said goodbye.",
        {},
        [],
    ),
]


def normalize_phone(raw: str | None) -> str | None:
    """Best-effort E.164 for US numbers. Returns None if it can't tell."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def money(cents: int | None) -> str:
    return "unknown" if cents is None else f"${cents / 100:.2f}"


@dataclass
class CallState:
    call_sid: str
    caller: str | None
    pending_end: dict | None = None
    log: list[str] = field(default_factory=list)


@dataclass
class ToolDeps:
    business: Business
    square: SquareClient | None
    parlor: ParlorClient | None
    sms: SmsClient | None


class ToolRunner:
    def __init__(self, deps: ToolDeps, state: CallState):
        self.deps = deps
        self.state = state
        self.tz = ZoneInfo(deps.business.timezone)

    async def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (result text, is_error)."""
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return f"Unknown tool {name}.", True
        try:
            return await handler(**args), False
        except (OrderError, ParlorRefused) as e:
            return str(e), True
        except TypeError as e:
            return f"Bad arguments for {name}: {e}", True
        except Exception:
            logger.exception("Tool %s failed", name)
            return (
                f"{name} failed because of a system problem. Apologize, and offer to take a message "
                "or transfer to the manager.",
                True,
            )

    def _phone(self, given: str | None) -> str:
        phone = normalize_phone(given) or self.state.caller
        if not phone:
            raise OrderError("I need a phone number. Ask the caller for one.")
        return phone

    async def _notify_staff(self, text: str) -> None:
        self.state.log.append(text)
        if self.deps.sms and self.deps.business.staff_sms:
            await self.deps.sms.send_many(self.deps.business.staff_sms, text)

    # -- takeout ---------------------------------------------------------

    def _require_takeout(self) -> SquareClient:
        if not self.deps.business.takeout.get("enabled", True) or self.deps.square is None:
            raise OrderError("Phone takeout ordering is not available. Offer to transfer or take a message.")
        return self.deps.square

    async def _quote_takeout_order(self, items: list[dict]) -> str:
        square = self._require_takeout()
        max_items = int(self.deps.business.takeout.get("max_items", 30))
        _, subtotal, summary = price_order(square.menu, items, max_items)
        return (
            "Valid order:\n- " + "\n- ".join(summary) + f"\nSubtotal before tax: {money(subtotal)}. "
            "Read this back and confirm before placing it."
        )

    async def _place_takeout_order(
        self, items: list[dict], customer_name: str, pickup_time: str, text_to: str | None = None
    ) -> str:
        square = self._require_takeout()
        takeout = self.deps.business.takeout
        max_items = int(takeout.get("max_items", 30))
        prep = int(takeout.get("prep_minutes", 25))
        lines, _, summary = price_order(square.menu, items, max_items)
        phone = self._phone(text_to)
        if not self.deps.sms:
            raise OrderError("Texting is not set up, so I can't send a payment link.")

        pickup_at = self._parse_pickup(pickup_time, prep)
        result = await square.create_pickup_link(
            lines, customer_name.strip(), phone, pickup_at, prep, note="Phone order"
        )
        when = pickup_at.strftime("%-I:%M %p") if pickup_at else f"about {prep} minutes after you pay"
        sent = await self.deps.sms.send(
            phone,
            f"{self.deps.business.name}: here's your takeout order for {customer_name}. "
            f"Total {money(result['total_cents'])}. Pay here to send it to the kitchen: {result['url']} "
            f"Pickup: {when}.",
        )
        await self._notify_staff(
            f"PHONE ORDER (awaiting payment) {customer_name} {phone}, pickup {when}: "
            + "; ".join(summary)
            + f". Total {money(result['total_cents'])}. Square order {result['order_id']}"
        )
        if not sent:
            return (
                f"The order was created but the text to {phone} failed. Total {money(result['total_cents'])}. "
                "Apologize and transfer to the manager to take payment."
            )
        return (
            f"Order created. Total with tax {money(result['total_cents'])}. Payment link texted to {phone}. "
            f"Pickup {when}. Tell the caller the kitchen starts it as soon as they pay through the link."
        )

    def _parse_pickup(self, pickup_time: str, prep: int) -> datetime | None:
        if not pickup_time or pickup_time.strip().upper() == "ASAP":
            return None
        try:
            hh, mm = (int(x) for x in pickup_time.strip().split(":"))
        except ValueError:
            raise OrderError("Pickup time must be ASAP or HH:MM in 24-hour time.") from None
        now = datetime.now(self.tz)
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at < now + timedelta(minutes=prep):
            raise OrderError(f"The earliest pickup is about {prep} minutes from now. Offer ASAP.")
        return at

    # -- reservations ----------------------------------------------------

    def _require_parlor(self) -> ParlorClient:
        if self.deps.parlor is None:
            raise OrderError("Reservations aren't connected. Offer to take a message.")
        return self.deps.parlor

    async def _request_reservation(
        self,
        guest_name: str,
        party_size: int,
        date: str,
        time: str,
        notes: str = "",
        phone: str | None = None,
    ) -> str:
        parlor = self._require_parlor()
        max_party = int(self.deps.business.reservations.get("max_party_phone", 10))
        if party_size > max_party:
            raise OrderError(
                f"Parties over {max_party} are handled by the events team. Offer create_event_lead."
            )
        phone = self._phone(phone)
        booking = await parlor.request_reservation(guest_name, phone, party_size, date, time, notes)
        nice = _nice_datetime(booking["date"], booking["time"])
        if self.deps.sms:
            await self.deps.sms.send(
                phone,
                f"{self.deps.business.name}: we received your table request for {booking['partySize']} "
                f"on {nice} under {booking['guestName']}. Our team will text or call to confirm.",
            )
        await self._notify_staff(
            f"PHONE RESERVATION REQUEST #{booking['id']}: {guest_name}, {party_size} guests, {nice}, "
            f"{phone}. {notes}".strip()
        )
        return (
            f"Request #{booking['id']} saved for {booking['partySize']} on {nice}. Status: {booking['status']}. "
            "Tell the caller our team will confirm it, and that a text is on its way."
        )

    async def _find_my_reservations(self, phone: str | None = None) -> str:
        parlor = self._require_parlor()
        found = await parlor.find_reservations(self._phone(phone))
        if not found:
            return "No reservations found under that number."
        return "\n".join(
            f"#{r['id']} {r['guestName']}, party of {r['partySize']}, {_nice_datetime(r['date'], r['time'])}, "
            f"status {r['status']}"
            for r in found
        )

    async def _create_event_lead(
        self,
        name: str,
        event_date: str = "",
        guest_count: str = "",
        occasion: str = "",
        notes: str = "",
        phone: str | None = None,
        email: str = "",
    ) -> str:
        parlor = self._require_parlor()
        phone = self._phone(phone)
        lead = await parlor.create_lead(
            name=name,
            phone=phone,
            email=email,
            eventDate=event_date,
            guestCount=guest_count,
            occasion=occasion,
            notes=("From the phone agent. " + notes).strip(),
        )
        await self._notify_staff(
            f"NEW EVENT LEAD #{lead['id']}: {name} {phone}, {occasion or 'event'} {event_date} "
            f"for {guest_count or '?'} guests. {notes}".strip()
        )
        return f"Lead #{lead['id']} created. Tell the caller the events team will reach out, usually within a day."

    async def _take_message(
        self, caller_name: str, message: str, callback_number: str | None = None, urgent: bool = False
    ) -> str:
        phone = self._phone(callback_number)
        text = f"{'URGENT ' if urgent else ''}MESSAGE from {caller_name} {phone}: {message}"
        await self._notify_staff(text)
        if self.deps.parlor:
            try:
                await self.deps.parlor.create_task(
                    f"Call back {caller_name} {phone}: {message}",
                    client=caller_name,
                    priority="High" if urgent else "Medium",
                )
            except Exception:
                logger.exception("Could not file Parlor task")
        return "Message delivered to the team."

    # -- call control ----------------------------------------------------

    async def _transfer_call(self, destination: str, summary: str) -> str:
        target = self.deps.business.transfers.get(destination)
        if not target or not normalize_phone(target.get("number")):
            options = ", ".join(self.deps.business.transfers) or "none"
            return f"Unknown transfer destination. Options: {options}. Or take a message."
        self.state.pending_end = {"action": "transfer", "to": destination, "summary": summary}
        return "Transfer queued. Say one short line like 'Connecting you now' and nothing else."

    async def _end_call(self) -> str:
        self.state.pending_end = {"action": "hangup"}
        return "The call will end after this turn. Do not say anything else."


def _nice_datetime(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        return dt.strftime("%A, %B %-d at %-I:%M %p")
    except (TypeError, ValueError):
        return f"{date} {time}"
