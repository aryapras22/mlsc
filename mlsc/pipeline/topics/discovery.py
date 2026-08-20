"""Candidate discovery over the residue pool.

UMAP reduces dimensionality first because density is meaningless in 384
dimensions; HDBSCAN then clusters the reduced space and is allowed to call
points noise rather than forcing them into a group (learn.md, "HDBSCAN over
UMAP, and why not k-means"). Both are injected so a test can swap in a fixed
stub and never touch a stochastic algorithm (design.md, "Dependencies,
injected").
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol

from mlsc.pipeline.topics.registry import ResidueMember

_NOISE_LABEL = -1


class ResidueUnclusterable(RuntimeError):
    """A residue pool large enough to attempt discovery produced no candidate.

    A recorded failure, not a silent no-op (design.md, "Failure strategy"):
    the pool is left intact for the next attempt rather than cleared.
    """


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A cluster from one discovery pass. Never stored — resolved into a merge
    or a new topic within the same pass (design.md, "Domain shapes")."""

    member_document_ids: list[uuid.UUID]
    centroid: list[float]


class Reducer(Protocol):
    def reduce(self, embeddings: list[list[float]]) -> list[list[float]]: ...


class Clusterer(Protocol):
    def cluster(self, reduced: list[list[float]]) -> list[int]: ...


class UmapReducer:
    """UMAP over cosine distance, since that is the space embeddings live in.

    The seed is fixed: UMAP is stochastic, and two runs on identical input
    must not produce different candidates (learn.md, "The pitfall: UMAP is
    stochastic").
    """

    def __init__(self, *, n_components: int = 5, random_state: int = 42) -> None:
        self._n_components = n_components
        self._random_state = random_state

    def reduce(self, embeddings: list[list[float]]) -> list[list[float]]:
        import umap

        n_neighbors = min(15, max(2, len(embeddings) - 1))
        model = umap.UMAP(
            n_components=self._n_components,
            n_neighbors=n_neighbors,
            metric="cosine",
            random_state=self._random_state,
        )
        return model.fit_transform(embeddings).tolist()


class HdbscanClusterer:
    """Density-based clustering over the reduced space; noise is a real label."""

    def __init__(self, *, min_cluster_size: int) -> None:
        self._min_cluster_size = min_cluster_size

    def cluster(self, reduced: list[list[float]]) -> list[int]:
        import hdbscan

        model = hdbscan.HDBSCAN(min_cluster_size=self._min_cluster_size)
        return model.fit_predict(reduced).tolist()


def discover_candidates(
    members: list[ResidueMember], *, reducer: Reducer, clusterer: Clusterer
) -> list[Candidate]:
    """Reduce, cluster, and group members by label. Raises ``ResidueUnclusterable``
    when every point comes back labelled noise."""
    reduced = reducer.reduce([member.embedding for member in members])
    labels = clusterer.cluster(reduced)

    grouped: dict[int, list[ResidueMember]] = {}
    for member, label in zip(members, labels, strict=True):
        if label == _NOISE_LABEL:
            continue
        grouped.setdefault(label, []).append(member)

    if not grouped:
        raise ResidueUnclusterable(f"no cluster found in a pool of {len(members)}")

    return [
        Candidate(
            member_document_ids=[member.document_id for member in cluster_members],
            centroid=_mean(member.embedding for member in cluster_members),
        )
        for cluster_members in grouped.values()
    ]


def _mean(vectors: object) -> list[float]:
    vectors = list(vectors)
    dimensions = len(vectors[0])
    return [sum(vector[i] for vector in vectors) / len(vectors) for i in range(dimensions)]
