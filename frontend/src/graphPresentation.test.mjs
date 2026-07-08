import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { displayNodeTitle, documentBadge, relativeNodeRole, edgeDirectionLabel } from './graphPresentation.js';

describe('graph presentation helpers', () => {
  it('labels generic Bank of England spreadsheet references as reporting templates', () => {
    const node = {
      node_type: 'external_reference',
      title: 'Bank of England',
      url: 'https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/reporting/close-links-monthly-report.xls',
    };

    assert.equal(displayNodeTitle(node), 'Reporting template · XLS');
    assert.deepEqual(documentBadge(node), { label: 'XLS', kind: 'spreadsheet' });
  });

  it('labels generic Bank of England PDF reporting references as reporting templates', () => {
    const node = {
      node_type: 'external_reference',
      title: 'Bank of England',
      url: 'https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/rep001atemplatedec18.pdf',
    };

    assert.equal(displayNodeTitle(node), 'Reporting template · PDF');
    assert.deepEqual(documentBadge(node), { label: 'PDF', kind: 'pdf' });
  });

  it('adds PDF badge text to specific external document titles without replacing the title', () => {
    const node = {
      node_type: 'external_reference',
      title: 'Supervisory statement SS1/23',
      url: 'https://www.bankofengland.co.uk/example/ss123.pdf',
    };

    assert.equal(displayNodeTitle(node), 'Supervisory statement SS1/23 · PDF');
    assert.deepEqual(documentBadge(node), { label: 'PDF', kind: 'pdf' });
  });

  it('adds concise workbook template names to reporting template labels', () => {
    const node = {
      node_type: 'Template',
      title: 'C05.01',
      metadata: {
        template_code: 'C05.01',
        title: '5.1 | C 05.01 - TRANSITIONAL PROVISIONS (CA5.1) Adjustments to CET1 | Adjustments to AT1',
      },
    };

    assert.equal(displayNodeTitle(node), 'C05.01 · Transitional provisions');
  });

  it('disambiguates reporting XML and XSD artefacts by taxonomy package', () => {
    const base = {
      node_type: 'SourceDocument',
      title: 'pra110-pre.xml',
      metadata: { file_type: 'xml' },
    };

    assert.equal(displayNodeTitle({
      ...base,
      url: 'https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/2026/february/boebanking400.zip#Banking_4.0.0/www.bankofengland.co.uk/data/xbrl/fws/banking/liquidity_pillarii/2019-08-14/mod/pra110-pre.xml',
    }), 'pra110-pre.xml · Banking 4.0.0');
    assert.equal(displayNodeTitle({
      ...base,
      url: 'https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/2025/boe-banking-370-hotfix.zip#boe-banking-370-hotfix/www.bankofengland.co.uk/data/xbrl/fws/banking/liquidity_pillarii/2019-08-14/mod/pra110-pre.xml',
    }), 'pra110-pre.xml · boe-banking-370-hotfix');
  });

  it('identifies parents and children relative to the selected node through contains edges', () => {
    const graph = { edges: [
      { edge_type: 'contains', from_node_id: 'parent', to_node_id: 'selected' },
      { edge_type: 'contains', from_node_id: 'selected', to_node_id: 'child' },
    ] };

    assert.equal(relativeNodeRole({ id: 'parent' }, 'selected', graph), 'parent');
    assert.equal(relativeNodeRole({ id: 'child' }, 'selected', graph), 'child');
    assert.equal(relativeNodeRole({ id: 'other' }, 'selected', graph), 'related');
  });

  it('labels edge direction relative to the selected node', () => {
    assert.equal(edgeDirectionLabel({ from_node_id: 'selected', to_node_id: 'target' }, 'selected'), 'outgoing');
    assert.equal(edgeDirectionLabel({ from_node_id: 'source', to_node_id: 'selected' }, 'selected'), 'incoming');
    assert.equal(edgeDirectionLabel({ from_node_id: 'source', to_node_id: 'target' }, 'selected'), 'related');
  });
});
