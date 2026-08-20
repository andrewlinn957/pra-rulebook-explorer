import sqlite3
import unittest

from backend.rulebook_scraper.enrich import _resolve_html_anchor_reference_edges
from backend.rulebook_scraper.models import Edge, Node
from backend.rulebook_scraper.parse import edge_id, node_id
from backend.rulebook_scraper.store import SCHEMA, upsert_edges, upsert_nodes, upsert_source


class SourceHrefResolutionTests(unittest.TestCase):
    def make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    def test_resolves_unhashed_placeholder_link_from_cached_source_href(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        upsert_source(
            conn,
            source_type="part",
            url=source_url,
            fetched_at="2026-06-22T00:00:00Z",
            raw_html='<p>See <a href="/pra-rules/target-part#abc123">Target Part 4.2</a>.</p>',
        )
        source = Node("source", "rule", "rule:source", "Source", url=source_url, metadata={"html_id": "src1"})
        target = Node("target", "rule", "rule:target", "Target Part 4.2", url="https://www.prarulebook.co.uk/pra-rules/target-part#abc123", metadata={"html_id": "abc123"})
        placeholder_key = "url:pra-rules/target-part"
        placeholder = Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Target Part", url="https://www.prarulebook.co.uk/pra-rules/target-part", metadata={"placeholder": True})
        upsert_nodes(conn, [source, target, placeholder])
        original = Edge(
            edge_id(source.id, placeholder.id, "references", "plain-target"),
            source.id,
            placeholder.id,
            "references",
            "html_link",
            1.0,
            "Target Part 4.2",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/target-part", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "target")
        self.assertEqual(resolved[0].metadata["html_id"], "abc123")
        self.assertEqual(resolved[0].metadata["resolution_basis"], "source_html_href")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (original.id,)).fetchone()[0], 0)

    def test_resolves_guidance_anchor_href_to_guidance_paragraph(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/guidance/statements-of-policy/sop/01-06-2026"
        upsert_nodes(conn, [
            Node("source", "guidance_paragraph", "guidance:7.8", "SoP 7.8", url=f"{source_url}#src"),
            Node("target", "guidance_paragraph", "guidance:7.7", "SoP 7.7", url=f"{source_url}#abc123", metadata={"html_id": "abc123"}),
            Node(node_id("external:https://www.prarulebook.co.uk/guidance/statements-of-policy/sop#abc123"), "external_reference", "external:https://www.prarulebook.co.uk/guidance/statements-of-policy/sop#abc123", "(c)"),
        ])
        original = Edge(
            edge_id("source", node_id("external:https://www.prarulebook.co.uk/guidance/statements-of-policy/sop#abc123"), "references", "guidance-anchor"),
            "source",
            node_id("external:https://www.prarulebook.co.uk/guidance/statements-of-policy/sop#abc123"),
            "references",
            "html_link",
            1.0,
            "(c)",
            source_url,
            {"href": "https://www.prarulebook.co.uk/guidance/statements-of-policy/sop#abc123", "target_key": "external:https://www.prarulebook.co.uk/guidance/statements-of-policy/sop#abc123"},
        )
        upsert_edges(conn, [original])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "target")
        self.assertEqual(resolved[0].metadata["html_id"], "abc123")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (original.id,)).fetchone()[0], 0)

    def test_resolves_when_source_href_matches_specific_placeholder_title_extension(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        upsert_source(
            conn,
            source_type="part",
            url=source_url,
            fetched_at="2026-06-22T00:00:00Z",
            raw_html='<p><a href="/pra-rules/smf#abc123">Senior Management Functions 6.2</a></p>',
        )
        placeholder_key = "url:pra-rules/smf"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("target", "rule", "rule:target", "6.2", metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Senior Management Functions 6.2", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "smf"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "Senior Management Functions",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/smf", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "target")
        self.assertEqual(resolved[0].metadata["resolution_basis"], "source_html_href")

    def test_scopes_fragment_resolution_to_canonical_document_path(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        placeholder_key = "url:pra-rules/target-part"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("wrong", "rule", "rule:wrong", "Wrong", url="https://www.prarulebook.co.uk/pra-rules/other-part/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node("right", "rule", "rule:right", "Right", url="https://www.prarulebook.co.uk/pra-rules/target-part/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Target Part", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "target"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "Target Part",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/target-part#abc123", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "right")
        self.assertEqual(resolved[0].metadata["canonical_document_path"], "pra-rules/target-part")
        self.assertEqual(resolved[0].metadata["document_resolution_basis"], "document_path_html_id")

    def test_selects_source_date_when_canonical_document_has_multiple_versions(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        placeholder_key = "url:pra-rules/target-part"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("july", "rule", "rule:july", "July", url="https://www.prarulebook.co.uk/pra-rules/target-part/01-07-2026#abc123", metadata={"html_id": "abc123"}),
            Node("june", "rule", "rule:june", "June", url="https://www.prarulebook.co.uk/pra-rules/target-part/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Target Part", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "target-version"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "Target Part",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/target-part#abc123", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "june")
        self.assertEqual(resolved[0].metadata["document_resolution_basis"], "document_path_html_id_source_version")

    def test_uses_fragment_only_when_html_id_is_globally_unique(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        placeholder_key = "url:pra-rules/missing-part"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("target", "rule", "rule:target", "Target", url="https://www.prarulebook.co.uk/pra-rules/target-part/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Missing Part", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "unique-fallback"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "Missing Part",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/missing-part#abc123", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "target")
        self.assertEqual(resolved[0].metadata["document_resolution_basis"], "global_html_id_unique")

    def test_leaves_fragment_ambiguous_across_documents_unresolved(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        placeholder_key = "url:pra-rules/missing-part"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("target-a", "rule", "rule:target-a", "Target A", url="https://www.prarulebook.co.uk/pra-rules/target-a/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node("target-b", "rule", "rule:target-b", "Target B", url="https://www.prarulebook.co.uk/pra-rules/target-b/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Missing Part", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "ambiguous-fallback"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "Missing Part",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/missing-part#abc123", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        self.assertEqual(_resolve_html_anchor_reference_edges(conn), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (original.id,)).fetchone()[0], 1)

    def test_does_not_choose_between_duplicate_nodes_in_one_document(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        placeholder_key = "url:pra-rules/target-part"
        target_url = "https://www.prarulebook.co.uk/pra-rules/target-part/01-06-2026#abc123"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("chapter", "chapter", "chapter:target", "Target heading", url=target_url, metadata={"html_id": "abc123"}),
            Node("rule", "rule", "rule:target", "Target rule", url=target_url, metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Target Part", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "duplicate-node"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "Target Part",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/target-part#abc123", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        self.assertEqual(_resolve_html_anchor_reference_edges(conn), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (original.id,)).fetchone()[0], 1)

    def test_rechecks_existing_resolved_edge_against_document_scope(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        target_href = "https://www.prarulebook.co.uk/pra-rules/target-part#abc123"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("wrong", "rule", "rule:wrong", "Wrong", url="https://www.prarulebook.co.uk/pra-rules/other-part/01-06-2026#abc123", metadata={"html_id": "abc123"}),
            Node("right", "rule", "rule:right", "Right", url="https://www.prarulebook.co.uk/pra-rules/target-part/01-06-2026#abc123", metadata={"html_id": "abc123"}),
        ])
        old = Edge(
            edge_id("source", "wrong", "references", "html_anchor:abc123"),
            "source",
            "wrong",
            "references",
            "html_anchor_resolved",
            0.98,
            "Target",
            source_url,
            {"href": target_href, "html_id": "abc123"},
        )
        upsert_edges(conn, [old])

        resolved = _resolve_html_anchor_reference_edges(conn)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].to_node_id, "right")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (old.id,)).fetchone()[0], 0)

    def test_removes_existing_resolved_edge_when_document_match_is_ambiguous(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        target_href = "https://www.prarulebook.co.uk/pra-rules/target-part#abc123"
        target_url = "https://www.prarulebook.co.uk/pra-rules/target-part/01-06-2026#abc123"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("chapter", "chapter", "chapter:target", "Target heading", url=target_url, metadata={"html_id": "abc123"}),
            Node("rule", "rule", "rule:target", "Target rule", url=target_url, metadata={"html_id": "abc123"}),
        ])
        old = Edge(
            edge_id("source", "chapter", "references", "html_anchor:abc123"),
            "source",
            "chapter",
            "references",
            "html_anchor_resolved",
            0.98,
            "Target",
            source_url,
            {"href": target_href, "html_id": "abc123"},
        )
        upsert_edges(conn, [old])

        self.assertEqual(_resolve_html_anchor_reference_edges(conn), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (old.id,)).fetchone()[0], 0)

    def test_does_not_use_unrelated_placeholder_title_to_resolve(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        upsert_source(
            conn,
            source_type="part",
            url=source_url,
            fetched_at="2026-06-22T00:00:00Z",
            raw_html='<p><a href="/pra-rules/standard-formula#abc123">Solvency Capital Requirement – Standard Formula 3D2</a></p>',
        )
        placeholder_key = "url:pra-rules/standard-formula"
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("target", "rule", "rule:target", "3D2", metadata={"html_id": "abc123"}),
            Node(node_id(placeholder_key), "rule_reference", placeholder_key, "Solvency Capital Requirement – Standard Formula 3D2", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id(placeholder_key), "references", "sf"),
            "source",
            node_id(placeholder_key),
            "references",
            "html_link",
            1.0,
            "3D3",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/standard-formula", "target_key": placeholder_key},
        )
        upsert_edges(conn, [original])

        self.assertEqual(_resolve_html_anchor_reference_edges(conn), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (original.id,)).fetchone()[0], 1)

    def test_does_not_resolve_when_source_href_match_is_ambiguous(self):
        conn = self.make_conn()
        source_url = "https://www.prarulebook.co.uk/pra-rules/source-part/01-06-2026"
        upsert_source(
            conn,
            source_type="part",
            url=source_url,
            fetched_at="2026-06-22T00:00:00Z",
            raw_html=' '.join([
                '<a href="/pra-rules/target-part#abc123">Target Part</a>',
                '<a href="/pra-rules/target-part#def456">Target Part</a>',
            ]),
        )
        upsert_nodes(conn, [
            Node("source", "rule", "rule:source", "Source", url=source_url),
            Node("target1", "rule", "rule:target1", "Target Part A", metadata={"html_id": "abc123"}),
            Node("target2", "rule", "rule:target2", "Target Part B", metadata={"html_id": "def456"}),
            Node(node_id("url:pra-rules/target-part"), "rule_reference", "url:pra-rules/target-part", "Target Part", metadata={"placeholder": True}),
        ])
        original = Edge(
            edge_id("source", node_id("url:pra-rules/target-part"), "references", "plain-target"),
            "source",
            node_id("url:pra-rules/target-part"),
            "references",
            "html_link",
            1.0,
            "Target Part",
            source_url,
            {"href": "https://www.prarulebook.co.uk/pra-rules/target-part", "target_key": "url:pra-rules/target-part"},
        )
        upsert_edges(conn, [original])

        self.assertEqual(_resolve_html_anchor_reference_edges(conn), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (original.id,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
