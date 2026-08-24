#!/usr/bin/env python3
"""Test RAG search with graph traversal.

Usage: python tests/test_rag_search.py

Note: This is a manual smoke-test script, not a pytest test. It calls the live
RAG MCP server functions and prints a human-readable summary. Run it directly
with the project venv's python (the package is `beagle`,
installed editable, so no sys.path munging is needed).
"""

import asyncio
import json

from beagle.infrastructure.mcp_rag_server import rag_search, rag_status


async def main():
    print("Testing RAG status...")
    status = await rag_status()
    status_data = json.loads(status)
    print(f"LanceDB: {status_data.get('lance_table_loaded')}")
    print(f"Kùzu: {status_data.get('kuzu_connected')}")
    print(f"Embeddings: {status_data.get('embed_model_loaded')}")
    print()

    print("Testing RAG search with graph traversal...")
    result = await rag_search("turboquant implementation", max_hops=2, top_k=3)
    data = json.loads(result)

    print(f"Status: {data.get('status')}")
    print(f"Vector results: {len(data.get('semantic_anchors', []))}")
    print(f"Graph relations: {len(data.get('structural_relations', []))}")

    if data.get("status") == "ok":
        print()
        print("=== Vector Results ===")
        for i, anchor in enumerate(data.get("semantic_anchors", [])[:3]):
            print(
                f"{i + 1}. {anchor.get('file')}:{anchor.get('node_name')} "
                f"(dist: {anchor.get('distance', 0):.4f})"
            )

        print()
        print("=== Graph Relations ===")
        rels = data.get("structural_relations", [])
        if rels:
            for i, rel in enumerate(rels[:5]):
                print(
                    f"{i + 1}. {rel.get('source_node')} "
                    f"--[{rel.get('relationship')}]--> {rel.get('target_node')}"
                )
        else:
            print("No graph relations found")
    else:
        print(f"Error: {data.get('message', 'Unknown error')}")


if __name__ == "__main__":
    asyncio.run(main())
