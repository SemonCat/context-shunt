"""Bounded, content-free persisted attempt observations and configured-rate reporting.

Identity digests retain equality without retaining arbitrary host strings. Pricing uses
only confirmed/resolved identities, never the requested model. Reports cover one stats
page, not the session lifetime; pruned and pre-upgrade observations remain unknown.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .provenance import Attribution, ModelIdentity
from .provider import CallIdentity, normalize_usage

MAX_ATTEMPTS = 256
MAX_OPERATIONS = 1024
COMPONENTS = (
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)


def identity_key(identity: ModelIdentity) -> str | None:
    if not isinstance(identity, ModelIdentity):
        return None
    if not isinstance(identity.provider, str) or not isinstance(identity.model, str):
        return None
    if not identity.provider or not identity.model:
        return None
    return hashlib.sha256(
        json.dumps([identity.provider, identity.model], separators=(",", ":")).encode()
    ).hexdigest()


def observations(records: tuple, count: int) -> list[dict]:
    result = []
    for record in records[: min(count, MAX_ATTEMPTS)]:
        if not isinstance(record, CallIdentity):
            record = CallIdentity()
        usage = asdict(normalize_usage(record.usage)[0])
        result.append(
            {
                "requested": identity_key(record.requested),
                "resolved": identity_key(record.resolved),
                "reported": identity_key(record.reported),
                "attribution": record.attribution.value
                if isinstance(record.attribution, Attribution)
                else "unknown",
                "usage": usage,
            }
        )
    return result
