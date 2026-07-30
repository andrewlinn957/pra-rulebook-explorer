import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.project_reporting_instruction_coordinates import (
    PROJECTOR,
    SOURCE_TABLE,
    expand_code_spec,
    normalise_template_code,
    parse_instruction_coordinates,
    project_instruction_coordinates,
)

ROOT = Path(__file__).resolve().parents[1]


class ReportingInstructionCoordinateTests(unittest.TestCase):
    def test_normalises_spaced_template_code(self):
        self.assertEqual(normalise_template_code("template:C 74.00"), "C74.00")

    def test_parses_explicit_cell_reference(self):
        mentions = parse_instruction_coordinates(
            "Institutions shall report figure from {C 72.00; r0030; c0040}.",
            default_template_code="C72.00",
        )

        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].template_code, "C72.00")
        self.assertEqual(mentions[0].row_spec, "0030")
        self.assertEqual(mentions[0].column_spec, "0040")
        self.assertEqual(
            mentions[0].relation,
            "normative_reporting_coordinate",
        )

    def test_keeps_format_example_out_of_normative_tier(self):
        mentions = parse_instruction_coordinates(
            "For example, {C 72.00; r0130; c0040} refers to a cell.",
            default_template_code="C72.00",
        )

        self.assertEqual(mentions[0].relation, "explicit_coordinate_reference")

    def test_parses_rows_and_reported_column_against_default_template(self):
        mentions = parse_instruction_coordinates(
            "For rows 0269-0295, 0309-0335 and for row 0490, "
            "credit institutions shall report in column 0040 the market value.",
            default_template_code="template:C74.00",
        )

        self.assertEqual(mentions[0].template_code, "C74.00")
        self.assertEqual(mentions[0].column_spec, "0040")
        self.assertIn("0269-0295", mentions[0].row_spec)

    def test_parses_reported_row_and_each_column_list(self):
        mentions = parse_instruction_coordinates(
            "Credit institutions shall report in row 0020 of C 74.00 "
            "for each column 0010, 0020 and 0030 the total amount; and "
            "for each column 0140, 0150 and 0160 total inflows."
        )

        self.assertEqual(
            {(m.row_spec, m.column_spec) for m in mentions},
            {
                ("0020", "0010, 0020 and 0030"),
                ("0020", "0140, 0150 and 0160"),
            },
        )

    def test_expands_ranges_only_to_rows_that_exist(self):
        self.assertEqual(
            expand_code_spec(
                "0040, 0060-0090 and 0120",
                ["0040", "0050", "0060", "0070", "0090", "0100", "0120"],
            ),
            ["0040", "0060", "0070", "0090", "0120"],
        )

    def test_exact_zero_padded_code_does_not_expand_to_alias(self):
        self.assertEqual(
            expand_code_spec("0010", ["0010", "010"]),
            ["0010"],
        )

    def test_projection_dry_run_is_read_only_and_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "projection.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.executescript((ROOT / "schema.sql").read_text())
            conn.execute(
                """
                INSERT INTO source_document(source_id,title,url,file_type)
                VALUES (
                  'instructions','Liquidity instructions',
                  'https://example.test/instructions.pdf','pdf'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO source_span(
                  span_id,source_id,span_type,raw_text,page_number
                ) VALUES (
                  'span:instruction','instructions','paragraph',
                  'For row 0010, credit institutions shall report in column 0040 in accordance with Article 4.',
                  12
                )
                """
            )
            conn.execute(
                """
                INSERT INTO template(
                  template_id,template_code,title,source_id
                ) VALUES (
                  'template:C74.00','C74.00','Inflows','instructions'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO template_row(
                  row_id,template_id,row_code,row_order,label
                ) VALUES (
                  'row:C74.00:0010','template:C74.00','0010',1,'Total'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO template_column(
                  column_id,template_id,column_code,column_order,label
                ) VALUES (
                  'column:C74.00:0040','template:C74.00','0040',1,'Amount'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO instruction(
                  instruction_id,instruction_set,applies_to_type,applies_to_id,
                  text,source_span_id
                ) VALUES (
                  'instruction:test','Liquidity instructions','template',
                  'template:C74.00',
                  'For row 0010, credit institutions shall report in column 0040 in accordance with Article 4.',
                  'span:instruction'
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO graph_node(
                  node_id,node_type,label,source_table,source_pk,properties_json
                ) VALUES (?,?,?,?,?,?)
                """,
                [
                    (
                        "source_document:instructions",
                        "SourceDocument",
                        "Liquidity instructions",
                        "source_document",
                        "instructions",
                        "{}",
                    ),
                    (
                        "template:C74.00",
                        "Template",
                        "Inflows",
                        "template",
                        "template:C74.00",
                        "{}",
                    ),
                    (
                        "row:C74.00:0010",
                        "TemplateRow",
                        "0010 Total",
                        "template_row",
                        "row:C74.00:0010",
                        "{}",
                    ),
                    (
                        "column:C74.00:0040",
                        "TemplateColumn",
                        "0040 Amount",
                        "template_column",
                        "column:C74.00:0040",
                        "{}",
                    ),
                    (
                        "provision:article-4",
                        "Provision",
                        "Article 4",
                        "provision",
                        "article-4",
                        json.dumps({"canonical_key": "article:regulatory:4"}),
                    ),
                ],
            )
            conn.commit()
            before = (
                conn.execute("SELECT COUNT(*) FROM graph_node").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM graph_edge").fetchone()[0],
            )
            conn.close()

            dry_run = project_instruction_coordinates(db_path)
            self.assertEqual(dry_run["status"], "dry_run")
            self.assertEqual(dry_run["nodes_by_type"]["InstructionProvision"], 1)
            self.assertEqual(dry_run["nodes_by_type"]["ReportingCoordinate"], 1)
            self.assertEqual(dry_run["edges_by_type"]["INSTRUCTS"], 1)

            conn = sqlite3.connect(db_path)
            after_dry_run = (
                conn.execute("SELECT COUNT(*) FROM graph_node").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM graph_edge").fetchone()[0],
            )
            conn.close()
            self.assertEqual(after_dry_run, before)

            first_apply = project_instruction_coordinates(db_path, apply=True)
            second_apply = project_instruction_coordinates(db_path, apply=True)
            self.assertEqual(first_apply, second_apply)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM graph_node WHERE source_table=?",
                    (SOURCE_TABLE,),
                ).fetchone()[0],
                2,
            )
            edges = conn.execute(
                """
                SELECT edge_type,properties_json
                FROM graph_edge
                WHERE extraction_method IN (?,?)
                ORDER BY edge_type
                """,
                (PROJECTOR, "instruction_legal_reference_projection"),
            ).fetchall()
            self.assertEqual(
                [row["edge_type"] for row in edges],
                ["APPLIES_TO", "EVIDENCED_BY", "INSTRUCTS", "REFERENCES_RULE"],
            )
            instructs = next(row for row in edges if row["edge_type"] == "INSTRUCTS")
            properties = json.loads(instructs["properties_json"])
            self.assertEqual(
                properties["coordinate_relation"],
                "normative_reporting_coordinate",
            )
            coordinate = conn.execute(
                """
                SELECT properties_json FROM graph_node
                WHERE node_type='ReportingCoordinate'
                """
            ).fetchone()
            self.assertEqual(
                json.loads(coordinate["properties_json"])["coverage_status"],
                "instruction_defined_not_materialized",
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
