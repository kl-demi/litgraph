"""Dictionary-based rice gene NER over paper text, built from Oryzabase's own symbols.

Exists because PubTator3's gene NER barely fires on rice literature: measured on the
51,166-paper Oryza corpus, only 3,791 papers (7.4%) get any gene mention, and just 188 of
the 3,826 distinct genes it names (4.9%) are actually rice genes -- the rest are human and
mouse orthologs (INS, Tnf, Il6) or Arabidopsis (NPR1, CO). Plant SOS1 normalizes to *human*
SOS1. See docs/plant_schema.md's PubTator assessment.

Rice gene nomenclature is highly conventionalized (`Os01g0194300`, `LOC_Os01g05060`,
`OsNRAMP5`, `GHD7`, `XA21`), which makes exact dictionary matching viable where a general
biomedical NER model isn't. This module deliberately does *not* use an LLM: a gazetteer is
free, deterministic, and reproducible, and it establishes the baseline any LLM pass has to
beat. Disambiguating the ambiguous tier below is the LLM's job, later.

Tier-1 only, by design
----------------------
The full Oryzabase symbol set is unusable raw. Its single highest-frequency match on real
abstracts is `SALT` (979 hits in a 6,000-paper sample) -- the English word, in
salt-tolerance papers, not the gene. `POT`, `ACT`, `DWARF`, `SPIKE`, `LOG`, `CAS` and `OAT`
fail the same way. So only *unambiguous* surface forms are admitted (see
``is_unambiguous``): locus ids, `Os`-prefixed symbols, and alphanumeric symbols. That
sacrifices recall for precision, deliberately -- a wrong gene mention is worse than a
missing one, because it silently produces a plausible-looking but false path in every
downstream trait/pathway query.
"""

import csv
import re
from collections.abc import Iterator
from pathlib import Path

from spokebio.models import EntityMention

# Oryzabase columns that carry an identifier or symbol for the row's gene.
_FORM_COLUMNS = ("CGSNL Gene Symbol", "Gene symbol synonym(s)", "RAP ID", "MSU ID")
_RAP_COLUMN = "RAP ID"
_MSU_COLUMN = "MSU ID"
_SYMBOL_COLUMNS = ("CGSNL Gene Symbol", "Gene symbol synonym(s)")

_RAP_ID = re.compile(r"Os\d{2}g\d{7}", re.I)
_MSU_ID = re.compile(r"LOC_Os\d{2}g\d{5}", re.I)

# Oryzabase decorates classical mutant names with brackets ("[CMS-54257]") and provisional
# symbols with a star ("Bc6*"); neither decoration appears in running text.
_DECORATIONS = "*[]() \t"
_MIN_FORM_LENGTH = 3

# A maximal token *including* internal hyphens/dots, so "GHD7-mediated" and "LOC_Os04g35210.1"
# are both seen whole before being decomposed. Splitting on hyphens up front would lose
# "ML-1"; not splitting at all would lose the "GHD7" inside "GHD7-mediated". Rice symbols
# appear in hyphenated compounds constantly ("Pi-ta", "xa13-based"), and getting this wrong
# is quiet: an earlier naive tokenizer that treated "-" as a word character silently cost
# ~4 percentage points of recall. Hence test_gene_gazetteer.py's tokenizer cases.
_SPAN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_EMBEDDED_SYMBOL = re.compile(r"[A-Z]+\d+[A-Z]*")


# A short alpha token followed by "-<digits>" is almost always a **unit with a negative
# exponent**, not a gene: rice papers are full of "µg mL-1", "mg kg-1", "mol L-1", "t ha-1".
# `ML-1` is a genuine Oryzabase gene (Os07g0159800, MD-2-related lipid-recognition protein)
# and it was the 3rd most-matched form on a full-corpus dry run at 154 hits -- every sampled
# occurrence was the unit. The gene stays reachable via the same row's `OsML1` synonym, so
# rejecting the hyphenated spelling costs no coverage. (Bare `ML1` is excluded too, by the
# 4-character minimum below.)
_ALPHA_HYPHEN_NUMBER = re.compile(r"^([A-Z]+)-(\d+)$")
_MAX_SAFE_ALPHA_STEM = 3
# Longer stems that are still units rather than gene prefixes.
_UNIT_STEMS = frozenset({"MOL", "MMOL", "UMOL", "NMOL", "KDA", "RPM", "MASL"})

# Forms that pass every structural test above but still aren't a specific rice gene.
# Derived by auditing the 120 most-matched forms on a full-corpus dry run; two kinds:
#
#   Protein domains, gene *families* and compound names -- not a locus under any reading.
#     WD40 is a repeat domain; R2R3-MYB and SnRK1/2 are transcription-factor/kinase
#     families; AMT1 is the ammonium-transporter family (the genes are OsAMT1;1 etc.);
#     GA20 is gibberellin A20, a hormone; ATP6 is a generic mitochondrial subunit.
#
#   Symbols whose canonical owner is another organism, where rice uses an Os-prefixed
#     name that this gazetteer already matches separately. A rice paper writing bare
#     "NPR1" or "BRI1" is usually discussing the Arabidopsis gene comparatively, so
#     attributing it to the rice locus is a plausible-looking but wrong edge. SOS1 is the
#     same trap PubTator3 falls into from the other side, normalizing plant SOS1 to *human*
#     SOS1 (ncbigene:6654) -- see docs/plant_schema.md.
#
# These only matter in the permissive tier (``include_unaudited=True``); the default policy
# admits nothing outside the structural test and the audited allowlist, so a form like WD40
# never gets close. Kept because the audit is the reasoning a future widening needs.
_NOT_RICE_SPECIFIC = frozenset(
    {
        # domains / families / compounds
        "WD40", "R2R3-MYB", "SNRK1", "SNRK2", "AMT1", "ATP6", "GA20",
        # canonical owner is another organism
        "HSP70", "HSP90", "NPR1", "BRI1", "GSK3", "SKP1", "P5CS",
        "SOS1", "ADH1", "ACT1", "BAK1", "CUC2", "ABI5", "BZR1",
    }
)

# Non-Os-prefixed rice symbols confirmed correct by reading their matches on the real corpus.
# These are structurally indistinguishable from the rejects above -- `GHD7` and `WD40` are
# both letters-plus-digits -- so nothing but the biology separates them, which is why this is
# an explicit list rather than a rule. Drawn from the 120 most-matched forms on a full-corpus
# dry run; extend it as more of the tail gets verified.
_AUDITED_RICE_SYMBOLS = frozenset(
    {
        "HD3A", "XA21", "EHD1", "GHD7", "RFT1", "SLR1", "SUB1", "XA13", "LSI1", "LSI2",
        "DEP1", "OSH1", "BADH2", "RAMY3D", "NAL1", "IPA1", "PBZ1", "WRKY45", "GID1",
        "PI54", "RGA1", "GLUB-1", "DTH8", "PHO1", "CENH3", "PR1A", "PUP1", "TMS5",
        "QSH1", "GN1A", "RAB16A", "SNAC1", "ZEP1", "ISA1", "PR10",
    }
)


def is_rice_specific(form: str) -> bool:
    """Whether a form is safe *by construction* -- it cannot denote another species' gene,
    an English word, or a protein domain.

    Two shapes qualify: a RAP-DB or MSU/TIGR locus id (`OS01G0194300`, `LOC_OS01G05060`),
    and an `Os`-prefixed symbol of 5+ characters (`OSNRAMP5`, `OSHKT1`) -- the rice
    nomenclature convention, which no English word and no other organism's symbol collides
    with. This is 55% of all matches on the real corpus.
    """
    return bool(_RAP_ID.fullmatch(form) or _MSU_ID.fullmatch(form)) or (form.startswith("OS") and len(form) >= 5)


def is_admissible(form: str, include_unaudited: bool = False) -> bool:
    """Whether an uppercased surface form may enter the gazetteer.

    Default (conservative) policy: structurally rice-specific, or on the audited allowlist.
    Nothing else. Chosen because a false `Paper -> Gene` edge doesn't stay contained -- the
    graph chains it onward through PARTICIPATES_IN and ASSOCIATED_WITH, so one bad match
    yields a complete, fluent, wrong answer to a trait query. Measured cost of this policy on
    the 51,166-paper corpus: 7,792 papers matched instead of 11,421.

    ``include_unaudited=True`` adds the permissive tier -- any 4+ character symbol mixing
    letters and digits, minus units and minus the audited rejects. That tier reaches 22.3% of
    papers but ~36% of its matches are forms nobody has verified. It exists so the planned LLM
    disambiguation pass has a defined candidate set, not for routine loading.
    """
    if is_rice_specific(form):
        return True
    if form in _AUDITED_RICE_SYMBOLS:
        return True
    if not include_unaudited:
        return False
    if form in _NOT_RICE_SPECIFIC:
        return False
    unit_like = _ALPHA_HYPHEN_NUMBER.match(form)
    if unit_like:
        stem = unit_like.group(1)
        if len(stem) <= _MAX_SAFE_ALPHA_STEM or stem in _UNIT_STEMS:
            return False
    return len(form) >= 4 and bool(re.search(r"\d", form)) and bool(re.search(r"[A-Z]{2}", form))


def _row_forms(row: dict) -> Iterator[str]:
    """Every surface form an Oryzabase row offers for its gene, uppercased."""
    for column in _FORM_COLUMNS:
        for token in re.split(r"[,;|]", row.get(column) or ""):
            token = token.strip().strip(_DECORATIONS)
            if len(token) >= _MIN_FORM_LENGTH:
                yield token.upper()


def _resolve_gene(row: dict, crosswalk: dict[str, str]) -> str | None:
    """Resolve a row's gene to an existing ``ncbigene:`` key, locus ids first.

    Same precedence rule as ingest/oryzabase.py: a locus id identifies exactly one gene,
    a symbol may not, so a row carrying both must resolve via the locus id.
    """
    for pattern, column in ((_RAP_ID, _RAP_COLUMN), (_MSU_ID, _MSU_COLUMN)):
        for match in pattern.findall(row.get(column) or ""):
            gene_id = crosswalk.get(match.upper())
            if gene_id:
                return gene_id
    for column in _SYMBOL_COLUMNS:
        for token in re.split(r"[,;|]", row.get(column) or ""):
            gene_id = crosswalk.get(token.strip().strip(_DECORATIONS).upper())
            if gene_id:
                return gene_id
    return None


def build_gazetteer(
    path: str | Path,
    crosswalk: dict[str, str],
    encoding: str = "utf-8-sig",
    include_unaudited: bool = False,
) -> dict[str, str]:
    """Build ``{uppercased surface form: ncbigene:<id>}`` from the Oryzabase export.

    Covers *every* resolvable row, not only the trait-annotated ones: on a 6,000-paper sample
    that widens coverage from 1,747 to 2,067 distinct genes, and 94% of the matched genes
    still reach a Trait or Pathway (the extra ones have GAF pathway edges without an
    Oryzabase trait annotation).

    ``include_unaudited`` is passed through to ``is_admissible``; the default is the
    conservative policy. First writer wins per form, so a form two rows disagree on keeps the
    earlier row's gene rather than flip-flopping between runs.
    """
    gazetteer: dict[str, str] = {}
    with open(path, encoding=encoding, errors="replace", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            gene_id = _resolve_gene(row, crosswalk)
            if gene_id is None:
                continue
            for form in _row_forms(row):
                if is_admissible(form, include_unaudited=include_unaudited):
                    gazetteer.setdefault(form, gene_id)
    return gazetteer


def _candidate_forms(span: str) -> Iterator[str]:
    """The forms a single text span could be: the whole span, its hyphen/dot-separated
    pieces, and any embedded letters+digits symbol. Yields uppercased."""
    span = span.upper().rstrip(".,;:")
    yield span
    for piece in re.split(r"[-.]", span):
        if piece:
            yield piece
    for match in _EMBEDDED_SYMBOL.finditer(span):
        yield match.group(0)


def find_gene_mentions(text: str, gazetteer: dict[str, str]) -> dict[str, str]:
    """Find rice gene mentions in ``text``, returning ``{ncbigene:<id>: matched form}``.

    One entry per gene regardless of how many times or under how many synonyms it appears
    -- MENTIONS is a Paper->Gene edge, not a per-occurrence record. Token lookup against a
    dict rather than a regex alternation over ~75K forms, which is orders of magnitude
    faster and behaves identically.
    """
    found: dict[str, str] = {}
    for span in _SPAN.finditer(text or ""):
        for form in _candidate_forms(span.group(0)):
            gene_id = gazetteer.get(form)
            if gene_id is not None:
                found.setdefault(gene_id, form)
    return found


def extract_mentions(title: str | None, abstract: str | None, gazetteer: dict[str, str]) -> list[EntityMention]:
    """Gene mentions for one paper, as EntityMentions ready for upsert.mentions.

    ``name`` is the matched surface form, which is why bootstrapped Gene nodes from this
    path get a readable symbol where Reactome/GAF-bootstrapped ones don't.
    """
    text = f"{title or ''} {abstract or ''}"
    return [
        EntityMention(vertex_type="Gene", entity_id=gene_id, name=form)
        for gene_id, form in find_gene_mentions(text, gazetteer).items()
    ]
