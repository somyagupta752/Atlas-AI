"""The global LLM budget of a patch execution: model requests, model time and cost.

Per-attempt limits (one timeout per call) do not bound the total: three
individually acceptable attempts, each with a repair call, can still add up
to more than intended. ``LLMBudget`` bounds the sum.

Protocol for every model call:

1. ``reserve``: before the call. Refused when the request count is reached,
   when less than ``MIN_CALL_SECONDS`` of model time remain, or when the
   call's worst-case cost (estimated input tokens at the highest input rate
   plus the full output token limit) exceeds the remaining money. Reserved
   cost and time count as spent until settled, so two concurrent calls cannot
   both claim the same remainder. The granted timeout is the per-call timeout
   clipped to the remaining model time.
2. ``settle``: after the call, with the actual cost from the usage the provider
   reported. The reservation is released and the actual amount charged. If
   the actual cost exceeds the estimate, the call is flagged
   (``cost_estimate_exceeded``): the money is spent, and later calls see the
   smaller remainder. A call that failed without usage keeps its full
   reservation as spent, because the provider may have billed it.

A cost cap with an unknown model price refuses to run: an unpriced call
cannot be bounded. A budget can have a ``parent`` (for example one budget for
a whole benchmark run); a reservation must fit both.
"""

import json
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.evaluation import PRICING
from app.agents.llm import LLMUsage

MIN_CALL_SECONDS = 1.0
# Conservative: source code is often 3-4 characters per token. Underestimates
# are caught after the call (cost_estimate_exceeded).
CHARS_PER_TOKEN = 2.5


class LLMBudgetExhaustedError(Exception):
    """The next call does not fit. ``limit`` is "requests", "seconds" or "cost"."""

    def __init__(self, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit


class LLMPricingUnknownError(Exception):
    """A cost cap is set, but the model's price is not known, so its cost cannot be bounded."""


def model_prices(llm: Any, model: str | None = None) -> dict[str, float] | None:
    """Per-million-token prices: the provider's own declaration, else the pricing table."""
    model = model or getattr(llm, "model", None)
    declared = getattr(llm, "pricing", None)
    if declared is not None and model == getattr(llm, "model", None):
        return declared
    return PRICING.get(model or "")


def estimate_input_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    chars = len(json.dumps(messages, default=str)) + len(json.dumps(tools, default=str))
    return math.ceil(chars / CHARS_PER_TOKEN)


def worst_case_cost(prices: dict[str, float], input_tokens: int, max_output_tokens: int) -> float:
    input_rate = max(prices["input"], prices.get("cache_write", prices["input"]))
    return (input_tokens * input_rate + max_output_tokens * prices["output"]) / 1_000_000


def usage_cost(prices: dict[str, float], usage: LLMUsage) -> float:
    return (
        usage.input_tokens * prices["input"]
        + usage.output_tokens * prices["output"]
        + usage.cache_read_input_tokens * prices.get("cache_read", prices["input"])
        + usage.cache_creation_input_tokens * prices.get("cache_write", prices["input"])
    ) / 1_000_000


@dataclass
class Reservation:
    id: str
    label: str
    cost: float
    seconds: float  # also the timeout granted to the call
    clipped: bool  # the timeout was cut short by the remaining model time
    started: float
    parent: "Reservation | None" = None
    settled: bool = False


@dataclass
class LLMBudget:
    max_requests: int
    max_seconds: float
    max_cost_usd: float | None
    parent: "LLMBudget | None" = None
    clock: Callable[[], float] = time.monotonic
    requests: int = 0
    seconds_spent: float = 0.0
    cost_spent: float = 0.0
    reserved_cost: float = 0.0
    reserved_seconds: float = 0.0
    exhausted_by: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_limits(cls, limits: Any, parent: "LLMBudget | None" = None) -> "LLMBudget":
        return cls(max_requests=limits.max_llm_requests, max_seconds=limits.max_llm_seconds,
                   max_cost_usd=limits.max_llm_cost_usd, parent=parent)

    def remaining_cost(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return self.max_cost_usd - self.cost_spent - self.reserved_cost

    def reserve(self, *, estimated_cost: float | None, timeout: float, label: str = "") -> Reservation:
        """Claim one request, model time and the worst-case cost. Raises ``LLMBudgetExhaustedError``."""
        with self._lock:
            if self.requests >= self.max_requests:
                self._exhaust("requests", f"the limit of {self.max_requests} model requests was reached")
            remaining_seconds = self.max_seconds - self.seconds_spent - self.reserved_seconds
            if remaining_seconds < MIN_CALL_SECONDS:
                self._exhaust("seconds", f"the model time budget of {self.max_seconds:.0f} s is spent")
            cost = 0.0
            if self.max_cost_usd is not None:
                if estimated_cost is None:
                    raise LLMPricingUnknownError("A cost cap is set, but the model's price is unknown")
                remaining = self.max_cost_usd - self.cost_spent - self.reserved_cost
                if estimated_cost > remaining:
                    self._exhaust(
                        "cost",
                        f"the next model call could cost up to ${estimated_cost:.4f}, and "
                        f"${max(remaining, 0):.4f} of the ${self.max_cost_usd:.2f} budget remains",
                    )
                cost = estimated_cost
            elif estimated_cost is not None:
                cost = estimated_cost
            granted = min(timeout, remaining_seconds)
            reservation = Reservation(str(uuid.uuid4()), label, cost, granted, granted < timeout, self.clock())
            self.requests += 1
            self.reserved_cost += cost if self.max_cost_usd is not None else 0.0
            self.reserved_seconds += granted
        if self.parent is not None:
            try:
                reservation.parent = self.parent.reserve(estimated_cost=estimated_cost, timeout=granted, label=label)
            except BaseException:
                self._release(reservation)
                raise
            if reservation.parent.clipped:
                reservation.seconds, reservation.clipped = reservation.parent.seconds, True
        return reservation

    def settle(self, reservation: Reservation, *, actual_cost: float | None, outcome: str,
               model: str | None = None) -> dict[str, Any]:
        """Release the reservation and charge what the call really cost (or the reservation, if unknown)."""
        elapsed = self.clock() - reservation.started
        with self._lock:
            if reservation.settled:
                raise ValueError("reservation already settled")
            reservation.settled = True
            if self.max_cost_usd is not None:
                self.reserved_cost -= reservation.cost
            self.reserved_seconds -= reservation.seconds
            self.seconds_spent += elapsed
            charged = actual_cost if actual_cost is not None else reservation.cost
            self.cost_spent += charged
            record = {
                "label": reservation.label,
                "model": model,
                "outcome": outcome,
                "estimated_cost_usd": round(reservation.cost, 6),
                "actual_cost_usd": None if actual_cost is None else round(actual_cost, 6),
                "charged_cost_usd": round(charged, 6),
                "seconds": round(elapsed, 3),
                "timeout_clipped": reservation.clipped,
                "cost_estimate_exceeded": actual_cost is not None and actual_cost > reservation.cost + 1e-9,
            }
            self.calls.append(record)
        if reservation.parent is not None:
            self.parent.settle(reservation.parent, actual_cost=actual_cost, outcome=outcome, model=model)
        return record

    def charge(self, cost: float, label: str) -> None:
        """Spending outside this budget's calls (for example a Task 12 investigation in a benchmark)."""
        with self._lock:
            self.cost_spent += cost
            self.calls.append({"label": label, "outcome": "external", "charged_cost_usd": round(cost, 6)})

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_requests": self.max_requests,
                "max_seconds": self.max_seconds,
                "max_cost_usd": self.max_cost_usd,
                "requests": self.requests,
                "seconds_spent": round(self.seconds_spent, 3),
                "cost_spent_usd": round(self.cost_spent, 6),
                "exhausted_by": self.exhausted_by,
                "cost_estimate_exceeded": any(c.get("cost_estimate_exceeded") for c in self.calls),
                "calls": [dict(c) for c in self.calls],
            }

    def _exhaust(self, limit: str, message: str) -> None:
        self.exhausted_by = limit
        raise LLMBudgetExhaustedError(limit, message)

    def _release(self, reservation: Reservation) -> None:
        with self._lock:
            self.requests -= 1
            if self.max_cost_usd is not None:
                self.reserved_cost -= reservation.cost
            self.reserved_seconds -= reservation.seconds
