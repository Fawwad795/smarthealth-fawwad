"""Retrieval eval (task 4.10): how often search finds the service a person
meant, and which similarity threshold POST /search should use.

Run against a seeded stack: `make eval`, or
docker compose exec api python -m scripts.eval_retrieval

EVAL_SET pairs a query, in a patient's own words, with the service a person
reading the catalogue would pick for it, or with None when the clinic does
not offer what it asks for. Every query is ranked against every published
service, using the embedding provider and vector store POST /search uses.
The report has:

- the hit rate: how often the expected service ranks first, in the top 3
  and in the top 5;
- a threshold sweep: for each candidate threshold, how many offered queries
  would still find their service in POST /search's top_k, and how many
  not-offered queries would get "we don't offer that" instead of a weak
  match.

The ranking applies no threshold on purpose. The threshold is what is being
chosen, so filtering by the current one would hide the very scores the
choice depends on.

Queries name a service, how it is done or how to prepare for it, never a
symptom: picking a test for a symptom is clinical reasoning, which this
project does not do (rule 7). Expected services are named, not numbered,
because ids differ from one database to the next.
"""

from dataclasses import dataclass
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider, get_embedding_provider
from app.ai.vector_store import get_vector_store
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Service
from app.models.enums import ServiceStatus


class EvalCase(NamedTuple):
    """One query and the service it should find, or None when the clinic
    does not offer what it asks for."""

    query: str
    expected: str | None


# Two queries per seeded service (scripts/seed.py), then ten for things the
# clinic does not offer. Several are deliberately hard: "MRI of my knee"
# has Knee X-Ray next to it, the nail polish question matches two
# Dermatology services' prep, and "chest x-ray", "pregnancy ultrasound",
# "sports massage" and "hair transplant" each share words with a service
# the clinic does offer.
EVAL_SET = (
    EvalCase("ultrasound scan of my heart", "Echocardiogram"),
    EvalCase("heart echo appointment", "Echocardiogram"),
    EvalCase("ECG test", "Resting ECG"),
    EvalCase("test of my heart's electrical activity", "Resting ECG"),
    EvalCase("wear a heart monitor at home for a day", "24-Hour Heart Monitor Fitting"),
    EvalCase("holter monitor", "24-Hour Heart Monitor Fitting"),
    EvalCase("appointment with a heart specialist", "Cardiology Consultation"),
    EvalCase("see a cardiologist", "Cardiology Consultation"),
    EvalCase("x-ray of my knee", "Knee X-Ray"),
    EvalCase("what should I wear for a knee x-ray", "Knee X-Ray"),
    EvalCase("MRI of my knee", "Knee MRI Scan"),
    EvalCase("do I need to take my watch off for the knee scanner", "Knee MRI Scan"),
    EvalCase("physio appointment", "Physiotherapy Session"),
    EvalCase("exercise and mobility session", "Physiotherapy Session"),
    EvalCase("see a bone and joint specialist", "Orthopaedic Consultation"),
    EvalCase("orthopaedic doctor appointment", "Orthopaedic Consultation"),
    EvalCase("full body skin check", "Full Skin Check"),
    EvalCase("can I wear nail polish to my skin examination", "Full Skin Check"),
    EvalCase("photos of my moles", "Mole Mapping"),
    EvalCase("mole mapping appointment", "Mole Mapping"),
    EvalCase("allergy patch test", "Patch Testing"),
    EvalCase("patches stuck on my back for two days", "Patch Testing"),
    EvalCase("see a skin doctor", "Dermatology Consultation"),
    EvalCase("appointment about my hair and nails", "Dermatology Consultation"),
    EvalCase("dental cleaning", None),
    EvalCase("eye test for new glasses", None),
    EvalCase("hearing test", None),
    EvalCase("flu vaccination", None),
    EvalCase("chest x-ray", None),
    EvalCase("pregnancy ultrasound scan", None),
    EvalCase("sports massage", None),
    EvalCase("tattoo removal", None),
    EvalCase("hair transplant", None),
    EvalCase("asdkjh qwe zzz", None),
)

# The thresholds the sweep tries. 0.65 is the placeholder set on Day 1,
# before there was anything to measure it against.
CANDIDATE_THRESHOLDS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65)

# The hit rate is reported at each of these ranks.
HIT_RATE_RANKS = (1, 3, 5)


@dataclass(frozen=True)
class CaseResult:
    """One query's outcome: every published service, by name, with its
    similarity to the query, most similar first."""

    case: EvalCase
    ranking: list[tuple[str, float]]

    @property
    def rank(self) -> int | None:
        """Where the expected service came, counting from 1. None for a
        not-offered query, or a service missing from the ranking."""
        names = [name for name, _ in self.ranking]
        if self.case.expected not in names:
            return None
        return names.index(self.case.expected) + 1

    @property
    def expected_similarity(self) -> float | None:
        """The expected service's similarity, or None if it has no rank."""
        rank = self.rank
        return None if rank is None else self.ranking[rank - 1][1]

    @property
    def best_similarity(self) -> float | None:
        """The top-ranked service's similarity, or None if nothing ranked."""
        return self.ranking[0][1] if self.ranking else None


@dataclass(frozen=True)
class SweepRow:
    """What POST /search would do over the whole eval set at one threshold."""

    threshold: float
    found: int  # offered queries whose service is in the results
    refused: int  # not-offered queries told "we don't offer that"

    @property
    def right(self) -> int:
        """Queries with the right outcome, offered and not offered."""
        return self.found + self.refused


def published_services(db: Session) -> dict[int, str]:
    """Every PUBLISHED service's name, by id: the catalogue being ranked."""
    return dict(
        db.execute(
            select(Service.id, Service.name).where(
                Service.status == ServiceStatus.PUBLISHED
            )
        ).all()
    )


def missing_services(eval_set: tuple[EvalCase, ...], catalogue: set[str]) -> list[str]:
    """Expected services that are not published, in eval-set order.

    main() stops when this is not empty. Otherwise a typo in EVAL_SET, or
    a stack seeded before the catalogue grew, would count as a miss and
    quietly lower the hit rate.
    """
    return [
        case.expected
        for case in eval_set
        if case.expected is not None and case.expected not in catalogue
    ]


def evaluate(
    db: Session,
    embeddings: EmbeddingProvider,
    eval_set: tuple[EvalCase, ...],
    catalogue: dict[int, str],
) -> list[CaseResult]:
    """Rank every service in `catalogue` against each query in `eval_set`.

    The same provider and vector store POST /search uses
    (app/ai/retrieval.py), with two differences. There is no threshold:
    cosine similarity is never below -1, so a minimum of -1 lets every
    vector through. And top_k is the size of the catalogue, so every
    service gets a rank. All the queries go to the provider in one batch,
    as the publish workflow sends a service's chunks.
    """
    query_vectors = embeddings.embed([case.query for case in eval_set])
    store = get_vector_store(db)
    results = []
    for case, query_vector in zip(eval_set, query_vectors, strict=True):
        hits = store.search(
            query_vector,
            model=embeddings.model,
            top_k=len(catalogue),
            min_similarity=-1.0,
        )
        ranking = [(catalogue[hit.service_id], hit.similarity) for hit in hits]
        results.append(CaseResult(case=case, ranking=ranking))
    return results


def hit_rate(results: list[CaseResult], k: int) -> tuple[int, int]:
    """How many offered queries ranked their service in the top `k`, out of
    how many offered queries there are. Not-offered queries have no right
    service to rank, so they do not count either way."""
    offered = [result for result in results if result.case.expected is not None]
    hits = sum(1 for result in offered if result.rank is not None and result.rank <= k)
    return hits, len(offered)


def sweep(
    results: list[CaseResult], thresholds: tuple[float, ...], top_k: int
) -> list[SweepRow]:
    """What POST /search would return for each query at each threshold.

    POST /search returns the `top_k` best services scoring at least the
    threshold. So an offered query finds its service when that service
    ranks within `top_k` and scores at least the threshold, and a
    not-offered query is refused when even its best match scores below
    it. A score equal to the threshold is returned, as in the SQL.
    """
    rows = []
    for threshold in thresholds:
        found = refused = 0
        for result in results:
            if result.case.expected is None:
                best = result.best_similarity
                if best is None or best < threshold:
                    refused += 1
            elif (
                result.rank is not None
                and result.rank <= top_k
                and result.expected_similarity >= threshold
            ):
                found += 1
        rows.append(SweepRow(threshold=threshold, found=found, refused=refused))
    return rows


def print_report(
    results: list[CaseResult],
    rows: list[SweepRow],
    model: str,
    catalogue_size: int,
    top_k: int,
    current_threshold: float,
) -> None:
    """Print each query's outcome, the hit rates and the threshold sweep."""
    offered = [result for result in results if result.case.expected is not None]
    not_offered = [result for result in results if result.case.expected is None]
    print(
        f"Retrieval eval: {len(results)} queries ({len(offered)} offered, "
        f"{len(not_offered)} not offered) against {catalogue_size} published "
        f"services, model {model}, no threshold."
    )

    print("\nOffered: the expected service's rank and similarity")
    for result in offered:
        line = (
            f"  {result.rank:>2}  {result.expected_similarity:.3f}  "
            f"{result.case.query:<54} {result.case.expected}"
        )
        if result.rank != 1:
            top_name, top_similarity = result.ranking[0]
            line += f"  (first: {top_name} {top_similarity:.3f})"
        print(line)

    print("\nNot offered: the best match and its similarity")
    for result in not_offered:
        top_name, top_similarity = result.ranking[0]
        print(f"  {top_similarity:.3f}  {result.case.query:<54} {top_name}")

    print("\nHit rate, no threshold")
    for k in HIT_RATE_RANKS:
        hits, total = hit_rate(results, k)
        print(f"  top-{k}  {hits}/{total}  {hits / total:.0%}")

    best = max(row.right for row in rows)
    print(f"\nThreshold sweep, POST /search with top_k {top_k}")
    print("  threshold  offered found  not offered refused  right")
    for row in rows:
        notes = []
        if row.right == best:
            notes.append("most right")
        if row.threshold == current_threshold:
            notes.append("current")
        print(
            f"  {row.threshold:.2f}       {row.found:>2}/{len(offered):<11} "
            f"{row.refused:>2}/{len(not_offered):<17} {row.right:>2}/{len(results)}"
            + (f"  <- {', '.join(notes)}" if notes else "")
        )


def main() -> None:
    """Entry point for `python -m scripts.eval_retrieval`."""
    db = SessionLocal()
    try:
        catalogue = published_services(db)
        missing = missing_services(EVAL_SET, set(catalogue.values()))
        if missing:
            raise SystemExit(
                f"Not published: {', '.join(missing)}. "
                "Run `make seed` (or `make reset`) first."
            )
        embeddings = get_embedding_provider()
        results = evaluate(db, embeddings, EVAL_SET, catalogue)
        # The configured threshold is always swept too, so the report shows
        # what the live setting does even when it is not a candidate.
        thresholds = tuple(
            sorted({*CANDIDATE_THRESHOLDS, settings.retrieval_min_similarity})
        )
        rows = sweep(results, thresholds, settings.retrieval_top_k)
        print_report(
            results,
            rows,
            model=embeddings.model,
            catalogue_size=len(catalogue),
            top_k=settings.retrieval_top_k,
            current_threshold=settings.retrieval_min_similarity,
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
