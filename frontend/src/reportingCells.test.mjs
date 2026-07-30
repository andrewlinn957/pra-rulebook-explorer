import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  reportingCellCoordinate,
  reportingCellCoverage,
  reportingCellPath,
  reportingCellTitle,
  reportingNodeSelectsTemplate,
  reportingTemplateForNode,
  reportingTemplateGrid,
  reportingTemplateTitle,
  reportingWorkbookCellStyle,
  reportingWorkbookColumnPixels,
  reportingWorkbookDatapoints,
} from './reportingCells.js';

const cell = {
  template_code: 'PRA115',
  template_title: 'Step-in risk',
  row_code: '010',
  row_label: 'Exposure',
  column_code: '020',
  column_label: 'Amount',
  concept_label: 'Step-in exposure',
};

describe('reporting cell navigation', () => {
  it('builds a novice-readable route from requirement to cell', () => {
    const path = reportingCellPath(cell, { return_code: 'PRA115', name: 'Step-in risk' });

    assert.deepEqual(path.map(item => item.kind), [
      'Reporting requirement', 'Template', 'Row', 'Column', 'Cell',
    ]);
    assert.equal(path.at(-1).code, 'r010 / c020');
    assert.equal(path.at(-1).label, 'Step-in exposure');
  });

  it('uses coordinates when a cell has no concept label', () => {
    assert.equal(reportingCellCoordinate(cell), 'r010 / c020');
    assert.equal(reportingCellTitle({ row_code: '010', column_code: '020' }), 'r010 / c020');
  });

  it('does not overstate cell coverage', () => {
    assert.equal(reportingCellCoverage('available').tone, 'available');
    assert.match(reportingCellCoverage('template_layout_available').title, /official template/i);
    assert.match(reportingCellCoverage('template_layout_available').detail, /datapoints are not yet available/i);
    assert.match(reportingCellCoverage('selected_template_unavailable').title, /selected template/i);
    assert.match(reportingCellCoverage('return_not_mapped').detail, /not yet linked/i);
  });

  it('reconstructs the underlying row and column matrix in template order', () => {
    const cells = [
      {
        datapoint_id: 'cell-20-30',
        row_id: 'row-20',
        row_code: '020',
        row_order: 20,
        row_label: 'Deductions',
        column_id: 'column-30',
        column_code: '030',
        column_order: 30,
        column_label: 'Adjustments',
        concept_label: 'Deduction adjustment',
      },
      {
        datapoint_id: 'cell-10-20',
        row_id: 'row-10',
        row_code: '010',
        row_order: 10,
        row_label: 'Own funds',
        column_id: 'column-20',
        column_code: '020',
        column_order: 20,
        column_label: 'Amount',
        concept_label: 'Own funds amount',
      },
      {
        datapoint_id: 'cell-20-20',
        row_id: 'row-20',
        row_code: '020',
        row_order: 20,
        row_label: 'Deductions',
        column_id: 'column-20',
        column_code: '020',
        column_order: 20,
        column_label: 'Amount',
        concept_label: 'Deduction amount',
      },
      {
        datapoint_id: 'unpositioned',
        row_code: '',
        row_label: 'Workbook note',
        column_code: '',
        concept_label: 'Cannot be placed in the matrix',
      },
    ];

    const grid = reportingTemplateGrid(cells);

    assert.deepEqual(grid.rows.map(row => row.code), ['010', '020']);
    assert.deepEqual(grid.columns.map(column => column.code), ['020', '030']);
    assert.equal(grid.cellsByCoordinate.get('row-10\u0000column-20')[0].datapoint_id, 'cell-10-20');
    assert.equal(grid.cellsByCoordinate.has('row-10\u0000column-30'), false);
    assert.equal(grid.populatedCells, 3);
    assert.equal(grid.populatedCoordinates, 3);
    assert.equal(grid.unpositionedCells, 1);
  });

  it('keeps column positions while narrowing search results to matching rows', () => {
    const cells = [
      { datapoint_id: 'exposure', row_id: 'row-10', row_code: '010', row_label: 'Exposure', column_id: 'column-10', column_code: '010', column_label: 'Amount' },
      { datapoint_id: 'capital', row_id: 'row-20', row_code: '020', row_label: 'Capital', column_id: 'column-20', column_code: '020', column_label: 'Ratio' },
    ];

    const grid = reportingTemplateGrid(cells, 'exposure');

    assert.deepEqual(grid.rows.map(row => row.code), ['010']);
    assert.deepEqual(grid.columns.map(column => column.code), ['010', '020']);
    assert.equal(grid.matchingCells, 1);
  });

  it('extracts a concise title from workbook-derived template text', () => {
    assert.equal(reportingTemplateTitle({
      template_code: 'C01.00',
      title: '1 C 01.00 - OWN FUNDS (CA1) Rows | ID | Item | Amount 0010 | 1',
    }), 'C 01.00 - OWN FUNDS (CA1)');
  });

  it('opens the cell template selected in the reporting graph', () => {
    const templates = [
      { template_id: 'template:C71.00', template_code: 'C71.00' },
      { template_id: 'template:COR011:C80.00_80', template_code: 'C80.00' },
    ];
    const selected = {
      id: 'logical_template:075f811f21b18eb0',
      node_type: 'LogicalTemplate',
      title: '80',
      metadata: { code: '80', name: '80' },
    };

    assert.equal(
      reportingTemplateForNode(selected, templates)?.template_id,
      'template:COR011:C80.00_80',
    );
  });

  it('prefers a stable template id and never uses a partial code match', () => {
    const templates = [
      { template_id: 'template:C8.00', template_code: 'C8.00' },
      { template_id: 'template:C80.00', template_code: 'C80.00' },
    ];

    assert.equal(reportingTemplateForNode({
      id: 'graph-node',
      node_type: 'Template',
      metadata: { template_id: 'template:C80.00', code: '8' },
    }, templates)?.template_code, 'C80.00');
    assert.equal(reportingTemplateForNode({
      id: 'logical-template-8',
      node_type: 'LogicalTemplate',
      metadata: { code: '8' },
    }, templates)?.template_code, 'C8.00');
  });

  it('maps a workbook index only when it is unambiguous within the return', () => {
    const selected = {
      node_type: 'Worksheet',
      title: 'Index',
      metadata: { name: 'Index', component_role: 'supporting_worksheet' },
    };

    assert.equal(reportingTemplateForNode(selected, [
      { template_id: 'template:index', template_code: 'C80.00 Index' },
    ])?.template_id, 'template:index');
    assert.equal(reportingTemplateForNode(selected, [
      { template_id: 'template:index-1', template_code: 'C80.00 Index' },
      { template_id: 'template:index-2', template_code: 'C81.00 Index' },
    ]), null);
  });

  it('normalises workbook suffixes and punctuation without guessing across ambiguous codes', () => {
    assert.equal(reportingTemplateForNode({
      node_type: 'LogicalTemplate',
      title: 'Capital+ Input',
      metadata: { code: 'Capital+ Input' },
    }, [
      { template_id: 'capital', template_code: 'C01.00 Capital Input' },
      { template_id: 'return', template_code: 'PRA101' },
    ])?.template_id, 'capital');
    assert.equal(reportingTemplateForNode({
      node_type: 'Worksheet',
      title: '75',
      metadata: { code: '75' },
    }, [
      { template_id: 'c75-01', template_code: 'C75.01' },
    ])?.template_id, 'c75-01');
    assert.equal(reportingTemplateForNode({
      node_type: 'Worksheet',
      title: '75',
      metadata: { code: '75' },
    }, [
      { template_id: 'c75-00', template_code: 'C75.00' },
      { template_id: 'c75-01', template_code: 'C75.01' },
    ]), null);
  });

  it('does not infer a cell template from numbers on a non-template node', () => {
    const node = {
      node_type: 'RequirementEdition',
      title: 'Reporting requirement 80',
    };
    assert.equal(reportingNodeSelectsTemplate(node), false);
    assert.equal(reportingTemplateForNode(node, [
      { template_id: 'template:C80.00', template_code: 'C80.00' },
    ]), null);
  });

  it('converts source workbook dimensions and styles without replacing them with generic grid styling', () => {
    assert.equal(reportingWorkbookColumnPixels(20.73046875, 80), 120);
    assert.deepEqual(reportingWorkbookCellStyle({
      font: { name: 'Verdana', size: 11, bold: true },
      fill: { pattern: 'solid', foreground: '#D9D9D9' },
      border: { bottom: { style: 'thin', colour: '#000000' } },
      alignment: { horizontal: 'center', vertical: 'center', wrap: true },
      number_format: 'General',
    }, 80), {
      backgroundColor: '#D9D9D9',
      color: '#000',
      fontFamily: '"Verdana", Arial, sans-serif',
      fontSize: `${11 * 96 / 72 * .8}px`,
      fontWeight: 700,
      fontStyle: 'normal',
      textDecoration: 'none',
      textAlign: 'center',
      verticalAlign: 'middle',
      justifyContent: 'center',
      alignItems: 'center',
      whiteSpace: 'normal',
      overflow: 'visible',
      paddingLeft: undefined,
      transform: undefined,
      writingMode: undefined,
      borderTop: undefined,
      borderRight: undefined,
      borderBottom: '1px solid #000000',
      borderLeft: undefined,
    });
  });

  it('keeps workbook positions connected to reporting datapoints', () => {
    const layout = {
      columns: [
        { index: 2, reporting_code: null },
        { index: 5, reporting_code: '0010' },
      ],
    };
    const datapoint = {
      datapoint_id: 'cell-0010-0010',
      row_code: '0010',
      column_code: '0010',
    };
    const workbook = reportingWorkbookDatapoints(layout, [datapoint]);

    assert.equal(workbook.cellFor(
      { reporting_code: '0010' },
      { column: 5 },
    ), datapoint);
    assert.equal(workbook.cellFor(
      { reporting_code: '0010' },
      { column: 2 },
    ), null);
  });
});
