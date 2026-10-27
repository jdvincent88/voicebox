import pytest
from conftest import CATALOG

from app.square import OrderError, build_menu, price_order


@pytest.fixture
def menu():
    return build_menu(CATALOG, "LOC1", ["Alcohol"])


def by_name(menu, name):
    return next(v for v in menu.variations.values() if v.item_name == name)


def test_menu_skips_hidden_sold_out_and_other_locations(menu):
    names = {v.label for v in menu.variations.values()}
    assert names == {"Fried Chicken Plate", "Sweet Tea"}


def test_menu_prompt_lists_aliases_prices_and_choices(menu):
    text = menu.as_prompt()
    chicken = by_name(menu, "Fried Chicken Plate")
    assert f"{chicken.alias} Fried Chicken Plate $14.95" in text
    assert "Side (choose 1)" in text
    assert "Mac and cheese +$1.50" in text


def test_price_order_builds_square_lines(menu):
    chicken = by_name(menu, "Fried Chicken Plate")
    mac = next(m for m in chicken.modifier_lists[0].modifiers if m.name == "Mac and cheese")
    lines, subtotal, summary = price_order(
        menu,
        [{"variation": chicken.alias.lower(), "quantity": 2, "modifiers": [mac.alias], "note": "extra crispy"}],
        max_items=5,
    )
    assert subtotal == 2 * (1495 + 150)
    assert lines == [
        {
            "catalog_object_id": "VAR1",
            "quantity": "2",
            "modifiers": [{"catalog_object_id": "MOD2"}],
            "note": "extra crispy",
        }
    ]
    assert summary == ["2 x Fried Chicken Plate with Mac and cheese (note: extra crispy)"]


def test_price_order_requires_choices(menu):
    chicken = by_name(menu, "Fried Chicken Plate")
    with pytest.raises(OrderError, match="needs a choice of Side"):
        price_order(menu, [{"variation": chicken.alias, "quantity": 1}], max_items=5)


def test_price_order_rejects_foreign_modifier_and_unknown_item(menu):
    tea = by_name(menu, "Sweet Tea")
    chicken = by_name(menu, "Fried Chicken Plate")
    mod = chicken.modifier_lists[0].modifiers[0].alias
    with pytest.raises(OrderError, match="not an option"):
        price_order(menu, [{"variation": tea.alias, "quantity": 1, "modifiers": [mod]}], max_items=5)
    with pytest.raises(OrderError, match="not on the current menu"):
        price_order(menu, [{"variation": "V999", "quantity": 1}], max_items=5)


def test_price_order_caps_phone_orders(menu):
    tea = by_name(menu, "Sweet Tea")
    with pytest.raises(OrderError, match="catering"):
        price_order(menu, [{"variation": tea.alias, "quantity": 6}], max_items=5)
