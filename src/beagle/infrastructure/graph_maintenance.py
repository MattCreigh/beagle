"""Graph maintenance utilities for Kùzu knowledge graph.

Provides pruning and optimization functions for the AST knowledge graph,
supporting the monthly AutoDream maintenance cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("Beagle.graph_maintenance")


@dataclass
class PruneResult:
    """Result of graph pruning operation."""

    nodes_removed: int = 0
    edges_removed: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def prune_low_connectivity(
    kuzu_conn: object,
    min_connections: int = 2,
    table_name: str = "ASTNode",
    dry_run: bool = False,
) -> PruneResult:
    """Remove nodes with fewer than min_connections from the Kùzu graph.

    This supports the monthly AutoDream maintenance job for graph optimization.
    Low-connectivity nodes (orphans and single-connection nodes) add noise without
    contributing meaningful structural context to RAG search.

    Args:
        kuzu_conn: Active Kùzu connection.
        min_connections: Minimum connections to keep a node (default: 2).
        table_name: AST node table name (default: "ASTNode").
        dry_run: If True, report what would be removed without removing.

    Returns:
        PruneResult with counts of removed nodes/edges.

    """
    import time

    start = time.monotonic()
    result = PruneResult()

    try:
        # Find nodes with low connectivity
        query = f"""
        MATCH (n:{table_name})
        OPTIONAL MATCH (n)-[r]-()
        WITH n, COUNT(r) AS conn_count
        WHERE conn_count < $min_conn
        RETURN n.id AS id, n.name AS name, conn_count
        """
        results = kuzu_conn.execute(query, parameters={"min_conn": min_connections})  # type: ignore[attr-defined]

        low_conn_nodes: list[str] = []
        while results.has_next():
            row = results.get_next()
            node_id = str(row[0])
            node_name = str(row[1])
            conn_count = int(row[2]) if row[2] is not None else 0
            low_conn_nodes.append(node_id)
            logger.debug(
                f"[GraphPrune] Low-connectivity node: {node_name} ({conn_count} connections)"
            )

        if not low_conn_nodes:
            logger.info("[GraphPrune] No low-connectivity nodes found — graph is healthy")
            result.elapsed_seconds = time.monotonic() - start
            return result

        logger.info(f"[GraphPrune] Found {len(low_conn_nodes)} low-connectivity nodes")

        if dry_run:
            result.nodes_removed = len(low_conn_nodes)
            result.elapsed_seconds = time.monotonic() - start
            logger.info(f"[GraphPrune] DRY RUN: would remove {len(low_conn_nodes)} nodes")
            return result

        # Remove edges first (to avoid dangling references)
        edges_removed = 0
        for node_id in low_conn_nodes:
            try:
                edge_q = f"""
                MATCH (n:{table_name} {{id: $nid}})-[r]-()
                DELETE r
                """
                kuzu_conn.execute(edge_q, parameters={"nid": node_id})  # type: ignore[attr-defined]
                edges_removed += 1
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — Kùzu boundary: the C++ bindings raise bare RuntimeError and are not enumerable
                result.errors.append(f"Failed to remove edges for {node_id}: {e}")

        # Then remove the nodes
        nodes_removed = 0
        for node_id in low_conn_nodes:
            try:
                node_q = f"""
                MATCH (n:{table_name} {{id: $nid}})
                DELETE n
                """
                kuzu_conn.execute(node_q, parameters={"nid": node_id})  # type: ignore[attr-defined]
                nodes_removed += 1
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — Kùzu boundary: the C++ bindings raise bare RuntimeError and are not enumerable
                result.errors.append(f"Failed to remove node {node_id}: {e}")

        result.nodes_removed = nodes_removed
        result.edges_removed = edges_removed
        result.elapsed_seconds = time.monotonic() - start

        logger.info(
            f"[GraphPrune] Removed {nodes_removed} nodes, {edges_removed} edges "
            f"in {result.elapsed_seconds:.2f}s"
        )
        return result

    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional — Kùzu boundary: the C++ bindings raise bare RuntimeError and are not enumerable
        result.errors.append(f"Graph pruning failed: {e}")
        result.elapsed_seconds = time.monotonic() - start
        logger.error(f"[GraphPrune] {e}")
        return result


__all__ = ["PruneResult", "prune_low_connectivity"]
