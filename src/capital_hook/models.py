"""Small result types returned by the client."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Confirmation:
    """Outcome of an order, as reported by ``GET /confirms/{dealReference}``.

    Opening a position (or creating a working order) only returns a *reference*; a 200 does not mean the
    deal happened. The confirmation says whether it was ``ACCEPTED`` or ``REJECTED`` and which deal it produced.
    """

    deal_reference: str
    deal_status: str                      # "ACCEPTED" | "REJECTED"
    reason: str | None
    status: str | None                    # e.g. "OPEN"
    deal_id: str | None
    affected_deals: tuple[dict, ...]
    epic: str | None
    direction: str | None
    size: float | None
    level: float | None
    raw: dict

    @property
    def position_deal_id(self) -> str | None:
        """The id of the position this order opened (``affectedDeals`` entry with status OPENED).

        ``deal_id`` is the id of the *order*, which is not the id used by ``get_positions`` / ``close_position``:
        a real confirmation carries both (the Postman example has ``dealId`` ...43 and an OPENED deal ...45).
        """
        return next((d.get("dealId") for d in self.affected_deals if d.get("status") == "OPENED"), None)

    @property
    def accepted(self) -> bool:
        return self.deal_status == "ACCEPTED"

    @property
    def rejected(self) -> bool:
        return self.deal_status == "REJECTED"

    @classmethod
    def from_response(cls, data: dict) -> "Confirmation":
        def num(key):
            value = data.get(key)
            return float(value) if value is not None else None

        return cls(
            deal_reference=data.get("dealReference", ""),
            deal_status=data.get("dealStatus", ""),
            reason=data.get("reason"),
            status=data.get("status"),
            deal_id=data.get("dealId"),
            affected_deals=tuple(data.get("affectedDeals") or ()),
            epic=data.get("epic"),
            direction=data.get("direction"),
            size=num("size"),
            level=num("level"),
            raw=data,
        )
