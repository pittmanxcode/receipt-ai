"""Receipt <-> Keep note text.

The note body is the canonical remote representation: it has to survive being
hand-edited in the Keep app, so parsing is deliberately forgiving. The note
title is *derived* on every push and ignored on pull -- editing the title in
Keep changes nothing, which is documented in the note itself.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .model import LineItem, Receipt, parse_money

MARKER_RE = re.compile(r"\[receipt:([0-9a-fA-F]{8,})\]")

# Header keys we write, mapped to the receipt field they carry. Parsing accepts
# any of the aliases so a user retyping "Payment method:" still round-trips.
_FIELD_ALIASES = {
    "vendor": "vendor",
    "merchant": "vendor",
    "store": "vendor",
    "date": "date",
    "total": "total",
    "amount": "total",
    "currency": "currency",
    "category": "category",
    "payment": "payment_method",
    "payment method": "payment_method",
    "tags": "tags",
}

_SECTION_ITEMS = "items"
_SECTION_NOTES = "notes"
_HEADER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]{0,20}?)\s*:\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*•]\s*(.*)$")
_QTY_RE = re.compile(r"^\s*([\d.,]+)\s*[x×]\s+(.*)$", re.IGNORECASE)
_TRAILING_AMOUNT_RE = re.compile(
    r"^(?P<desc>.*?)[\s—–|:-]*"
    r"(?P<amount>[$£€]?\(?-?[\d,]+(?:\.\d{1,2})?\)?)\s*$"
)


def _format_quantity(value) -> str:
    """Render 2.00 as "2" -- quantities read as counts, not money."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal("1")))
    return str(normalized)


def note_title(receipt: Receipt) -> str:
    """A scannable title for the Keep list view. Derived, never parsed back."""
    parts = [receipt.vendor or "Receipt"]
    if receipt.date:
        parts.append(receipt.date)
    parts.append(f"{receipt.total} {receipt.currency}".strip())
    return " · ".join(parts)


def note_text(receipt: Receipt) -> str:
    """Render a receipt as the body of a Keep note."""
    lines = [
        f"Vendor: {receipt.vendor}",
        f"Date: {receipt.date}",
        f"Total: {receipt.total}",
        f"Currency: {receipt.currency}",
        f"Category: {receipt.category}",
        f"Payment: {receipt.payment_method}",
        f"Tags: {', '.join(receipt.tags)}",
    ]
    if receipt.items:
        lines += ["", "Items:"]
        for item in receipt.items:
            qty = (
                f"{_format_quantity(item.quantity)} x "
                if item.quantity is not None
                else ""
            )
            lines.append(f"- {qty}{item.description} — {item.amount}")
    if receipt.notes:
        lines += ["", "Notes:", receipt.notes]
    lines += ["", f"[receipt:{receipt.id}]"]
    return "\n".join(lines)


def extract_marker(text: str) -> str | None:
    """The receipt id embedded in a note body, if it still carries one.

    This is the fallback that re-links a note to its record when sync state is
    lost or rebuilt, so a wiped state file does not duplicate every receipt.
    """
    match = MARKER_RE.search(text or "")
    return match.group(1) if match else None


def _parse_item(raw: str) -> LineItem | None:
    body = raw.strip()
    if not body:
        return None
    quantity = None
    qty_match = _QTY_RE.match(body)
    if qty_match:
        try:
            quantity = parse_money(qty_match.group(1))
            body = qty_match.group(2)
        except ValueError:
            quantity = None
    amount_match = _TRAILING_AMOUNT_RE.match(body)
    if not amount_match:
        return LineItem(description=body.strip(), amount=parse_money(0), quantity=quantity)
    description = amount_match.group("desc").strip(" \t—–|-:")
    try:
        amount = parse_money(amount_match.group("amount"))
    except ValueError:
        return LineItem(description=body.strip(), amount=parse_money(0), quantity=quantity)
    return LineItem(description=description, amount=amount, quantity=quantity)


def parse_note(text: str, receipt_id: str) -> Receipt:
    """Parse a Keep note body back into a receipt.

    Never raises on malformed input: an unparseable field falls back to its
    default so one bad line in Keep cannot break a whole sync run.
    """
    data: dict = {"id": receipt_id}
    items: list[LineItem] = []
    note_lines: list[str] = []
    section: str | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if MARKER_RE.search(line):
            continue

        header = _HEADER_RE.match(line)
        key = header.group(1).strip().lower() if header else None

        if key == _SECTION_ITEMS and not header.group(2).strip():
            section = _SECTION_ITEMS
            continue
        if key == _SECTION_NOTES:
            section = _SECTION_NOTES
            remainder = header.group(2).strip()
            if remainder:
                note_lines.append(remainder)
            continue

        if section == _SECTION_NOTES:
            note_lines.append(raw_line)
            continue

        bullet = _BULLET_RE.match(line)
        if bullet and section == _SECTION_ITEMS:
            item = _parse_item(bullet.group(1))
            if item is not None:
                items.append(item)
            continue

        if key in _FIELD_ALIASES:
            data[_FIELD_ALIASES[key]] = header.group(2).strip()
            section = None
            continue

        if line.strip() and section is None and note_lines:
            note_lines.append(raw_line)

    if items:
        data["items"] = [i.to_dict() for i in items]
    data["notes"] = "\n".join(note_lines).strip()

    try:
        data["total"] = parse_money(data.get("total", 0))
    except ValueError:
        data["total"] = parse_money(0)

    return Receipt.from_dict(data)
