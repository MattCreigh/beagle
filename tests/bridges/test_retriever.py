"""Tests for Phase 1: BeagleRetriever (LangChain BaseRetriever)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# ── Unit tests with mocked RAG backend ────────────────────────────────────────


class TestBeagleRetrieverUnit:
    """Unit tests for BeagleRetriever with mocked rag_search."""

    @pytest.fixture
    def mock_rag_results(self):
        """Sample RAG search results matching Beagle's output format."""
        return {
            "semantic_anchors": [
                {
                    "file_path": "beagle/core/a2a_protocol.py",
                    "snippet": "class A2AProtocol:\n    def __init__(self):\n        ...",
                    "ast_node_type": "class_def",
                    "start_line": 42,
                    "end_line": 55,
                    "relevance_score": 0.95,
                },
                {
                    "file_path": "beagle/core/nodes.py",
                    "snippet": "async def execute_goose_node(state, skill_name, ...):\n    ...",
                    "ast_node_type": "function_def",
                    "start_line": 120,
                    "end_line": 145,
                    "relevance_score": 0.82,
                },
            ],
            "structural_relations": [
                {
                    "source": "beagle/core/a2a_protocol.py",
                    "target": "beagle/core/nodes.py",
                    "relation_type": "IMPORTS",
                },
            ],
        }

    @pytest.fixture
    def retriever(self):
        """Create an BeagleRetriever with default config."""
        from beagle.bridges.retriever import BeagleRetriever

        return BeagleRetriever(kuzu_hops=1, top_k=5)

    def test_is_base_retriever(self, retriever):
        """Criterion 1.1: BeagleRetriever subclasses BaseRetriever successfully."""
        from langchain_core.retrievers import BaseRetriever

        assert isinstance(retriever, BaseRetriever)

    def test_config_defaults(self, retriever):
        """Criterion 1.4: Retriever reads max_hops and top_k from config."""
        assert retriever.kuzu_hops == 1
        assert retriever.top_k == 5
        assert retriever.include_relations is True

    @pytest.mark.asyncio
    async def test_async_retrieval(self, retriever, mock_rag_results):
        """Criterion 1.2+1.5: Async retrieval returns list[Document]."""
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            new_callable=AsyncMock,
            return_value=mock_rag_results,
        ):
            docs = await retriever._aget_relevant_documents("A2A protocol")
            assert isinstance(docs, list)
            assert len(docs) == 2
            assert all(hasattr(d, "page_content") for d in docs)

    @pytest.mark.asyncio
    async def test_document_metadata(self, retriever, mock_rag_results):
        """Criterion 1.3: Document metadata contains source, node_type, relations."""
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            new_callable=AsyncMock,
            return_value=mock_rag_results,
        ):
            docs = await retriever._aget_relevant_documents("A2A protocol")
            doc = docs[0]
            assert "source" in doc.metadata
            assert "node_type" in doc.metadata
            assert doc.metadata["source"] == "beagle/core/a2a_protocol.py"
            assert doc.metadata["node_type"] == "class_def"

    @pytest.mark.asyncio
    async def test_document_relations_included(self, retriever, mock_rag_results):
        """Relations from Kùzu graph are included in metadata when configured."""
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            new_callable=AsyncMock,
            return_value=mock_rag_results,
        ):
            docs = await retriever._aget_relevant_documents("A2A protocol")
            # First doc should have relations (it matches the source in structural_relations)
            a2a_doc = [d for d in docs if "a2a_protocol" in d.metadata.get("source", "")]
            if a2a_doc:
                assert "relations" in a2a_doc[0].metadata

    @pytest.mark.asyncio
    async def test_sync_retrieval(self, retriever, mock_rag_results):
        """Criterion 1.5: Sync retrieval works via _get_relevant_documents."""
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            new_callable=AsyncMock,
            return_value=mock_rag_results,
        ):
            docs = retriever._get_relevant_documents("A2A protocol")
            assert isinstance(docs, list)
            assert len(docs) == 2

    @pytest.mark.asyncio
    async def test_empty_results(self, retriever):
        """Retriever returns empty list when RAG returns no results."""
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            new_callable=AsyncMock,
            return_value={"semantic_anchors": [], "structural_relations": []},
        ):
            docs = await retriever._aget_relevant_documents("nonexistent query xyz")
            assert docs == []

    @pytest.mark.asyncio
    async def test_rag_search_error(self, retriever):
        """Retriever returns empty list when RAG search raises an error."""
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            new_callable=AsyncMock,
            side_effect=RuntimeError("RAG server unavailable"),
        ):
            docs = await retriever._aget_relevant_documents("test query")
            assert docs == []

    @pytest.mark.asyncio
    async def test_caching(self, retriever, mock_rag_results):
        """Caching: second query returns cached result without calling RAG again."""
        from beagle.bridges.retriever import clear_cache

        clear_cache()

        mock_search = AsyncMock(return_value=mock_rag_results)
        with patch(
            "beagle.bridges.retriever._rag_search_in_process",
            mock_search,
        ):
            docs1 = await retriever._aget_relevant_documents("A2A protocol")
            docs2 = await retriever._aget_relevant_documents("A2A protocol")
            # RAG should only be called once due to caching
            assert mock_search.call_count == 1
            assert len(docs1) == len(docs2)
        clear_cache()


class TestBridgeConfig:
    """Tests for the bridge configuration loader."""

    def test_retriever_config_defaults(self):
        """RetrieverConfig has sensible defaults."""
        from beagle.bridges.config import RetrieverConfig

        cfg = RetrieverConfig()
        assert cfg.max_hops == 1
        assert cfg.top_k == 5
        assert cfg.enabled is True
        assert cfg.communication_mode == "in_process"

    def test_tools_config_defaults(self):
        """ToolsConfig is disabled by default (opt-in)."""
        from beagle.bridges.config import ToolsConfig

        cfg = ToolsConfig()
        assert cfg.enabled is False
        assert cfg.fallback_on_error is True

    def test_chat_model_config_defaults(self):
        """ChatModelConfig is in dual mode by default."""
        from beagle.bridges.config import ChatModelConfig

        cfg = ChatModelConfig()
        assert cfg.executor_mode == "dual"
        assert cfg.respect_model_resolver is True

    def test_langsmith_config_defaults(self):
        """LangSmithConfig is disabled by default (requires API key)."""
        from beagle.bridges.config import LangSmithConfig

        cfg = LangSmithConfig()
        assert cfg.enabled is False
        assert cfg.project_name == "beagle-workflows"

    def test_a2a_config_defaults(self):
        """A2ABridgeConfig is disabled by default, bound to localhost."""
        from beagle.bridges.config import A2ABridgeConfig

        cfg = A2ABridgeConfig()
        assert cfg.enabled is False
        assert cfg.bind_address == "127.0.0.1"
        assert cfg.max_concurrent_tasks == 4

    def test_cloud_config_defaults(self):
        """CloudConfig defaults to local execution."""
        from beagle.bridges.config import CloudConfig

        cfg = CloudConfig()
        assert cfg.execution_env == "local"
        assert cfg.checkpoint_mode == "auto"
