"""LitGraph dashboard: browse what's been ingested, run queries, see results as a graph.

Navigation is a top bar (wordmark, page control, database picker); the sidebar is
retired. Pages: Home (landing -- semantic search bar plus the database's figures),
Search (papers, entities, raw SQL/Cypher), Database (schema map + per-type schema
inspected in place). Paper, Gene, Pathway and Trait views are reached from links and
canvas clicks, navigate in-session (same tab, Back restores where you were), and
mirror to ?paper=<id> and friends so they stay shareable.

Run with: streamlit run apps/dashboard.py
"""

import math
import random
import re
import threading
import time

import altair as alt
import httpx
import pandas as pd
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
from litgraph.search.pathways import compounds_produced, genes_in_pathway, get_pathway, papers_for_pathway
from litgraph.search.traits import genes_for_trait, get_trait, papers_for_trait
from litgraph.search.semantic import semantic_search
from litgraph.search.stats import latest_papers, overview, top_authors, type_counts

st.set_page_config(page_title="LitGraph", page_icon="📚", layout="wide", initial_sidebar_state="collapsed")

# Streamlit's built-in form hint ("Press ⌘+Enter to submit form") isn't configurable;
# reword it for the query editors only. Scoped to stTextArea so the search box's own
# "Press Enter to apply" hint is left alone.
st.markdown(
    """<style>
    .stTextArea [data-testid="InputInstructions"] > span { visibility: hidden; }
    .stTextArea [data-testid="InputInstructions"] > span::after {
        visibility: visible; float: right; content: "Press ⌘/Ctrl+Enter to run";
    }
    </style>""",
    unsafe_allow_html=True,
)


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

# Warm mid-tones, distinguishable without shouting; the acid accent is reserved for
# interaction (selection, focus) so it never competes with the data. Kept in step with
# chartCategoricalColors in .streamlit/config.toml.
_KIND_COLOR = {
    "Paper": "#57534E",
    "Gene": "#6F7F1F",
    "Pathway": "#B2643A",
    "Trait": "#3F7370",
    "Compound": "#7A5980",
    "Organism": "#A2812A",
    "Category": "#8C8279",
}
_DEFAULT_KIND_COLOR = "#8C8279"

# Ingestion bookkeeping, not biology: kept out of the landing figures and backdrop.
# The Database page still lists them, since there they are schema fact.
_BOOKKEEPING_TYPES = {"GraphStats", "IngestState", "PubtatorChecked", "ExtractionChecked"}

st.markdown(
    """<style>
    /* A lattice, not wallpaper: a fine dot grid on a coarser rule, at an opacity that
       reads as texture and never competes with text. The page is a plotting surface
       for connected data, and the background says so quietly. */
    [data-testid="stAppViewContainer"] {
        background-image:
            linear-gradient(rgba(28,25,23,0.028) 1px, transparent 1px),
            linear-gradient(90deg, rgba(28,25,23,0.028) 1px, transparent 1px),
            radial-gradient(circle at 1px 1px, rgba(28,25,23,0.09) 1.1px, transparent 0);
        background-size: 120px 120px, 120px 120px, 24px 24px;
        background-position: -1px -1px, -1px -1px, 0 0;
    }
    /* Navigation lives in a top bar; the sidebar is retired outright rather than left
       as an empty gutter. */
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] { display: none !important; }

    /* The jammed look was tight tracking plus a tall, delicate face. Bricolage is
       wide; these give it air and a settled baseline. */
    h1, h2, h3, h4 { line-height: 1.14; letter-spacing: -0.005em; }
    h1 { letter-spacing: -0.015em; margin-bottom: 0.1em; }

    /* Restrained, with exits faster than entrances: the base rule times the exit,
       the :hover rule times the entrance. */
    .stButton button, .stTabs button, [data-testid="stMetric"] { transition: all 90ms ease-out; }
    .stButton button:hover, .stTabs button:hover { transition: all 160ms ease-out; }
    /* Tertiary buttons are our in-app links: read as links, not as chrome. */
    .stButton button[kind="tertiary"] { padding-left: 0; padding-right: 0; text-align: left; }

    /* The search field is the front door: give it presence over every other input.
       Scoped to the main area by data-testid rather than to the hero div -- Streamlit
       wraps each element in its own container, so the hero and the input are not
       siblings and a `+` selector never matches. The query editors are textareas, so
       this only ever hits the search box. */
    section.main [data-testid="stTextInput"] input,
    [data-testid="stMain"] [data-testid="stTextInput"] input {
        font-size: 1.2rem; padding: 0.8rem 1rem;
    }
    </style>""",
    unsafe_allow_html=True,
)


def _md_escape(text: str) -> str:
    """Escape the brackets that would otherwise be parsed as markdown in a label."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _clip(text: str | None, n: int = 45) -> str:
    text = text or "?"
    return text if len(text) <= n else text[: n - 1] + "…"


# In-session navigation. Markdown links to ?paper=... open a new tab and start a fresh
# session (Streamlit hard-targets markdown links _blank), losing query results and the
# db selection -- so internal navigation is widgets + session state instead, and the
# current view is mirrored to the URL only for shareability. External links (PubMed,
# DOI, arXiv) stay markdown links, where the new tab is wanted.


# Node kinds with a page of their own -> the view name the router understands.
_VIEW_KINDS = {"Paper": "paper", "Gene": "gene", "Pathway": "pathway", "Trait": "trait"}


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


def node_meta(name: str | None, **fields: object) -> dict:
    """One node's detail-panel content: a full (unclipped) name plus labelled fields."""
    return {"name": name, "fields": {k.replace("_", " "): v for k, v in fields.items() if v}}


# Laid out and fitted synchronously on mount. The springy variants were tried and
# reverted: `animate: True` renders every simulation tick, which looks lively but
# leaves nodes drifting under the cursor and genuinely hard to click, and `"end"`
# animates the settle while the container is still being sized, so the fit lands on a
# stale viewport. Motion here costs legibility, which is the wrong trade for the one
# view people came to read.
_GRAPH_LAYOUT = {
    "name": "cose",
    "animate": False,
    "padding": 28,
    "fit": True,
    "nodeDimensionsIncludeLabels": True,
    "idealEdgeLength": 120,
    "nodeRepulsion": 6500,
}


def _node_detail(key: str, nodes: dict, meta: dict) -> None:
    """The panel beside a canvas: what the selected node is, and a way into its page.

    Selecting never navigates on its own -- inspecting a node and committing to it are
    different intentions, and a canvas click is too easy to make by accident.
    """
    selected = st.session_state.get(f"{key}--sel")
    if not selected or selected not in nodes:
        st.markdown("**Selection**")
        st.caption("Select a node to see its details.")
        return

    label, kind = nodes[selected]
    detail = meta.get(selected) or {}
    color = _KIND_COLOR.get(kind, _DEFAULT_KIND_COLOR)
    st.markdown(
        f'<span style="display:inline-block; background:{color}; color:#FBF9F5; '
        f'font-size:0.72rem; font-weight:600; letter-spacing:.07em; text-transform:uppercase; '
        f'padding:2px 8px; border-radius:3px">{kind or "node"}</span>',
        unsafe_allow_html=True,
    )
    # The canvas label is clipped to stay readable; the panel is where the full name goes.
    st.markdown(f"#### {_md_escape(str(detail.get('name') or label))}")
    for field, value in (detail.get("fields") or {}).items():
        st.caption(f"{field}: {value}")
    if kind in _VIEW_KINDS:
        st.button(
            f"Open {kind.lower()} page →",
            key=f"{key}--open",
            type="primary",
            on_click=_nav_to,
            args=(_VIEW_KINDS[kind], selected),
        )


def _graph_canvas(
    nodes: dict[str, tuple[str, str]],
    edges: list[tuple[str, str, str]],
    key: str,
    height: int = 480,
    meta: dict | None = None,
) -> None:
    """An interactive Cytoscape canvas plus its detail panel: pan, zoom, drag; clicking
    a node describes it alongside. nodes: id -> (label, kind); edges: (src, dst, label)."""
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
        NodeStyle(kind, _KIND_COLOR.get(kind, _DEFAULT_KIND_COLOR), caption="name")
        for kind in {kind for _, kind in nodes.values()}
    ]
    edge_styles = [
        EdgeStyle(label, caption="label", directed=True) for label in {e[2] or "" for e in edges}
    ]

    col_canvas, col_detail = st.columns([3, 1], gap="medium")
    with col_canvas:
        event = st_link_analysis(
            elements,
            layout=_GRAPH_LAYOUT,
            node_styles=node_styles,
            edge_styles=edge_styles,
            height=height,
            key=key,
            events=[Event("node_click", "click tap", "node")],
        )

    # The component's return value is its *last* event and replays on every rerun, so a
    # click is recorded once, by timestamp; otherwise returning to this page would
    # reselect whatever was last clicked here.
    if event and event.get("action") == "node_click":
        handled_key = f"{key}--handled"
        if event.get("timestamp") != st.session_state.get(handled_key):
            st.session_state[handled_key] = event.get("timestamp")
            st.session_state[f"{key}--sel"] = str(event.get("data", {}).get("target_id", ""))

    with col_detail:
        _node_detail(key, nodes, meta or {})


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


def _resolve_rids(rids: list[str], limit: int = _MAX_GRAPH_NODES) -> dict[str, dict]:
    """Fetch records an edge points at that the result itself didn't include."""
    safe = [rid for rid in rids if _RID.match(rid)][:limit]
    if not safe:
        return {}
    try:
        rows = run_query(f"SELECT FROM [{','.join(safe)}]")
    except httpx.HTTPError:
        return {}
    return {row["@rid"]: row for row in rows if row.get("@rid")}


def _result_graph(rows: list[dict], max_nodes: int = _MAX_GRAPH_NODES) -> tuple[dict, list, dict, str] | None:
    """Canvas nodes, edges and per-node metadata for a graph-shaped result, plus a note
    on anything left out.

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
    vertices |= _resolve_rids(unresolved, max_nodes)
    # Anything still unresolved is drawn by id, so its edges stay visible.
    for rid in endpoints - set(vertices):
        vertices[rid] = {"@rid": rid}

    kept = dict(list(vertices.items())[:max_nodes])
    # Every field the record carries, minus the embedding (768 floats) and ArcadeDB's
    # own bookkeeping keys.
    skip = {"embedding", "@props", "@cat", "@in", "@out"}
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

    # Built literally rather than through node_meta(): these field names come from the
    # records themselves, and one of them is "name", which would collide with its
    # first parameter.
    meta = {
        rid_to_node[rid]: {
            "name": v.get("name") or v.get("title"),
            "fields": {
                k.replace("_", " "): _clip(str(val), 90)
                for k, val in v.items()
                if k not in skip and val is not None
            },
        }
        for rid, v in kept.items()
    }

    notes = []
    if len(vertices) > len(nodes):
        notes.append(f"showing {len(nodes)} of {len(vertices)} nodes")
    hidden_edges = len(edge_rows) - len(edges)
    if hidden_edges:
        notes.append(f"{hidden_edges} edges hidden (an endpoint is outside the drawn nodes)")
    return nodes, edges, meta, "; ".join(notes).capitalize()


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


@st.cache_data(ttl=300, show_spinner=False)
def _schema_graph(db: str) -> tuple[dict, list, dict]:
    """The database's own shape: one node per type, sized by records, joined by the
    edge types that actually connect them.

    Endpoints are sampled rather than aggregated. `GROUP BY` over an edge type means a
    full scan, which is 17.8M rows for CITES on lg2; 200 rows is instant and enough to
    see which types an edge joins. A rare combination can therefore be missed.
    """
    types = {t["name"]: t for t in _schema_types(db)}
    nodes: dict[str, tuple[str, str]] = {}
    meta: dict[str, dict] = {}
    edges: list[tuple[str, str, str]] = []

    for name, spec in types.items():
        if spec["type"] != "vertex" or not spec.get("records"):
            continue
        nodes[name] = (f"{name} · {spec['records']:,}", name)
        meta[name] = node_meta(
            name,
            records=f"{spec['records']:,}",
            properties=len(spec.get("properties", [])),
            indexes=len(spec.get("indexes", [])),
        )

    for name, spec in types.items():
        if spec["type"] != "edge" or not spec.get("records"):
            continue
        try:
            rows = run_query(f"SELECT @out.@type AS src, @in.@type AS dst FROM {name} LIMIT 200")
        except httpx.HTTPError:
            continue
        for src, dst in {(r.get("src"), r.get("dst")) for r in rows}:
            if src in nodes and dst in nodes:
                edges.append((src, dst, name))
    return nodes, edges, meta


def _figure(title: str, rows: list[tuple[str, int]], color: str) -> None:
    """Ranked horizontal bars on a symlog axis.

    Counts here span five orders of magnitude -- 154,356 Authors against 1,587 Traits
    against a 1-row GraphStats singleton -- so a linear axis renders everything below
    the largest type as a hairline. Symlog rather than log because it is defined at
    zero and one, which the singleton bookkeeping types are.
    """
    if not rows:
        st.caption("Nothing to show.")
        return
    st.markdown(f"**{title}**")
    frame = pd.DataFrame({"type": [r[0] for r in rows], "records": [r[1] for r in rows]})
    chart = (
        alt.Chart(frame)
        .mark_bar(color=color, cornerRadiusEnd=2)
        .encode(
            x=alt.X("records:Q", scale=alt.Scale(type="symlog"), title="records (symlog)"),
            y=alt.Y("type:N", sort="-x", title=None),
            tooltip=["type", alt.Tooltip("records:Q", format=",")],
        )
        .properties(height=max(150, 26 * len(rows)))
    )
    st.altair_chart(chart, width="stretch")


@st.cache_data(ttl=300, show_spinner=False)
def _hero_backdrop(db: str) -> str:
    """The actual schema, drawn as a decorative SVG behind the landing hero.

    Deliberately not the interactive canvas: a backdrop must not fight the search bar
    for pointer events, and the component's iframe cannot sit behind other widgets.
    Nodes are the database's real types sized by record count, edges its real edge
    types, laid out with a few rounds of force-directed relaxation. Motion is a slow
    per-node drift, disabled under prefers-reduced-motion.
    """
    try:
        nodes, edges, _ = _schema_graph(db)
    except httpx.HTTPError:
        return ""
    names = [n for n in nodes if n not in _BOOKKEEPING_TYPES]
    if len(names) < 3:
        return ""
    counts = {t["name"]: t.get("records", 0) for t in _schema_types(db)}
    pairs = sorted({(s, d) for s, d, _ in edges if s in names and d in names and s != d})

    width, height = 1200, 430
    rng = random.Random(7)  # fixed seed: the backdrop should not reshuffle on rerun
    pos = {n: [rng.uniform(90, width - 90), rng.uniform(70, height - 70)] for n in names}
    k = math.sqrt(width * height / len(names)) * 0.62
    temperature = 60.0
    for _ in range(150):
        force = {n: [0.0, 0.0] for n in names}
        for i, a in enumerate(names):  # repulsion
            for b in names[i + 1 :]:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                d2 = max(dx * dx + dy * dy, 1.0)
                f = k * k / d2
                force[a][0] += dx * f
                force[a][1] += dy * f
                force[b][0] -= dx * f
                force[b][1] -= dy * f
        for s, d in pairs:  # attraction along edges
            dx, dy = pos[s][0] - pos[d][0], pos[s][1] - pos[d][1]
            dist = max(math.sqrt(dx * dx + dy * dy), 1.0)
            f = dist / k
            force[s][0] -= dx * f
            force[s][1] -= dy * f
            force[d][0] += dx * f
            force[d][1] += dy * f
        for n in names:
            fx, fy = force[n]
            mag = max(math.sqrt(fx * fx + fy * fy), 0.01)
            step = min(mag, temperature)
            pos[n][0] = min(width - 90, max(90, pos[n][0] + fx / mag * step))
            pos[n][1] = min(height - 60, max(60, pos[n][1] + fy / mag * step))
        temperature *= 0.96

    biggest = max(counts.get(n, 1) for n in names) or 1
    parts = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid slice" '
        'style="position:absolute; inset:0; width:100%; height:100%; pointer-events:none">',
        '<g stroke="#1C1917" stroke-opacity="0.08" stroke-width="1.2">',
    ]
    for s, d in pairs:
        parts.append(
            f'<line x1="{pos[s][0]:.0f}" y1="{pos[s][1]:.0f}" x2="{pos[d][0]:.0f}" y2="{pos[d][1]:.0f}"/>'
        )
    parts.append("</g>")
    for i, n in enumerate(names):
        color = _KIND_COLOR.get(n, _DEFAULT_KIND_COLOR)
        radius = 9 + math.sqrt(counts.get(n, 1) / biggest) * 24
        x, y = pos[n]
        parts.append(
            f'<g class="lg-drift" style="animation-delay:-{(i * 1.7) % 9:.1f}s">'
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{radius:.0f}" fill="{color}" fill-opacity="0.15"/>'
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3" fill="{color}" fill-opacity="0.5"/>'
            f'<text x="{x:.0f}" y="{y + radius + 15:.0f}" text-anchor="middle" fill="{color}" '
            f'fill-opacity="0.55" font-size="12" font-family="IBM Plex Sans, sans-serif">{n}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _home_search_go() -> None:
    """Landing-bar callback: carry the query into the Search page, semantic mode."""
    query = (st.session_state.get("home_search") or "").strip()
    if not query:
        return
    st.session_state["home_search"] = ""
    st.session_state["search_text"] = query
    st.session_state["search_box"] = query
    st.session_state["search_mode"] = "Semantic"
    st.session_state["_nav_last"] = "Search"
    st.session_state["nav_page"] = "Search"
    _reset_nav()


def page_home() -> None:
    db = st.session_state["db"]
    try:
        pulse = _pulse(db)
    except httpx.HTTPError:
        pulse = []
    papers = dict(pulse).get("Paper")
    subtitle = (
        f"A knowledge graph of {papers:,} papers connected to the genes, pathways, "
        "traits and compounds they study."
        if papers
        else "A knowledge graph of papers connected to the biology they study."
    )

    # The hero div is taller than its flow height: the negative bottom margin lets the
    # widgets that follow (the search bar, the chips) render over the backdrop's lower
    # half. The SVG is pointer-events:none, so nothing behind them steals a click.
    backdrop = _hero_backdrop(db)
    st.markdown(
        f'''<div id="lg-hero" style="position:relative; height:410px; margin-bottom:-170px">
        {backdrop}
        <div style="position:relative; z-index:1; text-align:center; padding-top:2.6rem">
        <h1 style="font-size:3.2rem; margin-bottom:0.25rem">LitGraph<span style="color:#B8D400">.</span></h1>
        <p style="font-size:1.12rem; opacity:0.78; margin-top:0; max-width:44rem; display:inline-block">{subtitle}</p>
        </div></div>
        <style>
        @keyframes lg-drift {{ from {{ transform: translateY(-7px); }} to {{ transform: translateY(7px); }} }}
        #lg-hero svg g.lg-drift {{ animation: lg-drift 9s ease-in-out infinite alternate; }}
        @media (prefers-reduced-motion: reduce) {{ #lg-hero svg g.lg-drift {{ animation: none; }} }}
        </style>''',
        unsafe_allow_html=True,
    )
    _, mid, _ = st.columns([1, 2.8, 1])
    with mid:
        st.text_input(
            "Semantic search",
            key="home_search",
            on_change=_home_search_go,
            placeholder="Describe what you are looking for — e.g. how rice tolerates drought",
            label_visibility="collapsed",
        )
        st.pills(
            "Suggestions",
            _SUGGESTIONS,
            key="home_sugg",
            on_change=_suggestion_picked,
            args=("home_sugg", True),
            label_visibility="collapsed",
        )
        st.caption(
            "Search finds papers by meaning, and genes, pathways, traits and compounds by "
            "name or identifier."
        )

    st.divider()
    if pulse:
        cols = st.columns(len(pulse))
        for col, (name, count) in zip(cols, pulse):
            col.metric(f"{name}s", f"{count:,}")

    try:
        types = _schema_types(db)
    except httpx.HTTPError as exc:
        st.error(f"Could not load schema: {exc}")
        return
    by_count = sorted(types, key=lambda t: t.get("records", 0), reverse=True)
    node_rows = [
        (t["name"], t["records"])
        for t in by_count
        if t["type"] == "vertex" and t.get("records") and t["name"] not in _BOOKKEEPING_TYPES
    ]
    edge_rows = [(t["name"], t["records"]) for t in by_count if t["type"] == "edge" and t.get("records")]

    col1, col2 = st.columns(2)
    with col1:
        _figure("Nodes", node_rows, "#6F7F1F")
    with col2:
        _figure("Edges", edge_rows, "#B2643A")
    st.caption("Ingestion bookkeeping types are not shown; the Database page lists every type.")

    data = _overview(db)
    full, stubs = data["papers"], data["stubs"]
    records = full + stubs
    st.markdown("**Paper coverage**")
    _coverage("Full records", full, records, "Paper records")
    _coverage("Enriched", data["enriched"], full, "full papers")
    _coverage("Embedded", data["embedded"], full, "full papers")
    st.caption(
        "A stub is a placeholder created by a citation edge and holds a title at most, so it "
        "can be neither enriched nor embedded. Both percentages are measured against full "
        "papers rather than all records."
    )


# Both searches return a "score", but they mean opposite things, so each names its own.
_SEARCH_MODES = {
    "Keyword": (keyword_search, "relevance", "Matches the words you typed."),
    "Semantic": (semantic_search, "distance", "Finds papers that mean something similar, in whatever words."),
}

_ENTITY_LABELS = ("Gene", "Pathway", "Trait", "Compound")

# Deliberately spread across the entity types, so the first click teaches what the
# graph holds. Tuned to the rice corpus; revisit when the bio reingest lands.
_SUGGESTIONS = ("drought tolerance", "HD3A", "photoperiodism", "grain weight")


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
    with st.container(border=True):
        st.markdown(f"#### {_md_escape(row.get('title') or 'Untitled')}")
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
        _entity_button("Open paper →", "paper", row["id"], key=f"hit-{row['id']}")


@st.cache_data(ttl=300, show_spinner=False)
def _concept_graph(db: str, papers: tuple[tuple[str, str], ...]) -> tuple[dict, list, dict]:
    """Result papers joined by the genes they mention: hits that share a gene cluster
    together, which is the graph's explanation of why they belong to the same query."""
    nodes: dict[str, tuple[str, str]] = {}
    edges = []
    meta: dict[str, dict] = {}
    for paper_id, title in papers:
        nodes[paper_id] = (_clip(title, 30), "Paper")
        meta[paper_id] = node_meta(title, id=paper_id)
        for gene in genes_in(paper_id, limit=10):
            nodes[gene["gene_id"]] = (gene["name"] or gene["gene_id"], "Gene")
            meta[gene["gene_id"]] = node_meta(gene["name"], gene_id=gene["gene_id"])
            edges.append((paper_id, gene["gene_id"], "MENTIONS"))
    return nodes, edges, meta


def _use_suggestion(term: str, go: bool = False) -> None:
    """Entry points for a blank page: the hardest search is the first one. With go=True
    (the landing page), also switch to the Search page."""
    st.session_state["search_text"] = term
    st.session_state["search_box"] = term
    if go:
        st.session_state["_nav_last"] = "Search"
        st.session_state["nav_page"] = "Search"
        _reset_nav()


def _suggestion_picked(key: str, go: bool) -> None:
    """Pill-group callback: run the picked suggestion, then clear the pills so they
    read as actions rather than a lingering selection."""
    term = st.session_state.get(key)
    if term:
        st.session_state[key] = None
        _use_suggestion(term, go)


@st.cache_data(ttl=300, show_spinner=False)
def _pulse(db: str) -> list[tuple[str, int]]:
    """Headline counts, as evidence there is something here worth searching.

    Read off the live schema rather than type_counts(), which enumerates the declared
    registry -- Trait lives only in the rice graph and isn't declared, so it would be
    silently missing from the one corpus where it matters most.
    """
    counts = {t["name"]: t.get("records", 0) for t in _schema_types(db)}
    wanted = ("Paper", "Gene", "Pathway", "Trait", "Compound")
    return [(name, counts[name]) for name in wanted if counts.get(name)]


def _hero() -> None:
    st.title("Search")
    st.caption("Papers by topic, or a gene, pathway, trait or compound by name or identifier.")


def _papers_search() -> None:
    _hero()
    # Seeded rather than passed as `value=`: setting both a default and the session
    # key is what Streamlit warns about, and the suggestion buttons write the key.
    # `search_text` is the copy that survives the widget unmounting when you navigate
    # to an entity page, and refills the box on the way back.
    st.session_state.setdefault("search_box", st.session_state.get("search_text", ""))
    query = st.text_input(
        "Search",
        placeholder="a topic, a gene, a pathway…",
        label_visibility="collapsed",
        key="search_box",
    ).strip()
    st.session_state["search_text"] = query

    db = st.session_state["db"]
    if not query:
        st.caption("Try")
        st.pills(
            "Suggestions",
            _SUGGESTIONS,
            key="search_sugg",
            on_change=_suggestion_picked,
            args=("search_sugg", False),
            label_visibility="collapsed",
        )
        return

    try:
        entities = _search_entities(db, query)
    except Exception as exc:
        entities = {}
        st.caption(f"Entity lookup unavailable: {exc}")
    if entities:
        total = sum(len(rows) for rows in entities.values())
        st.subheader(f"Entities ({total})")
        # One card per match rather than a column of plain text per type: a match is a
        # destination, and the type is the most useful thing to see first.
        flat = [(label, row) for label, rows in entities.items() for row in rows]
        for start in range(0, len(flat), 3):
            for col, (label, row) in zip(st.columns(3), flat[start : start + 3]):
                with col.container(border=True):
                    color = _KIND_COLOR.get(label, _DEFAULT_KIND_COLOR)
                    st.markdown(
                        f'<span style="display:inline-block; background:{color}; color:#FBF9F5; '
                        f'font-size:0.68rem; font-weight:600; letter-spacing:.07em; '
                        f'text-transform:uppercase; padding:2px 7px; border-radius:3px">{label}</span>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"##### {_md_escape(row['name'] or row['id'])}")
                    st.caption(row["id"])
                    # Compound has no page yet, so it gets a card but no link.
                    if label in _VIEW_KINDS:
                        _entity_button(
                            "Open →", _VIEW_KINDS[label], row["id"], key=f"ent-{label}-{row['id']}"
                        )

    st.subheader("Papers")
    col_mode, col_view = st.columns(2)
    mode = col_mode.radio(
        "Mode", list(_SEARCH_MODES), horizontal=True, label_visibility="collapsed", key="search_mode"
    )
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
            nodes, edges, meta = _concept_graph(
                db, tuple((r["id"], r.get("title") or "") for r in rows)
            )
        _graph_canvas(nodes, edges, key="search-graph", height=560, meta=meta)
        st.caption(
            "Papers from these results, joined to the genes they mention. Papers with no edges "
            "have no extracted genes."
        )
        return
    for row in rows:
        _paper_hit(row, score_label)


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
        return payload.get("detail") or payload.get("error") or exc.response.text
    except ValueError:
        return exc.response.text


_EDITOR_PLACEHOLDER = {
    "sql": "SELECT FROM Paper WHERE is_stub = false LIMIT 10",
    "cypher": "MATCH (p:Paper)-[m:MENTIONS]->(g:Gene) RETURN p.title, g.name LIMIT 20",
}


def _browser_behaviours() -> None:
    """Three things Streamlit's Python API can't express, injected into the parent page.

    1. Tab or → fills an empty query editor with its placeholder, the way shell
       autocomplete accepts a suggestion. The value must go through React's own
       setter, or React never sees it and an empty statement gets submitted.
    2. The graph component measures its container on mount, before Streamlit has
       given the iframe its final width, and fits the graph to that stale size --
       leaving it shrunk into a corner. Dispatching a resize makes it re-fit.
    3. The component's own detail drawer is hidden and its canvas matched to the page
       background; neither is exposed through its Python API.

    Both iframes are same-origin, so the parent document and the component's own
    document are reachable.
    """
    st.iframe(
        """<script>
        const W = window.parent, doc = W.document;

        const setValue = (el, text) => {
            const setter = Object.getOwnPropertyDescriptor(
                W.HTMLTextAreaElement.prototype, "value").set;
            setter.call(el, text);
            el.dispatchEvent(new Event("input", {bubbles: true}));
        };
        doc.addEventListener("keydown", (e) => {
            const el = e.target;
            if (el.tagName !== "TEXTAREA" || !el.placeholder) return;
            if (e.key !== "Tab" && e.key !== "ArrowRight") return;
            if (el.value !== "") return;   // only ever completes an empty editor
            e.preventDefault();
            setValue(el, el.placeholder);
        }, true);

        // Re-fit, but only for a genuinely new graph. Every rerun reloads this script,
        // and re-fitting each time yanks the viewport back mid-interaction and moves
        // nodes out from under the cursor. The view is mirrored into the query string,
        // which distinguishes "navigated somewhere new" from "clicked something here".
        // The flag lives on the parent window, which survives reruns. The iframe is
        // re-queried rather than captured: Streamlit replaces the element on rerun.
        const viewKey = W.location.search;
        if (W.__lgFitKey !== viewKey) {
            W.__lgFitKey = viewKey;
            // One nudge, once the layout has finished. Repeated nudges thrash the
            // viewport: each is a re-fit, and firing them across the animation left
            // the canvas blank as often as it left it framed.
            setTimeout(() => {
                doc.querySelectorAll('iframe[src*="st_link_analysis"]').forEach((f) => {
                    try { f.contentWindow.dispatchEvent(new Event("resize")); } catch (err) {}
                });
            }, 1400);
        }

        // The component ships its own detail drawer (#infopanel) -- the vertical "No
        // selected elements" strip. It is not configurable from Python and duplicates
        // our own panel, so it is hidden and the canvas background matched to the
        // page. Same origin, so the component's document is reachable.
        const styleGraphs = () => {
            doc.querySelectorAll('iframe[src*="st_link_analysis"]').forEach((f) => {
                let d;
                try { d = f.contentDocument; } catch (err) { return; }
                if (!d || !d.head || d.getElementById("lg-graph-style")) return;
                const s = d.createElement("style");
                s.id = "lg-graph-style";
                s.textContent = `
                    #infopanel, .infopanel { display: none !important; }
                    body, #container, .container { background: #FBF9F5 !important; }
                `;
                d.head.appendChild(s);
            });
        };
        styleGraphs();
        new W.MutationObserver(styleGraphs).observe(doc.body, {childList: true, subtree: true});
        </script>""",
        height=1,  # st.iframe rejects 0; everything it does happens in the parent page
    )


def _query_editor(lang: str) -> None:
    """One raw-query tab: editor, options, and persisted results. lang: 'sql' | 'cypher'."""
    # The form gives ⌘/Ctrl+Enter submit for free.
    with st.form(f"{lang}-form", border=False):
        # Options above the editor, Run right below it, wide columns so the toggle
        # labels don't wrap.
        cols = st.columns([1, 1.2, 1.5, 1.5], vertical_alignment="bottom")
        limit = cols[0].selectbox("Row limit", [20, 50, 100, 500], index=1, key=f"{lang}_limit")
        cols[1].selectbox("Graph nodes", [30, 60, 120, 250], index=1, key=f"{lang}_maxnodes")
        read_only = cols[2].toggle(
            "Read-only",
            value=True,
            key=f"{lang}_ro",
            help="On: query endpoint (rejects writes). Off: command endpoint.",
        )
        script = False
        if lang == "sql":
            script = cols[3].toggle(
                "Script",
                value=False,
                key="sql_script",
                help="Multi-statement SQLScript (BEGIN/IF/COMMIT); always runs on the command endpoint.",
            )
        command = st.text_area(
            "Statement",
            value=st.session_state.get(f"{lang}_text", ""),
            height=140,
            placeholder=_EDITOR_PLACEHOLDER[lang],
            key=f"{lang}_editor",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Run", type="primary")

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
        graph = _result_graph(rows, max_nodes=st.session_state.get(f"{lang}_maxnodes", _MAX_GRAPH_NODES))
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
            nodes, edges, meta, note = graph
            _graph_canvas(nodes, edges, key=f"{lang}-result-graph", height=560, meta=meta)
            st.caption("Drag to pan, scroll to zoom. Select a node to see its record.")
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
    """Search is the whole front door: the box is the first thing on the page, and the
    raw-query editors sit below it rather than beside it as equal tabs. Making SQL a
    peer of search implied you had to choose a language before you could look."""
    _papers_search()
    st.divider()
    st.subheader("Query the graph directly")
    tab_sql, tab_cypher = st.tabs(["SQL", "Cypher"])
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
        meta = {
            paper_id: node_meta(
                paper.get("title"),
                id=paper_id,
                published=paper.get("published_date"),
                source=paper.get("source"),
            )
        }
        edges = []
        for gene in genes[:12]:
            nodes[gene["gene_id"]] = (gene["name"] or gene["gene_id"], "Gene")
            meta[gene["gene_id"]] = node_meta(
                gene["name"], gene_id=gene["gene_id"], extracted_by=gene.get("source")
            )
            edges.append((paper_id, gene["gene_id"], "MENTIONS"))
        for category in categories[:8]:
            nodes[category["code"]] = (_clip(category["name"] or category["code"], 28), "Category")
            meta[category["code"]] = node_meta(
                category["name"], code=category["code"], vocabulary=category.get("vocabulary")
            )
            edges.append((paper_id, category["code"], "IN_CATEGORY"))
        _graph_canvas(nodes, edges, key=f"paper-graph-{paper_id}", meta=meta)
        st.caption("Drag to pan, scroll to zoom. Select a node to see its details.")

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
    meta = {gene_id: node_meta(gene.get("name"), gene_id=gene_id, locus_id=gene.get("locus_id"))}
    edges = []
    for pathway in pathways[:8]:
        nodes[pathway["pathway_id"]] = (_clip(pathway["name"], 28), "Pathway")
        meta[pathway["pathway_id"]] = node_meta(
            pathway["name"],
            pathway_id=pathway["pathway_id"],
            source=pathway.get("source_db"),
            evidence=pathway.get("evidence_code"),
        )
        edges.append((gene_id, pathway["pathway_id"], pathway.get("evidence_code") or "PARTICIPATES_IN"))
    for trait in traits[:8]:
        nodes[trait["trait_id"]] = (_clip(trait["name"], 28), "Trait")
        meta[trait["trait_id"]] = node_meta(
            trait["name"], trait_id=trait["trait_id"], source=trait.get("source_db")
        )
        edges.append((gene_id, trait["trait_id"], "ASSOCIATED_WITH"))
    for other in co_mentioned[:6]:
        nodes[other["gene_id"]] = (other["name"] or other["gene_id"], "Gene")
        meta[other["gene_id"]] = node_meta(
            other["name"], gene_id=other["gene_id"], shared_papers=other["shared_papers"]
        )
        edges.append((gene_id, other["gene_id"], f"co-mentioned ×{other['shared_papers']}"))
    if edges:
        _graph_canvas(nodes, edges, key=f"gene-graph-{gene_id}", meta=meta)
        st.caption("Drag to pan, scroll to zoom. Select a node to see its details.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader(f"Pathways ({len(pathways)})")
        if pathways:
            for row in pathways:
                _entity_button(row["name"], "pathway", row["pathway_id"], key=f"gpw-{row['pathway_id']}")
                st.caption(f"{row['pathway_id']} · {row.get('source_db') or '?'} · {row.get('evidence_code') or 'no evidence code'}")
        else:
            st.caption("No pathway memberships recorded for this gene.")
    with col2:
        st.subheader(f"Traits ({len(traits)})")
        if traits:
            for row in traits:
                _entity_button(row["name"], "trait", row["trait_id"], key=f"gtr-{row['trait_id']}")
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


def _evidence_papers(papers: list[dict], key_prefix: str, thing: str) -> None:
    """Papers reaching an entity through its genes, with how many genes each touches."""
    st.subheader(f"Papers via genes ({len(papers)})")
    if not papers:
        st.caption(f"No papers mention any gene of this {thing}.")
        return
    for row in papers:
        _entity_button(row.get("title"), "paper", row["id"], key=f"{key_prefix}-{row['id']}")
        genes = row.get("gene_count") or 0
        st.caption(f"touches {genes} gene{'s' if genes != 1 else ''} of this {thing}")


@st.cache_data(ttl=300, show_spinner=False)
def _pathway_bundle(db: str, pathway_id: str) -> dict:
    return {
        "pathway": get_pathway(pathway_id),
        "genes": genes_in_pathway(pathway_id, limit=25),
        "compounds": compounds_produced(pathway_id, limit=15),
        "papers": papers_for_pathway(pathway_id, limit=15),
    }


def page_pathway(pathway_id: str) -> None:
    st.button("← Back", type="tertiary", key="back-pathway", on_click=_nav_back)
    data = _pathway_bundle(st.session_state["db"], pathway_id)
    pathway = data["pathway"]
    if pathway is None:
        st.error(f"No pathway with id `{pathway_id}` in this database.")
        return

    genes, compounds, papers = data["genes"], data["compounds"], data["papers"]
    st.title(pathway.get("name") or pathway_id)
    st.caption(" · ".join(filter(None, (pathway_id, pathway.get("source_db")))))

    nodes = {pathway_id: (_clip(pathway.get("name"), 28), "Pathway")}
    meta = {
        pathway_id: node_meta(
            pathway.get("name"), pathway_id=pathway_id, source=pathway.get("source_db")
        )
    }
    edges = []
    for gene in genes[:12]:
        nodes[gene["gene_id"]] = (gene["name"] or gene["gene_id"], "Gene")
        meta[gene["gene_id"]] = node_meta(
            gene["name"], gene_id=gene["gene_id"], evidence=gene.get("evidence_code")
        )
        edges.append((gene["gene_id"], pathway_id, gene.get("evidence_code") or "PARTICIPATES_IN"))
    for compound in compounds[:8]:
        nodes[compound["compound_id"]] = (_clip(compound["name"], 28), "Compound")
        meta[compound["compound_id"]] = node_meta(
            compound["name"],
            compound_id=compound["compound_id"],
            evidence=compound.get("evidence_code"),
        )
        edges.append((pathway_id, compound["compound_id"], "PRODUCES"))
    if edges:
        _graph_canvas(nodes, edges, key=f"pathway-graph-{pathway_id}", meta=meta)
        st.caption("Drag to pan, scroll to zoom. Select a node to see its details.")

    st.subheader(f"Genes ({len(genes)})")
    if genes:
        _gene_pills([(g["name"], g["gene_id"]) for g in genes], key=f"pathway-genes-{pathway_id}")
    else:
        st.caption("No genes recorded for this pathway.")

    st.subheader(f"Compounds produced ({len(compounds)})")
    if compounds:
        for row in compounds:
            st.markdown(_md_escape(row["name"] or row["compound_id"]))
            st.caption(f"{row['compound_id']} · {row.get('evidence_code') or 'no evidence code'}")
    else:
        st.caption("No compounds recorded for this pathway.")

    _evidence_papers(papers, f"pwp-{pathway_id}", "pathway")


@st.cache_data(ttl=300, show_spinner=False)
def _trait_bundle(db: str, trait_id: str) -> dict:
    return {
        "trait": get_trait(trait_id),
        "genes": genes_for_trait(trait_id, limit=25),
        "papers": papers_for_trait(trait_id, limit=15),
    }


def page_trait(trait_id: str) -> None:
    st.button("← Back", type="tertiary", key="back-trait", on_click=_nav_back)
    data = _trait_bundle(st.session_state["db"], trait_id)
    trait = data["trait"]
    if trait is None:
        st.error(f"No trait with id `{trait_id}` in this database.")
        return

    genes, papers = data["genes"], data["papers"]
    st.title(trait.get("name") or trait_id)
    st.caption(" · ".join(filter(None, (trait_id, trait.get("source_db")))))

    if genes:
        nodes = {trait_id: (_clip(trait.get("name"), 28), "Trait")}
        meta = {
            trait_id: node_meta(trait.get("name"), trait_id=trait_id, source=trait.get("source_db"))
        }
        edges = []
        for gene in genes[:12]:
            nodes[gene["gene_id"]] = (gene["name"] or gene["gene_id"], "Gene")
            meta[gene["gene_id"]] = node_meta(
                gene["name"], gene_id=gene["gene_id"], source=gene.get("source_db")
            )
            edges.append((gene["gene_id"], trait_id, "ASSOCIATED_WITH"))
        _graph_canvas(nodes, edges, key=f"trait-graph-{trait_id}", meta=meta)
        st.caption("Drag to pan, scroll to zoom. Select a node to see its details.")

    st.subheader(f"Associated genes ({len(genes)})")
    if genes:
        _gene_pills([(g["name"], g["gene_id"]) for g in genes], key=f"trait-genes-{trait_id}")
    else:
        st.caption("No genes associated with this trait.")

    _evidence_papers(papers, f"trp-{trait_id}", "trait")


def _pick_type(group_key: str, other_key: str) -> None:
    """Selecting in one pill group clears the other, so one type is inspected at a time."""
    if st.session_state.get(group_key):
        st.session_state[other_key] = None


def page_database() -> None:
    """The schema, inspectable in place: a map of the types, then pills that open each
    type's properties and indexes right below."""
    db = st.session_state["db"]
    st.title("Database")
    st.caption(f"{db} · select a type to see its schema")
    try:
        types = _schema_types(db)
    except httpx.HTTPError as exc:
        st.error(f"Could not load schema: {exc}")
        return

    try:
        nodes, edges, meta = _schema_graph(db)
    except httpx.HTTPError as exc:
        st.error(f"Could not build the schema graph: {exc}")
        return
    if nodes:
        _graph_canvas(nodes, edges, key=f"schema-graph-{db}", height=380, meta=meta)
        st.caption(
            "Each node is a type, labelled with its record count; each edge is an edge type "
            "that joins them. Endpoints are sampled from 200 edges per type."
        )

    by_count = sorted(types, key=lambda t: t.get("records", 0), reverse=True)
    vertex = [t for t in by_count if t["type"] == "vertex"]
    edge = [t for t in by_count if t["type"] == "edge"]
    vlabels = {f"{t['name']} · {t.get('records', 0):,}": t for t in vertex}
    elabels = {f"{t['name']} · {t.get('records', 0):,}": t for t in edge}

    # Keys are per-database: a stale selection whose label (the count) no longer exists
    # in the options would otherwise be silently dropped.
    vkey, ekey = f"dbtype-v-{db}", f"dbtype-e-{db}"
    if vkey not in st.session_state and ekey not in st.session_state and vlabels:
        st.session_state[vkey] = next(iter(vlabels))

    st.caption(f"NODE TYPES ({len(vertex)})")
    st.pills("Node types", list(vlabels), key=vkey, on_change=_pick_type,
             args=(vkey, ekey), label_visibility="collapsed")
    st.caption(f"EDGE TYPES ({len(edge)})")
    st.pills("Edge types", list(elabels), key=ekey, on_change=_pick_type,
             args=(ekey, vkey), label_visibility="collapsed")

    chosen = vlabels.get(st.session_state.get(vkey) or "") or elabels.get(st.session_state.get(ekey) or "")
    if chosen is None:
        st.caption("Select a type above.")
        return

    with st.container(border=True):
        color = _KIND_COLOR.get(chosen["name"], _DEFAULT_KIND_COLOR)
        st.markdown(
            f'<span style="display:inline-block; background:{color}; color:#FBF9F5; '
            f'font-size:0.72rem; font-weight:600; letter-spacing:.07em; text-transform:uppercase; '
            f'padding:2px 8px; border-radius:3px">{chosen["type"]}</span>'
            f'&nbsp;&nbsp;<span style="font-family:\'Bricolage Grotesque\'; font-weight:600; '
            f'font-size:1.35rem">{chosen["name"]}</span>',
            unsafe_allow_html=True,
        )
        bits = [f"{chosen.get('records', 0):,} records"]
        if chosen.get("parentTypes"):
            bits.append(f"extends {', '.join(chosen['parentTypes'])}")
        if chosen.get("buckets"):
            bits.append(f"{len(chosen['buckets'])} buckets")
        st.caption(" · ".join(bits))

        col_props, col_idx = st.columns(2)
        with col_props:
            st.markdown("**Properties**")
            props = chosen.get("properties", [])
            if props:
                st.dataframe(
                    [{"name": q["name"], "type": q["type"], "default": q.get("default")} for q in props],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("No declared properties (schemaless records may still carry fields).")
        with col_idx:
            st.markdown("**Indexes**")
            indexes = chosen.get("indexes", [])
            if indexes:
                st.dataframe(
                    [
                        {
                            "name": i["name"],
                            "type": i["type"],
                            "unique": i.get("unique"),
                            "properties": ", ".join(i.get("properties", [])),
                        }
                        for i in indexes
                    ],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("No indexes.")

    with st.expander("Corpus tables (slow: full scans)"):
        if st.button("Load", key="corpus-load"):
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


_PAGES = {
    "Home": page_home,
    "Search": page_search,
    "Database": page_database,
}

# The dashboard's own default, deliberately not ARCADEDB_DATABASE: cron reads that from
# .env, so pointing the UI at a different graph must not go through it.
_DEFAULT_DB = "rice"


def _nav_changed() -> None:
    """Keep the top nav deselect-proof. A segmented control returns None when its
    selected item is clicked again; restore the page instead of falling off the nav.
    Either way, any nav interaction leaves the current entity view."""
    if st.session_state.get("nav_page") is None:
        st.session_state["nav_page"] = st.session_state.get("_nav_last", "Home")
    else:
        st.session_state["_nav_last"] = st.session_state["nav_page"]
    _reset_nav()


def _top_nav() -> str:
    """The app's single chrome: wordmark, page control, database picker. Replaces the
    sidebar, which spent a full gutter on three options."""
    try:
        db_options = _databases()
    except httpx.HTTPError:
        db_options = [get_settings().arcadedb_database]

    col_brand, col_pages, col_db = st.columns([1.6, 4.4, 1.2], vertical_alignment="center")
    with col_brand:
        st.markdown(
            '<span style="font-family:\'Bricolage Grotesque\', sans-serif; font-weight:700; '
            'font-size:1.5rem">LitGraph<span style="color:#B8D400">.</span></span>',
            unsafe_allow_html=True,
        )
    with col_pages:
        st.session_state.setdefault("nav_page", "Home")
        st.segmented_control(
            "Page",
            list(_PAGES),
            key="nav_page",
            on_change=_nav_changed,
            label_visibility="collapsed",
        )
    with col_db:
        if "db_select" not in st.session_state:
            st.session_state["db_select"] = (
                _DEFAULT_DB if _DEFAULT_DB in db_options else db_options[0]
            )
        st.selectbox("Database", db_options, key="db_select", label_visibility="collapsed")

    st.markdown(
        '<hr style="margin:0.1rem 0 1.2rem; border:none; border-top:1px solid #E4DBCE">',
        unsafe_allow_html=True,
    )
    return st.session_state.get("nav_page") or "Home"


page = _top_nav()
st.session_state["db"] = st.session_state["db_select"]
set_database(st.session_state["db"])

_ENTITY_VIEWS = {"paper": page_paper, "gene": page_gene, "pathway": page_pathway, "trait": page_trait}

# Entity views live in session state (so navigation is same-tab and Back can restore
# where you were); the URL only mirrors the view for sharing, and seeds it once when a
# shared link starts a fresh session.
if "view" not in st.session_state:
    st.session_state["view"] = next(
        ((kind, st.query_params[kind]) for kind in _ENTITY_VIEWS if st.query_params.get(kind)),
        None,
    )
    st.session_state.setdefault("nav_stack", [])

_browser_behaviours()

_view = st.session_state["view"]
if _view:
    st.query_params.from_dict({_view[0]: _view[1]})
    _ENTITY_VIEWS[_view[0]](_view[1])
else:
    st.query_params.clear()
    _PAGES[page]()
