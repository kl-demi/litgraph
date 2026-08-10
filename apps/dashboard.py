"""LitGraph dashboard: browse what's been ingested, run queries, see results as a graph.

Pages: Search (entity matches + keyword/semantic paper search), Overview (coverage and
corpus tables), Query (raw SQL/Cypher, like Studio's query tab), Database (type cards +
schema detail). The Paper and Gene pages are reached from links rather than the sidebar,
at ?paper=<id> and ?gene=<id>.

Run with: streamlit run apps/dashboard.py
"""

import time
from urllib.parse import quote

import httpx
import streamlit as st

import spokebio.schema_ext  # noqa: F401  -- registers bio types so type_counts sees them
from litgraph.config import get_settings
from litgraph.db.arcadedb_http import list_databases, run_query, run_raw
from litgraph.db.context import set_database
from litgraph.search.citations import get_citing_papers, get_references, most_cited
from litgraph.search.entities import search_entities
from litgraph.search.genes import (
    co_mentioned_genes,
    get_gene,
    papers_mentioning_gene,
    pathways_for_gene,
    traits_for_gene,
)
from litgraph.search.keyword import keyword_search
from litgraph.search.papers import authors_of, categories_of, genes_in, get_paper
from litgraph.search.semantic import semantic_search
from litgraph.search.stats import latest_papers, overview, top_authors, type_counts

st.set_page_config(page_title="LitGraph", page_icon="📚", layout="wide")

_NODE_STYLE = {
    "Paper": ("#dbeafe", "#1d4ed8"),
    "Gene": ("#dcfce7", "#15803d"),
    "Pathway": ("#ffedd5", "#c2410c"),
    "Compound": ("#f3e8ff", "#7e22ce"),
    "Organism": ("#fef9c3", "#a16207"),
    "Category": ("#e5e7eb", "#4b5563"),
    "Trait": ("#cffafe", "#0e7490"),
}


def _md_escape(text: str) -> str:
    """Escape the brackets that would otherwise break a markdown link label."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _paper_url(paper_id: str) -> str:
    """A shareable in-app link to a paper, read back by the router at the bottom."""
    return f"?paper={quote(paper_id, safe='')}"


def _gene_url(gene_id: str) -> str:
    return f"?gene={quote(gene_id, safe='')}"


def _link(label: str | None, url: str, fallback: str) -> str:
    return f"[{_md_escape(label or fallback)}]({url})"


def _dot(nodes: dict[str, tuple[str, str]], edges: list[tuple[str, str, str]]) -> str:
    """Build a DOT digraph. nodes: id -> (label, kind); edges: (src, dst, label)."""

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        "digraph G {",
        '  rankdir=LR; bgcolor=transparent; node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10]; edge [fontname="Helvetica", fontsize=8, color=gray50];',
    ]
    for node_id, (label, kind) in nodes.items():
        fill, border = _NODE_STYLE.get(kind, ("#f3f4f6", "#374151"))
        lines.append(f'  "{esc(node_id)}" [label="{esc(label)}", fillcolor="{fill}", color="{border}"];')
    for src, dst, label in edges:
        lines.append(f'  "{esc(src)}" -> "{esc(dst)}" [label="{esc(label)}"];')
    lines.append("}")
    return "\n".join(lines)


def _clip(text: str | None, n: int = 45) -> str:
    text = text or "?"
    return text if len(text) <= n else text[: n - 1] + "…"


# The `db` argument on the cached helpers below is only a cache key: routing to the
# selected database happens via set_database() in the sidebar.


@st.cache_data(ttl=300)
def _databases() -> list[str]:
    return list_databases()


@st.cache_data(ttl=300)
def _overview(db: str):
    return overview()


@st.cache_data(ttl=300)
def _type_counts(db: str):
    return type_counts()


@st.cache_data(ttl=300)
def _schema_types(db: str) -> list[dict]:
    return run_query("select from schema:types")


def _coverage(label: str, part: int, whole: int, unit: str) -> None:
    """One labelled progress bar, stating the denominator it is a share of."""
    share = part / whole if whole else 0.0
    st.progress(share, text=f"**{label}** — {share:.1%}  ·  {part:,} of {whole:,} {unit}")


def page_overview() -> None:
    st.title("📚 LitGraph overview")
    data = _overview(st.session_state["db"])

    # `papers` already excludes stubs, so the record total is the two added together.
    full, stubs = data["papers"], data["stubs"]
    records = full + stubs

    cols = st.columns(3)
    cols[0].metric("Paper records", f"{records:,}")
    cols[1].metric("Full papers", f"{full:,}")
    cols[2].metric("Authors", f"{data['authors']:,}")
    if data.get("earliest_published") and data.get("latest_published"):
        st.caption(f"Published range: {data['earliest_published']} → {data['latest_published']}")

    st.subheader("Paper coverage")
    _coverage("Full records", full, records, "Paper records")
    _coverage("Enriched", data["enriched"], full, "full papers")
    _coverage("Embedded", data["embedded"], full, "full papers")
    st.caption(
        "A stub is a placeholder created by a citation edge — a title at most — so it can be "
        "neither enriched nor embedded. Both are therefore measured against full papers, not "
        "against every record: on a citation-heavy graph the two denominators differ by more "
        "than tenfold."
    )

    if data.get("by_source"):
        st.subheader("By source")
        st.dataframe(data["by_source"], width="stretch")

    st.subheader("Node and edge counts")
    counts = _type_counts(st.session_state["db"])
    col1, col2 = st.columns(2)
    with col1:
        st.dataframe(
            [{"node type": k, "count": v} for k, v in counts["nodes"].items()],
            width="stretch",
            hide_index=True,
        )
    with col2:
        st.dataframe(
            [{"edge type": k, "count": v} for k, v in counts["edges"].items()],
            width="stretch",
            hide_index=True,
        )

    st.subheader("Corpus tables")
    # Behind a button: these are live scans over every Paper row and AUTHORED edge.
    if st.button("Load tables (slow: full scans)"):
        col1, col2 = st.columns(2)
        with col1:
            st.caption("Latest papers")
            st.dataframe(latest_papers(10), width="stretch", hide_index=True)
            st.caption("Top authors")
            st.dataframe(top_authors(10), width="stretch", hide_index=True)
        with col2:
            st.caption("Most cited")
            cited = most_cited(limit=10)
            if cited:
                st.dataframe(cited, width="stretch", hide_index=True)
            else:
                st.info("This database has no citation edges.")


# Both searches return a "score", but they mean opposite things, so each names its own.
_SEARCH_MODES = {
    "Keyword": (keyword_search, "relevance", "Matches the words you typed."),
    "Semantic": (semantic_search, "distance", "Finds papers that mean something similar, in whatever words."),
}

_ENTITY_LABELS = ("Gene", "Pathway", "Trait", "Compound")


@st.cache_data(ttl=300, show_spinner=False)
def _search_papers(db: str, mode: str, query: str) -> list[dict]:
    return _SEARCH_MODES[mode][0](query, top_k=10)


@st.cache_data(ttl=300, show_spinner=False)
def _search_entities(db: str, query: str) -> dict[str, list[dict]]:
    """Name matches per entity type, skipping types this database doesn't have."""
    found = {}
    for label in _ENTITY_LABELS:
        rows = search_entities(label, query, limit=5)
        if rows:
            found[label] = rows
    return found


def _paper_hit(row: dict, score_label: str) -> None:
    title = _md_escape(row.get("title") or "Untitled")
    st.markdown(f"**[{title}]({_paper_url(row['id'])})**")
    bits = []
    if row.get("pmid"):
        bits.append(f"[PMID {row['pmid']}](https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/)")
    if row.get("arxiv_id"):
        bits.append(f"[arXiv {row['arxiv_id']}](https://arxiv.org/abs/{row['arxiv_id']})")
    if row.get("score") is not None:
        bits.append(f"{score_label} {row['score']:.3f}")
    if bits:
        st.caption(" · ".join(bits))
    if row.get("abstract"):
        st.write(_clip(row["abstract"], 320))


def page_search() -> None:
    st.title("🔎 Search")
    query = st.text_input(
        "Search",
        placeholder="a topic, a gene, a pathway…",
        label_visibility="collapsed",
    ).strip()
    if not query:
        st.caption("Search papers by topic, or find a gene, pathway or trait by name.")
        return

    db = st.session_state["db"]
    try:
        entities = _search_entities(db, query)
    except Exception as exc:
        entities = {}
        st.caption(f"Entity lookup unavailable: {exc}")
    if entities:
        cols = st.columns(len(entities))
        for col, (label, rows) in zip(cols, entities.items()):
            with col.container(border=True):
                st.markdown(f"**{label}s**")
                for row in rows:
                    # Gene is the only entity type with a page so far; the rest stay plain
                    # text rather than becoming links that go nowhere.
                    if label == "Gene":
                        st.markdown(_link(row["name"], _gene_url(row["id"]), row["id"]))
                    else:
                        st.markdown(_md_escape(row["name"] or row["id"]))
                    st.caption(row["id"])

    st.subheader("Papers")
    mode = st.radio("Mode", list(_SEARCH_MODES), horizontal=True, label_visibility="collapsed")
    _, score_label, help_text = _SEARCH_MODES[mode]
    st.caption(help_text)

    # The embedding service sleeps when idle; the query that wakes it measured ~75s,
    # against ~1s once warm.
    waiting = "Searching…" if mode == "Keyword" else "Searching… (up to a minute if the embedding service is asleep)"
    started = time.perf_counter()
    with st.spinner(waiting):
        try:
            rows = _search_papers(db, mode, query)
        except Exception as exc:  # embedding service down, index missing on this database, ...
            st.error(f"Search unavailable: {exc}")
            return
    elapsed = time.perf_counter() - started

    if not rows:
        st.info("No papers matched. Try fewer words, or switch to semantic search.")
        return
    st.caption(f"{len(rows)} papers in {elapsed:.1f}s")
    for row in rows:
        st.divider()
        _paper_hit(row, score_label)


@st.cache_data(ttl=300, show_spinner=False)
def _paper_bundle(db: str, paper_id: str) -> dict:
    """The record plus every neighbour the page shows, as one cache entry."""
    return {
        "paper": get_paper(paper_id),
        "authors": authors_of(paper_id),
        "genes": genes_in(paper_id),
        "categories": categories_of(paper_id),
        "references": get_references(paper_id, limit=10),
        "citing": get_citing_papers(paper_id, limit=10),
    }


def page_paper(paper_id: str) -> None:
    st.markdown("[← Back](?)")
    data = _paper_bundle(st.session_state["db"], paper_id)
    paper = data["paper"]
    if paper is None:
        st.error(f"No paper with id `{paper_id}` in this database.")
        return

    st.title(paper.get("title") or "Untitled")

    provenance = [
        str(paper[field])[:60]
        for field in ("published_date", "journal_ref", "source")
        if paper.get(field)
    ]
    if provenance:
        st.caption(" · ".join(provenance))

    links = []
    if paper.get("pmid"):
        links.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/)")
    if paper.get("doi"):
        links.append(f"[DOI](https://doi.org/{paper['doi']})")
    if paper.get("arxiv_id"):
        links.append(f"[arXiv](https://arxiv.org/abs/{paper['arxiv_id']})")
    if links:
        st.markdown(" · ".join(links))

    if paper.get("is_stub"):
        st.warning("Stub: known only from a citation edge. Nothing beyond the title has been fetched.")

    abstract = paper.get("abstract")
    if abstract:
        st.subheader("Abstract")
        if len(abstract) > 1500:
            st.write(abstract[:1500].rstrip() + "…")
            with st.expander("Show full abstract"):
                st.write(abstract)
        else:
            st.write(abstract)

    authors, categories, genes = data["authors"], data["categories"], data["genes"]
    col1, col2 = st.columns(2)
    with col1:
        st.subheader(f"Authors ({len(authors)})")
        if authors:
            st.write(", ".join(a["name"] for a in authors))
        else:
            st.caption("No authors recorded.")
    with col2:
        st.subheader(f"Subjects ({len(categories)})")
        if categories:
            st.write(", ".join(c["name"] or c["code"] for c in categories))
        else:
            st.caption("No subject terms recorded.")

    st.subheader(f"Genes mentioned ({len(genes)})")
    if genes:
        st.markdown(", ".join(_link(g["name"], _gene_url(g["gene_id"]), g["gene_id"]) for g in genes))
    else:
        st.caption("No genes have been extracted from this paper yet.")

    if genes or categories:
        nodes = {paper_id: (_clip(paper.get("title"), 40), "Paper")}
        edges = []
        for gene in genes[:12]:
            nodes[gene["gene_id"]] = (gene["name"] or gene["gene_id"], "Gene")
            edges.append((paper_id, gene["gene_id"], "MENTIONS"))
        for category in categories[:8]:
            nodes[category["code"]] = (_clip(category["name"] or category["code"], 28), "Category")
            edges.append((paper_id, category["code"], "IN_CATEGORY"))
        st.graphviz_chart(_dot(nodes, edges), width="stretch")

    references, citing = data["references"], data["citing"]
    if not (references or citing):
        st.caption("No citation edges for this paper in this database.")
        return
    col1, col2 = st.columns(2)
    with col1:
        st.subheader(f"References ({len(references)})")
        for row in references:
            st.markdown(f"[{_md_escape(row.get('title') or row['id'])}]({_paper_url(row['id'])})")
    with col2:
        st.subheader(f"Cited by ({len(citing)})")
        for row in citing:
            st.markdown(f"[{_md_escape(row.get('title') or row['id'])}]({_paper_url(row['id'])})")


@st.cache_data(ttl=300, show_spinner=False)
def _gene_bundle(db: str, gene_id: str) -> dict:
    return {
        "gene": get_gene(gene_id),
        "papers": papers_mentioning_gene(gene_id, limit=25),
        "pathways": pathways_for_gene(gene_id, limit=25),
        "traits": traits_for_gene(gene_id, limit=25),
        "co_mentioned": co_mentioned_genes(gene_id, limit=10),
    }


def page_gene(gene_id: str) -> None:
    st.markdown("[← Back](?)")
    data = _gene_bundle(st.session_state["db"], gene_id)
    gene = data["gene"]
    if gene is None:
        st.error(f"No gene with id `{gene_id}` in this database.")
        return

    papers, pathways = data["papers"], data["pathways"]
    traits, co_mentioned = data["traits"], data["co_mentioned"]

    st.title(gene.get("name") or gene_id)
    identifiers = [gene_id] + ([gene["locus_id"]] if gene.get("locus_id") else [])
    st.caption(" · ".join(identifiers))

    cols = st.columns(4)
    cols[0].metric("Papers", len(papers))
    cols[1].metric("Pathways", len(pathways))
    cols[2].metric("Traits", len(traits))
    cols[3].metric("Co-mentioned", len(co_mentioned))
    st.caption("Counts reflect what this page loads, capped at 25 per section.")

    nodes = {gene_id: (gene.get("name") or gene_id, "Gene")}
    edges = []
    for pathway in pathways[:8]:
        nodes[pathway["pathway_id"]] = (_clip(pathway["name"], 28), "Pathway")
        edges.append((gene_id, pathway["pathway_id"], pathway.get("evidence_code") or "PARTICIPATES_IN"))
    for trait in traits[:8]:
        nodes[trait["trait_id"]] = (_clip(trait["name"], 28), "Trait")
        edges.append((gene_id, trait["trait_id"], "ASSOCIATED_WITH"))
    for other in co_mentioned[:6]:
        nodes[other["gene_id"]] = (other["name"] or other["gene_id"], "Gene")
        edges.append((gene_id, other["gene_id"], f"co-mentioned ×{other['shared_papers']}"))
    if edges:
        st.graphviz_chart(_dot(nodes, edges), width="stretch")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader(f"Pathways ({len(pathways)})")
        if pathways:
            for row in pathways:
                st.markdown(f"{row['name']}")
                st.caption(f"{row['pathway_id']} · {row.get('source_db') or '?'} · {row.get('evidence_code') or 'no evidence code'}")
        else:
            st.caption("No pathway memberships recorded for this gene.")
    with col2:
        st.subheader(f"Traits ({len(traits)})")
        if traits:
            for row in traits:
                st.markdown(f"{row['name']}")
                st.caption(f"{row['trait_id']} · {row.get('source_db') or '?'}")
        else:
            st.caption("No trait associations in this database.")

    st.subheader(f"Co-mentioned genes ({len(co_mentioned)})")
    if co_mentioned:
        st.markdown(
            " · ".join(
                f"{_link(row['name'], _gene_url(row['gene_id']), row['gene_id'])} ({row['shared_papers']})"
                for row in co_mentioned
            )
        )
    else:
        st.caption("This gene shares no papers with another gene.")

    st.subheader(f"Papers mentioning this gene ({len(papers)})")
    if not papers:
        st.caption("No papers mention this gene.")
        return
    for row in papers:
        st.markdown(_link(row.get("title"), _paper_url(row["id"]), row["id"]))
        st.caption(" · ".join(filter(None, (row.get("pmid") and f"PMID {row['pmid']}", row.get("source")))))


_QUERY_LANGUAGES = {"SQL": "sql", "SQL Script": "sqlscript", "Cypher": "opencypher"}


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
        return payload.get("detail") or payload.get("error") or exc.response.text
    except ValueError:
        return exc.response.text


def page_query() -> None:
    st.title("⌨️ Query")
    col_lang, col_limit, col_ro = st.columns([2, 1, 1], vertical_alignment="bottom")
    language = col_lang.selectbox("Language", list(_QUERY_LANGUAGES))
    limit = col_limit.selectbox("Limit", [20, 50, 100, 500], index=1)
    read_only = col_ro.toggle(
        "Read-only",
        value=True,
        help="On: query endpoint (rejects writes). Off: command endpoint. "
        "SQL Script always runs on the command endpoint.",
    )
    command = st.text_area(
        "Statement",
        height=140,
        placeholder="select from Paper where is_stub = false limit 10",
    )
    if not (st.button("Run", type="primary") and command.strip()):
        return

    lang = _QUERY_LANGUAGES[language]
    started = time.perf_counter()
    try:
        rows = run_raw(command, language=lang, read_only=read_only, limit=limit)
    except httpx.HTTPStatusError as exc:
        st.error(_http_error_detail(exc))
        return
    except httpx.HTTPError as exc:
        st.error(f"Request failed: {exc}")
        return
    elapsed = time.perf_counter() - started

    st.caption(f"{len(rows):,} rows in {elapsed:.2f}s")
    if not rows:
        st.info("No results.")
        return
    tab_table, tab_json = st.tabs(["Table", "JSON"])
    with tab_table:
        try:
            st.dataframe(rows, width="stretch")
        except Exception:  # nested/mixed values a dataframe can't hold
            st.warning("Result is not tabular; see the JSON tab.")
    with tab_json:
        st.json(rows)


def _type_cards(rows: list[dict]) -> None:
    per_row = 4
    for start in range(0, len(rows), per_row):
        cols = st.columns(per_row)
        for col, row in zip(cols, rows[start : start + per_row]):
            fill, border = _NODE_STYLE.get(row["name"], ("#f3f4f6", "#374151"))
            with col.container(border=True):
                st.markdown(
                    f'<span style="color:{border}; font-weight:600">{row["name"]}</span>',
                    unsafe_allow_html=True,
                )
                st.markdown(f"### {row.get('records', 0):,}")
                st.caption(f"{len(row.get('properties', []))} properties · {len(row.get('indexes', []))} indexes")


def page_database() -> None:
    db = st.session_state["db"]
    st.title(f"🗄️ Database: {db}")
    try:
        types = _schema_types(db)
    except httpx.HTTPError as exc:
        st.error(f"Could not load schema: {exc}")
        return

    by_count = sorted(types, key=lambda t: t.get("records", 0), reverse=True)
    nodes = [t for t in by_count if t["type"] == "vertex"]
    edges = [t for t in by_count if t["type"] == "edge"]
    documents = [t for t in by_count if t["type"] not in ("vertex", "edge")]

    tab_types, tab_schema = st.tabs(["Types", "Schema"])

    with tab_types:
        st.subheader(f"Nodes ({len(nodes)} types, {sum(t.get('records', 0) for t in nodes):,} records)")
        _type_cards(nodes)
        st.subheader(f"Edges ({len(edges)} types, {sum(t.get('records', 0) for t in edges):,} records)")
        _type_cards(edges)
        if documents:
            st.subheader(f"Documents ({len(documents)} types)")
            _type_cards(documents)

    with tab_schema:
        names = [t["name"] for t in sorted(types, key=lambda t: (t["type"], t["name"]))]
        chosen = st.selectbox("Type", names, format_func=lambda n: n)
        detail = next(t for t in types if t["name"] == chosen)
        st.caption(
            f"{detail['type']} · {detail.get('records', 0):,} records"
            + (f" · extends {', '.join(detail['parentTypes'])}" if detail.get("parentTypes") else "")
        )
        st.markdown("**Properties**")
        props = detail.get("properties", [])
        if props:
            st.dataframe(
                [{"name": p["name"], "type": p["type"], "default": p.get("default")} for p in props],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No declared properties (schemaless records may still carry fields).")
        st.markdown("**Indexes**")
        indexes = detail.get("indexes", [])
        if indexes:
            st.dataframe(
                [
                    {
                        "name": i["name"],
                        "type": i["type"],
                        "unique": i.get("unique"),
                        "properties": ", ".join(i.get("properties", [])),
                        "automatic": i.get("automatic"),
                    }
                    for i in indexes
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No indexes.")


_PAGES = {
    "Search": page_search,
    "Overview": page_overview,
    "Query": page_query,
    "Database": page_database,
}

# The dashboard's own default, deliberately not ARCADEDB_DATABASE: cron reads that from
# .env, so pointing the UI at a different graph must not go through it.
_DEFAULT_DB = "rice"

try:
    _db_options = _databases()
except httpx.HTTPError:
    _db_options = [get_settings().arcadedb_database]
_default = _DEFAULT_DB if _DEFAULT_DB in _db_options else get_settings().arcadedb_database
_db = st.sidebar.selectbox(
    "Database",
    _db_options,
    index=_db_options.index(_default) if _default in _db_options else 0,
)
st.session_state["db"] = _db
set_database(_db)

page = st.sidebar.radio("LitGraph", list(_PAGES))

# A ?paper=<id> link overrides the sidebar, so every paper has its own shareable URL
# without also having to be a navigation destination.
_paper_id = st.query_params.get("paper")
_gene_id = st.query_params.get("gene")
if _paper_id:
    page_paper(_paper_id)
elif _gene_id:
    page_gene(_gene_id)
else:
    _PAGES[page]()
