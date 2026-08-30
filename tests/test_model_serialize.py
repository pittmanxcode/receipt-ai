from decimal import Decimal

import pytest

from keep_bridge.model import Receipt, normalize_date, normalize_tags, parse_money
from keep_bridge.serialize import extract_marker, note_text, parse_note


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$1,234.50", Decimal("1234.50")),
        ("12", Decimal("12.00")),
        ("(12.30)", Decimal("-12.30")),
        ("", Decimal("0.00")),
        (7.5, Decimal("7.50")),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_money_rejects_garbage():
    with pytest.raises(ValueError):
        parse_money("twelve dollars")


@pytest.mark.parametrize(
    "raw,expected",
    [("8/14/2026", "2026-08-14"), ("2026/3/4", "2026-03-04"), ("2026-08-14", "2026-08-14")],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_unparseable_date_is_kept_verbatim():
    # A typo degrades one field; it must not fail the record.
    assert normalize_date("last tuesday") == "last tuesday"


def test_tags_are_a_lowercase_sorted_set():
    assert normalize_tags("Food, food , Reimbursable") == ["food", "reimbursable"]


def _sample() -> Receipt:
    return Receipt.from_dict(
        {
            "vendor": "Trader Joe's",
            "date": "2026-08-14",
            "total": "42.17",
            "category": "groceries",
            "payment_method": "visa-1234",
            "tags": ["food", "reimbursable"],
            "items": [
                {"description": "Bananas", "amount": "3.98", "quantity": "2"},
                {"description": "Oat milk", "amount": "4.49"},
            ],
            "notes": "split with Dana\nreimburse in Sept",
        }
    )


def test_round_trips_through_a_note_body():
    receipt = _sample()
    assert parse_note(note_text(receipt), receipt.id).content() == receipt.content()


def test_note_carries_its_receipt_id():
    receipt = _sample()
    assert extract_marker(note_text(receipt)) == receipt.id


def test_parses_a_hand_written_note():
    # What someone actually types into Keep on a phone: different key casing,
    # a currency symbol, an aliased key, and a bullet with no em dash.
    body = "\n".join(
        [
            "merchant: Blue Bottle",
            "date: 3/2/2026",
            "amount: $8.75",
            "Payment method: amex",
            "tags: coffee",
            "Items:",
            "* Latte 4.75",
            "- 1 x Croissant 4.00",
            "Notes:",
            "morning standup",
        ]
    )
    receipt = parse_note(body, "abc12345")
    assert receipt.vendor == "Blue Bottle"
    assert receipt.date == "2026-03-02"
    assert receipt.total == Decimal("8.75")
    assert receipt.payment_method == "amex"
    assert receipt.tags == ["coffee"]
    assert [i.description for i in receipt.items] == ["Latte", "Croissant"]
    assert [str(i.amount) for i in receipt.items] == ["4.75", "4.00"]
    assert receipt.notes == "morning standup"


def test_malformed_body_never_raises():
    receipt = parse_note("total: not a number\nvendor: X", "abc12345")
    assert receipt.total == Decimal("0.00")
    assert receipt.vendor == "X"
