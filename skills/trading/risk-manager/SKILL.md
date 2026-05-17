---
name: risk-manager
description: "Role 4 — Risk Manager: enforce the ADR-009 12-gate framework on a candidate trade; veto, resize, or allow. Fail-closed."
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, risk, gates, adr-009, role, veto, paper]
    role: risk_manager
    model_tier: T3
    related_skills: [orchestrator, quant, reviewer]
---

# Risk Manager (Role 4)

## Overview

The Risk Manager is the **order-path veto**. Given a candidate trade it runs the
deterministic ADR-009 12-gate pipeline and returns a `GateVerdict`: `ALLOW`,
`RESIZE` (stake adjusted), or a `BLOCK_*` (entry vetoed / cooldown imposed).

This role is **rules, not vibes** (model tier T3). The 12 gates are pure
deterministic functions — the LLM does **not** decide pass/fail. The skill is a
thin wrapper; an LLM is consulted **only** for a genuine novel market condition
not covered by the rule book, and even then the deterministic verdict stands on
any LLM/provider error.

Authority: VETO on the order path (block any entry, reduce any size, force exit
timing). **Cannot** override the Orchestrator's global pause; **cannot** approve
live capital. v0 is **paper-only**.

## Prerequisites

- `hermes-trader` importable (`hermes_trader.risk.risk_manager`).
- A `committee_to_risk.v1` link payload and a `PortfolioState`.

## Inputs

A kanban `task_links` payload validated against `hermes.role_link.committee_to_risk.v1`
(ADR-013A §6.5.2): `trade_id`, `asset`, `direction` (long|short), `stake_pct`,
`candidate_confidence`, `regime`, `intel_v3`, `source_signals`, `paper_only`
(MUST be true in v0). Plus account state — equity, starting equity, the
`shadow_orders` history, and active `blocked_trades` cooldowns.

## How to run

```python
from datetime import datetime, timezone
from hermes_trader.risk.risk_manager import evaluate, block, PortfolioState

verdict = evaluate(
    link_payload,                       # the committee_to_risk.v1 dict
    portfolio=PortfolioState(
        equity=equity, starting_equity=starting_equity,
        shadow_orders=shadow_orders, active_blocks=active_blocks,
    ),
    now_utc=datetime.now(timezone.utc),
)
# verdict.allowed -> the single bit the order path consumes
# verdict.block_to_record -> persist to hermes.blocked_trades if not None
```

`block(asset, gate, duration_s, now_utc=...)` constructs a time-bounded
`BlockRecord`; the caller's storage layer performs the `blocked_trades` INSERT.

## Outputs

`GateVerdict(trade_id, allowed, verdict, gate_name, reason, final_stake_pct,
block_to_record, gate_results, fail_closed)`. `allowed` is True only for
`ALLOW` / `RESIZE`.

## Fail-closed (mandatory)

A risk gate fails **CLOSED** — denies the trade — never a silent ALLOW:

- malformed / missing-field link payload → `BLOCK` (`fail_closed=true`)
- wrong schema, or `paper_only=false` in v0 → `BLOCK`
- pipeline exception → `BLOCK` + escalate to Orchestrator
- LLM/provider error on an edge-case judgement → the deterministic verdict stands

## Forbidden actions

Live order submission · direct exchange/wallet access · schema migration ·
systemd/cron mutation · paid API spend without operator approval · overriding the
Orchestrator global pause · approving a trade the pipeline blocked (the role may
only veto/resize, never un-block).

## Escalate to a human (Allaert via Orchestrator)

Drawdown breaches the maxDD threshold (15%, ADR-009 Amendment A) · correlation
> 0.70 breached · gate bypass attempted · multiple simultaneous gate failures ·
a novel market condition not in the rule book.

## References

- Implementation: `hermes-trader/src/hermes_trader/risk/risk_manager.py`
- Gate engine: `hermes-trader/src/hermes_trader/risk/risk_pipeline.py` (`evaluate_risk_pipeline`)
- v0 build contract: `~/hermes-plan/HERMES_ROLE4_RISK_MANAGER_V0_CONTRACT_2026-05-17.md`
- Architecture: ADR-013A `HERMES_ROLE_ARCHITECTURE_2026-05-16.md` § Role 4
