"""Educational gene explainer: search a gene, see what the graph knows about it.

Run with: streamlit run apps/gene_explainer.py
"""

import streamlit as st

from litgraph.search.genes import co_mentioned_genes, papers_mentioning_gene, pathways_for_gene, search_genes

st.set_page_config(page_title="Gene Explainer", page_icon="🧬")
st.title("🧬 Gene Explainer")
st.caption(
    "Search a gene to see what the literature graph knows about it: which papers "
    "talk about it, which biological pathways it's part of, and which other genes "
    "tend to come up alongside it."
)

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

st.divider()

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📄 Papers")
    st.caption("Papers whose text mentions this gene.")
    papers = papers_mentioning_gene(gene_id)
    if not papers:
        st.write("No papers found.")
    for p in papers:
        title = p["title"] or "(untitled)"
        ref = p["arxiv_id"] or p["pmid"] or p["id"]
        st.markdown(f"**{title}**  \n`{ref}` · via {p['source']}")

with col2:
    st.subheader("🔀 Pathways")
    st.caption("Biological pathways this gene participates in.")
    pathways = pathways_for_gene(gene_id)
    if not pathways:
        st.write("No pathways found.")
    for pw in pathways:
        st.markdown(f"**{pw['name']}**  \n{pw['source_db']} · evidence: {pw['evidence_code']}")

with col3:
    st.subheader("🧬 Related genes")
    st.caption("Genes most often mentioned in the same papers as this one.")
    related = co_mentioned_genes(gene_id)
    if not related:
        st.write("No related genes found.")
    for g in related:
        st.markdown(f"**{g['name']}**  \nshared in {g['shared_papers']} paper(s)")
