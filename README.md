# Trellis

A pipeline that turns SEC XBRL filings into fully-traced historical financial
statements, runs structural integrity checks against them, and projects a 5-year
baseline scenario on top — with every number attributable to a specific filing, a
cited analyst override, or an explicitly flagged assumption. Never a silent guess.

```
$ export TRELLIS_USER_AGENT="Trellis/0.1 you@example.com"
$ python scripts/run_forecast.py --cik 320187        # Nike
```

## What this actually is

Most tools that turn SEC data into a forecast trust the raw XBRL blindly: pull
`companyfacts`, trust SEC's own `fy` label, average whatever numbers show up, print a
5-year table. That approach is wrong more often than it looks, because SEC's XBRL data
is genuinely hostile in ways that don't announce themselves:

- **SEC's own `fy` field can mislabel a real number.** Nike's real FY2017 revenue
  ($34.35B) came back tagged `fiscal_year=2019` — correct value, correct period, wrong
  label — because comparative-column republication in later 10-Ks means the *filing*
  year and the *period* year aren't the same thing.
- **Filers switch which XBRL tag they use, mid-history, for the same concept.**
  Nike's inventory tag changed around FY2019; Amazon's capex tag changed around 2016.
  A naive "try tag A, then tag B" fallback silently truncates history the moment tag A
  returns *anything at all*, even three years out of twenty.
- **Not every tag means what it looks like it means.** `LongTermDebtNoncurrent` and
  `LongTermDebt` aren't synonyms — a filer can report both, for the same period,
  representing genuinely different scopes. Treating them as interchangeable aliases
  lets filing recency arbitrarily decide between two different numbers.
- **Some "annual" periods spuriously aren't.** Early-XBRL-era filings (~2008–2019)
  contain quarterly or half-year cumulative figures mislabeled `fp="FY"`. One of these
  wrongly won Nike's FY2014 lookup under a naive "latest date wins" rule; the same bug
  silently deleted most of Costco's FY2018–2019 data before it was caught and fixed.
- **Some filers don't tag what you'd expect at all.** Amazon has no consolidated SG&A
  line (five separate expense categories, no combining tag). J&J's `StockholdersEquity`
  tag returns *zero* entries — it needs the noncontrolling-interest variant instead.

None of these are hypothetical. Every one above is a real bug this project's own git
history found, on live data, and fixed — not a defensive design written in anticipation
of a problem that might exist. That's the actual differentiator here: **the ingestion
layer treats data integrity as the product**, not as plumbing in front of a forecast.

The forecast itself is deliberately simple: trailing-average drivers, straight-line
assumptions, a labeled 5-year scenario. It doesn't need to be clever. What it needs —
and has — is a historical foundation that's actually correct.

## Quick start

```bash
git clone <this repo> && cd trellis
pip install -e ".[dev]"
export TRELLIS_USER_AGENT="YourName/0.1 you@example.com"   # SEC requires this; see docs/NETWORK.md

python scripts/run_forecast.py --cik 320187      # Nike
pytest                                            # 80 tests, fully offline, no network needed
```

`--cik` accepts any SEC CIK number. Five companies are pre-registered with researched,
cited overrides (see below); any other non-financial company runs with zero code
changes, using auto-derived drivers and flagging whatever it can't derive as an
assumption rather than guessing.

## Architecture

```
SEC EDGAR (companyconcept API, per-tag pulls, retry+backoff, rate-limited)
        │  ingest.py — merge tag fallback chains, dedupe restatements, resolve
        │              tag-scope conflicts (alias vs priority merge strategies)
        ▼
Observation(canonical_name, matched_tag, period_start/end, filed, value, ...)
        │  statements.py — key by period_end (not SEC's own fy field), resolve
        │                  fiscal-year-end collisions by duration not recency,
        │                  fill 4 identity-derived gaps, run integrity checks
        ▼
AnnualTable {year: {canonical_field: value}} + provenance (derived/collided/stub flags)
        │  forecast.py — trailing-average drivers with 3-way provenance split
        │                (auto-derived / cited override / flagged assumption)
        ▼
Drivers → project_year() → 5-year forecast, cash-as-plug, two-sided financing
        │  (floor-and-sweep buybacks, revolver draw/paydown, insolvency flag)
        ▼
Self-consistency check + companies.py's industry gate (refuses financial-sector SIC codes)
```

Five modules, ~1,300 lines of source. No framework, no ORM, no database — a JSON cache
of raw API pulls would be the only justified addition to the persistence layer, and
even that's a "would help," not a "needs."

## Design decisions worth knowing about

**Tag fallback chains have two merge strategies, not one.** Most concepts use `alias`
merging: every candidate tag is queried, and for any period two tags both cover, the
most-recently-filed value wins — correct when the tags are true synonyms used at
different times (Nike's inventory tag switch). A few concepts — currently
`long_term_debt` — use `priority` merging instead: the higher-priority tag wins any
period it covers *at all*, regardless of which tag was filed more recently, because
`LongTermDebtNoncurrent` and `LongTermDebt` represent different scopes, not different
eras of the same fact. Filing recency is the right tiebreaker for a restatement of one
concept; it's the wrong tiebreaker for choosing between two different concepts.

**Fiscal years are keyed by `period_end`, not by SEC's own `fy` field, and the
authoritative period for a given calendar year is chosen by duration closeness to 365
days, not by which date is latest.** Both fixes came directly from bugs found on live
data (see above). The second one matters even for companies that have never changed
their fiscal year-end: early-XBRL-era mislabeled quarterly data can *look* like a
competing annual period, and only duration — not date — tells them apart.

**Every derived value tracks its own provenance, three ways: Demonstrated (auto-derived
from tagged data), Sourced-override (an analyst-researched, cited value — e.g. Nike's
interest rate is sourced from their actual FY2020 debt schedule, not estimated), or
Assumed (explicitly flagged, never silent).** No driver defaults to zero without
saying so — a real bug found on live Costco data: `gross_margin` silently averaged to
0.0 with no warning at all, producing a forecast with five years of losses that still
printed "reconciled cleanly."

**The self-consistency check is an implementation invariant, not economic validation,
and the CLI says so on every run.** `reconcile_forecast_year` checks that the
cash-as-plug balance sheet agrees with what the cash-flow statement independently
implies — but both are built from the same roll-forwards, so it's structurally
incapable of catching an economically absurd scenario (it caught a real equity-roll
algebra bug during development; it cannot catch unbounded negative cash). The
`insolvent` flag and the revolver-limit mechanism are what actually can fail on
economics — and only once a real, cited credit-facility size is supplied.

**A floor-and-sweep capital-return model, not a fixed percentage.** Cash above a
derived minimum (the *lowest* historical cash/revenue ratio, not the average — a floor
should reflect the most conservative cushion actually maintained) sweeps to buybacks;
a shortfall draws on a revolver, unbounded by default and honestly disclosed as such,
bounded once a real credit-facility limit is researched and cited.

**Dividends use a median, not a mean, specifically because a mean isn't robust to
lumpy payments.** Costco's dividend series contains real special dividends roughly
every 2.75 years — a plain average payout ratio came back 62% (badly distorted); the
median came back 27%, in line with Costco's actual regular dividend.

**The industry gate is a feature, not a missing one.** This schema assumes a
Revenue → COGS → SG&A → Operating Income waterfall and an inventory/AR/AP/PP&E balance
sheet — neither exists in recognizable form for banks, insurers, or REITs. Rather than
silently produce a complete-looking forecast built on numbers that don't mean what
they'd appear to, the pipeline checks SIC code first and refuses (with `--force`
available for anyone who wants to see the garbage on purpose).

## Companies validated live, and what each one actually found

| Company | Industry | What broke first (and got fixed) |
|---|---|---|
| Nike (320187) | Footwear/apparel | The `fy`-mislabeling bug; the inventory tag switch; the duration-vs-recency period-keying bug |
| Costco (909832) | Warehouse retail | Silent-zero `gross_margin`; special-dividend contamination (fixed via median) |
| Amazon (1018724) | E-commerce/cloud | No consolidated SG&A tag (derived via `gross_profit − operating_income` instead); real revolver draws in the forecast under its actual capex intensity |
| Apple (320193) | Consumer electronics | Zero missing tags, zero derived gaps — the cleanest validation of the five |
| Johnson & Johnson (200406) | Pharma/healthcare | `StockholdersEquity` returns zero entries (needs the NCI variant); `dividends_paid` needed a non-standard tag despite being a 60-year dividend aristocrat |

Five industries, five different failure modes, each one found on real data and closed
with a general fix — not a company-specific patch. J&J in particular was added with
**zero changes to the pipeline itself**, purely to test whether the architecture
actually generalizes; it did, on the first real attempt, modulo the two tag gaps above.

## Known limitations — stated plainly, not discovered by a reader

- **Regular vs. special dividends aren't separated.** The median fix reduces the
  distortion; it doesn't eliminate it. Doing this properly needs quarter-level parsing,
  a deliberately out-of-scope decision, not an oversight.
- **No per-share or share-count tracking.** Buybacks reduce cash and equity correctly;
  there's no mechanism to say what that means for EPS or share count.
- **Revolver draws don't accrue their own interest yet** — a disclosed simplification,
  printed as a note whenever a draw occurs.
- **A fiscal-year-boundary edge case for J&J is flagged, not fully resolved.** J&J's
  "nearest Sunday to Dec 31" convention can put two genuine ~365-day fiscal years in the
  same calendar-year bucket in some years (confirmed mechanism: Dec 31, 2016 was a
  Saturday, Dec 31, 2017 was a Sunday, so FY2016 and FY2017 could both plausibly land in
  bucket "2017"). Whether this is currently causing real data loss for J&J specifically
  hasn't been independently confirmed against live data — flagged in the company
  registry rather than papered over with an unverified fix.
- **No backtest harness.** The forecast's own historical accuracy (e.g. "would 2021's
  drivers have predicted 2025's actuals") isn't measured anywhere yet — the single
  highest-value addition not yet built.
- **No provenance object below the `Observation` level.** Once a value enters the
  annual table it's a bare float; the fact it came from a specific tag/accession/filing
  is only preserved at the point of ingestion, not threaded through to the forecast.
- **Financial-sector companies are out of scope by design.** See the industry gate
  above — this isn't a gap to close, it's a different project.

## Testing philosophy

Every hand-verifiable fixture in this test suite was worked out **by hand, on paper,
before the code was run against it** — not derived by running the code and asserting
whatever it happened to output. That distinction is the whole point: testing code
against its own output can't catch a bug that's internally consistent but wrong; it
only catches a bug that disagrees with itself. Testing against independently-computed
values catches both. Several tests exist specifically because a hand-verified fixture
caught a real algebra error before it shipped (an equity-roll formula that canceled to
zero algebraically, a debt-repayment figure that didn't match between the balance
sheet and the cash flow statement) — not because a linter or a type checker found them.

80 tests, fully offline (synthetic fixtures shaped like real API responses — no network
needed to run the suite), organized as: one clean fixture + one deliberately broken
fixture per structural check, a regression test for every real bug found on live data,
and hand-computed expected values wherever the arithmetic isn't trivial.

## Adding a new company

```bash
python scripts/run_forecast.py --cik <any CIK>
```

Works immediately for any non-financial company — no registry entry required. A
registry entry (`src/trellis/companies.py`) is worth adding once you've researched a
company-specific override (a real interest rate from an actual debt schedule, a real
credit-facility limit from a 10-Q) — every override requires a citation with enough
detail that someone else could go verify it, enforced by a test that fails on an empty
or vague source string.

## What's next

Roughly in priority order: a backtest harness (freeze drivers at year Y−5, project
forward, compare to actuals already in the pulled data — the single highest-leverage
addition, since it converts "this forecast is a labeled scenario" from a disclaimer
into a measured, falsifiable claim); a JSON/CSV output artifact per run (currently
terminal-only); resolving the J&J fiscal-boundary question with actual live-data
verification rather than a flagged hypothesis; a small on-disk cache of raw API pulls
(speeds iteration, reduces load on SEC's API, and would make the test fixtures
regenerable from real snapshots instead of hand-built).

Explicitly not planned: a DCF or valuation module (false precision on top of an honest
extrapolator), a web UI (the value is the pipeline, not a dashboard), an ML forecasting
layer (destroys the one property — every number traceable to a filing or a citation —
that makes this project different from the median GitHub XBRL scraper), or support for
financial-sector companies (a different schema, a different project).
