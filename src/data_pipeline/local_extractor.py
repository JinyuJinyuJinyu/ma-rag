"""
Local Text Extraction Module for HTML and DOCX files.

Extracts text from investor-relations press releases saved as web pages
(.html/.htm) and from Word documents (.docx) without calling any Azure service,
so these files use none of the Document Intelligence page quota.

- HTML: the page title plus the text of the <article> element (falling back to
  <main>, then the whole page), which leaves out site navigation and footers.
  Scripts and styles are skipped.
- DOCX: body paragraphs and tables in document order.

Tables become one line per row with cells joined by " | ". Files are parsed as
data only (stdlib HTMLParser; zipfile + defusedxml for DOCX); nothing in them
is executed.
"""

import logging
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Any, List

from defusedxml import ElementTree as ET

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".html", ".htm", ".docx"}

# Largest word/document.xml we will decompress (guards against zip bombs)
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "dl", "dt", "dd", "section", "header",
    "footer", "nav", "aside", "blockquote", "pre", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class LocalExtractionError(Exception):
    """Raised when local text extraction fails."""
    pass


class _HTMLText(HTMLParser):
    """Collects visible text, one block or table row per line.

    Each line remembers whether it was inside <main> and/or <article>, so the
    caller can keep just the page's main content.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: List[list] = []  # [text, in_main, in_article]
        self.title_parts: List[str] = []
        self.skip = 0
        self.cells = 0
        self.main = 0
        self.article = 0
        self.in_title = False
        self._new_line()

    def _new_line(self):
        self.lines.append(["", self.main > 0, self.article > 0])

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag in ("main", "article"):
            setattr(self, tag, getattr(self, tag) + 1)
            self._new_line()
        elif tag in ("td", "th"):
            self.lines[-1][0] += " | "
            self.cells += 1
        elif tag in ("tr", "table"):
            self._new_line()
        elif tag in _BLOCK_TAGS:
            if self.cells:
                self.lines[-1][0] += " "  # keep a table row on one line
            else:
                self._new_line()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in ("main", "article"):
            setattr(self, tag, max(0, getattr(self, tag) - 1))
            self._new_line()
        elif tag in ("td", "th"):
            self.cells = max(0, self.cells - 1)

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        # Line breaks inside HTML text are just spaces
        self.lines[-1][0] += re.sub(r"[\r\n]+", " ", data)


def _clean_lines(lines: List[str]) -> List[str]:
    """Collapse whitespace and drop lines with no text."""
    cleaned = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in lines)
    return [line for line in cleaned if line.strip(" |")]


class LocalExtractor:
    """
    Extracts text from HTML and DOCX files locally (no Azure calls).

    Mirrors DocumentExtractor's public shape so main.py can treat every file
    type the same way: extract_text_from_file() returns
    {"text": str, "page_count": int, "metadata": dict}.
    """

    def extract_text_from_file(self, file_path: str) -> Dict[str, Any]:
        """
        Extract text from an HTML or DOCX file.

        Args:
            file_path: Path to the .html, .htm or .docx file

        Returns:
            Dictionary containing:
                - text: Extracted text content
                - page_count: Always 1 (these formats have no fixed pages)
                - metadata: Additional document metadata

        Raises:
            LocalExtractionError: If extraction fails
            FileNotFoundError: If the file doesn't exist
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type '{suffix}'. Expected one of: "
                f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}."
            )

        logger.info(f"Starting local text extraction from: {file_path}")

        try:
            if suffix == ".docx":
                lines = self._docx_lines(path)
                method = "local_docx"
            else:
                lines = self._html_lines(path)
                method = "local_html"
        except Exception as e:
            logger.error(f"Error extracting text from {path.name}: {e}")
            raise LocalExtractionError(f"Extraction failed: {e}")

        extracted_text = "\n".join(lines)

        metadata = {
            "source_file": path.name,
            "file_path": str(path.absolute()),
            "page_count": 1,
            "file_size_bytes": path.stat().st_size,
            "extraction_method": method,
        }

        logger.info(
            f"Successfully extracted {len(extracted_text)} characters "
            f"({len(lines)} lines) from {path.name}"
        )

        return {
            "text": extracted_text,
            "page_count": 1,
            "metadata": metadata
        }

    @staticmethod
    def _html_lines(path: Path) -> List[str]:
        parser = _HTMLText()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        parser.close()

        lines = parser.lines
        for flag, container in ((2, "<article>"), (1, "<main>")):
            kept = [line for line in lines if line[flag]]
            if _clean_lines(line[0] for line in kept):
                logger.info(f"Keeping the text inside {container}")
                lines = kept
                break
        else:
            logger.info("No <article> or <main> element; keeping the whole page")

        title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
        return _clean_lines([title] + [line[0] for line in lines])

    @staticmethod
    def _docx_lines(path: Path) -> List[str]:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise LocalExtractionError(
                    f"word/document.xml is {info.file_size:,} bytes "
                    f"(limit {MAX_DOCX_XML_BYTES:,})"
                )
            root = ET.fromstring(archive.read(info))

        def text_of(element) -> str:
            parts = []
            for node in element.iter():
                if node.tag == _W + "t":
                    parts.append(node.text or "")
                elif node.tag in (_W + "tab", _W + "br", _W + "cr"):
                    parts.append(" ")
            return "".join(parts)

        lines = []

        def walk(element):
            for child in element:
                if child.tag == _W + "p":
                    lines.append(text_of(child))
                elif child.tag == _W + "tbl":
                    for row in child.findall(_W + "tr"):
                        cells = [
                            " ".join(text_of(p) for p in cell.iter(_W + "p"))
                            for cell in row.findall(_W + "tc")
                        ]
                        lines.append(" | ".join(cells))
                else:
                    walk(child)

        body = root.find(_W + "body")
        if body is None:
            raise LocalExtractionError("word/document.xml has no body")
        walk(body)
        return _clean_lines(lines)


def extract_local(file_path: str) -> Dict[str, Any]:
    """
    Convenience function to extract text from an HTML or DOCX file.

    Args:
        file_path: Path to the file

    Returns:
        Dictionary containing extracted text and metadata

    Example:
        >>> result = extract_local("datasets/ai-earnings-desk/AMD/ir/FY2026-Q2/AMD_FY2026-Q2_press_release.html")
        >>> print(result["text"][:200])
    """
    return LocalExtractor().extract_text_from_file(file_path)


if __name__ == "__main__":
    # Test the extractor
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if len(sys.argv) > 1:
        try:
            result = extract_local(sys.argv[1])
            print("\n" + "=" * 60)
            print("EXTRACTION RESULT")
            print("=" * 60)
            print(f"Source: {result['metadata']['source_file']}")
            print(f"Method: {result['metadata']['extraction_method']}")
            print(f"Characters: {len(result['text'])}")
            print("\n" + "=" * 60)
            print("EXTRACTED TEXT (first 500 chars):")
            print("=" * 60)
            print(result['text'][:500])
            print("\n...")
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        print("Usage: python local_extractor.py <path_to_html_or_docx>")
        sys.exit(1)
