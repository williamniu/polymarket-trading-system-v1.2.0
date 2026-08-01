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

## Approved M3 policy version 1

The user approved these defaults on 2026-08-01. They remain editable before the M3 runtime evidence clock starts. A later change creates a new M3 configuration version and restarts the affected M3 promotion evidence.

| Control | Approved default | Why |
|---|---|---|
| Venues | Polymarket US and Kalshi in paper mode | M1 has not selected a winner |
| Products | Simple binary contracts | Complex combo, leverage and scalar collateral are not needed to validate basic execution |
| First execution type | Marketable limit/IOC behavior | Bounded slippage without pretending a resting order filled |
| Slippage cap | Two instrument ticks | Small, explicit execution tolerance |
| Executable depth credit | 50% of observed depth | Haircut for cancellation and race risk |
| Latency | Measured p95 plus 250 ms processing buffer | Conservative and evidence-derived |
| Resting fill | Trade-through plus queue-ahead volume; touch is never enough | Prevents optimistic maker fills |
| Fees | Published fee plus a separate 1.25x stress case; rebates ignored | Profit cannot depend on a temporary promotion or maker rebate |
| Runtime probe size | Smallest valid whole-contract order | Exercise execution without manufacturing strategy PnL |
| Promotion gate | 168 hours, 600 intents, zero reconciliation errors | Matches the pre-registered evidence discipline of M1/M2 |

## Boundaries that are not preference knobs

- No look-ahead or favorable selection among later books.
- No midpoint or unlimited-depth fills.
- No retroactive evidence, ledger or result edits.
- No missing fee, latency, partial-fill or settlement accounting.
- No strategy or LLM changing its own risk or promotion test.
- No credential, live endpoint or active-service change without its separate approval.
