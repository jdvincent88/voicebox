import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PUBLIC_BASE_URL", "https://agent.example.com")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("BUSINESS_FILE", str(ROOT / "tests" / "business.test.yaml"))
os.environ.setdefault("VALIDATE_TWILIO_SIGNATURE", "false")

CATALOG = [
    {"type": "CATEGORY", "id": "CAT1", "category_data": {"name": "Plates"}},
    {"type": "CATEGORY", "id": "CAT2", "category_data": {"name": "Alcohol"}},
    {
        "type": "MODIFIER_LIST",
        "id": "ML1",
        "modifier_list_data": {
            "name": "Side",
            "selection_type": "SINGLE",
            "modifiers": [
                {"id": "MOD1", "modifier_data": {"name": "Collards", "price_money": {"amount": 0}}},
                {"id": "MOD2", "modifier_data": {"name": "Mac and cheese", "price_money": {"amount": 150}}},
            ],
        },
    },
    {
        "type": "ITEM",
        "id": "ITEM1",
        "present_at_all_locations": True,
        "item_data": {
            "name": "Fried Chicken Plate",
            "description": "Three pieces, buttermilk brined",
            "categories": [{"id": "CAT1"}],
            "modifier_list_info": [{"modifier_list_id": "ML1", "min_selected_modifiers": 1, "max_selected_modifiers": 1}],
            "variations": [
                {
                    "id": "VAR1",
                    "item_variation_data": {"name": "Regular", "price_money": {"amount": 1495, "currency": "USD"}},
                },
                {
                    "id": "VAR_SOLD_OUT",
                    "item_variation_data": {
                        "name": "Family",
                        "price_money": {"amount": 4995},
                        "location_overrides": [{"location_id": "LOC1", "sold_out": True}],
                    },
                },
            ],
        },
    },
    {
        "type": "ITEM",
        "id": "ITEM2",
        "present_at_all_locations": True,
        "item_data": {
            "name": "Sweet Tea",
            "category_id": "CAT1",
            "variations": [{"id": "VAR2", "item_variation_data": {"name": "Regular", "price_money": {"amount": 300}}}],
        },
    },
    {
        "type": "ITEM",
        "id": "ITEM3",
        "present_at_all_locations": True,
        "item_data": {
            "name": "Bourbon",
            "categories": [{"id": "CAT2"}],
            "variations": [{"id": "VAR3", "item_variation_data": {"price_money": {"amount": 900}}}],
        },
    },
    {
        "type": "ITEM",
        "id": "ITEM4",
        "present_at_all_locations": False,
        "present_at_location_ids": ["OTHER"],
        "item_data": {
            "name": "Other location only",
            "variations": [{"id": "VAR4", "item_variation_data": {"price_money": {"amount": 100}}}],
        },
    },
]
