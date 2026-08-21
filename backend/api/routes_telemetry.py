"""Authenticated, identity-free product telemetry ingestion."""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from backend.api.deps import get_current_user, get_db
from backend.core.telemetry import enforce_product_event_limits
from backend.db.database import now_iso

router = APIRouter()

_PERIODS = {"7d", "30d", "90d"}
_SOURCES = {"screener", "analysis", "positions"}
_QUERY_KINDS = {"address", "text"}
_DISMISS_STATES = {"confirming", "confirmed", "rejected", "reconciliation_required", "failed"}

# The exact property set per event — allowed and required are the same set,
# so this single mapping is the whole payload contract.
_EVENT_PROPERTIES: dict[str, frozenset[str]] = {
    "screener_search_submitted": frozenset({"query_kind", "period", "active_filters"}),
    "period_changed": frozenset({"period", "source"}),
    "advanced_filters_opened": frozenset({"period"}),
    "wallet_analysis_opened": frozenset({"period", "source"}),
    "copy_settings_opened": frozenset({"source"}),
    "close_modal_opened": frozenset({"source"}),
    "close_submitted": frozenset({"source"}),
    "close_confirmed": frozenset({"duration_ms"}),
    "close_rejected": frozenset({"duration_ms"}),
    "close_reconciliation_required": frozenset({"duration_ms"}),
    "close_failed": frozenset({"duration_ms"}),
    "modal_dismissed": frozenset({"state", "source"}),
}


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID4
    event_name: str = Field(min_length=1, max_length=64)
    properties: dict[str, Any] = Field(default_factory=dict, max_length=6)

    @model_validator(mode="after")
    def validate_allowlisted_payload(self):
        allowed = _EVENT_PROPERTIES.get(self.event_name)
        if allowed is None:
            raise ValueError("unsupported telemetry event")
        keys = set(self.properties)
        unknown = keys - allowed
        # Every allowed property is also required: the schema is exact-match.
        missing = allowed - keys
        if unknown:
            raise ValueError("unsupported telemetry property")
        if missing:
            raise ValueError("missing telemetry property")

        props = self.properties
        if "period" in props and props["period"] not in _PERIODS:
            raise ValueError("invalid period")
        if "source" in props and props["source"] not in _SOURCES:
            raise ValueError("invalid source")
        if "query_kind" in props and props["query_kind"] not in _QUERY_KINDS:
            raise ValueError("invalid query kind")
        if "active_filters" in props and not isinstance(props["active_filters"], bool):
            raise ValueError("active_filters must be boolean")
        if "duration_ms" in props:
            value = props["duration_ms"]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3_600_000:
                raise ValueError("invalid duration")
        if "state" in props and props["state"] not in _DISMISS_STATES:
            raise ValueError("invalid state")
        return self


@router.post("/events", status_code=202)
async def record_event(body: TelemetryEvent,
                       user=Depends(get_current_user), db=Depends(get_db)):
    # `user` is deliberately used only as an authentication gate. Persisting it
    # would turn aggregate product telemetry into wallet-linked behavior data.
    del user
    session_id = str(body.session_id)
    async with db.transaction(write=True) as tx:
        await enforce_product_event_limits(tx, session_id)
        await tx.execute(
            "INSERT INTO product_events(id,session_id,event_name,properties_json,ts) "
            "VALUES(?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                session_id,
                body.event_name,
                json.dumps(body.properties, sort_keys=True, separators=(",", ":")),
                now_iso(),
            ),
        )
    return {"accepted": True}
