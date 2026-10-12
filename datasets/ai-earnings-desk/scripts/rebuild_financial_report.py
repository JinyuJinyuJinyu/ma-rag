"""Rebuild a Financial_Report.xlsx workbook from a filing's R pages.

usage: python -I rebuild_financial_report.py <r_pages_dir> <out.xlsx>
<r_pages_dir> must hold FilingSummary.xml and every R page it lists. The workbook mirrors SEC's own
Financial_Report.xlsx: one worksheet per report in FilingSummary order, each the report table's grid copied
cell by cell (colspan/rowspan become merged cells). Cells the renderer marks as numbers (classes nump/num)
become numbers with SEC's number formats; everything else stays text. Footnotes stay where the R page puts
them, as merged rows after the table. Pages are parsed as data only (stdlib HTMLParser, defusedxml).
Needs openpyxl and defusedxml (both in requirements.txt). Makes no network requests.

Why: SEC stopped putting Financial_Report.xlsx in filing folders for filings made from mid-May 2025, but the
R pages it is made from are still there. In this corpus the rebuilt workbooks are saved next to the filing as
<TICKER>/sec/<TICKER>_<FORM>_<report date>_Financial_Report_rebuilt.xlsx.

Getting the R pages: the filing folder https://www.sec.gov/Archives/edgar/data/<CIK>/<accession without dashes>/
holds FilingSummary.xml and the R1.htm, R2.htm, ... pages it lists (<Report><HtmlFileName>). Download them into
an empty folder of their own. SEC requires a User-Agent that names you and an email address, and allows at
most 10 requests per second.

Fidelity: checked with compare_financial_reports.py against two filings that do have SEC's workbook (NVDA 10-K
FY2025, MSFT 10-Q FY2025-Q3). Sheet names, merged cells, all 2,746 numbers and their number formats, and all
short text matched, except one cell that SEC itself cuts short. Long note-text cells hold every word of SEC's
version, but the spacing differs (SEC's version also drops some text).
"""
import re
import sys
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

import openpyxl
from defusedxml import ElementTree as ET
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

NUMBER = re.compile(r"^(?P<sym1>[^\d\s().,%-]+)?\s*(?P<open>\()?\s*(?P<sym2>[^\d\s().,%-]+)?\s*(?P<minus>-)?"
                    r"(?P<num>\d[\d,]*(?:\.\d+)?|\.\d+)\s*(?P<pct>%)?\s*(?P<close>\))?$")
BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]+")  # SEC replaces each run with one "_"
BLOCK_TAGS = {"div", "p", "tr", "td", "th", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}


class Cell:
    def __init__(self, tag, cls, colspan, rowspan):
        self.tag, self.cls, self.colspan, self.rowspan = tag, cls, colspan, rowspan
        self.parts, self.bold, self.rich = [], False, False

    def text(self):
        raw = "".join(self.parts)
        if self.tag == "th":  # titles and column headers: one line, no-break spaces kept
            return re.sub(r"[ \t\r\n]+", " ", raw).strip(" ")
        if self.rich:  # text blocks and footnotes: SEC flattens to one line, no-break spaces become spaces
            return re.sub(r"[ \t\r\n\xa0]+", " ", raw).strip(" ")
        return raw.strip("\r\n\t")  # SEC keeps spaces and no-break spaces, trims only line breaks


class ReportTable(HTMLParser):
    """Collects the rows of the first <table class="report"> (its own rows only; nested tables become cell text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.depth, self.done = 0, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if self.depth == 0 and not self.done and "report" in (a.get("class") or "").split():
                self.depth = 1
            elif self.depth:
                self.depth += 1
                if self.cell is not None:
                    self.cell.rich = True
            return
        if not self.depth:
            return
        if self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in ("td", "th"):
            self.cell = Cell(tag, (a.get("class") or "").split(), int(a.get("colspan") or 1), int(a.get("rowspan") or 1))
        elif self.cell is not None:
            if tag in ("strong", "b"):
                self.cell.bold = True
            if tag == "br":  # a line break separates words ("Amortized<br>Cost")
                self.cell.parts.append("\n")
            if tag in BLOCK_TAGS:
                self.cell.rich = True
                self.cell.parts.append(" " if tag in ("td", "th") else "\n")

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.done = True
            return
        if self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            self.row.append(self.cell)
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif self.cell is not None and tag in BLOCK_TAGS:  # end of a block also separates words ("Heading</div>Text")
            self.cell.parts.append(" " if tag in ("td", "th") else "\n")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.parts.append(data)


def number(text):
    """Parse a rendered number ("$ 72,880", "(31)", "$ (0.05)", "3.60%") -> (value, number_format) or None."""
    m = NUMBER.match(text.replace("\xa0", " ").strip())
    if not m:
        return None
    try:
        value = Decimal(m["num"].replace(",", ""))
    except InvalidOperation:
        return None
    frac = m["num"].split(".")[1] if "." in m["num"] else ""
    decimals = len(frac) if frac.strip("0") else 0  # SEC drops all-zero decimals: "31.00%" -> #,##0%
    dec = "." + "0" * decimals if decimals else ""
    sym = m["sym1"] or m["sym2"]
    negative = bool((m["open"] and m["close"]) or m["minus"])
    if m["pct"] and negative:  # SEC leaves negative percentages such as "(3.50%)" as text
        return None
    if m["pct"]:
        value /= 100
        fmt = f"#,##0{dec}%_);(#,##0{dec}%)"
    elif sym:
        fmt = f'_("{sym} "#,##0{dec}_);_("{sym} "(#,##0{dec})'
    else:
        fmt = f"#,##0{dec}_);(#,##0{dec})"
    if negative:
        value = -value
    return (int(value) if value == value.to_integral_value() else float(value)), fmt


def sheet_title(short_name, used):
    base = BAD_SHEET_CHARS.sub("_", short_name)[:31] or "Sheet"
    title, n = base, 1
    while title.lower() in used:
        n += 1
        suffix = f"_{n}"
        title = base[:31 - len(suffix)] + suffix
    used.add(title.lower())
    return title


def write_sheet(ws, rows):
    occupied, widths = set(), {}
    for r, row in enumerate(rows, start=1):
        c = 1
        for cell in row:
            while (r, c) in occupied:
                c += 1
            text = cell.text()
            parsed = number(text) if cell.tag == "td" and ({"nump", "num"} & set(cell.cls)) else None
            target = ws.cell(row=r, column=c)
            if parsed:
                target.value, target.number_format = parsed
            elif text:
                target.value = text
            if cell.bold:
                target.font = Font(bold=True)
            if cell.colspan > 1 or cell.rowspan > 1:
                ws.merge_cells(start_row=r, start_column=c, end_row=r + cell.rowspan - 1, end_column=c + cell.colspan - 1)
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied.add((r + dr, c + dc))
            if cell.colspan == 1 and text:
                widths[c] = max(widths.get(c, 0), min(len(text), 80))
            c += cell.colspan
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = max(14, w + 2)


def main(rdir, out):
    rdir = Path(rdir)
    reports = [(rep.findtext("HtmlFileName"), rep.findtext("ShortName") or "")
               for rep in ET.parse(rdir / "FilingSummary.xml").getroot().iter("Report") if rep.findtext("HtmlFileName")]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used, empty = set(), []
    for html_name, short in reports:
        parser = ReportTable()
        parser.feed((rdir / html_name).read_text(encoding="utf-8", errors="replace"))
        ws = wb.create_sheet(sheet_title(short, used))
        write_sheet(ws, parser.rows)
        if not parser.rows:
            empty.append(html_name)
    wb.save(out)
    print(f"{out}: {len(reports)} sheets" + (f"; pages without a report table: {empty}" if empty else ""))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -I rebuild_financial_report.py <r_pages_dir> <out.xlsx>")
    main(sys.argv[1], sys.argv[2])
