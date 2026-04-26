"""
Semantic plagiarism detection using local mpnet embeddings.
Checks new asset content against all existing assets in LanceDB-style similarity.
Free, local, no API required.

Thresholds (governance-adjustable via StorageConfig):
  - BLOCK threshold (default 0.92): content is too similar, reject submission
  - WARN threshold (default 0.75): similar content, warn submitter but allow

Performance note:
  This embeds up to 500 assets on every submission (~2-5 seconds locally).
  Acceptable up to ~1000 assets. Beyond that, switch to a pre-built FAISS index
  for O(1) vector search instead of O(n) brute-force.
"""

import numpy as np
import logging

# Suppress sentence-transformers loading noise
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)

# Lazy-loaded model (same as LanceDB brain: all-mpnet-base-v2)
_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-mpnet-base-v2")
    return _model


def embed(text: str) -> np.ndarray:
    return get_model().encode([text[:2000]], convert_to_numpy=True)[0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def check_plagiarism(
    new_content: str,
    db,
    block_threshold: float = 0.92,
    warn_threshold: float = 0.75,
):
    """
    Check new_content against all existing assets.

    Returns one of:
      {"status": "ok"}
        — content is unique enough

      {"status": "warn", "similar_to": [{"id": N, "title": ..., "similarity": 0.78}], "message": "..."}
        — similar content found, warn submitter but allow

      {"status": "block", "similar_to": [...], "message": "..."}
        — content too similar, reject submission
    """
    from db import Asset

    # Get all non-deleted assets (limit to 500 most recent for performance)
    # TODO: When asset count > 1000, switch to pre-built FAISS index for O(1) search
    existing = (
        db.query(Asset)
        .filter(Asset.is_deleted == False)
        .order_by(Asset.id.desc())
        .limit(500)
        .all()
    )

    if not existing:
        return {"status": "ok"}

    # Embed new content
    new_vec = embed(new_content)

    # Check against each existing asset
    matches = []
    for asset in existing:
        if not asset.content or len(asset.content.strip()) < 20:
            continue
        try:
            existing_vec = embed(asset.content)
            sim = cosine_similarity(new_vec, existing_vec)
            if sim >= warn_threshold:
                matches.append({
                    "id": asset.id,
                    "title": asset.title,
                    "similarity": round(sim, 4),
                    "submitter_id": asset.submitter_id,
                })
        except Exception:
            continue

    if not matches:
        return {"status": "ok"}

    # Sort by similarity descending, return top 3
    matches.sort(key=lambda x: x["similarity"], reverse=True)
    top_matches = matches[:3]

    # Check if any exceeds block threshold
    if top_matches[0]["similarity"] >= block_threshold:
        return {
            "status": "block",
            "similar_to": top_matches,
            "message": (
                f"Content too similar to existing asset #{top_matches[0]['id']} "
                f"(similarity: {top_matches[0]['similarity']:.0%}). "
                "The network requires original work."
            ),
        }

    return {
        "status": "warn",
        "similar_to": top_matches,
        "message": (
            f"Content is similar to existing asset #{top_matches[0]['id']} "
            f"(similarity: {top_matches[0]['similarity']:.0%}). "
            "Proceed only if your work is meaningfully original."
        ),
    }
