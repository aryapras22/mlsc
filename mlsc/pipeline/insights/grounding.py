"""Evidence validation: the boundary this spec owns and cannot delegate to
the provider, which has no idea what documents were in its own prompt
(design.md, "Trust boundary").

Discards the whole completion on one bad citation rather than dropping just
the bad id — a model that invented one citation has demonstrated it is not
grounded in the supplied context, and the untouched citations are unverified,
not verified (design.md, "Failure strategy"; learn.md, "Grounding is a code
property, not a prompt property").
"""

from __future__ import annotations

from mlsc.pipeline.insights.context import TopicContext


class EvidenceUnlinkable(RuntimeError):
    """A returned evidence identifier is not one of the documents supplied
    in the context (design.md, "Named failures")."""


def validate(context: TopicContext, evidence_ids: list[str]) -> None:
    """Raises ``EvidenceUnlinkable`` if any cited id is not among the
    context's own representatives, or if none were cited at all — an
    insight without evidence violates C4 (requirement 2)."""
    if not evidence_ids:
        raise EvidenceUnlinkable("completion cited no evidence")

    supplied_ids = {str(representative.document_id) for representative in context.representatives}
    invented = [evidence_id for evidence_id in evidence_ids if evidence_id not in supplied_ids]
    if invented:
        raise EvidenceUnlinkable(f"cited document ids not in context: {invented}")
