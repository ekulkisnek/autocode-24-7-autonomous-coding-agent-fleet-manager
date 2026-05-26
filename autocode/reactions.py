from __future__ import annotations

from .store import Store


def evaluate_reactions(store: Store) -> dict[str, int]:
    """Lightweight deterministic reaction pass.

    This intentionally does not auto-spawn broad work yet. It records reaction
    opportunities so future provider plugins can opt into safe handlers.
    """
    failed = store.rows(
        """
        select * from jobs
        where status in ('failed','killed') and updated_at > datetime('now','-1 day')
        order by updated_at desc
        limit 50
        """
    )
    emitted = 0
    for job in failed:
        store.event("reaction_candidate", job["chat_id"], job["id"], trigger="job_failed", evidence_status=job["evidence_status"])
        emitted += 1
    return {"reaction_candidates": emitted}
