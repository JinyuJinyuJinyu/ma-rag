#!/usr/bin/env python3
"""Turn the MITRE ATT&CK Enterprise v18.1 Excel workbook into a small, RAG-friendly workbook
for the Excel Narrator Agent (it treats ROW 1 as the header and COLUMN A as each row's subject).

Usage:
    python -I clean.py <enterprise-attack-v18.1.xlsx> <out.xlsx>

Why: the full workbook has 12 sheets and 28,159 rows (~19,700 chunks once narrated). We keep
the 4 sheets a SOC analyst looks things up in, keep only useful columns, and shorten the long
descriptions:
  Tactics (14)      ID, name, short description, how many techniques each tactic has
  Techniques (691)  ID, name, tactics, platforms, parent technique, short description,
                    mitigations (joined from the 'relationships' sheet)
  Mitigations (44)  ID, name, short description, how many techniques each one mitigates
  Groups (172)      name, ID, aliases, short description, how many techniques each group uses
  Rankings (3)      top 5 for the three counts above (row sentences alone cannot answer
                    "which ... has the most ...", because retrieval only returns a few rows)
  About (5)         source, version, MITRE copyright notice and licence (the licence requires
                    copies to carry them), and what was changed
"""
import re
import sys
from collections import Counter, defaultdict

import openpyxl
from openpyxl import Workbook

CITATION = re.compile(r"\(Citation:[^)]*\)")          # "(Citation: Microsoft 2023)"
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")         # "[APT29](https://...)" -> "APT29"
HTML_TAG = re.compile(r"<[^>]+>")                      # "<code>sudo</code>" -> "sudo"
ABBREVIATIONS = {"e.g.": "for example", "i.e.": "that is", "U.S.": "US"}


def short_description(text, limit=250):
    """Clean an ATT&CK description and cut it to whole sentences of at most `limit` chars."""
    text = CITATION.sub(" ", text or "")              # a space, so "end.(Citation: X)Next" splits
    text = MD_LINK.sub(r"\1", text)
    text = HTML_TAG.sub("", text)
    for abbr, words in ABBREVIATIONS.items():       # so "U.S." is not mistaken for a sentence end
        text = text.replace(abbr, words)
    text = " ".join(text.split())                     # one line, single spaces
    text = text.replace(" .", ".").replace(" ,", ",")  # tidy "word (Citation: X)." -> "word."
    if len(text) > limit:
        cut = text[:limit]
        end = cut.rfind(". ")                         # last full sentence that fits
        text = cut[:end + 1] if end > 40 else cut[:cut.rfind(" ")] + " (shortened)"
    return text.rstrip(".")                           # the narrator adds its own full stop


def read_sheet(wb, name):
    """Return the rows of one sheet as dictionaries keyed by the header (row 1)."""
    rows = wb[name].iter_rows(values_only=True)
    header = [str(h) for h in next(rows)]
    return [dict(zip(header, r)) for r in rows if any(v is not None for v in r)]


def main():
    src, out_path = sys.argv[1], sys.argv[2]
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    tactics = read_sheet(wb, "tactics")
    techniques = read_sheet(wb, "techniques")
    mitigations = read_sheet(wb, "mitigations")
    groups = read_sheet(wb, "groups")
    relationships = read_sheet(wb, "relationships")

    # --- Joins and counts from the 'relationships' sheet -------------------------------
    mitigation_name = {m["ID"]: m["name"] for m in mitigations}
    technique_name = {t["ID"]: t["name"] for t in techniques}
    mitigations_of = defaultdict(set)      # technique ID -> mitigation IDs
    techniques_used_by = defaultdict(set)  # group ID -> technique IDs
    for r in relationships:
        if r["mapping type"] == "mitigates":                       # mitigation -> technique
            mitigations_of[r["target ID"]].add(r["source ID"])
        elif (r["mapping type"] == "uses" and r["source type"] == "group"
              and r["target type"] == "technique"):                 # group -> technique
            techniques_used_by[r["source ID"]].add(r["target ID"])
    mitigated_count = Counter(m for ids in mitigations_of.values() for m in ids)

    techniques_per_tactic, parents_per_tactic = Counter(), Counter()
    for t in techniques:
        for tactic in t["tactics"].split(", "):
            techniques_per_tactic[tactic] += 1
            if not t["is sub-technique"]:
                parents_per_tactic[tactic] += 1

    out = Workbook()
    out.remove(out.active)

    # --- Tactics --------------------------------------------------------------------
    ws = out.create_sheet("Tactics")
    ws.append(["Tactic ID", "Tactic", "Description",
               "Number of techniques incl. sub-techniques", "Number of parent techniques"])
    for t in tactics:
        ws.append([t["ID"], t["name"], short_description(t["description"], 400),
                   techniques_per_tactic[t["name"]], parents_per_tactic[t["name"]]])

    # --- Techniques -----------------------------------------------------------------
    ws = out.create_sheet("Techniques")
    ws.append(["Technique ID", "Technique", "Tactics", "Platforms", "Sub-technique of",
               "Description", "Mitigations"])
    for t in techniques:
        parent = t["sub-technique of"]
        mits = ", ".join(f"{m} {mitigation_name[m]}" for m in sorted(mitigations_of[t["ID"]]))
        ws.append([
            t["ID"], t["name"], t["tactics"],
            t["platforms"].replace("PRE", "PRE (pre-compromise)"),
            f"{parent} {technique_name[parent]}" if parent else None,   # empty for parents
            short_description(t["description"]),
            mits or None,                                                # 109 have none
        ])

    # --- Mitigations ----------------------------------------------------------------
    ws = out.create_sheet("Mitigations")
    ws.append(["Mitigation ID", "Mitigation", "Description", "Number of techniques mitigated"])
    for m in mitigations:
        ws.append([m["ID"], m["name"], short_description(m["description"]),
                   mitigated_count[m["ID"]]])

    # --- Groups (the name is the subject: ENISA and analysts refer to groups by name) --
    ws = out.create_sheet("Groups")
    ws.append(["Group", "Group ID", "Other names (aliases)", "Description", "Number of techniques used"])
    for g in groups:
        # 4 groups (e.g. APT-C-23) are linked only to software, not to techniques: leave empty
        ws.append([g["name"], g["ID"], g["associated groups"], short_description(g["description"]),
                   len(techniques_used_by[g["ID"]]) or None])

    # --- Rankings: top 5 per count ----------------------------------------------------
    ws = out.create_sheet("Rankings")
    ws.append(["Ranking", "1st", "2nd", "3rd", "4th", "5th"])
    tactic_id = {t["name"]: t["ID"] for t in tactics}
    group_name = {g["ID"]: g["name"] for g in groups}
    ws.append(["Tactics with the most techniques (including sub-techniques)"] +
              [f"{name} ({tactic_id[name]}) with {n}" for name, n in techniques_per_tactic.most_common(5)])
    ws.append(["Mitigations mapped to the most techniques"] +
              [f"{m} {mitigation_name[m]} with {n}" for m, n in mitigated_count.most_common(5)])
    top_groups = sorted(techniques_used_by.items(), key=lambda kv: -len(kv[1]))[:5]
    ws.append(["Threat groups using the most techniques"] +
              [f"{group_name[g]} ({g}) with {len(ids)}" for g, ids in top_groups])

    # --- About: source, version, licence ----------------------------------------------
    ws = out.create_sheet("About")
    ws.append(["Item", "Details"])
    ws.append(["Source", "MITRE ATT&CK Enterprise v18.1 Excel workbook (enterprise-attack-v18.1.xlsx), "
                         "the current release from 28 October 2025 to 27 April 2026"])
    ws.append(["Copyright", "© 2026 The MITRE Corporation. This work is reproduced and distributed "
                            "with the permission of The MITRE Corporation"])
    ws.append(["License", "The MITRE Corporation (MITRE) hereby grants you a non-exclusive, "
                          "royalty-free license to use ATT&CK® for research, development, and "
                          "commercial purposes. Any copy you make for such purposes is authorized "
                          "provided that you reproduce MITRE's copyright designation and this "
                          "license in any such copy"])
    ws.append(["Changes", "Subset of 4 of the 12 sheets; descriptions shortened to their first "
                          "sentences with citations and links removed; mitigations and counts "
                          "derived from the relationships sheet (a group's count is empty when "
                          "it is linked only to software); rankings added"])
    ws.append(["Version note", "Version 19 (April 2026) renamed Defense Evasion (TA0005) to Stealth "
                               "and added Defense Impairment (TA0112). This file keeps the v18.1 "
                               "names, which match the ENISA Threat Landscape 2025"])

    out.save(out_path)
    for s in out.worksheets:
        print(f"{s.title}: {s.max_row - 1} rows x {s.max_column} columns")
    print("wrote", out_path)


if __name__ == "__main__":
    main()
