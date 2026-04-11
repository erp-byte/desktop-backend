"""Job Card PDF generation — printable job card matching CFC/PRD/JC format."""

from fpdf import FPDF


class JobCardPDF(FPDF):
    def __init__(self, jc_data):
        super().__init__('P', 'mm', 'A4')
        self.jc = jc_data
        self.set_auto_page_break(auto=True, margin=10)

    def header(self):
        self.set_font('Helvetica', 'B', 10)
        self.cell(0, 5, 'Candor Foods Private Limited', 0, 1, 'C')
        self.set_font('Helvetica', '', 8)
        self.cell(0, 4, 'Location # : Factory', 0, 1, 'C')
        # Production order info
        p = self.jc.get('section_1_product', {})
        self.cell(0, 4, f"Production Order # : {self.jc.get('job_card_number', '')}  Date : {self.jc.get('created_at', '')[:10]}", 0, 1, 'C')
        self.cell(0, 4, f"Sales Order # {p.get('sales_order_ref', '--')}", 0, 1, 'C')
        self.ln(2)
        self.set_font('Helvetica', '', 7)
        self.cell(95, 3, f"Page : 1 / 2", 0, 1, 'R')

    def footer(self):
        self.set_y(-10)
        self.set_font('Helvetica', 'I', 7)
        self.cell(0, 5, f'Page {self.page_no()}/{{nb}}', 0, 0, 'C')

    def _safe(self, text):
        """Convert text to latin-1 safe string."""
        if text is None:
            return '--'
        s = str(text)
        try:
            s.encode('latin-1')
            return s
        except UnicodeEncodeError:
            return s.encode('latin-1', 'replace').decode('latin-1')

    def _row(self, label, value, w1=45, w2=50):
        self.set_font('Helvetica', '', 7)
        self.cell(w1, 5, self._safe(label), 1, 0)
        self.set_font('Helvetica', 'B', 7)
        self.cell(w2, 5, self._safe(value), 1, 1)


def _fmt_num(v):
    if v is None or v == '' or v == '--':
        return '--'
    try:
        n = float(v)
        return f"{n:,.3f}" if n % 1 != 0 else f"{int(n):,}"
    except (ValueError, TypeError):
        return str(v)


def generate_job_card_pdf(jc_data: dict, mode: str = 'full') -> bytes:
    """Generate a job card PDF.

    Args:
        jc_data: Full job card detail dict from get_job_card_detail()
        mode: 'full' (with actuals) or 'bom' (BOM only, empty for paperwork)

    Returns:
        PDF bytes
    """
    pdf = JobCardPDF(jc_data)
    pdf.alias_nb_pages()
    pdf.add_page()

    p = jc_data.get('section_1_product', {})
    team = jc_data.get('section_3_team', {})

    # ── Section 1: Product Details (table) ──
    pdf.set_font('Helvetica', '', 7)
    col1_l, col1_v, col2_l, col2_v = 30, 55, 30, 50
    row_h = 5

    def info_row(l1, v1, l2, v2):
        pdf.set_font('Helvetica', '', 7)
        pdf.cell(col1_l, row_h, pdf._safe(l1), 1, 0)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.cell(col1_v, row_h, pdf._safe(v1), 1, 0)
        pdf.set_font('Helvetica', '', 7)
        pdf.cell(col2_l, row_h, pdf._safe(l2), 1, 0)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.cell(col2_v, row_h, pdf._safe(v2), 1, 1)

    info_row('Customer', p.get('customer_name', ''), 'Batch', p.get('batch_number', ''))
    info_row('Product', p.get('fg_sku_name', ''), 'Article Code', p.get('article_code', ''))
    info_row('Quantity', f"{p.get('batch_size_kg', '')} Kg / {p.get('quantity_units', '')} units", 'MRP', p.get('mrp', '--'))
    info_row('EAN', p.get('ean', '--'), 'Best Before', p.get('best_before', '--'))
    info_row('Factory', p.get('factory', '--'), 'Floor', p.get('floor', '--'))
    info_row('Shelf Life', f"{p.get('shelf_life_days', '--')} days", 'SO Ref', p.get('sales_order_ref', '--'))
    pdf.ln(3)

    # ── Section 2: Bill of Material ──
    pdf.set_font('Helvetica', 'B', 8)
    pdf.cell(0, 5, 'Bill Of Material', 0, 1)

    rm_lines = jc_data.get('section_2a_rm_indent', [])
    consumption = jc_data.get('material_consumption', [])
    cons_map = {(c.get('material_sku_name', '')).lower(): c for c in consumption}

    # Header
    pdf.set_font('Helvetica', 'B', 7)
    if mode == 'full':
        pdf.cell(55, 5, 'Product Name', 1, 0)
        pdf.cell(25, 5, 'Required Qty', 1, 0, 'C')
        pdf.cell(25, 5, 'Issued Qty', 1, 0, 'C')
        pdf.cell(25, 5, 'Actual Qty', 1, 0, 'C')
        pdf.cell(20, 5, 'Batch No.', 1, 0, 'C')
        pdf.cell(15, 5, 'UOM', 1, 1, 'C')
    else:
        pdf.cell(65, 5, 'Product Name', 1, 0)
        pdf.cell(30, 5, 'Required Qty', 1, 0, 'C')
        pdf.cell(30, 5, 'Issued Qty', 1, 0, 'C')
        pdf.cell(25, 5, 'Batch No.', 1, 0, 'C')
        pdf.cell(15, 5, 'UOM', 1, 1, 'C')

    # Rows
    pdf.set_font('Helvetica', '', 7)
    for r in rm_lines:
        sku = r.get('material_sku_name', '')
        reqd = _fmt_num(r.get('reqd_qty'))
        issued = _fmt_num(r.get('issued_qty'))
        batch = r.get('batch_no', 'Primary Batch')
        uom = r.get('uom', 'Kgs')
        c = cons_map.get(sku.lower(), {})
        actual = _fmt_num(c.get('actual_consumed_qty')) if mode == 'full' else ''

        if mode == 'full':
            pdf.cell(55, 5, pdf._safe(sku), 1, 0)
            pdf.cell(25, 5, reqd, 1, 0, 'C')
            pdf.cell(25, 5, issued, 1, 0, 'C')
            pdf.cell(25, 5, actual, 1, 0, 'C')
            pdf.cell(20, 5, pdf._safe(batch), 1, 0, 'C')
            pdf.cell(15, 5, pdf._safe(uom), 1, 1, 'C')
        else:
            pdf.cell(65, 5, pdf._safe(sku), 1, 0)
            pdf.cell(30, 5, reqd, 1, 0, 'C')
            pdf.cell(30, 5, issued, 1, 0, 'C')
            pdf.cell(25, 5, pdf._safe(batch), 1, 0, 'C')
            pdf.cell(15, 5, pdf._safe(uom), 1, 1, 'C')

    pdf.ln(3)

    # ── Section 3: Team Details ──
    pdf.set_font('Helvetica', 'B', 8)
    pdf.cell(0, 5, 'Team Details', 0, 1)
    pdf.set_font('Helvetica', '', 7)
    info_row('Team Leader', team.get('team_leader', ''), 'Start Time', team.get('start_time', '--'))
    members = ', '.join(team.get('team_members') or []) if team.get('team_members') else '--'
    info_row('Team Member', members, 'End Time', team.get('end_time', '--'))
    pdf.ln(1)

    # Checkboxes row
    pdf.set_font('Helvetica', '', 7)
    checks = [
        ('Fumigation', team.get('fumigation', False)),
        ('Metal Detector Used', team.get('metal_detector_used', False)),
        ('Roasting/Pasteurization', team.get('roasting_pasteurization', False)),
        ('Control Sample Given', bool(team.get('control_sample_gm'))),
        ('Magnets Used', team.get('magnets_used', False)),
    ]
    for label, val in checks:
        mark = 'Yes' if val else 'No'
        pdf.cell(33, 5, f"> {mark}    {label}", 1, 0)
    pdf.ln(3)

    # ── Section 4: Material Return / Output ──
    if mode == 'full':
        output = jc_data.get('section_5_output') or {}
        acct = jc_data.get('material_accounting') or {}
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, 'Output & Material Accounting', 0, 1)

        pdf.set_font('Helvetica', '', 7)
        info_row('FG Output (Units)', output.get('fg_actual_units', '--'), 'FG Output (Kg)', _fmt_num(output.get('fg_actual_kg')))
        info_row('RM Consumed (Kg)', _fmt_num(output.get('rm_consumed_kg')), 'Material Return (Kg)', _fmt_num(output.get('material_return_kg')))
        info_row('Rejection (Kg)', _fmt_num(output.get('rejection_kg')), 'Off-grade (Kg)', _fmt_num(acct.get('offgrade_total_kg')))
        info_row('Process Loss (Kg)', _fmt_num(acct.get('process_loss_kg')), 'Process Loss %', _fmt_num(acct.get('process_loss_pct')) + '%' if acct.get('process_loss_pct') else '--')
        info_row('Extra Give Away (Kg)', _fmt_num(acct.get('extra_give_away_kg')), 'Balance Material (Kg)', _fmt_num(acct.get('balance_material_kg')))
        info_row('Control Sample (Kg)', _fmt_num(acct.get('control_sample_kg')), 'Wastage (Kg)', _fmt_num(acct.get('wastage_kg')))
        info_row('Total Issued (Kg)', _fmt_num(acct.get('total_material_issued_kg')), 'Total Loss %', _fmt_num(acct.get('total_loss_pct')) + '%' if acct.get('total_loss_pct') else '--')
        pdf.ln(2)
    else:
        # Empty output section for paperwork
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, 'Material Return', 0, 1)
        pdf.set_font('Helvetica', '', 7)
        info_row('FG (Units)', '', 'Rejection (kg/g)', '')
        info_row('', '', 'Process Loss Calculated', '')
        pdf.ln(2)

    # ── Section 5: Sign-offs ──
    pdf.set_font('Helvetica', 'B', 8)
    pdf.cell(0, 5, 'Sign-offs', 0, 1)
    signoffs = jc_data.get('section_6_signoffs', {})
    pdf.set_font('Helvetica', '', 7)
    for so_type in ['floor_incharge', 'qc_inspector', 'production_manager']:
        so = signoffs.get(so_type, {}) if isinstance(signoffs, dict) else {}
        label = so_type.replace('_', ' ').title()
        name = so.get('name', '') if isinstance(so, dict) else ''
        pdf.cell(55, 8, pdf._safe(label), 1, 0)
        pdf.cell(55, 8, pdf._safe(name), 1, 0, 'C')
        pdf.cell(55, 8, '', 1, 1)
    pdf.ln(3)

    # ═══ PAGE 2: ANNEXURES ═══
    pdf.add_page()
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(0, 6, 'Annexures', 0, 1, 'C')
    pdf.ln(2)

    # Annexure A/B: Metal Detection
    metal = jc_data.get('annexure_ab_metal', [])
    if metal:
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, 'Annexure A/B - Metal Detection', 0, 1)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.cell(30, 5, 'Check Type', 1, 0)
        pdf.cell(20, 5, 'Fe', 1, 0, 'C')
        pdf.cell(20, 5, 'NFe', 1, 0, 'C')
        pdf.cell(20, 5, 'SS', 1, 0, 'C')
        pdf.cell(25, 5, 'Failed Units', 1, 0, 'C')
        pdf.cell(50, 5, 'Remarks', 1, 1)
        pdf.set_font('Helvetica', '', 7)
        for m in metal:
            pdf.cell(30, 5, pdf._safe(m.get('check_type', '')), 1, 0)
            pdf.cell(20, 5, 'Pass' if m.get('fe_pass') else 'Fail', 1, 0, 'C')
            pdf.cell(20, 5, 'Pass' if m.get('nfe_pass') else 'Fail', 1, 0, 'C')
            pdf.cell(20, 5, 'Pass' if m.get('ss_pass') else 'Fail', 1, 0, 'C')
            pdf.cell(25, 5, str(m.get('failed_units', 0)), 1, 0, 'C')
            pdf.cell(50, 5, pdf._safe(m.get('remarks', '')), 1, 1)
        pdf.ln(3)

    # Annexure B: Weight Checks
    wc = jc_data.get('annexure_b_weight', {})
    samples = wc.get('samples', []) if isinstance(wc, dict) else []
    if samples:
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, f"Annexure B - Weight Checks (Target: {wc.get('target_wt_g', '--')}g, Tolerance: {wc.get('tolerance_g', '--')}g)", 0, 1)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.cell(15, 5, '#', 1, 0, 'C')
        pdf.cell(30, 5, 'Net Wt (g)', 1, 0, 'C')
        pdf.cell(30, 5, 'Gross Wt (g)', 1, 0, 'C')
        pdf.cell(25, 5, 'Leak Test', 1, 1, 'C')
        pdf.set_font('Helvetica', '', 7)
        for s in samples[:20]:
            pdf.cell(15, 5, str(s.get('sample_number', '')), 1, 0, 'C')
            pdf.cell(30, 5, _fmt_num(s.get('net_weight')), 1, 0, 'C')
            pdf.cell(30, 5, _fmt_num(s.get('gross_weight')), 1, 0, 'C')
            pdf.cell(25, 5, 'Pass' if s.get('leak_test_pass') else 'Fail', 1, 1, 'C')
        pdf.ln(3)

    # Annexure C: Environment
    env = jc_data.get('annexure_c_env', [])
    if env:
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, 'Annexure C - Environment Parameters', 0, 1)
        pdf.set_font('Helvetica', '', 7)
        for e in env:
            pdf.cell(50, 5, pdf._safe(e.get('parameter_name', '')), 1, 0)
            pdf.cell(50, 5, pdf._safe(e.get('value', '')), 1, 1)
        pdf.ln(3)

    # Annexure D: Loss Reconciliation
    loss = jc_data.get('annexure_d_loss', [])
    if loss:
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, 'Annexure D - Loss Reconciliation', 0, 1)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.cell(40, 5, 'Category', 1, 0)
        pdf.cell(25, 5, 'Budgeted %', 1, 0, 'C')
        pdf.cell(25, 5, 'Budgeted Kg', 1, 0, 'C')
        pdf.cell(25, 5, 'Actual Kg', 1, 0, 'C')
        pdf.cell(25, 5, 'Variance', 1, 0, 'C')
        pdf.cell(25, 5, 'Remarks', 1, 1)
        pdf.set_font('Helvetica', '', 7)
        for l in loss:
            pdf.cell(40, 5, pdf._safe(l.get('loss_category', '')), 1, 0)
            pdf.cell(25, 5, _fmt_num(l.get('budgeted_loss_pct')), 1, 0, 'C')
            pdf.cell(25, 5, _fmt_num(l.get('budgeted_loss_kg')), 1, 0, 'C')
            pdf.cell(25, 5, _fmt_num(l.get('actual_loss_kg')), 1, 0, 'C')
            pdf.cell(25, 5, _fmt_num(l.get('variance_kg')), 1, 0, 'C')
            pdf.cell(25, 5, pdf._safe(l.get('remarks', '')), 1, 1)
        pdf.ln(3)

    # Annexure E: Remarks
    remarks = jc_data.get('annexure_e_remarks', [])
    if remarks:
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 5, 'Annexure E - Remarks', 0, 1)
        pdf.set_font('Helvetica', '', 7)
        for r in remarks:
            rtype = (r.get('remark_type', '') or '').replace('_', ' ').title()
            pdf.cell(30, 5, pdf._safe(rtype), 1, 0)
            pdf.cell(100, 5, pdf._safe(r.get('content', '')), 1, 0)
            pdf.cell(35, 5, pdf._safe(r.get('recorded_by', '')), 1, 1)

    # Generate bytes — fpdf .output(dest='S') returns a string, encode to bytes
    raw = pdf.output(dest='S')
    return raw.encode('latin-1') if isinstance(raw, str) else bytes(raw)
