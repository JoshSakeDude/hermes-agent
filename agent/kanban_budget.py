"""Per-run token budget snapshot and ceiling definitions.

Phase A of the token-ceiling feature — storage and accounting only.
Dispatchers and agent-loop yield checks live in later phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class BudgetSnapshot:
    """A point-in-time snapshot of token usage across categories.

    Designed so cached context stays transparent: ``cache_read_tokens`` is
    exposed separately from ``input_tokens``, never silently merged. The
    ``billable_tokens`` property sums what the provider actually charges
    (input + output + cache_write, excluding cache_read).

    Properties match the semantics of ``CanonicalUsage`` so the two can
    be compared directly when projecting against a ceiling.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    estimated_cost_usd: Optional[Decimal] = None

    @property
    def prompt_tokens(self) -> int:
        """input + cache_read + cache_write (matches CanonicalUsage.prompt_tokens)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        """prompt_tokens + output_tokens (matches CanonicalUsage.total_tokens)."""
        return self.prompt_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """Tokens the provider charges for: input + output + cache_write.

        Excludes cache_read (heavily discounted) and reasoning_tokens
        (subsumed into output for billing in most models).
        """
        return self.input_tokens + self.output_tokens + self.cache_write_tokens

    def __add__(self, other: BudgetSnapshot) -> BudgetSnapshot:
        """Combine two snapshots into one across-sessions sum."""
        if not isinstance(other, BudgetSnapshot):
            return NotImplemented
        cost = None
        if self.estimated_cost_usd is not None and other.estimated_cost_usd is not None:
            cost = self.estimated_cost_usd + other.estimated_cost_usd
        elif self.estimated_cost_usd is not None:
            cost = self.estimated_cost_usd
        elif other.estimated_cost_usd is not None:
            cost = other.estimated_cost_usd
        return BudgetSnapshot(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            request_count=self.request_count + other.request_count,
            estimated_cost_usd=cost,
        )

    @classmethod
    def from_usage(cls, usage, *, estimated_cost_usd=None) -> BudgetSnapshot:
        """Project a ``CanonicalUsage`` into a BudgetSnapshot.

        ``estimated_cost_usd`` is optional — pass a ``CostResult.amount_usd``
        when available, otherwise the snapshot carries None.
        """
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            request_count=usage.request_count,
            estimated_cost_usd=estimated_cost_usd,
        )


__all__ = ["BudgetSnapshot"]