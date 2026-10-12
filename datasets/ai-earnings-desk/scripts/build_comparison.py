"""Build the cross-company comparison workbook of the AI earnings corpus from SEC XBRL company facts.

usage: python -I build_comparison.py [corpus_dir]
    corpus_dir defaults to the folder above this script (datasets/ai-earnings-desk).

Reads  <corpus>/<TICKER>/xbrl/<TICKER>_companyfacts.json for the 8 companies. No network access.
Writes <corpus>/comparison/ai_earnings_comparison.xlsx  quarterly, per-metric, annual, rankings, companies, definitions
       <corpus>/comparison/ai_earnings_provenance.csv   one row per value, with the XBRL facts behind it
and prints the validation checks: fiscal calendar, quarters vs full year, implausible values, gaps, fallback
tags and revised values. Rules and results are described in comparison/README.md. Needs only openpyxl.

Period rules
- Facts: us-gaap facts from 10-Q, 10-K and their /A amendments. When several filings report the same tag and
  period, the most recently filed value wins, so restated and split-adjusted figures are used.
- Lengths (days, both ends included): quarter 80-100, six months 170-190, nine months 260-285, full year 350-380.
- Calendar quarter of a fiscal period = the calendar quarter holding the period's midpoint. Window: CY2023 Q3 to
  CY2026 Q2; the 4 quarters before it are computed only as the base for year-over-year revenue growth.
- Income-statement and cash-flow items: the fact for exactly the quarter if there is one; otherwise the
  year-to-date total at the quarter end minus the year-to-date total at the previous quarter end of the same
  fiscal year, from the same tag (Q2 = six months - three months, Q3 = nine - six, Q4 = full year - nine months).
  Diluted EPS is never derived this way (per-share amounts do not subtract), so fiscal Q4 EPS is left blank.
- Balance-sheet items: the value at the period end date.
"""
import csv
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

# --- Companies -------------------------------------------------------------------------------------------------
# ticker, name used in the workbook, fiscal year end rule (kind, weekday, month, day); the rule is checked against
# every fiscal year end found in the data.
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
COMPANIES = [
    ("NVDA", "NVIDIA", ("last", SUN, 1, None)),
    ("AMD", "AMD", ("last", SAT, 12, None)),
    ("AVGO", "Broadcom", ("closest", SUN, 10, 31)),
    ("INTC", "Intel", ("last", SAT, 12, None)),
    ("MU", "Micron", ("closest", THU, 8, 31)),
    ("MSFT", "Microsoft", ("fixed", None, 6, 30)),
    ("GOOGL", "Alphabet", ("fixed", None, 12, 31)),
    ("META", "Meta", ("fixed", None, 12, 31)),
]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]
SNAPSHOT = "2026-10-06"  # when the companyfacts JSON files were downloaded

# --- Periods ---------------------------------------------------------------------------------------------------
ONE_DAY = dt.timedelta(days=1)
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}
BANDS = {"quarter": (80, 100), "half": (170, 190), "nine": (260, 285), "year": (350, 380)}
YTD_NAMES = {"quarter": "three months", "half": "six months", "nine": "nine months", "year": "full year"}


def length(start, end):
    return (end - start).days + 1


def band(start, end):
    days = length(start, end)
    return next((name for name, (lo, hi) in BANDS.items() if lo <= days <= hi), None)


def cq_of(day):
    return day.year, (day.month - 1) // 3 + 1


def cq_shift(cq, n):
    i = cq[0] * 4 + cq[1] - 1 + n
    return i // 4, i % 4 + 1


def cq_label(cq):
    return f"CY{cq[0]} Q{cq[1]}"


def cq_last_day(cq):
    year, q = cq_shift(cq, 1)
    return dt.date(year, q * 3 - 2, 1) - ONE_DAY


WINDOW = [cq_shift((2023, 3), i) for i in range(12)]  # CY2023 Q3 .. CY2026 Q2
BASE = [cq_shift(WINDOW[0], i - 4) for i in range(4)]  # CY2022 Q3 .. CY2023 Q2, for YoY growth only
LATEST = WINDOW[-1]
SPAN = f"{cq_label(WINDOW[0])} to {cq_label(WINDOW[-1])}"

# --- Metrics ---------------------------------------------------------------------------------------------------
REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet")
CALENDAR_TAGS = REVENUE_TAGS + ("OperatingIncomeLoss", "NetIncomeLoss", "ProfitLoss")
NCI_TAGS = ("NetIncomeLossAttributableToNoncontrollingInterest", "MinorityInterest")


@dataclass(frozen=True)
class Metric:
    key: str
    header: str           # column header in Quarterly results
    name: str             # used in rankings, notes and definitions
    units: str            # "USD millions", "USD per share" or "%"
    tags: tuple = ()      # tried in this order
    fallback: tuple = ()  # tried after `tags`; not in the main tag list, so every use is reported
    instant: bool = False
    unit: str = "USD"     # unit key in companyfacts
    additive: bool = True  # False for per-share amounts: a quarter cannot be derived from year-to-date totals


METRICS = [
    Metric("revenue", "Revenue (USD millions)", "revenue", "USD millions", REVENUE_TAGS),
    Metric("cost", "Cost of revenue (USD millions)", "cost of revenue", "USD millions",
           ("CostOfRevenue", "CostOfGoodsAndServicesSold")),
    Metric("gross_profit", "Gross profit (USD millions)", "gross profit", "USD millions", ("GrossProfit",)),
    Metric("rnd", "R&D expense (USD millions)", "R&D expense", "USD millions",
           ("ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost")),
    Metric("op_income", "Operating income (USD millions)", "operating income", "USD millions", ("OperatingIncomeLoss",)),
    # ProfitLoss includes non-controlling interests; it is tried only for companies that report none (see tags_for)
    Metric("net_income", "Net income (USD millions)", "net income", "USD millions", ("NetIncomeLoss",),
           fallback=("ProfitLoss",)),
    # 10-Ks report only full-year EPS, and full year minus nine months is not a quarter's EPS (share counts change),
    # so fiscal Q4 EPS stays blank; see the Diluted EPS row of Definitions
    Metric("eps", "Diluted EPS (USD)", "diluted EPS", "USD per share", ("EarningsPerShareDiluted",), unit="USD/shares",
           additive=False),
    Metric("ocf", "Operating cash flow (USD millions)", "operating cash flow", "USD millions",
           ("NetCashProvidedByUsedInOperatingActivities",)),
    Metric("capex", "Capital expenditures (USD millions)", "capital expenditures", "USD millions",
           ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")),
    Metric("fcf", "Free cash flow (USD millions)", "free cash flow", "USD millions"),
    Metric("cash", "Cash and cash equivalents at quarter end (USD millions)", "cash and cash equivalents",
           "USD millions", ("CashAndCashEquivalentsAtCarryingValue",),
           fallback=("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",), instant=True),
    Metric("gross_margin", "Gross margin (%)", "gross margin", "%"),
    Metric("op_margin", "Operating margin (%)", "operating margin", "%"),
    Metric("net_margin", "Net margin (%)", "net margin", "%"),
    Metric("rnd_pct", "R&D as % of revenue", "R&D as % of revenue", "%"),
    Metric("growth", "Revenue growth YoY (%)", "revenue growth YoY", "%"),
]
METRIC = {m.key: m for m in METRICS}
FLOWS = ["revenue", "cost", "gross_profit", "rnd", "op_income", "net_income", "ocf", "capex", "fcf"]
RATIOS = [("gross_margin", "gross_profit"), ("op_margin", "op_income"), ("net_margin", "net_income"), ("rnd_pct", "rnd")]
WIDE = [("Revenue", "revenue"), ("Revenue growth YoY", "growth"), ("Gross margin", "gross_margin"),
        ("Operating margin", "op_margin"), ("Net income", "net_income"), ("R&D expense", "rnd"),
        ("Capital expenditures", "capex"), ("Free cash flow", "fcf")]
MILLION = Decimal(1_000_000)


def dec(x):
    return Decimal(str(x))


def display(units, raw):
    """Workbook value: USD millions as whole numbers, per-share amounts to the cent, percentages to one decimal."""
    if units == "USD millions":
        return int((raw / MILLION).quantize(Decimal(1), ROUND_HALF_UP))
    if units == "USD per share":
        return float(raw.quantize(Decimal("0.01"), ROUND_HALF_UP))
    return float(raw.quantize(Decimal("0.1"), ROUND_HALF_UP))


def text(units, raw):
    v = display(units, raw)
    return f"{v:,}" if units == "USD millions" else f"{v:.2f}" if units == "USD per share" else f"{v:.1f}"


@dataclass
class Value:
    raw: Decimal                               # USD, USD per share, or percent for ratios
    tag: str = ""                              # XBRL tag(s) behind the value
    facts: list = field(default_factory=list)  # the companyfacts entries used
    derived: bool = False
    how: str = ""
    basis: str = ""                            # short description for Definitions (tag name or "derived: ...")


# --- Facts and fiscal calendar ---------------------------------------------------------------------------------
class Facts:
    """One company's us-gaap facts from 10-Q/10-K filings and amendments, indexed by (tag, unit) and period.
    The most recently filed value wins; the first filed value is kept as well, to count later revisions."""

    def __init__(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.cik, self.entity = data["cik"], data["entityName"]
        self.latest, self.first, self.conflicts = defaultdict(dict), defaultdict(dict), []
        for tag, body in data["facts"].get("us-gaap", {}).items():
            for unit, entries in body["units"].items():
                latest, first = self.latest[tag, unit], self.first[tag, unit]
                for f in entries:
                    if f["form"] not in FORMS:
                        continue
                    key, rank = (f.get("start"), f["end"]), (f["filed"], f["accn"])
                    old = latest.get(key)
                    if old and old["accn"] == f["accn"] and old["val"] != f["val"]:
                        self.conflicts.append((tag, key, old["val"], f["val"], f["accn"]))
                    if old is None or rank > (old["filed"], old["accn"]):
                        latest[key] = f
                    if key not in first or rank < (first[key]["filed"], first[key]["accn"]):
                        first[key] = f

    def get(self, tag, unit, start, end):
        return self.latest.get((tag, unit), {}).get((start.isoformat() if start else None, end.isoformat()))

    def nonzero_since(self, tag, since):
        return any(end >= since and f["val"] for (t, _), periods in self.latest.items() if t == tag
                   for (_, end), f in periods.items())


@dataclass
class FiscalQuarter:
    fy: int
    q: int
    start: dt.date
    end: dt.date
    fy_start: dt.date
    prev_end: dt.date | None  # end of the previous quarter of the same fiscal year; None for Q1

    @property
    def label(self):
        return f"FY{self.fy} Q{self.q}"

    @property
    def cq(self):  # calendar quarter holding the midpoint
        return cq_of(self.start + (self.end - self.start) // 2)

    @property
    def ytd(self):
        return (self.fy_start, self.prev_end) if self.prev_end else None


@dataclass
class FiscalYear:
    fy: int
    start: dt.date
    end: dt.date | None  # None while the year is in progress
    quarters: list

    @property
    def label(self):
        return f"FY{self.fy}"


def fiscal_calendar(facts):
    """Fiscal years and quarters from the reporting periods of the main income-statement tags. A fiscal year starts
    where year-to-date periods (six months, nine months, full year) start; its quarters end where the three-month,
    six-month, nine-month and full-year periods from that start end. Fiscal year N is the one ending in year N."""
    ends = defaultdict(lambda: defaultdict(set))  # start -> band -> {end}
    for tag in CALENDAR_TAGS:
        for start, end in facts.latest.get((tag, "USD"), {}):
            if start:
                s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
                if b := band(s, e):
                    ends[s][b].add(e)
    starts = {s for s, bands in ends.items() if bands.keys() & {"half", "nine", "year"}}
    # a fiscal year whose first quarter is its only report so far has no year-to-date period yet
    starts |= {e + ONE_DAY for s in list(starts) for e in ends[s].get("year", ())
               if "quarter" in ends.get(e + ONE_DAY, {})}
    years = []
    for s in sorted(starts):
        quarters, prev = [], None
        for q, b in enumerate(BANDS, start=1):
            qs = prev + ONE_DAY if prev else s
            found = ends[s].get(b) or (ends.get(qs, {}).get("quarter") if prev else None)
            if not found:
                break
            if len(found) > 1:
                sys.exit(f"ambiguous {b} periods starting {s}: {sorted(found)}")
            e = next(iter(found))
            if band(qs, e) != "quarter":
                sys.exit(f"quarter {qs}..{e} of the fiscal year starting {s} is {length(qs, e)} days")
            quarters.append(FiscalQuarter(0, q, qs, e, s, prev))
            prev = e
        end = quarters[-1].end if len(quarters) == 4 else None
        fy = (end or s + dt.timedelta(days=364)).year
        for fq in quarters:
            fq.fy = fy
        years.append(FiscalYear(fy, s, end, quarters))
    return years


# --- Values ----------------------------------------------------------------------------------------------------
def flow(facts, metric, tags, start, end, ytd):
    """Total for start..end: the fact for exactly that period or, when ytd = (fiscal year start, previous quarter
    end) is given, the year-to-date total at `end` minus the year-to-date total at the previous quarter end."""
    for tag in tags:
        f = facts.get(tag, metric.unit, start, end)
        if f:
            return Value(dec(f["val"]), tag, [f], basis=tag)
        if ytd and metric.additive:
            fy_start, prev_end = ytd
            cur, prev = facts.get(tag, metric.unit, fy_start, end), facts.get(tag, metric.unit, fy_start, prev_end)
            if cur and prev:
                a, b = dec(cur["val"]), dec(prev["val"])
                how = (f"{YTD_NAMES[band(fy_start, end)]} {fy_start} to {end} ({text(metric.units, a)}) minus "
                       f"{YTD_NAMES[band(fy_start, prev_end)]} {fy_start} to {prev_end} ({text(metric.units, b)})")
                return Value(a - b, tag, [cur, prev], True, how, tag)
    return None


def instant(facts, metric, tags, end):
    for tag in tags:
        f = facts.get(tag, metric.unit, None, end)
        if f:
            return Value(dec(f["val"]), tag, [f], basis=tag)
    return None


def join_tags(*values):
    """'A; B' for the tags behind several values, each tag once."""
    return "; ".join(dict.fromkeys(t for v in values for t in v.tag.split("; ")))


def difference(a, b, name_a, name_b, units="USD millions"):
    how = f"{name_a} ({text(units, a.raw)}) minus {name_b} ({text(units, b.raw)})"
    if a.derived or b.derived:
        how += "; inputs derived as in their own rows"
    return Value(a.raw - b.raw, join_tags(a, b), a.facts + b.facts, True, how,
                 f"derived: {name_a} minus {name_b}")


def ratio(num, den, name_num, name_den="revenue"):
    how = f"{name_num} ({text('USD millions', num.raw)}) ÷ {name_den} ({text('USD millions', den.raw)}) × 100"
    return Value(num.raw / den.raw * 100, join_tags(num, den), num.facts + den.facts, True, how, "derived")


def growth(cur, base, cur_name, base_name):
    how = (f"revenue {cur_name} ({text('USD millions', cur.raw)}) ÷ revenue {base_name} "
           f"({text('USD millions', base.raw)}), minus 1, × 100")
    return Value((cur.raw / base.raw - 1) * 100, join_tags(cur, base), cur.facts + base.facts, True, how, "derived")


def period_values(facts, tags, start, end, ytd):
    """Every metric except revenue growth for one fiscal quarter (ytd given) or fiscal year (ytd None)."""
    v = {}
    for m in METRICS:
        if m.instant:
            v[m.key] = instant(facts, m, tags[m.key], end)
        elif m.tags:
            v[m.key] = flow(facts, m, tags[m.key], start, end, ytd)
    if v["gross_profit"] is None and v["revenue"] and v["cost"]:  # Alphabet and Meta report no gross profit
        v["gross_profit"] = difference(v["revenue"], v["cost"], "revenue", "cost of revenue")
    v["fcf"] = difference(v["ocf"], v["capex"], "operating cash flow", "capital expenditures") \
        if v["ocf"] and v["capex"] else None
    for key, num in RATIOS:
        ok = v["revenue"] and v[num] and v["revenue"].raw > 0
        v[key] = ratio(v[num], v["revenue"], METRIC[num].name) if ok else None
    v["growth"] = None
    return v


def tags_for(metric, facts):
    tags = list(metric.tags)
    for tag in metric.fallback:
        if tag == "ProfitLoss" and any(facts.nonzero_since(t, "2022-01-01") for t in NCI_TAGS):
            continue  # the company has non-controlling interests, so ProfitLoss is not net income attributable to it
        tags.append(tag)
    return tags


@dataclass
class Company:
    ticker: str
    name: str
    rule: tuple
    facts: Facts
    years: list
    by_cq: dict    # calendar quarter -> FiscalQuarter
    quarters: dict  # calendar quarter -> {metric key: Value | None}
    annual: list   # [(FiscalYear, values)] for fiscal years whose Q4 falls in the window


def build_company(corpus, ticker, name, rule):
    facts = Facts(corpus / ticker / "xbrl" / f"{ticker}_companyfacts.json")
    years = fiscal_calendar(facts)
    by_cq = {}
    for fy in years:
        for fq in fy.quarters:
            if fq.cq in by_cq:
                sys.exit(f"{ticker}: {by_cq[fq.cq].label} and {fq.label} both fall in {cq_label(fq.cq)}")
            by_cq[fq.cq] = fq
    tags = {m.key: tags_for(m, facts) for m in METRICS if m.tags}
    quarters = {cq: period_values(facts, tags, by_cq[cq].start, by_cq[cq].end, by_cq[cq].ytd)
                for cq in BASE + WINDOW if cq in by_cq}
    for cq in WINDOW:
        cur, base = quarters.get(cq, {}).get("revenue"), quarters.get(cq_shift(cq, -4), {}).get("revenue")
        if cur and base and base.raw > 0:
            quarters[cq]["growth"] = growth(cur, base, cq_label(cq), cq_label(cq_shift(cq, -4)))
    annual = []
    for i, fy in enumerate(years):
        if fy.end and fy.quarters[3].cq in WINDOW:
            v = period_values(facts, tags, fy.start, fy.end, None)
            prior = years[i - 1] if i and years[i - 1].end == fy.start - ONE_DAY else None
            base = flow(facts, METRIC["revenue"], tags["revenue"], prior.start, prior.end, None) if prior else None
            if v["revenue"] and base and base.raw > 0:
                v["growth"] = growth(v["revenue"], base, fy.label, prior.label)
            annual.append((fy, v))
    return Company(ticker, name, rule, facts, years, by_cq, quarters, annual)


# --- Text for the Companies and Definitions sheets -------------------------------------------------------------
def fy_end_text(rule):
    kind, wd, month, day = rule
    if kind == "fixed":
        return f"{MONTH_NAMES[month]} {day}"
    if kind == "last":
        return f"Last {DAY_NAMES[wd]} in {MONTH_NAMES[month]} (52/53-week year)"
    return f"{DAY_NAMES[wd]} closest to {MONTH_NAMES[month]} {day} (52/53-week year)"


def fy_end_ok(rule, day):
    kind, wd, month, d = rule
    if kind == "fixed":
        return (day.month, day.day) == (month, d)
    if day.weekday() != wd:
        return False
    if kind == "last":
        return day.month == month and (day + dt.timedelta(days=7)).month != month
    return abs((day - dt.date(day.year, month, d)).days) <= 3


def mapping_text(c):
    """How the company's fiscal quarters line up with calendar quarters, worked out from the window quarters."""
    fqs = [c.by_cq[cq] for cq in WINDOW if cq in c.by_cq]
    lag = statistics.median((fq.end - cq_last_day(fq.cq)).days for fq in fqs)
    if lag == 0:
        when = "Fiscal quarters end on calendar quarter ends."
    elif abs(lag) <= 7:
        when = "Fiscal quarters end within a week of calendar quarter ends."
    else:
        when = (f"Each fiscal quarter ends about {round(abs(lag) / 7)} weeks {'after' if lag > 0 else 'before'} the "
                f"end of the calendar quarter it is assigned to.")
    pairs = {}
    for fq in fqs:
        pairs.setdefault(fq.q, set()).add((fq.cq[0] - fq.fy, fq.cq[1]))
    if any(len(p) != 1 for p in pairs.values()):
        sys.exit(f"{c.ticker}: fiscal quarters map to calendar quarters inconsistently: {pairs}")
    parts = []
    for q in sorted(pairs):
        (offset, cal_q), = pairs[q]
        parts.append(f"Q{q} = CY N{'' if offset == 0 else f'{offset:+d}'.replace('-', '−')} Q{cal_q}")
    example = c.by_cq[LATEST]
    return (f"{when} For fiscal year N: {', '.join(parts)} (e.g. {example.label} = {cq_label(LATEST)}, "
            f"{example.start} to {example.end}).")


def periods(c, key):
    """(position, label, value) for the company's window quarters and fiscal years; position orders runs."""
    out = [(("quarter", i), cq_label(cq), c.quarters[cq].get(key)) for i, cq in enumerate(WINDOW)]
    return out + [(("year", i), fy.label, v.get(key)) for i, (fy, v) in enumerate(c.annual)]


def span(items):
    """[(position, label)] -> 'CY2023 Q3 to CY2024 Q3, CY2025 Q1, FY2023' (consecutive periods become ranges)."""
    runs = []
    for (kind, i), label in items:
        if runs and runs[-1][0] == kind and runs[-1][1] == i - 1:
            runs[-1][1], runs[-1][3] = i, label
        else:
            runs.append([kind, i, label, label])
    return ", ".join(first if first == last else f"{first} to {last}" for _, _, first, last in runs)


def usage(companies, key):
    """Which tags (or derivations) each company's values use, with period ranges where a company uses several."""
    single, mixed = defaultdict(list), []
    for c in companies:
        by_basis = defaultdict(list)
        for pos, label, value in periods(c, key):
            if value:
                by_basis[value.basis].append((pos, label))
        if len(by_basis) == 1:
            single[next(iter(by_basis))].append(c.name)
        elif by_basis:
            mixed.append(f"{c.name}: " + ", ".join(f"{basis} ({span(items)})" for basis, items in by_basis.items()))
    return ". ".join([f"{basis}: {', '.join(names)}" for basis, names in single.items()] + mixed)


def expected_gap(c, cq, key):
    return key == "eps" and c.by_cq[cq].q == 4  # fiscal Q4 EPS is blank by design


def missing(companies, key):
    """Blank cells other than the fiscal Q4 EPS cells that are blank by design."""
    out = []
    for c in companies:
        gaps = [(pos, label) for pos, label, value in periods(c, key)
                if value is None and not (pos[0] == "quarter" and expected_gap(c, WINDOW[pos[1]], key))]
        if gaps:
            out.append(f"{c.name}: {span(gaps)}")
    return ". ".join(out)


def fallback_uses(companies):
    """(company, metric, [(position, label)]) for every value taken from a tag outside the main tag list."""
    out = []
    for c in companies:
        for m in METRICS:
            used = [(pos, label) for pos, label, value in periods(c, m.key) if value and value.tag in m.fallback]
            if used:
                out.append((c, m, used))
    return out


# --- Workbook --------------------------------------------------------------------------------------------------
FORMATS = {"USD millions": "#,##0", "USD per share": "0.00", "%": "0.0", "percentage points": "0.0", "date": "yyyy-mm-dd"}


def write_sheet(wb, title, headers, rows, formats=None, wide_text=()):
    """Row 1 = header, column A = subject; no merged cells or title rows (shape expected by the Excel Narrator)."""
    ws = wb.create_sheet(title)
    ws.append(headers)
    for row in rows:
        ws.append(row)
    formats = formats or {}
    for i, header in enumerate(headers, start=1):
        col = get_column_letter(i)
        head = ws[f"{col}1"]
        head.font = Font(bold=True)
        head.alignment = Alignment(wrap_text=True, vertical="top")
        longest = max([len(str(c.value)) for c in ws[col][1:] if c.value is not None] or [0])
        for c in ws[col][1:]:
            if header in formats:
                c.number_format = formats[header]
            if header in wide_text:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        width = 60 if header in wide_text else min(max(longest + 2, min(len(header), 24) + 2, 10), 48)
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "B2"
    return ws


def cell(units, value):
    return None if value is None else display(units, value.raw)


def row_notes(fq, v):
    notes = []
    if fq.q == 4:
        notes.append("Fiscal Q4: income-statement and cash-flow figures are full year minus nine months. Diluted EPS "
                     "is blank because the 10-K reports only full-year EPS")
    if length(fq.start, fq.end) > 92:
        notes.append(f"{length(fq.start, fq.end) // 7}-week quarter")
    gaps = [m.name for m in METRICS if v.get(m.key) is None and not (m.key == "eps" and fq.q == 4)]
    if gaps:
        notes.append(f"Not available: {', '.join(gaps)} (see Definitions)")
    return ". ".join(notes) or None


def build_workbook(companies, path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    metric_headers = [m.header for m in METRICS]
    formats = {m.header: FORMATS[m.units] for m in METRICS} | {"Period start": FORMATS["date"], "Period end": FORMATS["date"]}

    rows = []
    for c in companies:
        for cq in WINDOW:
            fq, v = c.by_cq[cq], c.quarters[cq]
            rows.append([f"{c.name} {cq_label(cq)}", c.name, c.ticker, cq_label(cq), fq.label, fq.start, fq.end]
                        + [cell(m.units, v.get(m.key)) for m in METRICS] + [row_notes(fq, v)])
    write_sheet(wb, "Quarterly results", ["Company quarter", "Company", "Ticker", "Calendar quarter", "Fiscal period",
                                          "Period start", "Period end"] + metric_headers + ["Notes"],
                rows, formats, wide_text=("Notes",))

    quarter_headers = [cq_label(cq) for cq in WINDOW]
    for title, key in WIDE:
        m = METRIC[key]
        units = "% change vs the same calendar quarter a year earlier" if key == "growth" else m.units
        rows = [[c.name] + [cell(m.units, c.quarters[cq].get(key)) for cq in WINDOW] + [units] for c in companies]
        write_sheet(wb, title, ["Company"] + quarter_headers + ["Units"], rows,
                    {h: FORMATS[m.units] for h in quarter_headers})

    headers = ["Company fiscal year", "Company", "Ticker", "Fiscal year", "Period start", "Period end"]
    annual_headers = [{"cash": "Cash and cash equivalents at year end (USD millions)",
                       "growth": "Revenue growth vs prior fiscal year (%)"}.get(m.key, m.header) for m in METRICS]
    rows = []
    for c in companies:
        for fy, v in c.annual:
            days = length(fy.start, fy.end)
            note = f"{days // 7}-week fiscal year" if days > 366 else None
            rows.append([f"{c.name} {fy.label}", c.name, c.ticker, fy.label, fy.start, fy.end]
                        + [cell(m.units, v.get(m.key)) for m in METRICS] + [note])
    annual_formats = {h: FORMATS[m.units] for h, m in zip(annual_headers, METRICS)}
    annual_formats |= {"Period start": FORMATS["date"], "Period end": FORMATS["date"]}
    write_sheet(wb, "Annual results", headers + annual_headers + ["Notes"], rows, annual_formats)

    write_sheet(wb, "Rankings", ["Ranking", "Company", "Value", "Runner-up", "Runner-up value", "Units"],
                ranking_rows(companies), wide_text=("Ranking",))

    rows = []
    for c in companies:
        last_end = max(fy.end for fy in c.years if fy.end)
        first, last = c.by_cq[WINDOW[0]], c.by_cq[LATEST]
        rows.append([c.name, c.ticker, c.facts.entity, f"{c.facts.cik:010d}",
                     f"{fy_end_text(c.rule)}, latest full year ended {last_end}", mapping_text(c),
                     f"{first.label} to {last.label}", company_notes(c, companies)])
    write_sheet(wb, "Companies", ["Company", "Ticker", "SEC entity name", "CIK", "Fiscal year end",
                                  "Fiscal quarters vs calendar quarters", "Fiscal quarters in the window", "Notes"],
                rows, wide_text=("Fiscal quarters vs calendar quarters", "Notes"))

    write_sheet(wb, "Definitions", ["Term", "Meaning", "XBRL tags used", "Derivation", "Units"],
                definition_rows(companies), wide_text=("Meaning", "XBRL tags used", "Derivation"))
    wb.properties.title = "AI earnings comparison: 8 companies, CY2023 Q3 to CY2026 Q2"
    wb.properties.creator = "build_comparison.py"
    wb.save(path)
    return wb


def company_notes(c, companies):
    notes = []
    for comp, m, used in fallback_uses(companies):
        if comp is c:
            tag = next(v.tag for v in all_values(c, m.key) if v.tag in m.fallback)
            notes.append(f"{m.name.capitalize()} comes from tag {tag} for {span(used)} (see Definitions)")
    for m in METRICS:  # a company that changed tags within the main tag list
        by_tag = defaultdict(list)
        for pos, label, value in periods(c, m.key):
            if value and value.tag in m.tags:
                by_tag[value.tag].append((pos, label))
        if len(by_tag) > 1:
            notes.append(f"{m.name.capitalize()} tag changed: " + ", ".join(f"{t} ({span(i)})" for t, i in by_tag.items()))
    long_quarters = [f"{c.by_cq[cq].label} ({length(c.by_cq[cq].start, c.by_cq[cq].end) // 7} weeks)"
                     for cq in WINDOW if length(c.by_cq[cq].start, c.by_cq[cq].end) > 92]
    if long_quarters:
        notes.append("Longer quarters in the window: " + ", ".join(long_quarters))
    return ". ".join(notes) or None


def all_values(c, key):
    return [v[key] for v in list(c.quarters.values()) + [v for _, v in c.annual] if v.get(key)]


RANKED = ["revenue", "growth", "gross_margin", "op_margin", "net_income", "rnd", "rnd_pct", "capex", "fcf"]


def window_measure(c, kind, key):
    """Whole-window value for rankings: a sum of 12 quarters, a ratio of 12-quarter sums, or a change."""
    qs = [c.quarters.get(cq, {}) for cq in WINDOW]
    if kind == "sum":
        vals = [q.get(key) for q in qs]
        return None if None in vals else sum(v.raw for v in vals)
    if kind == "ratio":
        num, den = [q.get(key) for q in qs], [q.get("revenue") for q in qs]
        return None if None in num + den else sum(v.raw for v in num) / sum(v.raw for v in den) * 100
    first, last = qs[0].get(key), qs[-1].get(key)
    if first is None or last is None:
        return None
    return (last.raw / first.raw - 1) * 100 if kind == "growth" else last.raw - first.raw


def ranking_rows(companies):
    rows = []

    def add(label, scores, units):
        scores = [(name, raw) for name, raw in scores if raw is not None]
        for high in (True, False):
            ranked = sorted(scores, key=lambda s: s[1], reverse=high)
            (top, top_raw), (second, second_raw) = ranked[0], ranked[1]
            rows.append([f"{'Highest' if high else 'Lowest'} {label}", top, display(units, top_raw), second,
                         display(units, second_raw), units])

    latest = cq_label(LATEST)
    for key in RANKED:
        m = METRIC[key]
        label = f"{m.name}, {latest}" + (f" (vs {cq_label(cq_shift(LATEST, -4))})" if key == "growth" else "")
        add(label, [(c.name, v.raw if (v := c.quarters[LATEST].get(key)) else None) for c in companies], m.units)
    sums = "(sum of 12 quarters)"
    window = [
        ("total revenue", "sum", "revenue", "USD millions", sums),
        ("revenue growth", "growth", "revenue", "%", f"(change: {latest} revenue vs {cq_label(WINDOW[0])} revenue)"),
        ("gross margin", "ratio", "gross_profit", "%", "(12-quarter totals: gross profit ÷ revenue)"),
        ("gross margin change", "change", "gross_margin", "percentage points",
         f"(change: {latest} minus {cq_label(WINDOW[0])})"),
        ("operating margin", "ratio", "op_income", "%", "(12-quarter totals: operating income ÷ revenue)"),
        ("operating margin change", "change", "op_margin", "percentage points",
         f"(change: {latest} minus {cq_label(WINDOW[0])})"),
        ("total net income", "sum", "net_income", "USD millions", sums),
        ("total R&D expense", "sum", "rnd", "USD millions", sums),
        ("R&D as % of revenue", "ratio", "rnd", "%", "(12-quarter totals: R&D expense ÷ revenue)"),
        ("total capital expenditures", "sum", "capex", "USD millions", sums),
        ("total free cash flow", "sum", "fcf", "USD millions", sums),
    ]
    for label, kind, key, units, basis in window:
        add(f"{label}, {SPAN} {basis}", [(c.name, window_measure(c, kind, key)) for c in companies], units)
    return rows


def definition_rows(companies):
    fallbacks = fallback_uses(companies)

    def fallback_text(key):
        uses = [f"{c.name} uses {next(v.tag for v in all_values(c, key) if v.tag in m.fallback)} for "
                f"{span(used)}." for c, m, used in fallbacks if m.key == key]
        return " ".join(uses)

    def gap_text(key):
        gaps = missing(companies, key)
        return f" Blank (not available): {gaps}." if gaps else ""

    q4 = "Fiscal Q4 (no 3-month fact in the 10-K) = full year minus nine months, marked derived."
    ytd = ("10-Qs report cash flows year to date, so Q2 = six months minus three months, Q3 = nine minus six and "
           "Q4 = full year minus nine months, marked derived. A 3-month fact is used as is where one exists "
           "(Microsoft reports them).")
    usd = "USD millions"
    rows = [
        ["Company quarter", "Row label of Quarterly results: company name and calendar quarter, e.g. 'NVIDIA CY2025 Q2'.",
         None, None, "text"],
        ["Calendar quarter", "The calendar quarter (CY = calendar year) that holds the midpoint of the company's fiscal "
         "quarter. This assigns every fiscal quarter to exactly one calendar quarter, so companies with different "
         f"fiscal years line up. The window is {SPAN} (12 quarters).", None,
         "Midpoint = period start + half the period length", "CYyyyy Qn"],
        ["Fiscal period", "The company's own label: fiscal year (FY) and quarter. Fiscal year N is the one that ends in "
         "calendar year N. See Companies for how each company's quarters line up.", None, None, "FYyyyy Qn"],
        ["Period start / Period end", "First and last day of the fiscal quarter or fiscal year, from the XBRL facts.",
         None, None, "date (yyyy-mm-dd)"],
        ["Revenue", "Total net revenue for the period." + gap_text("revenue"), usage(companies, "revenue"),
         "3-month fact. " + q4, usd],
        ["Cost of revenue", "Cost of revenue (cost of sales) for the period, as the company tags it." + gap_text("cost"),
         usage(companies, "cost"), "3-month fact. " + q4, usd],
        ["Gross profit", "Revenue minus cost of revenue, as reported by the company." + gap_text("gross_profit"),
         usage(companies, "gross_profit"), "3-month fact. " + q4 + " Alphabet and Meta report no gross profit, so it "
         "is revenue minus cost of revenue (derived).", usd],
        ["R&D expense", "Research and development expense." + gap_text("rnd"), usage(companies, "rnd"),
         "3-month fact. " + q4, usd],
        ["Operating income", "Operating income. Negative values are operating losses." + gap_text("op_income"),
         usage(companies, "op_income"), "3-month fact. " + q4, usd],
        ["Net income", "Net income attributable to the company (excludes non-controlling interests). Negative values "
         "are net losses." + gap_text("net_income"), usage(companies, "net_income"),
         "3-month fact. " + q4 + " " + fallback_text("net_income") + (
             " ProfitLoss is the net income tag that includes non-controlling interests. It is used only for "
             "companies that report no non-controlling interests, where it equals net income attributable to the "
             "company." if fallback_text("net_income") else ""), usd],
        ["Diluted EPS", "Diluted earnings per share, as reported. Later filings restate earlier periods for stock "
         "splits (NVIDIA 10-for-1 in June 2024, Broadcom 10-for-1 in July 2024) and those restated values are used. "
         "Fiscal Q4 cells are blank: 10-K filings report only full-year EPS, and a quarter's EPS cannot be found by "
         "subtraction because share counts change (checked against the companies' Q4 press releases, full year minus "
         "nine months missed the reported figure in 11 of 23 quarters, by up to $0.04). The Q4 press releases in "
         "<TICKER>/ir/ give the reported figures." + gap_text("eps"), usage(companies, "eps"),
         "3-month fact (Q1 to Q3). The full-year fact in Annual results. Never derived.", "USD per share"],
        ["Operating cash flow", "Net cash provided by (used in) operating activities." + gap_text("ocf"),
         usage(companies, "ocf"), ytd, usd],
        ["Capital expenditures", "Cash paid for property, plant and equipment, shown as a positive amount spent. It is "
         "gross spending: government incentives and partner contributions (Intel, Micron) are not netted."
         + gap_text("capex"), usage(companies, "capex"),
         ytd + " NVIDIA's line also includes intangible assets ('Purchases related to property and equipment and "
         "intangible assets').", usd],
        ["Free cash flow", "Operating cash flow minus capital expenditures. Companies' own free cash flow measures can "
         "differ (for example Intel's adjusted free cash flow nets partner contributions and incentives)."
         + gap_text("fcf"), "derived from the two rows above", "Operating cash flow minus capital expenditures "
         "(derived).", usd],
        ["Cash and cash equivalents", "Balance at the end of the quarter (or fiscal year)." + gap_text("cash"),
         usage(companies, "cash"), "Value at the period end date. " + fallback_text("cash") + (
             " That tag is the cash-flow statement total including restricted cash. Intel used it for the "
             "balance-sheet line 'Cash and cash equivalents' until mid-2025, and the values used match that line in "
             "SEC's own workbooks of the same filings." if fallback_text("cash") else ""), usd],
        ["Gross margin", "Gross profit as a percentage of revenue." + gap_text("gross_margin"), None,
         "Gross profit ÷ revenue × 100, from unrounded values.", "%"],
        ["Operating margin", "Operating income as a percentage of revenue." + gap_text("op_margin"), None,
         "Operating income ÷ revenue × 100.", "%"],
        ["Net margin", "Net income as a percentage of revenue." + gap_text("net_margin"), None,
         "Net income ÷ revenue × 100.", "%"],
        ["R&D as % of revenue", "R&D expense as a percentage of revenue." + gap_text("rnd_pct"), None,
         "R&D expense ÷ revenue × 100.", "%"],
        ["Revenue growth YoY", "Change in revenue versus the same calendar quarter a year earlier (Annual results: "
         "versus the prior fiscal year)." + gap_text("growth"), None,
         f"(revenue ÷ revenue a year earlier − 1) × 100. The 4 quarters before the window "
         f"({cq_label(BASE[0])} to {cq_label(BASE[-1])}) are computed only for this.", "%"],
        ["Derived", "A value not read directly from one XBRL fact: fiscal Q4 figures, cash-flow quarters from "
         "year-to-date totals, gross profit for Alphabet and Meta, free cash flow, ratios and growth. "
         "ai_earnings_provenance.csv lists the facts and arithmetic behind every value.", None, None, None],
        ["Restated values", "When several filings report the same period, the most recently filed value is used, "
         "so restatements and stock-split adjustments win.", None, None, None],
        ["Notes", "Quarterly results and Annual results flag fiscal Q4 rows (derived figures, blank EPS), quarters "
         "or years with an extra week, and any other value that is not available.", None, None, "text"],
        ["Rankings", f"Highest and lowest company for the latest quarter ({cq_label(LATEST)}) and for the whole window "
         f"({SPAN}). Each window ranking says whether its value is a sum of 12 quarters, a ratio of 12-quarter "
         "totals, or a change between the first and last quarter. Runner-up = second highest (or second lowest).",
         None, None, "as stated in each row"],
        ["Segment revenue", "Not included. SEC company facts exclude segment-level (dimensional) facts, so revenue by "
         "segment, such as NVIDIA's Data Center revenue, is not available from this source.", None, None, None],
        ["Source", f"SEC EDGAR XBRL company facts (data.sec.gov/api/xbrl/companyfacts), downloaded {SNAPSHOT}. Facts "
         "from 10-Q, 10-K and their amendments only. Built by scripts/build_comparison.py.", None, None, None],
        ["USD millions", "Millions of US dollars, rounded to whole millions. These companies report in millions, so "
         "no rounding was needed in practice.", None, None, None],
    ]
    return rows


# --- Provenance ------------------------------------------------------------------------------------------------
PROVENANCE_COLUMNS = ["company", "ticker", "period type", "calendar quarter", "fiscal period", "metric", "value",
                      "units", "XBRL tag", "accession number", "filed date", "period start", "period end",
                      "derived", "how derived"]


def provenance_rows(companies):
    def row(c, kind, cq, period, start, end, m, v):
        accns = list(dict.fromkeys(f["accn"] for f in v.facts))
        filed = [next(f["filed"] for f in v.facts if f["accn"] == a) for a in accns]
        return [c.name, c.ticker, kind, cq, period, m.name, display(m.units, v.raw), m.units, v.tag, "; ".join(accns),
                "; ".join(filed), start, end, "yes" if v.derived else "no", v.how]

    rows = []
    for c in companies:
        for cq in BASE + WINDOW:
            fq, v = c.by_cq[cq], c.quarters[cq]
            kind = "quarter" if cq in WINDOW else "quarter (base for YoY growth only)"
            for m in METRICS if cq in WINDOW else [METRIC["revenue"]]:
                if v.get(m.key):
                    rows.append(row(c, kind, cq_label(cq), fq.label, fq.start, fq.end, m, v[m.key]))
        for fy, v in c.annual:
            for m in METRICS:
                if v.get(m.key):
                    rows.append(row(c, "fiscal year", "", fy.label, fy.start, fy.end, m, v[m.key]))
    return rows


# --- Validation ------------------------------------------------------------------------------------------------
def validate(companies):
    out = []
    say = out.append
    say("== Fiscal calendar")
    for c in companies:
        bad_end = [str(fy.end) for fy in c.years if fy.end and fy.end.year >= 2022 and not fy_end_ok(c.rule, fy.end)]
        gaps = [cq_label(cq) for cq in BASE + WINDOW if cq not in c.by_cq]
        breaks = [f"{a.label}->{b.label}" for a, b in zip(c.years, c.years[1:])
                  if a.end and a.end.year >= 2021 and b.start != a.end + ONE_DAY]
        labels = []
        for cq in BASE + WINDOW:  # the filing that first reported the period should carry the same fiscal label
            fq = c.by_cq.get(cq)
            if not fq:
                continue
            start, fp = (fq.start, f"Q{fq.q}") if fq.q < 4 else (fq.fy_start, "FY")
            firsts = [f for t in CALENDAR_TAGS if (f := c.facts.first.get((t, "USD"), {}).get((str(start), str(fq.end))))]
            if firsts:
                f = min(firsts, key=lambda f: (f["filed"], f["accn"]))
                if (f.get("fy"), f.get("fp")) != (fq.fy, fp):
                    labels.append(f"{fq.label} filed as fy={f.get('fy')} fp={f.get('fp')} ({f['accn']})")
        say(f"{c.ticker}: {cq_label(BASE[0])}..{cq_label(LATEST)} = {c.by_cq[BASE[0]].label}..{c.by_cq[LATEST].label}; "
            f"missing quarters: {gaps or 'none'}; fiscal year ends off rule: {bad_end or 'none'}; "
            f"breaks between years: {breaks or 'none'}; label mismatches: {labels or 'none'}; "
            f"same-filing conflicts: {len(c.facts.conflicts)}")

    say("\n== Quarters vs full year (sum of the 4 quarterly values minus the annual value, USD; all fiscal years "
        "whose quarters are all in the window; latest marked *)")
    for c in companies:
        complete = [(fy, v) for fy, v in c.annual if all(fq.cq in c.quarters for fq in fy.quarters)]
        for i, (fy, v) in enumerate(complete):
            diffs = {}
            for key in FLOWS:
                parts = [c.quarters[fq.cq].get(key) for fq in fy.quarters]
                if v.get(key) is None or None in parts:
                    diffs[key] = "n/a"
                else:
                    diffs[key] = sum(p.raw for p in parts) - v[key].raw
            direct = all(not c.quarters[fq.cq]["revenue"].derived for fq in fy.quarters[:3])
            flag = "*" if i == len(complete) - 1 else " "
            say(f"{flag}{c.ticker} {fy.label}: " + ", ".join(f"{k}={d}" for k, d in diffs.items())
                + ("" if direct else " (Q1-Q3 revenue not all 3-month facts)"))

    say("\n== Implausible or notable values (window quarters and annual rows)")
    for c in companies:
        rows = [(cq_label(cq), c.quarters[cq]) for cq in WINDOW] + [(fy.label, v) for fy, v in c.annual]
        for label, v in rows:
            gm, rev = v.get("gross_margin"), v.get("revenue")
            if gm and not 0 <= gm.raw <= 100:
                say(f"{c.ticker} {label}: gross margin {text('%', gm.raw)}% outside 0-100")
            if rev and rev.raw <= 0:
                say(f"{c.ticker} {label}: revenue {rev.raw} not positive")
            for key in ("op_margin", "net_margin"):
                if (x := v.get(key)) and x.raw < -25:
                    say(f"{c.ticker} {label}: {METRIC[key].name} {text('%', x.raw)}% (verify against the filing)")
            ni, eps = v.get("net_income"), v.get("eps")
            if ni and eps and ni.raw * eps.raw < 0:
                say(f"{c.ticker} {label}: net income and EPS have opposite signs ({ni.raw} vs {eps.raw})")
            if (g := v.get("growth")) and not -50 <= g.raw <= 200:
                say(f"{c.ticker} {label}: revenue growth {text('%', g.raw)}%")
        shares = []  # implied diluted shares = net income / EPS; a jump suggests a split mix-up or an approximate EPS
        for cq in WINDOW:
            v = c.quarters[cq]
            if v.get("net_income") and v.get("eps") and v["eps"].raw:
                shares.append((cq, v["net_income"].raw / v["eps"].raw / MILLION))
        for (a, x), (b, y) in zip(shares, shares[1:]):
            if abs(y / x - 1) > Decimal("0.10"):
                say(f"{c.ticker}: implied diluted shares {x:,.0f}m ({cq_label(a)}) -> {y:,.0f}m ({cq_label(b)})")
    say("\n== Blank values")
    q4_eps = sum(expected_gap(c, cq, "eps") for c in companies for cq in WINDOW)
    say(f"diluted EPS: {q4_eps} fiscal Q4 cells blank by design")
    for m in METRICS:
        if gaps := missing(companies, m.key):
            say(f"{m.name}: {gaps}")
    say("\n== Fallback tags (not in the main tag list), and agreement with the listed tag where both exist")
    for c, m, used in fallback_uses(companies):
        tag = next(v.tag for v in all_values(c, m.key) if v.tag in m.fallback)
        mine, listed = c.facts.latest.get((tag, m.unit), {}), c.facts.latest.get((m.tags[0], m.unit), {})
        both = [k for k in mine if k in listed and k[1] >= "2021-01-01"]
        differ = [k for k in both if mine[k]["val"] != listed[k]["val"]]
        say(f"{c.ticker} {m.name}: {tag} for {span(used)}; periods since 2021 reported under both tags: "
            f"{len(both)}, differing: {len(differ)}")
    say("\n== USD facts that are not whole millions (direct facts in the workbook)")
    for c in companies:
        odd = {f["val"] for v in [v for q in c.quarters.values() for v in q.values()] + [v for _, a in c.annual
               for v in a.values()] if v for f in v.facts if isinstance(f["val"], int) and abs(f["val"]) >= 10**6
               and f["val"] % 10**6}
        say(f"{c.ticker}: {len(odd)}" + (f" e.g. {sorted(odd)[:3]}" if odd else ""))
    say("\n== Gross profit vs revenue minus cost of revenue (window quarters, USD millions)")
    for c in companies:
        diffs = [(cq_label(cq), (v["revenue"].raw - v["cost"].raw - v["gross_profit"].raw) / MILLION)
                 for cq in WINDOW if (v := c.quarters[cq])["revenue"] and v["cost"] and v["gross_profit"]]
        nonzero = [(q, d) for q, d in diffs if d]
        say(f"{c.ticker}: {'all equal' if not nonzero else f'{len(nonzero)} differ, e.g. {nonzero[:3]}'}")
    say("\n== Values revised after first filing (window quarters and annual rows, direct facts only)")
    for c in companies:
        revised = []
        for label, v in [(cq_label(cq), c.quarters[cq]) for cq in WINDOW] + [(fy.label, v) for fy, v in c.annual]:
            for key, value in v.items():
                if value and not value.derived and len(value.facts) == 1:
                    f = value.facts[0]
                    first = c.facts.first[value.tag, METRIC[key].unit][f.get("start"), f["end"]]
                    if first["val"] != f["val"]:
                        revised.append(f"{label} {METRIC[key].name} {first['val']} -> {f['val']}")
        say(f"{c.ticker}: {len(revised)}" + (f" e.g. {revised[:4]}" if revised else ""))
    return out


def main(corpus):
    corpus = Path(corpus)
    out = corpus / "comparison"
    out.mkdir(exist_ok=True)
    companies = [build_company(corpus, *spec) for spec in COMPANIES]
    build_workbook(companies, out / "ai_earnings_comparison.xlsx")
    rows = provenance_rows(companies)
    with open(out / "ai_earnings_provenance.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(PROVENANCE_COLUMNS)
        writer.writerows(rows)
    print("\n".join(validate(companies)))
    print(f"\nwrote {out / 'ai_earnings_comparison.xlsx'} and {out / 'ai_earnings_provenance.csv'} ({len(rows)} rows)")


if __name__ == "__main__":
    if len(sys.argv) > 2:
        sys.exit("usage: python -I build_comparison.py [corpus_dir]")
    main(sys.argv[1] if len(sys.argv) == 2 else Path(__file__).resolve().parent.parent)
