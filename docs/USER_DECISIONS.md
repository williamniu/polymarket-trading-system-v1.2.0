# User decision register

This file keeps the user inside the construction loop. A system default is not a user decision until the user approves it.

## Change protocol

Every material proposal must state:

1. what the user can change;
2. the recommended value and why;
3. the effect of changing it;
4. which evidence or promotion gate restarts;
5. the approval required before execution.

User policy may change prospectively. Immutable market evidence, historical orders, fills, settlements, and old test results may never be rewritten. A changed policy receives a new version, and later evidence records that version.

## Whole-system control map

| Stage | User-owned choices | Evidence-owned facts |
|---|---|---|
| M0 | Capital tiers; per-trade, event, theme, portfolio, daily, rolling and drawdown limits; prohibited actions | Losses, exposure and whether a hard limit was crossed |
| M1 | Eligible jurisdictions, venues, market families and quality thresholds | API availability, quotes, depth, rules, latency and legal/account eligibility |
| M2 | Collection cadence, alert severity, retention, backup frequency and downtime tolerance | Heartbeats, failures, database integrity and completed runtime cycles |
| M3 | Order style, slippage cap, latency percentile, depth credit, queue conservatism, fee stress and promotion sample | Observed books/trades, official fee rules, possible fills, settlements and reconciliation |
| M4 | Signal families, expert sources, markets, holding horizons and explainability requirements | Time-aligned out-of-sample predictive value after execution costs |
| M5 | Return/drawdown/stability utility and champion/challenger promotion thresholds | Replayed and shadow results under pre-registered tests |
| M6 | Allowed parameter/code edit surface, experiment budget and rollback boundary | Whether a challenger passed the unchanged harness |
| M7 | Venue, live tranche, funding, stop lines and profit-withdrawal policy | Actual fills, balances, losses, compliance and operational incidents |

## Approved M0 policy version 1

| Control | Current value | User may revise? | Change consequence |
|---|---:|---|---|
| Maximum loss per trade | 2% | Yes, with approval | New M0 policy version; rerun risk tests and segment later strategy evidence |
| Maximum event risk | 5% | Yes, with approval | Same |
| Maximum theme risk | 10% | Yes, with approval | Same |
| Maximum total worst-case loss | 20% | Yes, with approval | Same |
| Daily hard stop | 5% | Yes, with approval | Same |
| Rolling three-day hard stop | 10% | Yes, with approval | Same |
| High-watermark drawdown freeze | 20% | Yes, with approval | Same |
| Paper capital | $5,000 | Yes, with approval | Requires account reconciliation and an explicit M2 migration decision |
| First live tranche | $200-$300 | Yes, only at M7 | Requires all earlier gates and fresh live approval |

## Approved M3 runtime policy version 2

The user approved the M3.5 runtime connection on 2026-08-01. Evidence-changing settings are locked once the M3 clock starts. Changing one later requires an approved configuration version and a new M3 evidence segment; history is retained.

| Control you can edit | Approved default | Consequence of changing it after the clock starts |
|---|---|---|
| Runtime probe switch | Enabled; may be disabled immediately | Safe stop only; does not rewrite or reset evidence, and skipped cycles do not count as intents |
| Venue order | Polymarket US, then Kalshi, alternating | New configuration version and M3 evidence segment |
| Products | Simple binary contracts only | Adding scalar, combo, leverage or fractional products requires new implementation and approval |
| Execution type | Marketable limit behavior | Resting execution requires complete trade evidence and a new evidence segment |
| Slippage cap | Two instrument ticks | New configuration version and M3 evidence segment |
| Executable depth credit | 50% of displayed depth | New configuration version and M3 evidence segment; higher values make fills more optimistic |
| Latency | Point-request p95 plus 250 ms | New configuration version and M3 evidence segment; p99 is a valid more-conservative alternative |
| Fee stress | 1.25x published fee, rebates ignored | New configuration version and M3 evidence segment; lowering below 1x is prohibited |
| Probe size | One whole contract, enforced in M3.5 | Larger size requires complete per-cycle marking for every open position, approval, a new configuration version and a new M3 evidence segment |
| Outcome | YES | New configuration version and M3 evidence segment; NO uses the same complementary-book validation |
| Minimum time before close/start | 60 minutes | New configuration version and M3 evidence segment; a shorter buffer raises in-play and closure risk |
| Minimum top-quote notional | $10 | New configuration version and M3 evidence segment; lowering it tests thinner books |
| Promotion gate | 168 hours, 600 intents, zero reconciliation errors | New pre-registration and a fresh M3 evidence segment |

The probe is not an alpha strategy. It buys the smallest valid contract and, on the venue's next turn, prioritizes closing an existing probe position. Its PnL measures execution friction, not predictive skill.

## Boundaries that are not preference knobs

- No look-ahead or favorable selection among later books.
- No midpoint or unlimited-depth fills.
- No retroactive evidence, ledger or result edits.
- No missing fee, latency, partial-fill or settlement accounting.
- No strategy or LLM changing its own risk or promotion test.
- No credential, live endpoint or active-service change without its separate approval.
