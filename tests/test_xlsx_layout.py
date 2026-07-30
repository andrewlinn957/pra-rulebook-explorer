from pathlib import Path
import unittest

from backend.app.db import DEFAULT_DB, connect
from backend.app.reporting import (
    reporting_catalog_cells,
    reporting_template_layout,
)
from backend.app.xlsx_layout import parse_xlsx_layout


ROOT = Path(__file__).resolve().parents[1]
NSFR_WORKBOOK = (
    ROOT
    / "backend/data/raw/reporting-sources/cor011-lcr-final/files"
    / "corep-nsfr-6a1f68bd35cc071e.xlsx"
)


class XlsxLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = parse_xlsx_layout(
            NSFR_WORKBOOK,
            template_id="template:COR011:C80.00_80",
            template_code="C80.00",
            title="80 C 80.00 - NSFR - REQUIRED STABLE FUNDING",
        )

    def test_selects_the_exact_underlying_worksheet(self):
        self.assertIsNotNone(self.layout)
        self.assertEqual(self.layout["sheet_name"], "80")
        self.assertEqual(self.layout["dimension"], "B1:Q118")
        self.assertEqual(len(self.layout["rows"]), 118)
        self.assertEqual(len(self.layout["columns"]), 16)
        self.assertEqual(self.layout["zoom"], 80)

    def test_preserves_merged_headers_dimensions_and_styles(self):
        title = self.layout["rows"][1]["cells"][0]
        self.assertEqual(title["value"], "C 80.00 - NSFR - REQUIRED STABLE FUNDING")
        self.assertEqual(title["column_span"], 16)
        self.assertEqual(len(self.layout["merge_refs"]), 12)
        self.assertAlmostEqual(self.layout["columns"][2]["width"], 89.33203125)
        self.assertEqual(self.layout["styles"][129]["fill"]["foreground"], "#D9D9D9")
        self.assertEqual(self.layout["styles"][226]["font"]["name"], "Verdana")
        self.assertEqual(self.layout["styles"][226]["font"]["size"], 16)

    def test_detects_reporting_coordinates_without_changing_sheet_values(self):
        columns = {
            column["letter"]: column["reporting_code"]
            for column in self.layout["columns"]
        }
        rows = {
            row["index"]: row["reporting_code"]
            for row in self.layout["rows"]
        }
        self.assertEqual(columns["E"], "0010")
        self.assertEqual(columns["Q"], "0130")
        self.assertEqual(rows[10], "0010")
        self.assertEqual(rows[11], "0020")
        self.assertEqual(self.layout["rows"][9]["cells"][2]["value"], "REQUIRED STABLE FUNDING")

    def test_recovers_the_official_workbook_when_template_source_metadata_is_wrong(self):
        conn = connect(DEFAULT_DB)
        try:
            layout = reporting_template_layout(
                conn,
                "template:C71.00",
                project_root=ROOT,
            )
        finally:
            conn.close()

        self.assertIsNotNone(layout)
        self.assertEqual(layout["sheet_name"], "71")
        self.assertEqual(layout["dimension"], "A1:BN21")
        self.assertIn("corep-counterbalancing-capacity.xlsx", layout["source_url"])

    def test_resolves_graph_native_template_names_to_their_exact_worksheets(self):
        expected_sheets = {
            "template:COREP-CCR:C26.00_26": "26",
            "template:COREP-CCR:C26.00_Index": "Index",
            "template:COREP-CCR:C27.00_27": "27",
            "template:COREP-CCR:C28.00_28-29": "28-29",
            "template:COREP-LARGE-EXPOSURES:C26.00_Index": "Index",
            "template:COREP-LOSSES-IMMOVABLE-PROPERTY:C15.00_15": "15",
            "template:COREP-LOSSES-IMMOVABLE-PROPERTY:C15.00_Index": "Index",
            "template:COREP-OWN-FUNDS:C01.00_Index": "Index",
            "template:COREP-OWN-FUNDS:C08.05_8.5": "8.5",
            "template:PRA101:C01.00_Capital_Input": "Capital+ Input",
            "template:PRA102:C01.00_Capital_Input": "Capital+ Input",
            "template:PRA103:C01.00_Capital_Input": "Capital+ Input",
            "template:PRA111:PRA111_Capital_items": "Capital_items",
        }
        conn = connect(DEFAULT_DB)
        try:
            for template_id, expected_sheet in expected_sheets.items():
                with self.subTest(template_id=template_id):
                    layout = reporting_template_layout(
                        conn,
                        template_id,
                        project_root=ROOT,
                    )
                    self.assertIsNotNone(layout)
                    self.assertEqual(layout["sheet_name"], expected_sheet)
        finally:
            conn.close()

    def test_nsfr_cells_only_offers_the_declared_80_to_84_worksheets(self):
        conn = connect(DEFAULT_DB)
        try:
            reporting_return = conn.execute(
                """
                SELECT return_id
                FROM reporting_return_catalog
                WHERE return_code='CRR-ANNEXES-XII-XIII'
                """
            ).fetchone()
            result = reporting_catalog_cells(
                conn,
                reporting_return["return_id"],
                limit=1,
            )
            template_codes = {
                template["template_code"] for template in result["templates"]
            }
            c80 = next(
                template
                for template in result["templates"]
                if template["template_code"] == "C80.00"
            )
            layout = reporting_template_layout(
                conn,
                c80["template_id"],
                project_root=ROOT,
            )
        finally:
            conn.close()

        self.assertEqual(
            template_codes,
            {
                "C80.00",
                "C80.00 Index",
                "C81.00",
                "C82.00",
                "C83.00",
                "C84.00",
            },
        )
        self.assertNotIn("C71.00", template_codes)
        self.assertIsNotNone(layout)
        self.assertEqual(layout["sheet_name"], "80")
        self.assertEqual(
            layout["rows"][1]["cells"][0]["value"],
            "C 80.00 - NSFR - REQUIRED STABLE FUNDING",
        )


if __name__ == "__main__":
    unittest.main()
