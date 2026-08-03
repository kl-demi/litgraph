"""Build and score a hand-labelled evaluation set for rice gene extraction.

Answers three questions the gazetteer's own numbers cannot:
  1. How precise is the conservative policy that is live in the graph today?
  2. Would the permissive tier (`include_unaudited`) be safe to load? ~36% of its matches
     are forms nobody has verified, which is exactly why it was held back.
  3. What is recall -- how many real rice gene mentions does the dictionary miss entirely?

Sampling is **stratified**, not uniform: only ~15% of papers get a conservative match, so a
uniform sample of 150 would contain ~23 matched papers and measure almost nothing. The three
strata map onto the three questions above.

Labelling is split into two worksheets because the two tasks cost very different effort.
Judging "is this match correct?" given the sentence takes seconds, so precision gets the
large sample. Finding every gene in an abstract from scratch takes minutes, so recall gets a
small one. Both are plain CSV, editable in a spreadsheet.
"""

import csv
import random
import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from spokebio.ingest.gene_gazetteer import _MSU_ID, _RAP_ID, find_gene_mentions

# Deliberately naive sentence splitter: abstracts are full of "et al.", "e.g.", "0.05" and
# "Fig. 2", and a smarter splitter would need a model. Over-splitting only shortens the
# context shown to the annotator, which is a cosmetic problem, whereas under-splitting would
# dump a whole abstract into one cell.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
_CONTEXT_CHARS = 260

STRATUM_CONSERVATIVE = "conservative"
STRATUM_PERMISSIVE_ONLY = "permissive_only"
STRATUM_NO_MATCH = "no_match"

PRECISION_FIELDS = [
    "paper_id", "pmid", "tier", "matched_form", "gene_id", "gene_symbol",
    "sentence", "correct", "notes",
]
RECALL_FIELDS = ["paper_id", "pmid", "title", "abstract", "found_by_gazetteer", "missed_genes", "notes"]


class Candidate(NamedTuple):
    paper_id: str
    pmid: str
    tier: str
    matched_form: str
    gene_id: str
    sentence: str


def sentence_containing(text: str, form: str) -> str:
    """The sentence in ``text`` where ``form`` occurs, trimmed to a readable length.

    Matches on a word boundary so a form isn't located inside a longer token, and falls back
    to a character window when no sentence boundary is found (some abstracts arrive as one
    unbroken block).
    """
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", re.I)
    for sentence in _SENTENCE_END.split(text or ""):
        if pattern.search(sentence):
            stripped = sentence.strip()
            return stripped if len(stripped) <= _CONTEXT_CHARS else stripped[:_CONTEXT_CHARS] + "..."
    match = pattern.search(text or "")
    if not match:
        return ""
    start = max(0, match.start() - _CONTEXT_CHARS // 2)
    return (text or "")[start : start + _CONTEXT_CHARS].strip()


def stratify(
    papers: Iterable[dict], conservative: dict[str, str], permissive: dict[str, str]
) -> dict[str, list[dict]]:
    """Bucket papers by which gazetteer tier matched them.

    ``permissive_only`` is the interesting stratum: papers the shipped policy passed over but
    the wider tier would claim. Its precision is the whole question of whether to widen.
    """
    buckets: dict[str, list[dict]] = {
        STRATUM_CONSERVATIVE: [],
        STRATUM_PERMISSIVE_ONLY: [],
        STRATUM_NO_MATCH: [],
    }
    for paper in papers:
        text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
        if find_gene_mentions(text, conservative):
            buckets[STRATUM_CONSERVATIVE].append(paper)
        elif find_gene_mentions(text, permissive):
            buckets[STRATUM_PERMISSIVE_ONLY].append(paper)
        else:
            buckets[STRATUM_NO_MATCH].append(paper)
    return buckets


def build_candidates(
    papers: Iterable[dict], gazetteer: dict[str, str], tier: str, exclude: dict[str, str] | None = None
) -> list[Candidate]:
    """One Candidate per (paper, gene) match, with the sentence that justifies it.

    ``exclude`` suppresses matches a narrower gazetteer already found, so the
    ``permissive_only`` rows really are the incremental ones rather than a re-audit of forms
    already verified.
    """
    candidates: list[Candidate] = []
    for paper in papers:
        text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
        found = find_gene_mentions(text, gazetteer)
        already = find_gene_mentions(text, exclude) if exclude else {}
        for gene_id, form in found.items():
            if gene_id in already:
                continue
            candidates.append(
                Candidate(
                    paper_id=paper["id"],
                    pmid=paper.get("pmid") or "",
                    tier=tier,
                    matched_form=form,
                    gene_id=gene_id,
                    sentence=sentence_containing(text, form),
                )
            )
    return candidates


def write_precision_worksheet(path: str | Path, candidates: Iterable[Candidate], symbols: dict[str, str]) -> int:
    """Write the precision worksheet. ``correct`` is left blank for the annotator (y/n)."""
    rows = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRECISION_FIELDS)
        writer.writeheader()
        for c in candidates:
            writer.writerow(
                {
                    "paper_id": c.paper_id, "pmid": c.pmid, "tier": c.tier,
                    "matched_form": c.matched_form, "gene_id": c.gene_id,
                    "gene_symbol": symbols.get(c.gene_id, ""),
                    "sentence": c.sentence, "correct": "", "notes": "",
                }
            )
            rows += 1
    return rows


def write_recall_worksheet(path: str | Path, papers: Iterable[dict], conservative: dict[str, str]) -> int:
    """Write the recall worksheet. ``missed_genes`` is left blank for the annotator.

    ``found_by_gazetteer`` is pre-filled so the annotator only has to name what's *missing*
    rather than re-list everything.
    """
    rows = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RECALL_FIELDS)
        writer.writeheader()
        for paper in papers:
            text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
            found = find_gene_mentions(text, conservative)
            writer.writerow(
                {
                    "paper_id": paper["id"], "pmid": paper.get("pmid") or "",
                    "title": paper.get("title") or "", "abstract": paper.get("abstract") or "",
                    "found_by_gazetteer": ", ".join(sorted(found.values())),
                    "missed_genes": "", "notes": "",
                }
            )
            rows += 1
    return rows


def display_symbols(gazetteer: dict[str, str]) -> dict[str, str]:
    """Pick the most human-readable form per gene, for the worksheet's `gene_symbol` column.

    A gene's forms include both locus ids (`OS06G0579200`) and symbols (`OsLCT1`); a plain
    dict inversion keeps whichever came last, which is usually the locus id -- unhelpful when
    the whole point of the column is to let the annotator recognise the gene. Prefers a
    non-locus form, shortest first.
    """
    best: dict[str, str] = {}
    for form, gene_id in gazetteer.items():
        is_locus = bool(_RAP_ID.fullmatch(form) or _MSU_ID.fullmatch(form))
        current = best.get(gene_id)
        if current is None:
            best[gene_id] = form
            continue
        current_is_locus = bool(_RAP_ID.fullmatch(current) or _MSU_ID.fullmatch(current))
        if current_is_locus and not is_locus:
            best[gene_id] = form
        elif current_is_locus == is_locus and len(form) < len(current):
            best[gene_id] = form
    return best


# Minimum abstract length for a paper to be worth putting on the recall sheet. Some records
# have a non-null but effectively empty abstract, and asking someone to find genes in an empty
# cell is wasted effort.
MIN_ABSTRACT_CHARS = 100


def has_usable_abstract(paper: dict) -> bool:
    return len((paper.get("abstract") or "").strip()) >= MIN_ABSTRACT_CHARS


def sample(items: list, n: int, seed: int) -> list:
    """Deterministic sample, so rebuilding the worksheet doesn't reshuffle work already done."""
    if n >= len(items):
        return list(items)
    return random.Random(seed).sample(items, n)


_YES = {"y", "yes", "1", "true", "t", "correct"}
_NO = {"n", "no", "0", "false", "f", "wrong", "incorrect"}


def score_precision(rows: Iterable[dict]) -> dict[str, dict[str, float | int]]:
    """Per-tier precision over the labelled rows. Unlabelled rows are counted as skipped
    rather than silently treated as either verdict."""
    per_tier: dict[str, dict[str, float | int]] = {}
    for row in rows:
        tier = row.get("tier") or "unknown"
        bucket = per_tier.setdefault(tier, {"correct": 0, "wrong": 0, "skipped": 0, "precision": 0.0})
        verdict = (row.get("correct") or "").strip().lower()
        if verdict in _YES:
            bucket["correct"] += 1
        elif verdict in _NO:
            bucket["wrong"] += 1
        else:
            bucket["skipped"] += 1
    for bucket in per_tier.values():
        labelled = bucket["correct"] + bucket["wrong"]
        bucket["labelled"] = labelled
        bucket["precision"] = bucket["correct"] / labelled if labelled else 0.0
    return per_tier


def score_recall(rows: Iterable[dict]) -> dict[str, float | int]:
    """Recall over the labelled recall worksheet.

    Counts gene *mentions*, treating each comma-separated entry in ``missed_genes`` as one
    true positive the gazetteer failed to find.

    A row counts as reviewed only if the **annotator** wrote something -- `missed_genes` or
    `notes`. `found_by_gazetteer` is pre-filled by the builder, so treating it as evidence of
    review made a completely untouched worksheet score 100% recall (10 prefilled rows, zero
    recorded misses). A falsely reassuring metric is worse than no metric, hence the
    explicit "nothing missed here" convention: leave `missed_genes` blank but put a word in
    `notes`.
    """
    found = missed = reviewed = skipped = 0
    for row in rows:
        raw_missed = (row.get("missed_genes") or "").strip()
        raw_found = (row.get("found_by_gazetteer") or "").strip()
        if not raw_missed and not (row.get("notes") or "").strip():
            skipped += 1
            continue
        reviewed += 1
        if raw_found:
            found += len([p for p in raw_found.split(",") if p.strip()])
        if raw_missed:
            missed += len([p for p in raw_missed.split(",") if p.strip()])
    total = found + missed
    return {
        "papers_reviewed": reviewed,
        "papers_skipped": skipped,
        "mentions_found": found,
        "mentions_missed": missed,
        "recall": found / total if total else 0.0,
    }
