"""Offline tests for the local .docx extractor (no Azure calls)."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from src.data_pipeline.local_extractor import extract_local

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def tbl(*rows):
    """A table; each row is a list of cells, each cell a string of cell XML."""
    return "<w:tbl>" + "".join(
        "<w:tr>" + "".join(f"<w:tc>{cell}</w:tc>" for cell in row) + "</w:tr>" for row in rows
    ) + "</w:tbl>"


class DocxExtractionTest(unittest.TestCase):
    def extract(self, body_xml):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", f"<w:document {NS}><w:body>{body_xml}</w:body></w:document>")
            return extract_local(str(path))["text"].split("\n")

    def test_paragraphs_and_table_rows_in_order(self):
        lines = self.extract(p("Intro") + tbl([p("Revenue"), p("$90,007")], [p("Net income"), p("$35,766")]) + p("Outro"))
        self.assertEqual(lines, ["Intro", "Revenue | $90,007", "Net income | $35,766", "Outro"])

    def test_cell_paragraphs_join_with_spaces(self):
        lines = self.extract(tbl([p("Operating") + p("income"), p("$40,603")]))
        self.assertEqual(lines, ["Operating income | $40,603"])

    def test_table_inside_a_cell_keeps_its_rows(self):
        # A layout table whose one cell holds a note and the real table: the inner table's
        # rows used to merge into the note's line, losing every row boundary
        inner = tbl([p("Segment"), p("Operating income")],
                    [p("Intelligent Cloud"), p("$15,955")],
                    [p("More Personal Computing"), p("$2,748")])
        lines = self.extract(tbl([p("We have recast prior periods.") + p("SEGMENT RESULTS") + inner]))
        self.assertEqual(lines, [
            "We have recast prior periods. SEGMENT RESULTS",
            "Segment | Operating income",
            "Intelligent Cloud | $15,955",
            "More Personal Computing | $2,748",
        ])

    def test_nested_table_rows_follow_their_row(self):
        inner = tbl([p("a1"), p("a2")])
        lines = self.extract(tbl([p("left"), p("right") + inner], [p("next"), p("row")]))
        self.assertEqual(lines, ["left | right", "a1 | a2", "next | row"])


if __name__ == "__main__":
    unittest.main()
