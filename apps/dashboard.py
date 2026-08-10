"""LitGraph dashboard: browse what's been ingested, run queries, see results as a graph.

Pages: Overview (counts), Papers (keyword/semantic search + stats tables), Citations
(references/citing + neighborhood graph), Biology (gene explorer + neighborhood graph).

Run with: streamlit run apps/dashboard.py
"""

import streamlit as st

import spokebio.schema_ext  # noqa: F401  -- registers bio types so type_counts sees them
from litgraph.search.citations import get_citing_papers, get_references, most_cited
from litgraph.search.genes import co_mentioned_genes, papers_mentioning_gene, pathways_for_gene, search_genes
from litgraph.search.keyword import keyword_search
from litgraph.search.semantic import semantic_search
from litgraph.search.stats import latest_papers, overview, top_authors, type_counts

st.set_page_config(page_title="LitGraph", page_icon="📚", layout="wide")

_NODE_STYLE = {
    "Paper": ("#dbeafe", "#1d4ed8"),
    "Gene": ("#dcfce7", "#15803d"),
    "Pathway": ("#ffedd5", "#c2410c"),
    "Compound": ("#f3e8ff", "#7e22ce"),
    "Organism": ("#fef9c3", "#a16207"),
}


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


@st.cache_data(ttl=300)
def _overview():
    return overview()


@st.cache_data(ttl=300)
def _type_counts():
    return type_counts()


def page_overview() -> None:
    st.title("📚 LitGraph overview")
    data = _overview()

    cols = st.columns(5)
    cols[0].metric("Papers", f"{data['papers']:,}")
    cols[1].metric("Stubs", f"{data['stubs']:,}")
    cols[2].metric("Enriched", f"{data['enriched']:,}")
    cols[3].metric("Embedded", f"{data['embedded']:,}")
    cols[4].metric("Authors", f"{data['authors']:,}")
    if data.get("earliest_published") and data.get("latest_published"):
        st.caption(f"Published range: {data['earliest_published']} → {data['latest_published']}")

    if data.get("by_source"):
        st.subheader("By source")
        st.dataframe(data["by_source"], width="stretch")

    st.subheader("Node and edge counts")
    counts = _type_counts()
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


def _search_results(search_fn, query: str) -> None:
    try:
        rows = search_fn(query, top_k=10)
    except Exception as exc:  # embedding server down, index missing on this database, ...
        st.error(f"Search unavailable: {exc}")
        return
    st.dataframe(
        [{k: r.get(k) for k in ("id", "title", "score")} for r in rows],
        width="stretch",
        hide_index=True,
    )


def page_papers() -> None:
    st.title("📄 Papers")
    tab_keyword, tab_semantic, tab_tables = st.tabs(["Keyword search", "Semantic search", "Stats"])

    with tab_keyword:
        query = st.text_input("Keyword query", placeholder="e.g. transformer attention", key="kw")
        if query:
            _search_results(keyword_search, query)

    with tab_semantic:
        query = st.text_input("Semantic query", placeholder="e.g. how do plants respond to drought", key="sem")
        if query:
            _search_results(semantic_search, query)

    with tab_tables:
        # Behind a button: st.tabs renders every tab on page load, and these are live
        # scans over millions of Paper rows / AUTHORED edges.
        if st.button("Load stats tables (slow: full scans)"):
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Latest papers")
                st.dataframe(latest_papers(10), width="stretch", hide_index=True)
                st.subheader("Top authors")
                st.dataframe(top_authors(10), width="stretch", hide_index=True)
            with col2:
                st.subheader("Most cited")
                st.dataframe(most_cited(limit=10), width="stretch", hide_index=True)


def page_citations() -> None:
    st.title("🔗 Citations")
    paper_id = st.text_input("Paper id (arXiv id or PMID)", placeholder="e.g. 2101.00001 or 33574319")
    if not paper_id:
        st.stop()

    references = get_references(paper_id, limit=15)
    citing = get_citing_papers(paper_id, limit=15)
    if not references and not citing:
        st.warning("No citation edges found for that id.")
        st.stop()

    nodes = {paper_id: (_clip(paper_id), "Paper")}
    edges = []
    for row in references:
        nodes[row["id"]] = (_clip(row["title"]), "Paper")
        edges.append((paper_id, row["id"], "CITES"))
    for row in citing:
        nodes[row["id"]] = (_clip(row["title"]), "Paper")
        edges.append((row["id"], paper_id, "CITES"))
    st.graphviz_chart(_dot(nodes, edges), use_container_width=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader(f"References ({len(references)})")
        st.dataframe(references, width="stretch", hide_index=True)
    with col2:
        st.subheader(f"Cited by ({len(citing)})")
        st.dataframe(citing, width="stretch", hide_index=True)


def page_biology() -> None:
    st.title("🧬 Biology")
    query = st.text_input("Search for a gene by name", placeholder="e.g. TP53, insulin, BRCA1")
    if not query:
        st.stop()

    matches = search_genes(query)
    if not matches:
        st.warning(f"No genes found matching '{query}'.")
        st.stop()

    options = {f"{m['name']} ({m['gene_id']})": m["gene_id"] for m in matches}
    choice = st.selectbox("Matching genes", options.keys())
    gene_id = options[choice]

    papers = papers_mentioning_gene(gene_id, limit=10)
    pathways = pathways_for_gene(gene_id, limit=10)
    co_mentioned = co_mentioned_genes(gene_id, limit=8)

    nodes = {gene_id: (choice.split(" (")[0], "Gene")}
    edges = []
    for row in papers:
        nodes[row["id"]] = (_clip(row["title"]), "Paper")
        edges.append((row["id"], gene_id, row.get("source") or "MENTIONS"))
    for row in pathways:
        nodes[row["pathway_id"]] = (_clip(row["name"]), "Pathway")
        edges.append((gene_id, row["pathway_id"], row.get("evidence_code") or "PARTICIPATES_IN"))
    for row in co_mentioned:
        nodes[row["gene_id"]] = (row["name"] or row["gene_id"], "Gene")
        edges.append((gene_id, row["gene_id"], f"co-mentioned ×{row['shared_papers']}"))
    st.graphviz_chart(_dot(nodes, edges), use_container_width=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.subheader(f"Papers ({len(papers)})")
        st.dataframe(papers, width="stretch", hide_index=True)
    with col2:
        st.subheader(f"Pathways ({len(pathways)})")
        st.dataframe(pathways, width="stretch", hide_index=True)
    with col3:
        st.subheader(f"Co-mentioned genes ({len(co_mentioned)})")
        st.dataframe(co_mentioned, width="stretch", hide_index=True)


_PAGES = {
    "Overview": page_overview,
    "Papers": page_papers,
    "Citations": page_citations,
    "Biology": page_biology,
}

page = st.sidebar.radio("LitGraph", list(_PAGES))
_PAGES[page]()
