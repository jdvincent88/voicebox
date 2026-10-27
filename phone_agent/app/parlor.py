"""Parlor HQ API client: reservations, event leads and staff tasks.

Parlor checks every reservation request against the venue's real rules
(hours, closed days, pacing, party limits, notice) and refuses with a
plain-English reason, so it doubles as the availability check. Phone
bookings land as *Requested* for staff to confirm.
"""

from __future__ import annotations

import logging
import uuid

import httpx

logger = logging.getLogger(__name__)


class ParlorRefused(Exception):
    """Parlor said no for a reason the caller should hear."""

    def __init__(self, message: str, reason: str | None):
        super().__init__(message)
        self.reason = reason


class ParlorClient:
    def __init__(self, base_url: str, api_key: str = ""):
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-Parlor-Key"] = api_key
        self._http = httpx.AsyncClient(base_url=base_url, timeout=15.0, headers=headers)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        r = await self._http.post(path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        if r.status_code in (400, 409):
            err = r.json()
            raise ParlorRefused(err.get("error", "Parlor refused the request."), err.get("reason"))
        r.raise_for_status()
        return r.json()

    async def request_reservation(
        self,
        guest_name: str,
        phone: str,
        party_size: int,
        date: str,
        time: str,
        notes: str = "",
        email: str = "",
    ) -> dict:
        body = {
            "guestName": guest_name,
            "phone": phone,
            "partySize": party_size,
            "date": date,
            "time": time,
            "notes": ("Booked by phone agent. " + notes).strip(),
        }
        if email:
            body["email"] = email
        return (await self._post("/api/v1/reservations", body))["reservation"]

    async def find_reservations(self, phone: str) -> list[dict]:
        r = await self._http.get("/api/v1/reservations/find", params={"phone": phone})
        r.raise_for_status()
        return r.json()

    async def create_lead(self, **fields) -> dict:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        return (await self._post("/api/v1/leads", body))["lead"]

    async def create_task(self, title: str, client: str = "", priority: str = "High") -> dict:
        body = {"title": title[:200], "category": "Follow-Up", "priority": priority}
        if client:
            body["client"] = client[:200]
        return (await self._post("/api/v1/tasks", body))["task"]
