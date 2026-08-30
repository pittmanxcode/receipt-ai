"""The receipt record: the unit that gets synced to and from a Keep note."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

# Fields that participate in sync and conflict detection, in display order.
# `id` is deliberately absent: it is identity, not content, and never merges.
SYNCED_FIELDS = (
    "vendor",
    "date",
    "total",
    "currency",
    "category",
    "payment_method",
    "tags",
    "items",
    "notes",
    "trashed",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY_CLEAN_RE = re.compile(r"[,\s $£€]")


def new_id() -> str:
    """A stable local identity for a receipt, independent of any Keep note id."""
    return uuid.uuid4().hex


def parse_money(raw: object) -> Decimal:
    """Parse a money value tolerantly; a user typing in Keep is not a parser.

    Accepts "$1,234.50", "1234.5", Decimal, int/float. Rejects anything else
    loudly rather than silently zeroing a total.
    """
    if isinstance(raw, Decimal):
        return raw.quantize(Decimal("0.01"))
    if isinstance(raw, (int, float)):
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    text = _MONEY_CLEAN_RE.sub("", str(raw or "")).strip()
    if not text:
        return Decimal("0.00")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a money value: {raw!r}") from exc
    if negative:
        value = -value
    return value.quantize(Decimal("0.01"))


def normalize_date(raw: object) -> str:
    """Dates are stored ISO-8601. Anything unparseable is kept verbatim.

    Keeping a bad date rather than raising means a typo in Keep degrades one
    field instead of failing the whole sync run.
    """
    text = str(raw or "").strip()
    if not text or _DATE_RE.match(text):
        return text
    for sep in ("/", "."):
        parts = text.split(sep)
        if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
            a, b, c = (p.strip() for p in parts)
            if len(a) == 4:  # YYYY/MM/DD
                return f"{a}-{int(b):02d}-{int(c):02d}"
            if len(c) == 4:  # MM/DD/YYYY
                return f"{c}-{int(a):02d}-{int(b):02d}"
    return text


def normalize_tags(raw: object) -> list[str]:
    """Tags are a lowercase, de-duplicated, sorted set stored as a list."""
    if raw is None:
        values: list[str] = []
    elif isinstance(raw, str):
        values = re.split(r"[,;]", raw)
    else:
        values = [str(v) for v in raw]
    seen = {v.strip().lower() for v in values if v and v.strip()}
    return sorted(seen)


@dataclass(frozen=True)
class LineItem:
    """One line on a receipt. Quantity is optional; amount is not."""

    description: str
    amount: Decimal
    quantity: Decimal | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "LineItem":
        qty = data.get("quantity")
        return cls(
            description=str(data.get("description", "")).strip(),
            amount=parse_money(data.get("amount", 0)),
            quantity=None if qty in (None, "") else parse_money(qty),
        )

    def to_dict(self) -> dict:
        out: dict = {"description": self.description, "amount": str(self.amount)}
        if self.quantity is not None:
            out["quantity"] = str(self.quantity)
        return out


@dataclass(frozen=True)
class Receipt:
    """A receipt. Frozen so merge results are new objects, never mutations."""

    id: str = field(default_factory=new_id)
    vendor: str = ""
    date: str = ""
    total: Decimal = Decimal("0.00")
    currency: str = "USD"
    category: str = ""
    payment_method: str = ""
    tags: list[str] = field(default_factory=list)
    items: list[LineItem] = field(default_factory=list)
    notes: str = ""
    trashed: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Receipt":
        return cls(
            id=str(data.get("id") or new_id()),
            vendor=str(data.get("vendor", "")).strip(),
            date=normalize_date(data.get("date", "")),
            total=parse_money(data.get("total", 0)),
            currency=(str(data.get("currency") or "USD").strip().upper() or "USD"),
            category=str(data.get("category", "")).strip(),
            payment_method=str(data.get("payment_method", "")).strip(),
            tags=normalize_tags(data.get("tags")),
            items=[LineItem.from_dict(i) for i in data.get("items") or []],
            notes=str(data.get("notes", "")).strip(),
            trashed=bool(data.get("trashed", False)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vendor": self.vendor,
            "date": self.date,
            "total": str(self.total),
            "currency": self.currency,
            "category": self.category,
            "payment_method": self.payment_method,
            "tags": list(self.tags),
            "items": [i.to_dict() for i in self.items],
            "notes": self.notes,
            "trashed": self.trashed,
        }

    def content(self) -> dict:
        """The synced fields only -- what merge and change detection compare."""
        full = self.to_dict()
        return {k: full[k] for k in SYNCED_FIELDS}

    def with_content(self, content: dict) -> "Receipt":
        """This receipt's identity carrying a merged field set."""
        return Receipt.from_dict({**content, "id": self.id})

    def replace(self, **changes) -> "Receipt":
        return replace(self, **changes)


def content_differs(a: dict, b: dict) -> bool:
    """Whether two content dicts differ on any synced field."""
    return any(a.get(k) != b.get(k) for k in SYNCED_FIELDS)
