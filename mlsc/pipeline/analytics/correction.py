"""Multiple-comparisons correction over one day's whole candidate set.

Requirement 6: dozens of simultaneous tests manufacture a significant-
looking trend every day by chance, so this sits between candidates and
events as its own node — never folded into a detector, which only ever
sees one topic and cannot correct across a set it cannot see (design.md,
"Success path": "That position in the graph is what makes it impossible to
bypass").

Benjamini-Hochberg (false discovery rate) rather than Bonferroni for this
step: Bonferroni divides by the count of everything tested and gets
punishing as a monitor grows more topics, while FDR controls the expected
proportion of false positives among what is reported, which is the
guarantee a user-facing "changes worth your attention" feed actually needs.
"""

from __future__ import annotations

from statsmodels.stats.multitest import multipletests

from mlsc.pipeline.analytics.detectors.base import Candidate


def apply(candidates: list[Candidate], *, alpha: float) -> list[Candidate]:
    """Requirement 6: correct every candidate produced for one bucket
    together, before any of them may become an event. Returns the survivors
    in their original relative order.
    """
    if not candidates:
        return []

    p_values = [candidate.test_result.p_value for candidate in candidates]
    rejected, _, _, _ = multipletests(p_values, alpha=alpha, method="fdr_bh")

    return [candidate for candidate, survived in zip(candidates, rejected, strict=True) if survived]
