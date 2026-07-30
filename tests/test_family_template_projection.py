from pathlib import Path
import unittest

from backend.app.db import DEFAULT_DB, connect
from backend.app.reporting import (
    reporting_catalog_cells,
    reporting_template_document_path,
    reporting_template_layout,
)
from scripts.project_family_templates import (
    ROOT,
    WORKBOOK_TYPES,
    artifact_sources,
    existing_sheet_templates,
    project,
    template_rows_for_source,
    workbook_sheets,
)


class FamilyTemplateProjectionTests(unittest.TestCase):
    def test_every_visible_workbook_sheet_and_pdf_template_is_projected(self):
        conn = connect(DEFAULT_DB)
        workbook_templates = 0
        pdf_templates = 0
        try:
            for artifact in artifact_sources(conn):
                path = ROOT / artifact["local_path"]
                if artifact["file_type"] in WORKBOOK_TYPES:
                    expected = workbook_sheets(path)
                    resolved = existing_sheet_templates(
                        conn,
                        artifact["source_id"],
                        path,
                    )
                    missing = [
                        sheet for sheet in expected if sheet not in resolved
                    ]
                    self.assertEqual(
                        missing,
                        [],
                        f"{artifact['return_code']} {artifact['source_id']}",
                    )
                    workbook_templates += len(expected)
                else:
                    templates = template_rows_for_source(
                        conn,
                        artifact["source_id"],
                    )
                    self.assertTrue(
                        templates,
                        f"{artifact['return_code']} {artifact['source_id']}",
                    )
                    layout = reporting_template_layout(
                        conn,
                        templates[0]["node_id"],
                        project_root=ROOT,
                    )
                    self.assertIsNotNone(layout)
                    self.assertEqual(layout["format"], "pdf")
                    self.assertGreater(layout["page_count"], 0)
                    document = reporting_template_document_path(
                        conn,
                        templates[0]["node_id"],
                        project_root=ROOT,
                    )
                    self.assertEqual(document, path.resolve())
                    pdf_templates += 1
        finally:
            conn.close()

        self.assertEqual(workbook_templates, 83)
        self.assertEqual(pdf_templates, 8)

    def test_projection_is_idempotent_after_full_family_load(self):
        conn = connect(DEFAULT_DB)
        try:
            result = project(conn)
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(
            result["counts"].get("workbook_templates_created", 0),
            0,
        )
        self.assertEqual(
            result["counts"].get("pdf_templates_created", 0),
            0,
        )

    def test_every_target_return_exposes_its_exact_templates_in_cells(self):
        conn = connect(DEFAULT_DB)
        expected_total = 0
        actual_total = 0
        returns = conn.execute(
            """
            SELECT return_id,return_code,status
            FROM reporting_return_catalog
            WHERE upper(return_code) GLOB 'PRA[0-9][0-9][0-9]'
               OR upper(return_code) GLOB 'RFB[0-9][0-9][0-9]'
               OR upper(return_code) GLOB 'LVR[0-9][0-9][0-9]'
            ORDER BY return_code,status
            """
        ).fetchall()
        try:
            for reporting_return in returns:
                artifacts = conn.execute(
                    """
                    SELECT DISTINCT lower(a.file_type) AS file_type,
                           sd.local_path,sd.source_id
                    FROM reporting_return_artifact ra
                    JOIN reporting_artifact a
                      ON a.artifact_id=ra.artifact_id
                    JOIN source_document sd ON sd.url=a.url
                    WHERE ra.return_id=?
                      AND ra.relationship='template'
                      AND lower(a.file_type) IN (
                        'xlsx','xlsm','xltx','pdf'
                      )
                    """,
                    (reporting_return["return_id"],),
                ).fetchall()
                expected = sum(
                    len(workbook_sheets(ROOT / artifact["local_path"]))
                    if artifact["file_type"] in WORKBOOK_TYPES
                    else 1
                    for artifact in artifacts
                )
                result = reporting_catalog_cells(
                    conn,
                    reporting_return["return_id"],
                    limit=1,
                    offset=0,
                )
                self.assertIsNotNone(result)
                self.assertEqual(
                    len(result["templates"]),
                    expected,
                    (
                        reporting_return["return_code"],
                        reporting_return["status"],
                    ),
                )
                for template in result["templates"]:
                    layout = reporting_template_layout(
                        conn,
                        template["template_id"],
                        project_root=ROOT,
                    )
                    self.assertIsNotNone(
                        layout,
                        (
                            reporting_return["return_code"],
                            template["template_id"],
                        ),
                    )
                expected_total += expected
                actual_total += len(result["templates"])
        finally:
            conn.close()

        self.assertEqual(len(returns), 32)
        self.assertEqual(expected_total, 97)
        self.assertEqual(actual_total, expected_total)


if __name__ == "__main__":
    unittest.main()
