"""Compare a rebuilt workbook with SEC's own Financial_Report.xlsx, sheet by sheet and cell by cell.

usage: python -I compare_financial_reports.py <sec.xlsx> <rebuilt.xlsx> [max_examples]
Checks sheet names and order, merged ranges, every cell value (numbers within 1e-9 relative), the number
format of numeric cells, and bold. Prints totals and the first differences of each kind. Use it on a filing
that has SEC's workbook, after running rebuild_financial_report.py on the same filing's R pages.
Needs openpyxl.
"""
import sys
from collections import Counter, defaultdict

import openpyxl


def same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return a == b or abs(a - b) <= 1e-9 * max(abs(a), abs(b))
    return a == b


def main(sec_path, new_path, limit=6):
    sec, new = openpyxl.load_workbook(sec_path), openpyxl.load_workbook(new_path)
    diffs, examples, cells = Counter(), defaultdict(list), 0

    def note(kind, msg):
        diffs[kind] += 1
        if len(examples[kind]) < limit:
            examples[kind].append(msg)

    if [ws.title for ws in sec.worksheets] != [ws.title for ws in new.worksheets]:
        for i, (a, b) in enumerate(zip(sec.sheetnames, new.sheetnames), start=1):
            if a != b:
                note("sheet name", f"#{i}: {a!r} vs {b!r}")
        if len(sec.sheetnames) != len(new.sheetnames):
            note("sheet count", f"{len(sec.sheetnames)} vs {len(new.sheetnames)}")
    for i, (a, b) in enumerate(zip(sec.worksheets, new.worksheets), start=1):
        ma, mb = sorted(map(str, a.merged_cells.ranges)), sorted(map(str, b.merged_cells.ranges))
        if ma != mb:
            note("merged ranges", f"#{i} {a.title}: {sorted(set(ma) - set(mb))[:3]} vs {sorted(set(mb) - set(ma))[:3]}")
        rows, cols = max(a.max_row, b.max_row), max(a.max_column, b.max_column)
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                x, y = a.cell(r, c), b.cell(r, c)
                if x.value is None and y.value is None:
                    continue
                cells += 1
                if not same(x.value, y.value):
                    note("value", f"#{i} {a.title}!{x.coordinate}: {x.value!r:.70} vs {y.value!r:.70}")
                elif isinstance(x.value, (int, float)) and x.number_format != y.number_format:
                    note("number format", f"#{i} {a.title}!{x.coordinate}: {x.number_format!r} vs {y.number_format!r}")
                if bool(x.font and x.font.b) != bool(y.font and y.font.b) and x.value not in (None, "\xa0"):
                    note("bold", f"#{i} {a.title}!{x.coordinate}: {x.value!r:.40}")
    print(f"sheets {len(sec.sheetnames)} vs {len(new.sheetnames)}; non-empty cells compared: {cells}; "
          f"differences: {dict(diffs) or 'none'}")
    for kind, msgs in examples.items():
        print(f"-- {kind}:")
        for m in msgs:
            print("   ", m)


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: python -I compare_financial_reports.py <sec.xlsx> <rebuilt.xlsx> [max_examples]")
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) == 4 else 6)
