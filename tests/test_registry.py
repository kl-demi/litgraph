import pytest

from litgraph.db.registry import (
    EdgeType,
    NodeType,
    Prop,
    PropType,
    Registry,
    arcadedb_ddl,
    indexed_props,
    neo4j_ddl,
    registry,
)


def _fixture_registry() -> Registry:
    """A local registry, since the module-level one is process-global and gains the biology
    types as soon as any test imports spokebio."""
    reg = Registry()
    reg.register(
        NodeType(
            "Thing",
            key="thing_id",
            props=(Prop("label"), Prop("seen_at", PropType.DATETIME, indexed=True)),
            fulltext=("label", "body"),
            vector="embedding",
        ),
        NodeType("Tag", key="code"),
        EdgeType("TAGGED", src="Thing", dst="Tag", props=(Prop("weight", PropType.FLOAT),)),
    )
    return reg


# --- Registry bookkeeping ---------------------------------------------------------------


def test_registering_the_same_type_twice_is_a_noop():
    reg = Registry()
    node = NodeType("Thing", key="id")
    reg.register(node)
    reg.register(node)
    assert len(reg.nodes) == 1


def test_registering_a_conflicting_definition_raises():
    reg = Registry()
    reg.register(NodeType("Thing", key="id"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(NodeType("Thing", key="other_id"))


def test_nodes_and_edges_are_separate_namespaces():
    reg = Registry()
    reg.register(NodeType("Thing", key="id"), EdgeType("Thing", src="Thing", dst="Thing"))
    assert set(reg.nodes) == {"Thing"} and set(reg.edges) == {"Thing"}


def test_edge_to_an_unregistered_node_type_raises():
    reg = Registry()
    reg.register(NodeType("Thing", key="id"), EdgeType("TAGGED", src="Thing", dst="Nonexistent"))
    with pytest.raises(ValueError, match="unregistered node type Nonexistent"):
        reg.validate()


def test_a_multi_typed_endpoint_validates_every_declared_type():
    reg = Registry()
    reg.register(
        NodeType("Thing", key="id"),
        NodeType("Tag", key="code"),
        EdgeType("TAGGED", src="Thing", dst=("Tag", "Nonexistent")),
    )
    with pytest.raises(ValueError, match="unregistered node type Nonexistent"):
        reg.validate()


@pytest.mark.parametrize("emit", [arcadedb_ddl, neo4j_ddl])
def test_both_emitters_validate_before_emitting(emit):
    reg = Registry()
    reg.register(NodeType("Thing", key="id"), EdgeType("TAGGED", src="Thing", dst="Nonexistent"))
    with pytest.raises(ValueError):
        list(emit(reg, embedding_dimensions=4))


def test_indexed_props_includes_the_key():
    assert list(indexed_props(_fixture_registry(), "Thing")) == ["thing_id", "seen_at"]


# --- ArcadeDB dialect -------------------------------------------------------------------


def test_arcadedb_orders_types_before_properties_before_indexes():
    """ArcadeDB rejects a property on an undeclared type, and an index on an undeclared
    property, so emission order is load-bearing."""
    statements = list(arcadedb_ddl(_fixture_registry(), embedding_dimensions=4))

    def first(fragment: str) -> int:
        return next(i for i, s in enumerate(statements) if fragment in s)

    assert first("CREATE VERTEX TYPE Thing") < first("CREATE EDGE TYPE TAGGED")
    assert first("CREATE EDGE TYPE TAGGED") < first("CREATE PROPERTY Thing.thing_id")
    assert first("CREATE PROPERTY Thing.thing_id") < first("CREATE INDEX ON Thing (thing_id)")


def test_arcadedb_makes_only_the_key_unique():
    statements = list(arcadedb_ddl(_fixture_registry(), embedding_dimensions=4))
    assert "CREATE INDEX ON Thing (thing_id) UNIQUE" in statements
    assert "CREATE INDEX ON Thing (seen_at) NOTUNIQUE" in statements
    assert not any("UNIQUE" in s and "seen_at" in s and "NOTUNIQUE" not in s for s in statements)


def test_arcadedb_declares_an_unindexed_property_without_indexing_it():
    statements = list(arcadedb_ddl(_fixture_registry(), embedding_dimensions=4))
    assert "CREATE PROPERTY Thing.label STRING" in statements
    assert not any("INDEX ON Thing (label)" in s for s in statements)


def test_arcadedb_declares_fulltext_and_vector_properties():
    """`body` and `embedding` appear only in the fulltext/vector specs, so nothing else
    would declare them."""
    statements = list(arcadedb_ddl(_fixture_registry(), embedding_dimensions=4))
    assert "CREATE PROPERTY Thing.body STRING" in statements
    assert "CREATE PROPERTY Thing.embedding ARRAY_OF_FLOATS" in statements
    assert "CREATE INDEX ON Thing (label, body) FULL_TEXT" in statements


def test_arcadedb_vector_index_carries_the_passed_dimensions():
    statements = list(arcadedb_ddl(_fixture_registry(), embedding_dimensions=768))
    assert (
        'CREATE INDEX ON Thing (embedding) LSM_VECTOR METADATA {"dimensions": 768, "similarity": "COSINE"}'
        in statements
    )


def test_arcadedb_declares_edge_properties():
    assert "CREATE PROPERTY TAGGED.weight FLOAT" in list(arcadedb_ddl(_fixture_registry(), embedding_dimensions=4))


def test_arcadedb_skips_fulltext_and_vector_when_unset():
    reg = Registry()
    reg.register(NodeType("Tag", key="code"))
    statements = list(arcadedb_ddl(reg, embedding_dimensions=4))
    assert not any("FULL_TEXT" in s or "LSM_VECTOR" in s for s in statements)


# --- Neo4j dialect ----------------------------------------------------------------------


def test_neo4j_emits_constraints_and_indexes_only():
    statements = list(neo4j_ddl(_fixture_registry(), embedding_dimensions=4))
    assert not any("VERTEX TYPE" in s or "EDGE TYPE" in s or "CREATE PROPERTY" in s for s in statements)


def test_neo4j_statements_are_all_idempotent():
    assert all("IF NOT EXISTS" in s for s in neo4j_ddl(_fixture_registry(), embedding_dimensions=4))


def test_neo4j_key_becomes_a_uniqueness_constraint():
    statements = list(neo4j_ddl(_fixture_registry(), embedding_dimensions=4))
    assert "CREATE CONSTRAINT thing_thing_id IF NOT EXISTS FOR (t:Thing) REQUIRE t.thing_id IS UNIQUE" in statements
    assert "CREATE INDEX thing_seen_at IF NOT EXISTS FOR (t:Thing) ON (t.seen_at)" in statements


def test_neo4j_key_named_id_does_not_double_up_in_the_index_name():
    reg = Registry()
    reg.register(NodeType("Thing", key="id"))
    assert "CREATE CONSTRAINT thing_id IF NOT EXISTS" in list(neo4j_ddl(reg, embedding_dimensions=4))[0]


def test_neo4j_vector_index_carries_the_passed_dimensions():
    statements = list(neo4j_ddl(_fixture_registry(), embedding_dimensions=768))
    assert any("`vector.dimensions`: 768" in s and "`vector.similarity_function`: 'cosine'" in s for s in statements)


# --- Core paper schema ------------------------------------------------------------------
# Pins every statement the hand-written schema.py emitted, so the declarative port can't
# silently stop creating a live index.

_LEGACY_ARCADEDB = [
    "CREATE VERTEX TYPE Paper IF NOT EXISTS",
    "CREATE VERTEX TYPE Category IF NOT EXISTS",
    "CREATE VERTEX TYPE Author IF NOT EXISTS",
    "CREATE VERTEX TYPE GraphStats IF NOT EXISTS",
    "CREATE EDGE TYPE CITES IF NOT EXISTS",
    "CREATE EDGE TYPE IN_CATEGORY IF NOT EXISTS",
    "CREATE EDGE TYPE AUTHORED IF NOT EXISTS",
    "CREATE PROPERTY Paper.id STRING",
    "CREATE PROPERTY Paper.arxiv_id STRING",
    "CREATE PROPERTY Paper.pmid STRING",
    "CREATE PROPERTY Paper.s2_paper_id STRING",
    "CREATE PROPERTY Paper.enriched_at DATETIME",
    "CREATE PROPERTY Paper.is_stub BOOLEAN",
    "CREATE PROPERTY Paper.title STRING",
    "CREATE PROPERTY Paper.abstract STRING",
    "CREATE PROPERTY Paper.embedding ARRAY_OF_FLOATS",
    "CREATE PROPERTY Category.code STRING",
    "CREATE PROPERTY Author.name STRING",
    "CREATE PROPERTY GraphStats.id STRING",
    "CREATE INDEX ON Paper (id) UNIQUE",
    "CREATE INDEX ON Category (code) UNIQUE",
    "CREATE INDEX ON Author (name) UNIQUE",
    "CREATE INDEX ON GraphStats (id) UNIQUE",
    "CREATE INDEX ON Paper (arxiv_id) NOTUNIQUE",
    "CREATE INDEX ON Paper (pmid) NOTUNIQUE",
    "CREATE INDEX ON Paper (s2_paper_id) NOTUNIQUE",
    "CREATE INDEX ON Paper (enriched_at) NOTUNIQUE",
    "CREATE INDEX ON Paper (is_stub) NOTUNIQUE",
    "CREATE INDEX ON Paper (title, abstract) FULL_TEXT",
    'CREATE INDEX ON Paper (embedding) LSM_VECTOR METADATA {"dimensions": 768, "similarity": "COSINE"}',
]

_LEGACY_NEO4J = [
    "CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT category_code IF NOT EXISTS FOR (c:Category) REQUIRE c.code IS UNIQUE",
    "CREATE CONSTRAINT author_name IF NOT EXISTS FOR (a:Author) REQUIRE a.name IS UNIQUE",
    "CREATE CONSTRAINT graphstats_id IF NOT EXISTS FOR (g:GraphStats) REQUIRE g.id IS UNIQUE",
    "CREATE INDEX paper_arxiv_id IF NOT EXISTS FOR (p:Paper) ON (p.arxiv_id)",
    "CREATE INDEX paper_pmid IF NOT EXISTS FOR (p:Paper) ON (p.pmid)",
    "CREATE INDEX paper_s2_paper_id IF NOT EXISTS FOR (p:Paper) ON (p.s2_paper_id)",
    "CREATE INDEX paper_enriched_at IF NOT EXISTS FOR (p:Paper) ON (p.enriched_at)",
    "CREATE INDEX paper_is_stub IF NOT EXISTS FOR (p:Paper) ON (p.is_stub)",
    "CREATE FULLTEXT INDEX paper_fulltext IF NOT EXISTS FOR (p:Paper) ON EACH [p.title, p.abstract]",
    "CREATE VECTOR INDEX paper_embedding IF NOT EXISTS FOR (p:Paper) ON (p.embedding) "
    "OPTIONS {indexConfig: {`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}",
]


@pytest.mark.parametrize("statement", _LEGACY_ARCADEDB)
def test_core_arcadedb_ddl_still_emitted(statement):
    import litgraph.db.schema  # noqa: F401  -- registers the core types

    assert statement in list(arcadedb_ddl(registry, embedding_dimensions=768))


@pytest.mark.parametrize("statement", _LEGACY_NEO4J)
def test_core_neo4j_ddl_still_emitted(statement):
    import litgraph.db.schema  # noqa: F401  -- registers the core types

    assert statement in list(neo4j_ddl(registry, embedding_dimensions=768))


def test_paper_identifier_columns_are_derived_from_the_model():
    """Adding a paper source means one entry in PAPER_IDENTIFIERS, with no matching edit
    to the schema."""
    import litgraph.db.schema as schema
    from litgraph.models import PAPER_IDENTIFIERS

    declared = {prop.name for prop in schema.PAPER.props}
    assert {ns.column for ns in PAPER_IDENTIFIERS} <= declared
    assert all(prop.indexed for prop in schema.PAPER.props if prop.name in {ns.column for ns in PAPER_IDENTIFIERS})


def test_category_carries_vocabulary_and_name_unindexed():
    """Category count is bounded, so an index on either would only add write cost."""
    import litgraph.db.schema  # noqa: F401

    statements = list(arcadedb_ddl(registry, embedding_dimensions=768))
    assert "CREATE PROPERTY Category.vocabulary STRING" in statements
    assert "CREATE PROPERTY Category.name STRING" in statements
    assert not any("ON Category (vocabulary)" in s or "ON Category (name)" in s for s in statements)


def test_biology_types_register_onto_the_same_registry():
    """spokebio contributes types by import rather than owning a second schema module, so
    both halves land on one instance and MENTIONS can point at Paper."""
    import litgraph.db.schema  # noqa: F401
    from spokebio import schema_ext  # noqa: F401

    assert {"Gene", "Pathway", "Compound", "Organism"} <= set(registry.nodes)
    assert registry.edges["MENTIONS"].src == "Paper"
    registry.validate()


def test_biology_ddl_is_emitted_for_both_backends():
    """spokebio used to raise NotImplementedError for Neo4j while the core supported it."""
    import litgraph.db.schema  # noqa: F401
    from spokebio import schema_ext  # noqa: F401

    assert "CREATE INDEX ON Gene (gene_id) UNIQUE" in list(arcadedb_ddl(registry, embedding_dimensions=768))
    neo4j = list(neo4j_ddl(registry, embedding_dimensions=768))
    assert any("REQUIRE g.gene_id IS UNIQUE" in s for s in neo4j)
