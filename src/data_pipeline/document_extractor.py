"""
Document Extraction Module using Azure AI Document Intelligence.

This module extracts text content from PDF documents using Azure's
Document Intelligence service with the built-in Layout model.

The Free (F0) tier analyzes only the first 2 pages of each request, so when
AZURE_DOCUMENT_INTELLIGENCE_PAGES_PER_REQUEST is set (default 2) longer PDFs
are split into pieces of that many pages, each piece is analyzed separately,
and the texts are joined in page order.
"""

import io
import logging
from typing import Dict, Any, List, Tuple
from pathlib import Path
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from pypdf import PdfReader, PdfWriter

from config import get_config

logger = logging.getLogger(__name__)

# pypdf logs a warning for every damaged cross-reference entry it repairs (over a
# thousand lines for some IR PDFs); the repaired pages split correctly.
logging.getLogger("pypdf").setLevel(logging.ERROR)


class DocumentExtractionError(Exception):
    """Raised when document extraction fails."""
    pass


class DocumentExtractor:
    """
    Extracts text from PDF documents using Azure Document Intelligence.

    Uses the built-in Layout model which provides:
    - Text extraction
    - Layout structure (paragraphs, tables, etc.)
    - OCR for scanned documents
    """

    def __init__(self):
        """Initialize the Document Intelligence client."""
        config = get_config()

        try:
            endpoint = config.doc_intelligence_endpoint.rstrip('/')

            self.client = DocumentAnalysisClient(
                endpoint=endpoint,
                credential=AzureKeyCredential(config.doc_intelligence_key)
            )
            self.pages_per_request = config.doc_intelligence_pages_per_request
            logger.info("DocumentExtractor initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize DocumentExtractor: {e}")
            raise DocumentExtractionError(f"Initialization failed: {e}")

    def extract_text_from_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        Extract text content from a PDF file.

        Args:
            pdf_path: Path to the PDF file

        Returns:
            Dictionary containing:
                - text: Extracted text content
                - page_count: Number of pages
                - metadata: Additional document metadata

        Raises:
            DocumentExtractionError: If extraction fails
            FileNotFoundError: If PDF file doesn't exist
        """
        pdf_file = Path(pdf_path)

        if not pdf_file.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        if not pdf_file.suffix.lower() == '.pdf':
            raise ValueError(f"File must be a PDF, got: {pdf_file.suffix}")

        logger.info(f"Starting text extraction from: {pdf_path}")

        try:
            # Read the PDF and split it into pieces the service tier can analyze
            pieces = self._split_pdf(pdf_file.read_bytes())

            texts = []
            page_count = 0
            for first_page, last_page, piece in pieces:
                if len(pieces) > 1:
                    logger.info(f"Analyzing pages {first_page}-{last_page} of {pieces[-1][1]}...")
                else:
                    logger.info(f"Analyzing document...")
                poller = self.client.begin_analyze_document(
                    "prebuilt-layout",
                    document=piece
                )

                logger.info("Waiting for analysis to complete...")
                result = poller.result()
                logger.info("Document analysis completed")

                # Extract text content
                if result.content:
                    texts.append(result.content)
                page_count += len(result.pages) if result.pages else 0

            extracted_text = "\n\n".join(texts)

            # Get metadata
            metadata = {
                "source_file": pdf_file.name,
                "file_path": str(pdf_file.absolute()),
                "page_count": page_count,
                "file_size_bytes": pdf_file.stat().st_size
            }

            logger.info(
                f"Successfully extracted {len(extracted_text)} characters "
                f"from {metadata['page_count']} pages"
            )

            return {
                "text": extracted_text,
                "page_count": metadata["page_count"],
                "metadata": metadata
            }

        except Exception as e:
            logger.error(f"Error extracting text from PDF: {e}")
            raise DocumentExtractionError(f"Extraction failed: {e}")

    def _split_pdf(self, data: bytes) -> List[Tuple[int, int, bytes]]:
        """
        Split a PDF into pieces of at most pages_per_request pages.

        Args:
            data: The PDF file's bytes

        Returns:
            List of (first_page, last_page, piece_bytes), pages numbered from 1.
            A PDF that fits in one request (or pages_per_request of 0) is
            returned whole as a single piece.
        """
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")  # IR PDFs may carry an owner password but no user password
        total = len(reader.pages)
        size = self.pages_per_request

        if not size or total <= size:
            return [(1, total, data)]

        pieces = []
        for start in range(0, total, size):
            end = min(start + size, total)
            writer = PdfWriter()
            for index in range(start, end):
                writer.add_page(reader.pages[index])
            buffer = io.BytesIO()
            writer.write(buffer)
            pieces.append((start + 1, end, buffer.getvalue()))

        logger.info(f"Split {total} pages into {len(pieces)} pieces of up to {size} pages")
        return pieces

    def extract_text_from_url(self, document_url: str) -> Dict[str, Any]:
        """
        Extract text from a document URL.

        Useful for documents stored in Azure Blob Storage or other URLs.

        Args:
            document_url: URL to the document

        Returns:
            Dictionary containing extracted text and metadata

        Raises:
            DocumentExtractionError: If extraction fails
        """
        logger.info(f"Starting text extraction from URL: {document_url}")

        try:
            poller = self.client.begin_analyze_document_from_url(
                "prebuilt-layout",
                document_url=document_url
            )

            logger.info("Waiting for analysis to complete...")
            result = poller.result()
            logger.info("Document analysis completed")

            # Extract text content
            extracted_text = result.content if result.content else ""

            # Get metadata
            metadata = {
                "source_url": document_url,
                "page_count": len(result.pages) if result.pages else 0
            }

            logger.info(
                f"Successfully extracted {len(extracted_text)} characters "
                f"from {metadata['page_count']} pages"
            )

            return {
                "text": extracted_text,
                "page_count": metadata["page_count"],
                "metadata": metadata
            }

        except Exception as e:
            logger.error(f"Error extracting text from URL: {e}")
            raise DocumentExtractionError(f"Extraction failed: {e}")


def extract_pdf(pdf_path: str) -> Dict[str, Any]:
    """
    Convenience function to extract text from a PDF file.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        Dictionary containing extracted text and metadata

    Example:
        >>> result = extract_pdf("datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf")
        >>> print(result["text"])
        >>> print(f"Pages: {result['page_count']}")
    """
    extractor = DocumentExtractor()
    return extractor.extract_text_from_pdf(pdf_path)


if __name__ == "__main__":
    # Test the extractor
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        try:
            result = extract_pdf(pdf_path)
            print("\n" + "=" * 60)
            print("EXTRACTION RESULT")
            print("=" * 60)
            print(f"Source: {result['metadata']['source_file']}")
            print(f"Pages: {result['page_count']}")
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
        print("Usage: python document_extractor.py <path_to_pdf>")
        sys.exit(1)