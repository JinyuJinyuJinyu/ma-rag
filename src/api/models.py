"""
Pydantic models for FastAPI request/response validation.
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


# Chunk Information Model
class ChunkInfo(BaseModel):
    """Detailed information about a retrieved chunk."""
    chunk_id: str = Field(..., description="The ID from Azure Search index")
    source_file: str = Field(..., description="Source file name")
    chunk_index: int = Field(..., description="Chunk position in the document")
    score: float = Field(..., description="Search score (vector similarity + BM25 if hybrid)")
    content_preview: str = Field(..., description="First 200 characters of the chunk")
    full_content: str = Field(..., description="Full chunk text for transparency")

    class Config:
        json_schema_extra = {
            "example": {
                "chunk_id": "ai_earnings_comparison_xlsx_chunk_148",
                "source_file": "ai_earnings_comparison.xlsx",
                "chunk_index": 148,
                "score": 0.0331,
                "content_preview": "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions.",
                "full_content": "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions.\nRankings — Ranking 'Lowest revenue, CY2026 Q2': Company is AMD; Value is 11536; Runner-up is Intel; Runner-up value is 16128; Units is USD millions."
            }
        }


# Request Models
class QueryRequest(BaseModel):
    """Request model for query endpoint."""
    question: str = Field(..., min_length=1, description="User's question")
    session_id: str = Field(..., min_length=1, description="Session identifier provided by backend")
    top_k: Optional[int] = Field(5, ge=1, le=20, description="Number of chunks to retrieve")
    user_id: Optional[str] = Field(None, description="Optional user identifier for tracking")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional additional context")

    class Config:
        json_schema_extra = {
            "example": {
                "question": "Which company had the highest revenue in calendar Q2 2026?",
                "session_id": "session_12345",
                "top_k": 5,
                "user_id": "user_67890",
                "metadata": {"source": "web_app"}
            }
        }


class UploadResponse(BaseModel):
    """Response model for upload endpoint."""
    success: bool = Field(..., description="Whether upload was successful")
    message: str = Field(..., description="Status message")
    file_name: str = Field(..., description="Name of uploaded file")
    chunks_created: int = Field(..., description="Number of chunks created")
    chunks_indexed: int = Field(..., description="Number of chunks indexed")
    processing_time_seconds: float = Field(..., description="Total processing time")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Document processed successfully",
                "file_name": "NVDA_FY2027-Q2_cfo_commentary.pdf",
                "chunks_created": 21,
                "chunks_indexed": 21,
                "processing_time_seconds": 12.5
            }
        }


class QueryResponse(BaseModel):
    """Response model for query endpoint."""
    success: bool = Field(..., description="Whether query was successful")
    question: str = Field(..., description="Original question")
    answer: str = Field(..., description="Generated answer")
    intent: str = Field(..., description="Identified intent")
    session_id: str = Field(..., description="Session identifier echoed back for tracking")
    conversation_turn: int = Field(..., description="Turn number in this session")
    chunks_retrieved: int = Field(..., description="Number of chunks retrieved")
    retrieved_chunks: List[ChunkInfo] = Field(..., description="Detailed chunk information")
    sources: List[str] = Field(..., description="List of source files")
    processing_time_seconds: float = Field(..., description="Query processing time")
    guardrail_passed: bool = Field(..., description="Whether question passed the relevance guardrail check")
    input_guardrail: str = Field(
        "not evaluated",
        description="Input guardrail (Azure AI Content Safety) result: 'nothing detected', 'detected', or 'not evaluated'"
    )
    output_guardrail: str = Field(
        "not evaluated",
        description="Output guardrail (Azure AI Content Safety) result: 'nothing detected', 'detected', or 'not evaluated'"
    )
    input_guardrail_details: Optional[Dict[str, Any]] = Field(
        None, description="Input guardrail category severities and triggers (Content Safety)"
    )
    output_guardrail_details: Optional[Dict[str, Any]] = Field(
        None, description="Output guardrail category severities and triggers (Content Safety)"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "question": "Which company had the highest revenue in calendar Q2 2026?",
                "answer": "Alphabet had the highest revenue in calendar Q2 2026, $119,796 million, ahead of NVIDIA with $96,221 million (Source 1)...",
                "intent": "comparison",
                "input_guardrail": "nothing detected",
                "output_guardrail": "nothing detected",
                "session_id": "session_12345",
                "conversation_turn": 1,
                "chunks_retrieved": 5,
                "retrieved_chunks": [
                    {
                        "chunk_id": "ai_earnings_comparison_xlsx_chunk_148",
                        "source_file": "ai_earnings_comparison.xlsx",
                        "chunk_index": 148,
                        "score": 0.0331,
                        "content_preview": "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions.",
                        "full_content": "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions."
                    }
                ],
                "sources": ["ai_earnings_comparison.xlsx"],
                "processing_time_seconds": 2.3,
                "guardrail_passed": True
            }
        }


class HealthResponse(BaseModel):
    """Response model for health check endpoint."""
    status: str = Field(..., description="Service status")
    version: str = Field(..., description="API version")
    timestamp: str = Field(..., description="Current timestamp")
    services: Dict[str, str] = Field(..., description="Status of dependent services")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "version": "1.0.0",
                "timestamp": "2024-01-15T10:30:00Z",
                "services": {
                    "azure_search": "connected",
                    "azure_ai_foundry": "connected",
                    "document_intelligence": "connected"
                }
            }
        }


class IndexStatusResponse(BaseModel):
    """Response model for index status endpoint."""
    index_name: str = Field(..., description="Name of the search index")
    exists: bool = Field(..., description="Whether the index exists")
    document_count: int = Field(..., description="Number of documents in index")
    vector_search_enabled: bool = Field(..., description="Whether vector search is enabled")
    semantic_search_enabled: bool = Field(..., description="Whether semantic search is enabled")
    fields: Optional[List[str]] = Field(None, description="List of index fields")

    class Config:
        json_schema_extra = {
            "example": {
                "index_name": "ai-earnings-desk",
                "exists": True,
                "document_count": 150,
                "vector_search_enabled": True,
                "semantic_search_enabled": True,
                "fields": ["id", "content", "content_vector", "source_file"]
            }
        }


class ErrorResponse(BaseModel):
    """Response model for errors."""
    success: bool = Field(False, description="Always False for errors")
    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Error message")
    detail: Optional[str] = Field(None, description="Detailed error information")

    class Config:
        json_schema_extra = {
            "example": {
                "success": False,
                "error": "ProcessingError",
                "message": "Failed to process document",
                "detail": "Document format not supported"
            }
        }
