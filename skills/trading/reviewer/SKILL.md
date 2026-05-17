---
name: reviewer
description: "Role 5 — Reviewer / Auditor: critique closed-trade trajectories, score quality, and hold the institutional veto on arm promotion. Supervised v0."
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, review, audit, improver-loop, role, veto, paper]
    role: reviewer
    model_tier: T2
    related_skills: [orchestrator, risk-manager, quant]
---

# Reviewer / Auditor (Role 5)

## Overview

The Reviewer is the **cognitive owner of the Improver loop**. It critiques
closed-trade trajectories, detects prompt drift, scores skill confidence, and
holds an **institutional veto** on arm promotion: no arm promotes past the
paper gate without a Reviewer evidence-quality sign-off.

This role is **HIGH RISK** — ADR-013A §6.3 #2, *"Reviewer produces noise, not
signal."* v0 therefore runs in **supervised mode**: the first 50 reflections are
written for human spot-check, and the Reviewer may not propose a prompt change
autonomously until it has ≥50 spot-checked reflections with a **>60 %** success
rate. The institutional arm-promotion veto, by contrast, is active from
reflection 1 — a veto fails closed (it withholds a sign-off, never grants one).

Authority: **ADVISORY** on prompt/skill changes (the Orchestrator approves) +
**INSTITUTIONAL VETO** on arm promotion. **Cannot** override a Risk Manager veto
or the Orchestrator global pause; **cannot** self-apply any change.

## Prerequisites

- `hermes-trader` importable (`hermes_trader.learning.reviewer`).
- Trajectory capture (`batch_runner`) + compression (`trajectory_compressor`).
- Arm scorecards from the eval harness (`t_eval_harness`) — the Reviewer
  *consumes* the harness, it does not build it.

## How to run

```python
from hermes_trader.learning.reviewer import (
    critique, review_arm_promotion, emit_review_link, SupervisedModeState,
)

# Post-trajectory critique — the runtime supplies the LLM's findings/summary.
reflection = critique(trajectory, review_id=rid, findings=findings,
                      summary=summary, confidence=confidence)
record = reflection.to_dspy_example()   # store DSPy-consumable from day 1

# Institutional veto on an arm promotion.
findings = review_arm_promotion(arm_id, scorecard, review_id=rid)
link = emit_review_link(findings)        # reviewer_to_orchestrator.v1 payload
```

## Outputs

- `Reflection` — structured critique; `to_dspy_example()` serialises it
  DSPy-consumable (`metric` filled later by human spot-check).
- `ReviewFindings` → `hermes.role_link.reviewer_to_orchestrator.v1` link with
  `mandatory_gate=true` (the Orchestrator cannot `no_action`) and `signoff`.
- `PromptDelta` — a prompt-change *proposal*, always `escalated` in v0.

## Fail-closed (mandatory)

A Reviewer fails **closed** — it withholds, never silently approves:

- malformed trajectory / missing outcome data → `no_action`
- model/provider error → `no_action` + escalate; the deterministic verdict stands
- confidence < 0.70 → auto-escalate to Allaert (ADR §6.5.3)
- cannot critique reliably → **withhold** the arm-promotion sign-off (promotion blocked)

## Forbidden actions

Autonomous prompt promotion · approving an arm promotion without evidence ·
editing any role SOUL · live order submission · exchange/wallet access · schema
migration · systemd/cron mutation · overriding a Risk Manager veto or the
Orchestrator global pause · paid API spend without operator approval.

## Escalate to a human (Allaert via Orchestrator)

Confidence < 0.70 · a prompt change touching a risk gate (Risk Manager +
Orchestrator dual-approval) · a skill flagged for deprecation · a learning loop
stuck >5× · every one of the first 50 reflections.

## References

- Implementation: `hermes-trader/src/hermes_trader/learning/reviewer.py`
- v0 build contract: `~/hermes-plan/HERMES_ROLE5_REVIEWER_V0_CONTRACT_2026-05-17.md`
- SOUL (draft, profile-collision decision pending): `~/hermes-plan/HERMES_ROLE5_REVIEWER_SOUL_2026-05-17.md`
- Architecture: ADR-013A `HERMES_ROLE_ARCHITECTURE_2026-05-16.md` § Role 5
