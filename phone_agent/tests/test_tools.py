from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import CATALOG

from app.config import get_business
from app.parlor import ParlorRefused
from app.square import build_menu
from app.tools import CallState, ToolDeps, ToolRunner, normalize_phone


class FakeSms:
    def __init__(self):
        self.sent = []

    async def send(self, to, body):
        self.sent.append((to, body))
        return True

    async def send_many(self, numbers, body):
        for n in numbers:
            await self.send(n, body)


class FakeParlor:
    def __init__(self, refuse=None):
        self.refuse = refuse
        self.calls = []

    async def request_reservation(self, guest_name, phone, party_size, date, time, notes=""):
        self.calls.append(("reservation", guest_name, phone, party_size, date, time))
        if self.refuse:
            raise ParlorRefused(self.refuse, "pacing-covers-full")
        return {"id": 7, "guestName": guest_name, "partySize": party_size, "date": date, "time": time, "status": "Requested"}

    async def find_reservations(self, phone):
        return [{"id": 7, "guestName": "Ann", "partySize": 2, "date": "2026-11-06", "time": "18:30", "status": "Confirmed"}]

    async def create_lead(self, **fields):
        self.calls.append(("lead", fields))
        return {"id": 11}

    async def create_task(self, title, client="", priority="High"):
        self.calls.append(("task", title, priority))
        return {"id": 1}


class FakeSquare:
    def __init__(self):
        self.menu = build_menu(CATALOG, "LOC1", [])
        self.created = []

    async def create_pickup_link(self, lines, customer_name, phone, pickup_at, prep, note=""):
        self.created.append((lines, customer_name, phone, pickup_at))
        return {"url": "https://square.link/u/abc", "order_id": "ORD1", "total_cents": 1720, "tax_cents": 225}


def runner(parlor=None, square=None, caller="+15551234567"):
    sms = FakeSms()
    deps = ToolDeps(business=get_business(), square=square or FakeSquare(), parlor=parlor or FakeParlor(), sms=sms)
    state = CallState(call_sid="CA1", caller=caller)
    return ToolRunner(deps, state), sms, state


def tea_alias(square):
    return next(v.alias for v in square.menu.variations.values() if v.item_name == "Sweet Tea")


def test_normalize_phone():
    assert normalize_phone("(555) 123-4567") == "+15551234567"
    assert normalize_phone("1 555 123 4567") == "+15551234567"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("123") is None


async def test_reservation_request_texts_guest_and_staff():
    parlor = FakeParlor()
    r, sms, _ = runner(parlor=parlor)
    out, err = await r.run(
        "request_reservation", {"guest_name": "Ann", "party_size": 4, "date": "2026-11-06", "time": "18:30"}
    )
    assert not err and "Request #7" in out and "Friday, November 6 at 6:30 PM" in out
    assert parlor.calls[0] == ("reservation", "Ann", "+15551234567", 4, "2026-11-06", "18:30")
    assert [to for to, _ in sms.sent] == ["+15551234567", "+15550002222"]


async def test_reservation_refusal_is_returned_to_model():
    r, sms, _ = runner(parlor=FakeParlor(refuse="The venue is fully booked at that time."))
    out, err = await r.run(
        "request_reservation", {"guest_name": "Ann", "party_size": 2, "date": "2026-11-06", "time": "19:00"}
    )
    assert err and "fully booked" in out
    assert sms.sent == []


async def test_large_party_routed_to_events():
    parlor = FakeParlor()
    r, _, _ = runner(parlor=parlor)
    out, err = await r.run(
        "request_reservation", {"guest_name": "Ann", "party_size": 20, "date": "2026-11-06", "time": "19:00"}
    )
    assert err and "create_event_lead" in out
    assert parlor.calls == []


async def test_place_order_creates_link_and_texts_customer():
    square = FakeSquare()
    r, sms, _ = runner(square=square)
    out, err = await r.run(
        "place_takeout_order",
        {"items": [{"variation": tea_alias(square), "quantity": 2}], "customer_name": "Bo", "pickup_time": "ASAP"},
    )
    assert not err and "$17.20" in out
    lines, name, phone, pickup_at = square.created[0]
    assert lines == [{"catalog_object_id": "VAR2", "quantity": "2"}] and phone == "+15551234567"
    assert pickup_at is None
    customer_text = sms.sent[0]
    assert customer_text[0] == "+15551234567" and "https://square.link/u/abc" in customer_text[1]
    assert sms.sent[1][0] == "+15550002222" and "2 x Sweet Tea" in sms.sent[1][1]


async def test_scheduled_pickup_must_allow_prep_time():
    square = FakeSquare()
    r, _, _ = runner(square=square)
    soon = (datetime.now(ZoneInfo("America/New_York")) + timedelta(minutes=5)).strftime("%H:%M")
    out, err = await r.run(
        "place_takeout_order",
        {"items": [{"variation": tea_alias(square), "quantity": 1}], "customer_name": "Bo", "pickup_time": soon},
    )
    assert err and "earliest pickup" in out
    assert square.created == []


async def test_transfer_and_end_set_pending_end():
    r, _, state = runner()
    out, err = await r.run("transfer_call", {"destination": "nowhere", "summary": "x"})
    assert "Unknown transfer destination" in out and state.pending_end is None
    await r.run("transfer_call", {"destination": "manager", "summary": "refund question"})
    assert state.pending_end == {"action": "transfer", "to": "manager", "summary": "refund question"}
    await r.run("end_call", {})
    assert state.pending_end == {"action": "hangup"}


async def test_take_message_files_task():
    parlor = FakeParlor()
    r, sms, _ = runner(parlor=parlor)
    out, err = await r.run("take_message", {"caller_name": "Cy", "message": "call about cake", "urgent": True})
    assert not err
    assert sms.sent[0][1].startswith("URGENT MESSAGE from Cy +15551234567")
    assert parlor.calls[0][0] == "task" and parlor.calls[0][2] == "High"


async def test_bad_arguments_are_reported_not_raised():
    r, _, _ = runner()
    out, err = await r.run("request_reservation", {"guest_name": "Ann"})
    assert err and "Bad arguments" in out
