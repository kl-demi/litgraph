"""LitGraph dashboard: browse what's been ingested, run queries, see results as a graph.

Pages: Search (papers + raw SQL/Cypher tabs), Overview (coverage and corpus tables),
Database (type cards + schema detail). Paper and Gene views are reached from links and
canvas clicks, navigate in-session (same tab, Back restores where you were), and mirror
to ?paper=<id> / ?gene=<id> so they stay shareable.

Run with: streamlit run apps/dashboard.py
"""

import re
import threading
import time

import httpx
import streamlit as st
from st_link_analysis import EdgeStyle, Event, NodeStyle, st_link_analysis

import spokebio.schema_ext  # noqa: F401  -- registers bio types so type_counts sees them
from litgraph.config import get_settings
from litgraph.db.arcadedb_http import list_databases, run_query, run_raw
from litgraph.db.context import set_database
from litgraph.ingest.embeddings import embed_texts
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


@st.cache_resource(show_spinner=False)
def _warm_embedding_service() -> bool:
    """Wake the remote embedding pod at app start instead of on the first semantic
    search: it sleeps when idle, and the request that wakes it measured ~75s against
    ~1s warm. cache_resource runs this once per server process, not per session. A
    failure is left for the first real search to report meaningfully."""
    if not get_settings().embedding_service_url:
        return False  # in-process model: nothing to wake, and don't load torch here

    def _ping() -> None:
        try:
            embed_texts(["warm-up"])
        except Exception:
            pass

    threading.Thread(target=_ping, daemon=True, name="embedding-warmup").start()
    return True


_warm_embedding_service()

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
    """Escape the brackets that would otherwise be parsed as markdown in a label."""
    return text.replace("[", "\\[").replace("]", "\\]")


# In-session navigation. Markdown links to ?paper=... open a new tab and start a fresh
# session (Streamlit hard-targets markdown links _blank), losing query results and the
# db selection -- so internal navigation is widgets + session state instead, and the
# current view is mirrored to the URL only for shareability. External links (PubMed,
# DOI, arXiv) stay markdown links, where the new tab is wanted.


def _nav_to(kind: str, entity_id: str) -> None:
    """on_click/on_change callback: open an entity view, remembering where we came from."""
    st.session_state.setdefault("nav_stack", []).append(st.session_state.get("view"))
    st.session_state["view"] = (kind, entity_id)


def _nav_back() -> None:
    stack = st.session_state.get("nav_stack") or []
    st.session_state["view"] = stack.pop() if stack else None


def _reset_nav() -> None:
    st.session_state["view"] = None
    st.session_state["nav_stack"] = []


def _entity_button(label: str | None, kind: str, entity_id: str, key: str, bold: bool = False) -> None:
    """A link-styled button that navigates in-session to a Paper or Gene view."""
    text = _md_escape(label or entity_id)
    st.button(
        f"**{text}**" if bold else text,
        type="tertiary",
        key=key,
        on_click=_nav_to,
        args=(kind, entity_id),
    )


def _gene_pills(genes: list[tuple[str | None, str]], key: str, label: str = "genes") -> None:
    """Genes as clickable pills; selecting one opens its page. genes: (name, gene_id)."""
    mapping: dict[str, str] = {}
    for name, gene_id in genes:
        text = name or gene_id
        if text in mapping:  # gene names are not unique; disambiguate with the id
            text = f"{text} · {gene_id}"
        mapping[text] = gene_id

    def _go() -> None:
        choice = st.session_state.get(key)
        if choice:
            _nav_to("gene", mapping[choice])

    st.pills(label, list(mapping), key=key, on_change=_go, label_visibility="collapsed")


def _graph_canvas(
    nodes: dict[str, tuple[str, str]],
    edges: list[tuple[str, str, str]],
    key: str,
    height: int = 480,
) -> None:
    """An interactive Cytoscape canvas: pan, zoom, drag; clicking a Paper or Gene node
    opens its page. nodes: id -> (label, kind); edges: (src, dst, label)."""
    elements = {
        "nodes": [
            {"data": {"id": node_id, "label": kind, "name": label}}
            for node_id, (label, kind) in nodes.items()
        ],
        "edges": [
            {"data": {"id": f"__edge-{i}", "source": src, "target": dst, "label": label or ""}}
            for i, (src, dst, label) in enumerate(edges)
        ],
    }
    node_styles = [
        NodeStyle(kind, _NODE_STYLE.get(kind, ("", "#374151"))[1], caption="name")
        for kind in {kind for _, kind in nodes.values()}
    ]
    edge_styles = [
        EdgeStyle(label, caption="label", directed=True) for label in {e[2] or "" for e in edges}
    ]
    event = st_link_analysis(
        elements,
        layout="cose",
        node_styles=node_styles,
        edge_styles=edge_styles,
        height=height,
        key=key,
        events=[Event("node_click", "click tap", "node")],
    )

    # The component's return value is its *last* event and replays on every rerun, so a
    # click is acted on once, by timestamp, or revisiting this page would re-trigger the
    # navigation it caused.
    if not (event and event.get("action") == "node_click"):
        return
    handled_key = f"{key}--handled"
    if event.get("timestamp") == st.session_state.get(handled_key):
        return
    st.session_state[handled_key] = event.get("timestamp")
    target = str(event.get("data", {}).get("target_id", ""))
    kind = nodes.get(target, ("", ""))[1]
    if kind in ("Paper", "Gene"):
        _nav_to("paper" if kind == "Paper" else "gene", target)
        st.rerun()


def _clip(text: str | None, n: int = 45) -> str:
    text = text or "?"
    return text if len(text) <= n else text[: n - 1] + "…"


# Record ids come back from the server, but they are about to be pasted into a SQL
# statement, so they are shape-checked rather than trusted.
_RID = re.compile(r"^#\d+:\d+$")
_MAX_GRAPH_NODES = 60


def _node_label(record: dict) -> str:
    """The most human field on a record: a name, a title, else any identifier."""
    for key in ("name", "title"):
        if record.get(key):
            return _clip(str(record[key]), 30)
    for key, value in record.items():
        if key.endswith("_id") and value:
            return _clip(str(value), 30)
    return record.get("@rid", "?")


def _resolve_rids(rids: list[str]) -> dict[str, dict]:
    """Fetch records an edge points at that the result itself didn't include."""
    safe = [rid for rid in rids if _RID.match(rid)][:_MAX_GRAPH_NODES]
    if not safe:
        return {}
    try:
        rows = run_query(f"SELECT FROM [{','.join(safe)}]")
    except httpx.HTTPError:
        return {}
    return {row["@rid"]: row for row in rows if row.get("@rid")}


def _result_graph(rows: list[dict]) -> tuple[dict, list, str] | None:
    """Canvas nodes and edges for a graph-shaped result, plus a note on anything left out.

    Returns None when the rows carry no record identity — a projection such as
    `SELECT name FROM Gene`, or any Cypher result.
    """
    vertices = {r["@rid"]: r for r in rows if r.get("@cat") == "v" and r.get("@rid")}
    edge_rows = [r for r in rows if r.get("@cat") == "e"]
    if not vertices and not edge_rows:
        return None

    endpoints = {
        str(r[end]) for r in edge_rows for end in ("@out", "@in") if _RID.match(str(r.get(end, "")))
    }
    unresolved = sorted(endpoints - set(vertices))
    vertices |= _resolve_rids(unresolved)
    # Anything still unresolved is drawn by id, so its edges stay visible.
    for rid in endpoints - set(vertices):
        vertices[rid] = {"@rid": rid}

    kept = dict(list(vertices.items())[:_MAX_GRAPH_NODES])
    # Nodes are keyed by natural id where one exists, so clicking a Paper or Gene on the
    # canvas navigates to its page; a @rid is only a key of last resort. Edges arrive
    # keyed by @rid either way and are remapped.
    natural = {"Paper": "id", "Gene": "gene_id"}
    rid_to_node = {
        rid: str(v.get(natural.get(v.get("@type"), ""), "") or rid) for rid, v in kept.items()
    }
    nodes = {rid_to_node[rid]: (_node_label(v), v.get("@type", "")) for rid, v in kept.items()}
    edges = [
        (rid_to_node[str(r["@out"])], rid_to_node[str(r["@in"])], r.get("@type", ""))
        for r in edge_rows
        if str(r.get("@out")) in rid_to_node and str(r.get("@in")) in rid_to_node
    ]
    if not nodes:
        return None

    notes = []
    if len(vertices) > len(nodes):
        notes.append(f"showing {len(nodes)} of {len(vertices)} nodes")
    hidden_edges = len(edge_rows) - len(edges)
    if hidden_edges:
        notes.append(f"{hidden_edges} edges hidden (an endpoint is outside the drawn nodes)")
    return nodes, edges, "; ".join(notes).capitalize()


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
    _entity_button(row.get("title") or "Untitled", "paper", row["id"], key=f"hit-{row['id']}", bold=True)
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


@st.cache_data(ttl=300, show_spinner=False)
def _concept_graph(db: str, papers: tuple[tuple[str, str], ...]) -> tuple[dict, list]:
    """Result papers joined by the genes they mention: hits that share a gene cluster
    together, which is the graph's explanation of why they belong to the same query."""
    nodes: dict[str, tuple[str, str]] = {}
    edges = []
    for paper_id, title in papers:
        nodes[paper_id] = (_clip(title, 30), "Paper")
        for gene in genes_in(paper_id, limit=10):
            nodes[gene["gene_id"]] = (gene["name"] or gene["gene_id"], "Gene")
            edges.append((paper_id, gene["gene_id"], "MENTIONS"))
    return nodes, edges


def _papers_search() -> None:
    query = st.text_input(
        "Search",
        value=st.session_state.get("search_text", ""),
        placeholder="a topic, a gene, a pathway…",
        label_visibility="collapsed",
        key="search_box",
    ).strip()
    # Copied out of widget state: navigating to a paper unmounts the widget (dropping
    # its state), and this copy is what refills it on the way back.
    st.session_state["search_text"] = query
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
                        _entity_button(row["name"], "gene", row["id"], key=f"ent-{row['id']}")
                    else:
                        st.markdown(_md_escape(row["name"] or row["id"]))
                    st.caption(row["id"])

    st.subheader("Papers")
    col_mode, col_view = st.columns(2)
    mode = col_mode.radio("Mode", list(_SEARCH_MODES), horizontal=True, label_visibility="collapsed")
    view = col_view.radio("View", ["List", "Graph"], horizontal=True, label_visibility="collapsed")
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

    if view == "Graph":
        with st.spinner("Linking papers through their genes…"):
            nodes, edges = _concept_graph(db, tuple((r["id"], r.get("title") or "") for r in rows))
        _graph_canvas(nodes, edges, key="search-graph", height=560)
        st.caption(
            "Result papers joined by the genes they mention — papers sharing a gene cluster "
            "together. Click a paper or gene to open its page. A paper with no edges has no "
            "extracted genes yet."
        )
        return
    for row in rows:
        st.divider()
        _paper_hit(row, score_label)


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
        return payload.get("detail") or payload.get("error") or exc.response.text
    except ValueError:
        return exc.response.text


_EDITOR_PLACEHOLDER = {
    "sql": "select from Paper where is_stub = false limit 10",
    "cypher": "MATCH (p:Paper)-[m:MENTIONS]->(g:Gene) RETURN p.title, g.name LIMIT 20",
}


def _query_editor(lang: str) -> None:
    """One raw-query tab: editor, options, and persisted results. lang: 'sql' | 'cypher'."""
    # The form gives ⌘/Ctrl+Enter submit for free.
    with st.form(f"{lang}-form", border=False):
        command = st.text_area(
            "Statement",
            value=st.session_state.get(f"{lang}_text", ""),
            height=140,
            placeholder=_EDITOR_PLACEHOLDER[lang],
            key=f"{lang}_editor",
            label_visibility="collapsed",
        )
        cols = st.columns([1, 1, 1, 3], vertical_alignment="bottom")
        limit = cols[0].selectbox("Limit", [20, 50, 100, 500], index=1, key=f"{lang}_limit")
        read_only = cols[1].toggle(
            "Read-only",
            value=True,
            key=f"{lang}_ro",
            help="On: query endpoint (rejects writes). Off: command endpoint.",
        )
        script = False
        if lang == "sql":
            script = cols[2].toggle(
                "Script",
                value=False,
                key="sql_script",
                help="Multi-statement SQLScript (BEGIN/IF/COMMIT); always runs on the command endpoint.",
            )
        submitted = st.form_submit_button("Run", type="primary")
        st.caption("⌘/Ctrl+Enter also runs.")

    if submitted and command.strip():
        st.session_state[f"{lang}_text"] = command
        language = "sqlscript" if script else ("opencypher" if lang == "cypher" else "sql")
        started = time.perf_counter()
        try:
            rows = run_raw(command, language=language, read_only=read_only, limit=limit)
        except httpx.HTTPStatusError as exc:
            st.session_state.pop(f"{lang}_result", None)
            st.error(_http_error_detail(exc))
            return
        except httpx.HTTPError as exc:
            st.session_state.pop(f"{lang}_result", None)
            st.error(f"Request failed: {exc}")
            return
        # Results outlive the submit in session state: any later interaction (a tab
        # switch, a canvas click) reruns the script with the form back to unsubmitted,
        # and rendering only on submit would blank the results mid-exploration.
        st.session_state[f"{lang}_result"] = {
            "rows": rows,
            "elapsed": time.perf_counter() - started,
            "db": st.session_state["db"],
        }

    result = st.session_state.get(f"{lang}_result")
    if result is None or result["db"] != st.session_state["db"]:
        return
    rows = result["rows"]

    st.caption(f"{len(rows):,} rows in {result['elapsed']:.2f}s")
    if not rows:
        st.info("No results.")
        return
    tab_graph, tab_table, tab_json = st.tabs(["Graph", "Table", "JSON"])
    with tab_graph:
        graph = _result_graph(rows)
        if graph is None:
            if lang == "cypher":
                st.info(
                    "Cypher over HTTP returns plain property maps with no record identity, "
                    "so its results can't be drawn — use the SQL tab for graph views."
                )
            else:
                st.info(
                    "Nothing to draw. A result is drawable when it holds whole records — "
                    "`SELECT FROM Gene`, not `SELECT name FROM Gene`."
                )
        else:
            nodes, edges, note = graph
            _graph_canvas(nodes, edges, key=f"{lang}-result-graph", height=560)
            st.caption("Drag to pan, scroll to zoom. Click a paper or gene to open its page.")
            if note:
                st.caption(note)
    with tab_table:
        try:
            st.dataframe(rows, width="stretch")
        except Exception:  # nested/mixed values a dataframe can't hold
            st.warning("Result is not tabular; see the JSON tab.")
    with tab_json:
        st.json(rows)


def page_search() -> None:
    st.title("🔎 Search")
    tab_papers, tab_sql, tab_cypher = st.tabs(["Papers", "SQL", "Cypher"])
    with tab_papers:
        _papers_search()
    with tab_sql:
        _query_editor("sql")
    with tab_cypher:
        _query_editor("cypher")


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
    st.button("← Back", type="tertiary", key="back-paper", on_click=_nav_back)
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
        _gene_pills([(g["name"], g["gene_id"]) for g in genes], key=f"paper-genes-{paper_id}")
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
        _graph_canvas(nodes, edges, key=f"paper-graph-{paper_id}")
        st.caption("Drag to pan, scroll to zoom. Click a gene to open its page.")

    references, citing = data["references"], data["citing"]
    if not (references or citing):
        st.caption("No citation edges for this paper in this database.")
        return
    col1, col2 = st.columns(2)
    with col1:
        st.subheader(f"References ({len(references)})")
        for row in references:
            _entity_button(row.get("title"), "paper", row["id"], key=f"ref-{row['id']}")
    with col2:
        st.subheader(f"Cited by ({len(citing)})")
        for row in citing:
            _entity_button(row.get("title"), "paper", row["id"], key=f"cit-{row['id']}")


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
    st.button("← Back", type="tertiary", key="back-gene", on_click=_nav_back)
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
        _graph_canvas(nodes, edges, key=f"gene-graph-{gene_id}")
        st.caption("Drag to pan, scroll to zoom. Click a gene to open its page.")

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
        _gene_pills(
            [(f"{row['name'] or row['gene_id']} ({row['shared_papers']})", row["gene_id"]) for row in co_mentioned],
            key=f"gene-co-{gene_id}",
        )
    else:
        st.caption("This gene shares no papers with another gene.")

    st.subheader(f"Papers mentioning this gene ({len(papers)})")
    if not papers:
        st.caption("No papers mention this gene.")
        return
    for row in papers:
        _entity_button(row.get("title"), "paper", row["id"], key=f"gp-{row['id']}")
        st.caption(" · ".join(filter(None, (row.get("pmid") and f"PMID {row['pmid']}", row.get("source")))))


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

# Changing sidebar page while inside an entity view returns to the sidebar's pages.
page = st.sidebar.radio("LitGraph", list(_PAGES), key="nav_page", on_change=_reset_nav)

# Entity views live in session state (so navigation is same-tab and Back can restore
# where you were); the URL only mirrors the view for sharing, and seeds it once when a
# shared link starts a fresh session.
if "view" not in st.session_state:
    if st.query_params.get("paper"):
        st.session_state["view"] = ("paper", st.query_params["paper"])
    elif st.query_params.get("gene"):
        st.session_state["view"] = ("gene", st.query_params["gene"])
    else:
        st.session_state["view"] = None
    st.session_state.setdefault("nav_stack", [])

_view = st.session_state["view"]
if _view:
    st.query_params.from_dict({_view[0]: _view[1]})
    (page_paper if _view[0] == "paper" else page_gene)(_view[1])
else:
    st.query_params.clear()
    _PAGES[page]()
