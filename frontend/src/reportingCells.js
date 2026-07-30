export function reportingCellCoordinate(cell) {
  const row = cell?.row_code || '—';
  const column = cell?.column_code || '—';
  return `r${row} / c${column}`;
}

export function reportingCellTitle(cell) {
  return cell?.concept_label
    || cell?.label
    || cell?.row_label
    || reportingCellCoordinate(cell);
}

export function reportingCellPath(cell, reportingReturn) {
  return [
    {
      kind: 'Reporting requirement',
      code: reportingReturn?.return_code || '',
      label: reportingReturn?.name || reportingReturn?.return_code || '',
    },
    {
      kind: 'Template',
      code: cell?.template_code || '',
      label: cell?.template_title || cell?.template_code || '',
    },
    {
      kind: 'Row',
      code: cell?.row_code || '',
      label: cell?.row_label || cell?.row_code || '',
    },
    {
      kind: 'Column',
      code: cell?.column_code || '',
      label: cell?.column_label || cell?.column_code || '',
    },
    {
      kind: 'Cell',
      code: reportingCellCoordinate(cell),
      label: reportingCellTitle(cell),
    },
  ];
}

export function reportingCellCoverage(coverage) {
  if (coverage === 'available') {
    return {
      tone: 'available',
      title: 'Cell-level data available',
      detail: 'Search and inspect the parsed rows, columns and cells for this reporting edition.',
    };
  }
  if (coverage === 'selected_template_unavailable') {
    return {
      tone: 'partial',
      title: 'No parsed cells for the selected template',
      detail: 'This graph template is not linked to parsed cells. Choose another available template below instead.',
    };
  }
  if (coverage === 'template_layout_available') {
    return {
      tone: 'partial',
      title: 'Official template available',
      detail: 'Browse the exact official worksheet or PDF; mapped reporting datapoints are not yet available for this edition.',
    };
  }
  return {
    tone: 'missing',
    title: 'No cell-bearing template mapping',
    detail: 'This catalogue edition is not yet linked to a parsed template in the existing reporting corpus.',
  };
}

function reportingTemplateAliases(value) {
  const raw = String(value || '')
    .trim()
    .toUpperCase()
    .replace(/^TEMPLATE\s+/, '');
  const compact = raw.replace(/[^A-Z0-9]/g, '');
  if (!compact) return [];

  const aliases = new Set([compact]);
  const numbered = raw.replace(/\s+/g, '').match(/^C?0*(\d+)(?:\.(\d+))?$/);
  if (numbered) {
    aliases.add(String(Number(numbered[1])));
    if (numbered[2] && !/^0+$/.test(numbered[2])) {
      aliases.add(`${Number(numbered[1])}.${numbered[2]}`);
    }
  }

  const codedSuffix = raw.match(/^C\s*0*(\d+)(?:\.(\d+))?\s+(.+)$/);
  if (codedSuffix) {
    const suffix = codedSuffix[3].replace(/[^A-Z0-9]/g, '');
    if (suffix) aliases.add(suffix);
  }

  if (compact.endsWith('INDEX')) {
    aliases.add('INDEX');
  }
  return [...aliases];
}

export function reportingNodeSelectsTemplate(node) {
  const metadata = node?.metadata || {};
  return /template|worksheet/i.test(String(node?.node_type || ''))
    || metadata.component_role === 'reporting_template'
    || metadata.reporting_role === 'LogicalTemplate';
}

function reportingNodeTemplateAliases(node) {
  const metadata = node?.metadata || {};
  if (!reportingNodeSelectsTemplate(node)) return [];

  return [
    metadata.template_code,
    metadata.code,
    metadata.resolved_display_name,
    metadata.name,
    node?.title,
  ].flatMap(reportingTemplateAliases);
}

export function reportingTemplateForNode(node, templates) {
  if (!node || !(templates || []).length) return null;
  const metadata = node.metadata || {};
  const directIds = new Set([
    node.id,
    node.source_pk,
    metadata.template_id,
    metadata.node_id,
    metadata.source_pk,
  ].filter(Boolean).map(String));
  const direct = (templates || []).filter(template => (
    directIds.has(String(template?.template_id || ''))
    || directIds.has(String(template?.node_id || ''))
  ));
  if (direct.length === 1) return direct[0];

  const aliases = new Set(reportingNodeTemplateAliases(node));
  if (!aliases.size) return null;
  let matches = (templates || []).filter(template => (
    reportingTemplateAliases(template?.template_code).some(alias => aliases.has(alias))
  ));
  if (matches.length <= 1) return matches[0] || null;

  const sourceUrl = String(node.source_url || metadata.source_url || '');
  if (sourceUrl) {
    const sameSource = matches.filter(template => String(template?.source_url || '') === sourceUrl);
    if (sameSource.length === 1) return sameSource[0];
    if (sameSource.length) matches = sameSource;
  }
  return matches.length === 1 ? matches[0] : null;
}

function axisKey(cell, axis) {
  return cell?.[`${axis}_id`]
    || `${axis}:${cell?.[`${axis}_code`] || '—'}`;
}

function compareAxes(left, right) {
  const leftHasOrder = left.order !== null && left.order !== undefined && left.order !== '';
  const rightHasOrder = right.order !== null && right.order !== undefined && right.order !== '';
  if (leftHasOrder && rightHasOrder) {
    const order = Number(left.order) - Number(right.order);
    if (Number.isFinite(order) && order) return order;
  } else if (leftHasOrder !== rightHasOrder) {
    return leftHasOrder ? -1 : 1;
  }
  return String(left.code || '').localeCompare(String(right.code || ''), undefined, {
    numeric: true,
    sensitivity: 'base',
  });
}

function cellMatches(cell, query) {
  if (!query) return true;
  const haystack = [
    cell?.datapoint_id,
    cell?.concept_label,
    cell?.label,
    cell?.row_code,
    cell?.row_label,
    cell?.column_code,
    cell?.column_label,
    cell?.data_type,
    cell?.unit_type,
    reportingCellCoordinate(cell),
  ].filter(Boolean).join(' ').toLowerCase();
  return haystack.includes(query);
}

export function reportingTemplateGrid(cells, query = '') {
  const rows = new Map();
  const columns = new Map();
  const cellsByCoordinate = new Map();
  const matches = new Set();
  const needle = String(query || '').trim().toLowerCase();
  let unpositionedCells = 0;

  for (const cell of cells || []) {
    if (!cell?.row_code || !cell?.column_code) {
      unpositionedCells += 1;
      continue;
    }
    const rowId = axisKey(cell, 'row');
    const columnId = axisKey(cell, 'column');
    if (!rows.has(rowId)) {
      rows.set(rowId, {
        id: rowId,
        code: cell.row_code || '—',
        label: cell.row_label || cell.row_code || 'Unlabelled row',
        order: cell.row_order,
      });
    }
    if (!columns.has(columnId)) {
      columns.set(columnId, {
        id: columnId,
        code: cell.column_code || '—',
        label: cell.column_label || cell.column_code || 'Unlabelled column',
        order: cell.column_order,
      });
    }
    const coordinate = `${rowId}\u0000${columnId}`;
    if (!cellsByCoordinate.has(coordinate)) cellsByCoordinate.set(coordinate, []);
    cellsByCoordinate.get(coordinate).push(cell);
    if (cellMatches(cell, needle)) matches.add(cell.datapoint_id);
  }

  const orderedRows = [...rows.values()].sort(compareAxes);
  const orderedColumns = [...columns.values()].sort(compareAxes);
  const visibleRows = needle
    ? orderedRows.filter(row => (
        (cells || []).some(cell => (
          axisKey(cell, 'row') === row.id && matches.has(cell.datapoint_id)
        ))
      ))
    : orderedRows;

  return {
    rows: visibleRows,
    allRows: orderedRows,
    columns: orderedColumns,
    cellsByCoordinate,
    matchingIds: matches,
    matchingCells: matches.size,
    populatedCells: (cells || []).length - unpositionedCells,
    populatedCoordinates: cellsByCoordinate.size,
    unpositionedCells,
    query: needle,
  };
}

export function reportingWorkbookColumnPixels(width, zoom = 100) {
  const sourceWidth = Number(width) || 8.43;
  const pixels = sourceWidth < 1
    ? Math.floor(sourceWidth * 12 + 0.5)
    : Math.floor(sourceWidth * 7 + 5);
  return Math.max(1, Math.round(pixels * (Number(zoom) || 100) / 100));
}

function workbookBorder(side) {
  if (!side?.style) return undefined;
  const colour = side.colour || '#000';
  const styles = {
    hair: `1px solid ${colour}`,
    thin: `1px solid ${colour}`,
    medium: `2px solid ${colour}`,
    thick: `3px solid ${colour}`,
    double: `3px double ${colour}`,
    dotted: `1px dotted ${colour}`,
    dashed: `1px dashed ${colour}`,
    dashDot: `1px dashed ${colour}`,
    dashDotDot: `1px dashed ${colour}`,
    mediumDashed: `2px dashed ${colour}`,
    mediumDashDot: `2px dashed ${colour}`,
    mediumDashDotDot: `2px dashed ${colour}`,
    slantDashDot: `2px dashed ${colour}`,
  };
  return styles[side.style] || `1px solid ${colour}`;
}

export function reportingWorkbookCellStyle(style, zoom = 100) {
  const scale = (Number(zoom) || 100) / 100;
  const font = style?.font || {};
  const fill = style?.fill || {};
  const border = style?.border || {};
  const alignment = style?.alignment || {};
  const horizontal = {
    center: 'center',
    right: 'right',
    left: 'left',
    justify: 'justify',
    distributed: 'justify',
  }[alignment.horizontal] || (style?.number_format && style.number_format !== 'General' ? 'right' : 'left');
  const vertical = {
    top: 'flex-start',
    center: 'center',
    bottom: 'flex-end',
    justify: 'center',
    distributed: 'center',
  }[alignment.vertical] || 'flex-end';
  const rotation = Number(alignment.rotation) || 0;

  return {
    backgroundColor: fill.pattern === 'solid' ? fill.foreground || fill.background || '#fff' : '#fff',
    color: font.colour || '#000',
    fontFamily: font.name ? `"${font.name}", Arial, sans-serif` : 'Arial, sans-serif',
    fontSize: `${Math.max(7, (Number(font.size) || 10) * 96 / 72 * scale)}px`,
    fontWeight: font.bold ? 700 : 400,
    fontStyle: font.italic ? 'italic' : 'normal',
    textDecoration: font.underline ? 'underline' : 'none',
    textAlign: horizontal,
    verticalAlign: alignment.vertical === 'top' ? 'top' : alignment.vertical === 'center' ? 'middle' : 'bottom',
    justifyContent: horizontal === 'center' ? 'center' : horizontal === 'right' ? 'flex-end' : 'flex-start',
    alignItems: vertical,
    whiteSpace: alignment.wrap ? 'normal' : 'nowrap',
    overflow: alignment.shrink ? 'hidden' : 'visible',
    paddingLeft: alignment.indent ? `${alignment.indent * 9 * scale}px` : undefined,
    transform: rotation && rotation !== 255 ? `rotate(${-rotation}deg)` : undefined,
    writingMode: rotation === 255 ? 'vertical-rl' : undefined,
    borderTop: workbookBorder(border.top),
    borderRight: workbookBorder(border.right),
    borderBottom: workbookBorder(border.bottom),
    borderLeft: workbookBorder(border.left),
  };
}

export function reportingWorkbookDatapoints(layout, cells) {
  const datapoints = new Map();
  for (const cell of cells || []) {
    const rowCode = String(cell?.row_code || '');
    const columnCode = String(cell?.column_code || '');
    if (!rowCode || !columnCode) continue;
    const key = `${rowCode}\u0000${columnCode}`;
    if (!datapoints.has(key)) datapoints.set(key, []);
    datapoints.get(key).push(cell);
  }
  const columnCodes = new Map(
    (layout?.columns || []).map(column => [column.index, column.reporting_code]),
  );
  return {
    datapoints,
    columnCodes,
    cellFor(row, cell) {
      const rowCode = String(row?.reporting_code || '');
      const columnCode = String(columnCodes.get(cell?.column) || '');
      if (!rowCode || !columnCode) return null;
      return datapoints.get(`${rowCode}\u0000${columnCode}`)?.[0] || null;
    },
  };
}

export function reportingTemplateTitle(template) {
  const code = String(template?.template_code || '').trim();
  const raw = String(template?.title || '').replace(/\s+/g, ' ').trim();
  if (!raw) return code || 'Reporting template';
  const concise = raw
    .split(/\s+Rows?\s*\|\s*|\s+\d{4}\s*\|\s*/i)[0]
    .replace(/^\d+(?:\.\d+)?\s+(?=[A-Z])/, '')
    .trim();
  if (!concise || concise.toLowerCase() === `template ${code}`.toLowerCase()) {
    return code || concise || 'Reporting template';
  }
  return concise.length > 140 ? `${concise.slice(0, 137)}…` : concise;
}
