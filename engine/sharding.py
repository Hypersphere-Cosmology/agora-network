"""
Agora — consistent hashing for asset sharding.
Each asset's content_hash determines which nodes are responsible for it.
Replication factor K = min(3, N) where N = number of nodes.
"""

import hashlib
from typing import List


def _sha256_first16_int(s: str) -> int:
    """SHA256 of string, take first 16 hex chars as int."""
    digest = hashlib.sha256(s.encode()).hexdigest()
    return int(digest[:16], 16)


def node_hash(node_id: str) -> int:
    """Hash a node_id onto the ring."""
    return _sha256_first16_int(node_id)


def asset_hash(content_hash: str) -> int:
    """Hash an asset content_hash onto the ring."""
    return _sha256_first16_int(content_hash)


def assign_nodes(content_hash: str, all_node_ids: List[str], replication_factor: int = None) -> List[str]:
    """
    Place asset on hash ring, pick K=min(3, N) closest nodes clockwise.
    Returns list of node_ids responsible for this asset.
    """
    if not all_node_ids:
        return []

    n = len(all_node_ids)
    k = min(3, n) if replication_factor is None else min(replication_factor, n)

    # Build sorted ring of (hash_value, node_id)
    ring = sorted((node_hash(nid), nid) for nid in all_node_ids)

    asset_pos = asset_hash(content_hash)

    # Find first node clockwise (>= asset_pos)
    start_idx = 0
    for i, (h, _) in enumerate(ring):
        if h >= asset_pos:
            start_idx = i
            break
    else:
        # Wrap around
        start_idx = 0

    # Pick K nodes clockwise (with wrap-around)
    responsible = []
    for i in range(k):
        idx = (start_idx + i) % n
        responsible.append(ring[idx][1])

    return responsible


def shard_coverage(node_id: str, all_node_ids: List[str]) -> float:
    """
    Fraction of the ring this node covers.
    With K=min(3,N) replication, each node covers min(1.0, K/N) of the ring.
    """
    n = len(all_node_ids)
    if n == 0:
        return 0.0
    k = min(3, n)
    return min(1.0, k / n)
