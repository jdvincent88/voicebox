"""Square catalog + takeout orders.

The menu is pulled from the Square catalog so prices and items never drift
from the POS. Each sellable variation and modifier gets a short alias (V12,
M4) that the model uses instead of Square's 24-character ids.

A takeout order is created as a Square payment link with a PICKUP
fulfillment. Square only shows API orders on the POS / KDS once they are
paid, so the kitchen never starts an unpaid ticket.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

SQUARE_VERSION = "2025-01-23"


class OrderError(ValueError):
    """A problem with an order the caller can fix (shown to the model)."""


@dataclass
class Modifier:
    alias: str
    square_id: str
    name: str
    price_cents: int


@dataclass
class ModifierList:
    name: str
    min_selected: int
    max_selected: int  # 0 = no limit
    modifiers: list[Modifier]


@dataclass
class Variation:
    alias: str
    square_id: str
    item_name: str
    name: str
    price_cents: int
    category: str
    description: str
    modifier_lists: list[ModifierList] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.name and self.name.lower() not in ("regular", ""):
            return f"{self.item_name} ({self.name})"
        return self.item_name


@dataclass
class Menu:
    variations: dict[str, Variation]
    modifiers: dict[str, Modifier]

    def as_prompt(self) -> str:
        if not self.variations:
            return "(The menu is unavailable right now. Do not take takeout orders.)"
        lines: list[str] = []
        current = None
        for v in sorted(self.variations.values(), key=lambda v: (v.category, v.item_name)):
            if v.category != current:
                current = v.category
                lines.append(f"\n## {current or 'Other'}")
            line = f"{v.alias} {v.label} ${v.price_cents / 100:.2f}"
            if v.description:
                line += f" - {v.description[:140]}"
            lines.append(line)
            for ml in v.modifier_lists:
                rule = _choice_rule(ml)
                opts = ", ".join(
                    f"{m.alias} {m.name}" + (f" +${m.price_cents / 100:.2f}" if m.price_cents else "")
                    for m in ml.modifiers
                )
                lines.append(f"    {ml.name} ({rule}): {opts}")
        return "\n".join(lines).strip()


def _choice_rule(ml: ModifierList) -> str:
    if ml.min_selected and ml.min_selected == ml.max_selected:
        return f"choose {ml.min_selected}"
    if ml.min_selected:
        return f"choose at least {ml.min_selected}"
    if ml.max_selected:
        return f"optional, up to {ml.max_selected}"
    return "optional"


class SquareClient:
    def __init__(self, access_token: str, location_id: str, base_url: str):
        self.location_id = location_id
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=15.0,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
            },
        )
        self.menu = Menu({}, {})

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- catalog ---------------------------------------------------------

    async def _list_catalog(self) -> list[dict]:
        objects: list[dict] = []
        cursor = None
        while True:
            params = {"types": "ITEM,MODIFIER_LIST,CATEGORY"}
            if cursor:
                params["cursor"] = cursor
            r = await self._http.get("/v2/catalog/list", params=params)
            r.raise_for_status()
            body = r.json()
            objects.extend(body.get("objects", []))
            cursor = body.get("cursor")
            if not cursor:
                return objects

    async def refresh_menu(self, hidden_categories: list[str] | None = None) -> Menu:
        self.menu = build_menu(await self._list_catalog(), self.location_id, hidden_categories or [])
        logger.info("Loaded %d menu variations from Square", len(self.menu.variations))
        return self.menu

    # -- orders ----------------------------------------------------------

    async def create_pickup_link(
        self,
        lines: list[dict],
        customer_name: str,
        phone_e164: str,
        pickup_at: datetime | None,
        prep_minutes: int,
        note: str = "",
    ) -> dict:
        """Create a Square payment link for a pickup order. Returns url + totals."""
        pickup: dict = {
            "recipient": {"display_name": customer_name, "phone_number": phone_e164},
            "note": note[:500] if note else None,
        }
        if pickup_at:
            pickup["schedule_type"] = "SCHEDULED"
            pickup["pickup_at"] = pickup_at.isoformat()
        else:
            pickup["schedule_type"] = "ASAP"
            pickup["prep_time_duration"] = f"PT{prep_minutes}M"
        pickup = {k: v for k, v in pickup.items() if v is not None}

        body = {
            "idempotency_key": str(uuid.uuid4()),
            "description": f"Phone order for {customer_name}",
            "order": {
                "location_id": self.location_id,
                "source": {"name": "Phone agent"},
                "line_items": lines,
                "fulfillments": [{"type": "PICKUP", "state": "PROPOSED", "pickup_details": pickup}],
                "pricing_options": {"auto_apply_taxes": True, "auto_apply_discounts": True},
            },
            "checkout_options": {"ask_for_shipping_address": False},
            "pre_populated_data": {"buyer_phone_number": phone_e164},
        }
        r = await self._http.post("/v2/online-checkout/payment-links", json=body)
        if r.status_code >= 400:
            logger.error("Square payment link failed: %s %s", r.status_code, r.text[:500])
            raise RuntimeError("Square rejected the order")
        data = r.json()
        link = data["payment_link"]
        order = (data.get("related_resources", {}).get("orders") or [{}])[0]
        return {
            "url": link["url"],
            "order_id": link.get("order_id"),
            "total_cents": (order.get("total_money") or {}).get("amount"),
            "tax_cents": (order.get("total_tax_money") or {}).get("amount"),
        }


def build_menu(objects: list[dict], location_id: str, hidden_categories: list[str]) -> Menu:
    hidden = {c.lower() for c in hidden_categories}
    categories = {
        o["id"]: o.get("category_data", {}).get("name", "")
        for o in objects
        if o.get("type") == "CATEGORY"
    }

    modifiers: dict[str, Modifier] = {}
    modifier_lists: dict[str, ModifierList] = {}
    for o in objects:
        if o.get("type") != "MODIFIER_LIST" or o.get("is_deleted"):
            continue
        data = o.get("modifier_list_data", {})
        mods = []
        for m in data.get("modifiers", []):
            if m.get("is_deleted"):
                continue
            md = m.get("modifier_data", {})
            alias = f"M{len(modifiers) + 1}"
            mod = Modifier(alias, m["id"], md.get("name", ""), (md.get("price_money") or {}).get("amount", 0))
            modifiers[alias] = mod
            mods.append(mod)
        single = data.get("selection_type") == "SINGLE"
        modifier_lists[o["id"]] = ModifierList(
            data.get("name", ""), 0, 1 if single else 0, mods
        )

    variations: dict[str, Variation] = {}
    for o in objects:
        if o.get("type") != "ITEM" or o.get("is_deleted") or not _present_at(o, location_id):
            continue
        item = o.get("item_data", {})
        if item.get("is_archived"):
            continue
        cat_ids = [c.get("id") for c in item.get("categories") or []] or [item.get("category_id")]
        category = next((categories[c] for c in cat_ids if c in categories), "")
        if category.lower() in hidden:
            continue

        item_lists = []
        for info in item.get("modifier_list_info") or []:
            base = modifier_lists.get(info.get("modifier_list_id"))
            if base is None or info.get("enabled") is False:
                continue
            min_sel = max(info.get("min_selected_modifiers", 0) or 0, 0)
            max_sel = info.get("max_selected_modifiers", base.max_selected)
            max_sel = base.max_selected if max_sel in (None, -1) else max_sel
            item_lists.append(ModifierList(base.name, min_sel, max_sel, base.modifiers))

        for var in item.get("variations") or []:
            vd = var.get("item_variation_data", {})
            if var.get("is_deleted") or vd.get("pricing_type") == "VARIABLE_PRICING":
                continue
            if not _present_at(var, location_id) or _sold_out(vd, location_id):
                continue
            price = (vd.get("price_money") or {}).get("amount")
            if price is None:
                continue
            alias = f"V{len(variations) + 1}"
            variations[alias] = Variation(
                alias=alias,
                square_id=var["id"],
                item_name=item.get("name", ""),
                name=vd.get("name", ""),
                price_cents=price,
                category=category,
                description=(item.get("description_plaintext") or item.get("description") or "").strip(),
                modifier_lists=item_lists,
            )
    return Menu(variations, modifiers)


def _present_at(obj: dict, location_id: str) -> bool:
    if location_id in (obj.get("absent_at_location_ids") or []):
        return False
    if obj.get("present_at_all_locations", True):
        return True
    return location_id in (obj.get("present_at_location_ids") or [])


def _sold_out(variation_data: dict, location_id: str) -> bool:
    for override in variation_data.get("location_overrides") or []:
        if override.get("location_id") == location_id and override.get("sold_out"):
            return True
    return False


def price_order(menu: Menu, items: list[dict], max_items: int) -> tuple[list[dict], int, list[str]]:
    """Validate a cart against the menu.

    ``items`` is ``[{"variation": "V3", "quantity": 2, "modifiers": ["M1"], "note": ""}]``.
    Returns (square line items, pre-tax subtotal in cents, spoken summary lines).
    Raises OrderError with a caller-fixable message.
    """
    if not items:
        raise OrderError("The order is empty.")
    lines: list[dict] = []
    summary: list[str] = []
    subtotal = 0
    count = 0
    for raw in items:
        alias = str(raw.get("variation", "")).strip().upper()
        var = menu.variations.get(alias)
        if var is None:
            raise OrderError(f"{alias or 'An item'} is not on the current menu.")
        qty = int(raw.get("quantity") or 1)
        if qty < 1:
            raise OrderError(f"Quantity for {var.label} must be at least 1.")
        count += qty

        chosen = [str(m).strip().upper() for m in raw.get("modifiers") or []]
        allowed = {m.alias: (ml, m) for ml in var.modifier_lists for m in ml.modifiers}
        picked: dict[str, list[Modifier]] = {}
        for m_alias in chosen:
            if m_alias not in allowed:
                raise OrderError(f"{m_alias} is not an option for {var.label}.")
            ml, mod = allowed[m_alias]
            picked.setdefault(ml.name, []).append(mod)
        for ml in var.modifier_lists:
            n = len(picked.get(ml.name, []))
            if n < ml.min_selected:
                raise OrderError(f"{var.label} needs a choice of {ml.name} ({_choice_rule(ml)}).")
            if ml.max_selected and n > ml.max_selected:
                raise OrderError(f"{var.label} allows at most {ml.max_selected} {ml.name}.")

        mods = [mod for group in picked.values() for mod in group]
        unit = var.price_cents + sum(m.price_cents for m in mods)
        subtotal += unit * qty
        line: dict = {
            "catalog_object_id": var.square_id,
            "quantity": str(qty),
        }
        if mods:
            line["modifiers"] = [{"catalog_object_id": m.square_id} for m in mods]
        note = str(raw.get("note") or "").strip()
        if note:
            line["note"] = note[:500]
        lines.append(line)
        desc = f"{qty} x {var.label}"
        if mods:
            desc += " with " + ", ".join(m.name for m in mods)
        if note:
            desc += f" (note: {note})"
        summary.append(desc)

    if count > max_items:
        raise OrderError(
            f"That is {count} items, more than we take by phone ({max_items}). Offer catering instead."
        )
    return lines, subtotal, summary
