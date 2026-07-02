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
