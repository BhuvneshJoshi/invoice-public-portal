# =============================================================================
# PUBLIC DOCUMENT PORTAL — JB Corporation / Swami Devi Dyal Hospital
# =============================================================================
# Deploy this file as a SEPARATE Streamlit Cloud app (your public repository).
# It shares the same Firestore database as the private ERP app.
#
# Required Streamlit secrets (set in the Streamlit Cloud dashboard):
#
#   [gcp_service_account]
#   type = "service_account"
#   project_id = "your-project-id"
#   private_key_id = "..."
#   private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
#   client_email = "..."
#   ... (same JSON keys as your private app)
#
# HOW IT WORKS:
#   1. Private ERP generates QR code linking to:
#      https://your-portal.streamlit.app/?share_doc=INV-101&token=abc123&dtype=Tax+Invoice
#   2. Customer scans QR → lands on this app → PDF downloads instantly.
#   3. Token is validated against the value stored in Firestore before any PDF is served.
# =============================================================================

import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, date, timedelta
import hashlib
import os
import io
import json
import urllib.parse
import hmac
from xml.sax.saxutils import escape as xml_escape

# --- REPORTLAB PDF DEPENDENCIES ---
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.platypus.flowables import HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.barcode.qr import QrCodeWidget

DEFAULT_TERMS_AND_CONDITIONS = (
    "1. Overdue bills Carry an interest @18% per annum.\n"
    "2. All disputes subject to Ambala jurisdiction.\n"
    "3. Goods once sold will not be taken back to exchange.\n"
    "4. Our responsibility ceases when the goods leave our godown.\n"
    "5. Delivery is subject to receipt/realization of advance payment along with the purchase order."
)

DIGITAL_SIGNATURE_NOTE = "This is a digitally signed document. Signature is not required."

st.set_page_config(
    page_title="Document Portal — JB Corporation",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# =============================================================================
# 🔥 FIRESTORE INITIALISATION (SHARED DATABASE WITH PRIVATE ERP)
# =============================================================================
if not firebase_admin._apps:
    try:
        if "gcp_service_account" in st.secrets:
            secret_data = dict(st.secrets["gcp_service_account"])
            if "private_key" in secret_data:
                secret_data["private_key"] = secret_data["private_key"].replace("\\n", "\n")
            proj_id = secret_data.get("project_id", "jb-corp-erp")
            os.environ["GOOGLE_CLOUD_PROJECT"] = proj_id
            cred = credentials.Certificate(secret_data)
            firebase_admin.initialize_app(cred, {"projectId": proj_id})
        elif os.path.exists("jb-corp-erp.json"):
            with open("jb-corp-erp.json") as f:
                local_data = json.load(f)
            proj_id = local_data.get("project_id", "jb-corp-erp")
            os.environ["GOOGLE_CLOUD_PROJECT"] = proj_id
            cred = credentials.Certificate("jb-corp-erp.json")
            firebase_admin.initialize_app(cred, {"projectId": proj_id})
        else:
            st.error("⚠️ Portal misconfigured: no Firestore credentials found. Contact the system administrator.")
            st.stop()
    except Exception as e:
        st.error(f"Database initialisation error: {e}")
        st.stop()

try:
    db = firestore.client()
except Exception as e:
    st.error(f"Cannot connect to database: {e}")
    st.stop()


# =============================================================================
# 🛠️ SHARED UTILITY FUNCTIONS  (mirrored from private app.py)
# =============================================================================

def clean_text_value(raw_value, fallback=""):
    try:
        if pd.isna(raw_value):
            return fallback
    except Exception:
        pass
    text = str(raw_value).strip()
    if text.lower() in ["", "nan", "none", "n/a", "nat"]:
        return fallback
    return text


def pdf_safe(raw_value, fallback=""):
    """Escapes text before embedding it in ReportLab Paragraph XML."""
    return xml_escape(clean_text_value(raw_value, fallback))


def coerce_float_value(raw_value, fallback=0.0):
    parsed = pd.to_numeric(raw_value, errors="coerce")
    if pd.isna(parsed):
        return float(fallback)
    return float(parsed)


def format_expiry_mmyy(exp_str):
    if not exp_str or str(exp_str).strip().lower() in ["nan", "none", "", "n/a"]:
        return "N/A"
    s = str(exp_str).strip().replace("-", "/")
    parts = s.split("/")
    if len(parts) == 2:
        try:
            m, y = int(parts[0]), int(parts[1])
            if 1 <= m <= 12:
                return f"{m:02d}/{str(y)[-2:]}"
        except (ValueError, IndexError):
            pass
    try:
        dt = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if not pd.isna(dt):
            return dt.strftime("%m/%y")
    except Exception:
        pass
    return "N/A"


def coerce_document_issue_date(raw_value=None):
    if raw_value is None:
        return date.today()
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if isinstance(raw_value, date):
        return raw_value
    try:
        if pd.isna(raw_value):
            return date.today()
    except Exception:
        pass
    raw_text = str(raw_value).strip()
    if raw_text.lower() in ["", "nan", "none", "n/a", "nat"]:
        return date.today()
    try:
        parsed = pd.to_datetime(raw_text, dayfirst=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.date()
    except Exception:
        pass
    return date.today()


def format_document_issue_date(raw_value=None):
    return coerce_document_issue_date(raw_value).strftime("%d-%m-%Y")


def build_public_share_token(doc_number, customer_name="", issue_date=None):
    """Must be IDENTICAL to the private app implementation to produce matching tokens."""
    token_seed = f"{doc_number}|{customer_name}|{format_document_issue_date(issue_date)}".upper()
    return hashlib.sha256(token_seed.encode("utf-8")).hexdigest()[:24]


def build_public_document_url(base_url, doc_type, doc_number, share_token):
    """Builds the tokenised public URL — mirrors the private app exactly."""
    import urllib.parse as _up
    url_text = clean_text_value(base_url, "").strip()
    if not url_text:
        return ""
    normalized = url_text if "://" in url_text else f"https://{url_text}"
    separator = "&" if "?" in normalized else "?"
    params = {"share_doc": doc_number, "token": share_token, "dtype": doc_type}
    return f"{normalized.rstrip('/')}{separator}{_up.urlencode(params)}"


def normalize_inventory_dataframe(inv_df):
    if inv_df is None or inv_df.empty:
        return pd.DataFrame() if inv_df is None else inv_df.copy()
    inv = inv_df.copy()
    text_defaults = {
        "sector": "N/A", "invoice_no": "N/A", "item_name": "N/A",
        "vendor_name": "N/A", "batch_no": "N/A", "entry_date": "N/A",
        "timestamp": "N/A", "expiry_date": "N/A", "billing_date": "N/A",
        "hsn_code": "3004", "uom": "Strip"
    }
    numeric_defaults = {
        "quantity": 0.0, "unit_price": 0.0, "reorder_level": 0.0,
        "total_value": 0.0, "tabs_per_strip": 1.0, "mrp": 0.0,
        "gst_rate": 0.0, "total_internal_quantity": 0.0
    }
    for col, fallback in text_defaults.items():
        if col not in inv.columns:
            inv[col] = fallback
        inv[col] = inv[col].fillna(fallback).astype(str).replace(["", "nan", "None", "N/A"], fallback)
    for col, fallback in numeric_defaults.items():
        if col not in inv.columns:
            inv[col] = fallback
        inv[col] = pd.to_numeric(inv[col], errors="coerce").fillna(fallback)
    inv["tabs_per_strip"] = inv["tabs_per_strip"].replace(0, 1)
    return inv


def resolve_cart_line_hsn(line_item, inventory_df=None):
    direct_hsn = clean_text_value(line_item.get("hsn_code", ""), "")
    if direct_hsn:
        return direct_hsn
    if inventory_df is None or inventory_df.empty:
        return "3004"
    inv = normalize_inventory_dataframe(inventory_df)
    match = pd.DataFrame()
    doc_id = clean_text_value(line_item.get("inventory_doc_id", ""), "")
    item_name = clean_text_value(line_item.get("item_name", ""), "").upper()
    batch_no = clean_text_value(line_item.get("batch_no", ""), "").upper()
    if doc_id and "id" in inv.columns:
        match = inv[inv["id"].astype(str) == doc_id]
    if match.empty and item_name and batch_no:
        match = inv[
            (inv["item_name"].astype(str).str.upper() == item_name) &
            (inv["batch_no"].astype(str).str.upper() == batch_no)
        ]
    if not match.empty:
        return clean_text_value(match.iloc[0].get("hsn_code", ""), "3004")
    return "3004"


# =============================================================================
# 🧾 GST BILLING ENGINE (FULL PDF RENDERER — IDENTICAL TO PRIVATE APP)
# =============================================================================

class GSTBillingEngine:
    """Regenerates the exact same PDF that the private ERP produces."""

    STATE_CODES = {
        "Andaman & Nicobar Islands": "35", "Andhra Pradesh": "37", "Arunachal Pradesh": "12",
        "Assam": "18", "Bihar": "10", "Chandigarh": "04", "Chhattisgarh": "22",
        "Dadra & Nagar Haveli & Daman & Diu": "26", "Delhi": "07", "Goa": "30",
        "Gujarat": "24", "Haryana": "06", "Himachal Pradesh": "02", "Jammu & Kashmir": "01",
        "Jharkhand": "20", "Karnataka": "29", "Kerala": "32", "Ladakh": "38",
        "Lakshadweep": "31", "Madhya Pradesh": "23", "Maharashtra": "27", "Manipur": "14",
        "Meghalaya": "17", "Mizoram": "15", "Nagaland": "13", "Odisha": "21",
        "Puducherry": "34", "Punjab": "03", "Rajasthan": "08", "Sikkim": "11",
        "Tamil Nadu": "33", "Telangana": "36", "Tripura": "16", "Uttar Pradesh": "09",
        "Uttarakhand": "05", "West Bengal": "19"
    }

    def __init__(self, items_list, seller_details, customer_state="Haryana"):
        self.df = pd.DataFrame(items_list)
        self.seller = seller_details
        self.customer_state = customer_state
        seller_state = str(self.seller.get("state", "Haryana")).strip().lower()
        buyer_state = str(customer_state).strip().lower()
        self.is_interstate = (buyer_state != seller_state)
        if not self.df.empty:
            if "expiry_date" in self.df.columns:
                self.df["expiry_date"] = self.df["expiry_date"].apply(format_expiry_mmyy)
            self._process_calculations()

    def _process_calculations(self):
        numeric_defaults = {"mrp": 0.0, "discount_pct": 0.0, "qty": 0.0, "gst_rate": 0.0}
        text_defaults = {"item_name": "N/A", "batch_no": "N/A", "hsn_code": "3004", "uom": "Units", "expiry_date": "N/A"}
        for column, fallback in numeric_defaults.items():
            if column not in self.df.columns:
                self.df[column] = fallback
            self.df[column] = pd.to_numeric(self.df[column], errors="coerce").fillna(fallback)
        for column, fallback in text_defaults.items():
            if column not in self.df.columns:
                self.df[column] = fallback
            self.df[column] = self.df[column].fillna(fallback).astype(str)

        self.df["qty"] = self.df["qty"].clip(lower=0)
        self.df["discount_pct"] = self.df["discount_pct"].clip(lower=0, upper=100)
        self.df["gst_rate"] = self.df["gst_rate"].clip(lower=0, upper=100)
        self.df["sale_price_per_unit"] = self.df["mrp"] * (1 - self.df["discount_pct"] / 100)
        self.df["total_sale_price"]    = self.df["sale_price_per_unit"] * self.df["qty"]
        self.df["taxable_value"]       = self.df["total_sale_price"] / (1 + self.df["gst_rate"] / 100)
        self.df["total_gst"]           = self.df["total_sale_price"] - self.df["taxable_value"]
        if self.is_interstate:
            self.df["cgst"], self.df["sgst"], self.df["igst"] = 0.0, 0.0, self.df["total_gst"]
        else:
            self.df["cgst"] = self.df["total_gst"] / 2
            self.df["sgst"] = self.df["total_gst"] / 2
            self.df["igst"] = 0.0
        self.df["total_amount"] = self.df["total_sale_price"]
        cols = ["sale_price_per_unit", "total_sale_price", "taxable_value",
                "total_gst", "cgst", "sgst", "igst", "total_amount"]
        self.df[cols] = self.df[cols].astype(float).round(2)

    def generate_upi_qr(self, amount, doc_number):
        """Generates a UPI deep-link QR — identical to private app."""
        vpa  = self.seller.get("upi_id", "").strip()
        name = self.seller.get("firm_name", "MERCHANT").replace(" ", "%20")
        if not vpa:
            return None
        upi_string = f"upi://pay?pa={vpa}&pn={name}&am={amount:.2f}&tn={doc_number}"
        try:
            qr = QrCodeWidget(upi_string)
            qr.barWidth = 65
            qr.barHeight = 65
            d = Drawing(65, 65)
            d.add(qr)
            return d
        except Exception:
            return None

    def generate_soft_copy_qr(self, doc_type, doc_number, customer_name, issue_date_label):
        """Generates the scan-to-download QR that appears in the PDF footer."""
        base_url = clean_text_value(
            self.seller.get("soft_copy_base_url", self.seller.get("document_base_url", "")), ""
        ).strip()
        if not base_url:
            return None, ""
        share_token  = build_public_share_token(doc_number, customer_name, issue_date_label)
        soft_copy_url = build_public_document_url(base_url, doc_type, doc_number, share_token)
        if not soft_copy_url:
            return None, ""
        try:
            qr = QrCodeWidget(soft_copy_url)
            qr.barWidth  = 58
            qr.barHeight = 58
            d = Drawing(58, 58)
            d.add(qr)
            return d, soft_copy_url
        except Exception:
            return None, soft_copy_url

    def generate_pdf(self, doc_type, doc_number, customer_info=None, issue_date=None):
        if customer_info is None:
            customer_info = {"name": "Counter Retail Cash Customer", "address": "N/A",
                             "contact": "N/A", "gstin": "N/A", "payment_mode": "Cash"}

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4),
                                rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
        elements = []
        styles = getSampleStyleSheet()

        primary_navy    = colors.HexColor("#1E293B")
        secondary_slate = colors.HexColor("#475569")
        accent_theme    = colors.HexColor("#0F766E") if "invoice" in doc_type.lower() else colors.HexColor("#B45309")

        title_left      = ParagraphStyle("TitleL", fontSize=18, leading=22, textColor=primary_navy, fontName="Helvetica-Bold")
        meta_right      = ParagraphStyle("MetaR", fontSize=9, leading=14, alignment=2, textColor=secondary_slate, fontName="Helvetica")
        body_text_style = ParagraphStyle("BodyT", fontSize=8.5, leading=12, textColor=secondary_slate, fontName="Helvetica")

        grand_total     = float(self.df["total_amount"].sum()) if not self.df.empty else 0.0
        p_mode          = customer_info.get("payment_mode", "Cash").upper()
        issue_date_label = format_document_issue_date(issue_date)

        seller_state_name = self.seller.get("state", "Haryana")
        seller_state_code = self.STATE_CODES.get(seller_state_name, "N/A")

        _seller_gstin = self.seller.get("gstin", "").strip()
        _gstin_part   = f" | <b>GSTIN:</b> {pdf_safe(_seller_gstin)}" if _seller_gstin and _seller_gstin.upper() not in ("N/A", "NONE", "") else ""

        seller_html = f"""<b>{pdf_safe(self.seller.get('firm_name', '')).upper()}</b><br/>
        <font size="8.5" color="#64748B">
        {pdf_safe(self.seller.get('address', ''))}<br/>
        Contact Communications: {pdf_safe(self.seller.get('contact', ''))}<br/>
        <b>State:</b> {pdf_safe(seller_state_name)} ({pdf_safe(seller_state_code)}){_gstin_part}
        </font>"""

        meta_html = f"""<font size="14" color="{accent_theme.hexval()}"><b>{pdf_safe(doc_type).upper()}</b></font><br/><br/>
        <b>Document No:</b> {pdf_safe(doc_number)}<br/>
        <b>Date of Issue:</b> {issue_date_label}<br/>
        <b>Payment Mode:</b> <font color="{accent_theme.hexval()}"><b>{pdf_safe(p_mode)}</b></font>"""

        header_table = Table(
            [[Paragraph(seller_html, title_left), Paragraph(meta_html, meta_right)]],
            colWidths=[doc.width * 0.60, doc.width * 0.40]
        )
        header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        elements.append(header_table)
        elements.append(Spacer(1, 8))
        elements.append(HRFlowable(width="100%", thickness=2, color=accent_theme, spaceBefore=0, spaceAfter=12))

        cust_code = self.STATE_CODES.get(self.customer_state, "N/A")
        _pan      = self.seller.get("pan_no", "").strip()
        _pan_line = f"<b>PAN No:</b> {pdf_safe(_pan)}<br/>" if _pan and _pan.upper() not in ("N/A", "NONE", "") else ""

        left_profile_html = f"""<b><font color="{primary_navy.hexval()}">SUPPLY CHAIN INFRASTRUCTURE:</font></b><br/>
        <font size="8.5" color="#475569">
        <b>Drug License (DL) No:</b> {pdf_safe(self.seller.get('dl_no', 'N/A'))}<br/>
        {_pan_line}</font>"""

        c_gst      = customer_info.get("gstin", "").strip()
        c_gst_html = f"<b>Recipient GSTIN:</b> {pdf_safe(c_gst)}<br/>" if c_gst and c_gst.upper() != "N/A" else ""

        right_profile_html = f"""<b><font color="{primary_navy.hexval()}">BILLED RECIPIENT PROFILE:</font></b><br/>
        <font size="8.5" color="#475569">
        <b>Name:</b> {pdf_safe(customer_info.get('name', 'Counter Retail Customer'))}<br/>
        <b>Contact:</b> {pdf_safe(customer_info.get('contact', 'N/A'))}<br/>
        <b>Address:</b> {pdf_safe(customer_info.get('address', 'N/A'))}<br/>
        {c_gst_html}<b>Place of Supply:</b> {pdf_safe(self.customer_state)} (State Code: {pdf_safe(cust_code)})
        </font>"""

        profile_table = Table(
            [[Paragraph(left_profile_html, body_text_style), Paragraph(right_profile_html, body_text_style)]],
            colWidths=[doc.width * 0.46, doc.width * 0.54]
        )
        profile_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#F8FAFC")),
            ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#F8FAFC")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(profile_table)
        elements.append(Spacer(1, 14))

        # --- ITEM TABLE ---
        cell_center = ParagraphStyle("CellC", fontSize=7.2, leading=9.2, alignment=1, fontName="Helvetica", wordWrap="CJK")
        cell_left   = ParagraphStyle("CellL", fontSize=7.2, leading=9.2, alignment=0, fontName="Helvetica", wordWrap="CJK")
        cell_hdr    = ParagraphStyle("CellHdr", fontSize=7.0, leading=8.6, alignment=1, fontName="Helvetica-Bold", textColor=colors.white, wordWrap="CJK")

        show_batch = self.seller.get("show_batch_invoice", True) and ("batch_no" in self.df.columns)
        show_uom   = self.seller.get("show_uom_invoice", True) and ("uom" in self.df.columns)

        headers = ["S.No", "Item Description"]
        if show_batch: headers.append("Batch No")
        headers.extend(["HSN", "Expiry"])
        if show_uom: headers.append("UOM")
        headers.extend(["Qty", "Unit Rate", "Taxable", "CGST", "SGST", "IGST", "Gross Total"])

        table_payload = [[Paragraph(h, cell_hdr) for h in headers]]

        for idx, row in self.df.iterrows():
            row_cells = [Paragraph(str(idx + 1), cell_center), Paragraph(pdf_safe(row["item_name"]), cell_left)]
            if show_batch:
                row_cells.append(Paragraph(pdf_safe(row.get("batch_no", "N/A")), cell_center))
            row_cells.extend([
                Paragraph(pdf_safe(row["hsn_code"]), cell_center),
                Paragraph(pdf_safe(row.get("expiry_date", "N/A")), cell_center),
            ])
            if show_uom:
                row_cells.append(Paragraph(pdf_safe(row.get("uom", "Pills")), cell_center))
            row_cells.extend([
                Paragraph(f"{row['qty']:.0f}", cell_center),
                Paragraph(f"{row['mrp']:,.2f}", cell_center),
                Paragraph(f"{row['taxable_value']:,.2f}", cell_center),
                Paragraph(f"{row['cgst']:,.2f}", cell_center),
                Paragraph(f"{row['sgst']:,.2f}", cell_center),
                Paragraph(f"{row['igst']:,.2f}", cell_center),
                Paragraph(f"<b>{row['total_amount']:,.2f}</b>", cell_center),
            ])
            table_payload.append(row_cells)

        # Totals row
        cell_total_lbl = ParagraphStyle("TotalLbl", fontSize=7.0, leading=9, alignment=0, fontName="Helvetica-Bold")
        cell_total_val = ParagraphStyle("TotalVal", fontSize=7.0, leading=9, alignment=1, fontName="Helvetica-Bold")
        totals_by_header = {
            "Taxable":     f"{self.df['taxable_value'].sum():,.2f}",
            "CGST":        f"{self.df['cgst'].sum():,.2f}",
            "SGST":        f"{self.df['sgst'].sum():,.2f}",
            "IGST":        f"{self.df['igst'].sum():,.2f}",
            "Gross Total": f"<b>{grand_total:,.2f}</b>",
        }
        bottom_totals = []
        for i, h in enumerate(headers):
            if i == 1:
                bottom_totals.append(Paragraph("GRAND TOTALS", cell_total_lbl))
            elif h in totals_by_header:
                bottom_totals.append(Paragraph(totals_by_header[h], cell_total_val))
            else:
                bottom_totals.append(Paragraph("", cell_center))
        table_payload.append(bottom_totals)

        available_width = doc.width
        col_allocations = {"S.No": 0.035, "Item Description": 0.20, "Batch No": 0.075,
                           "HSN": 0.065, "Expiry": 0.055, "UOM": 0.04, "Qty": 0.045}
        used_fixed = sum(col_allocations.get(h, 0) for h in headers)
        standard_fin = max((1.0 - used_fixed) / max(len([h for h in headers if h not in col_allocations]), 1), 0.055)
        col_widths = [
            col_allocations.get(h, standard_fin) * available_width for h in headers
        ]

        main_grid = Table(table_payload, colWidths=col_widths, repeatRows=1)
        main_grid.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), primary_navy),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F1F5F9")]),
            ("BACKGROUND", (0, -1), (-1, -1), accent_theme),
            ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 1.4, colors.HexColor("#0F172A")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]))
        elements.append(main_grid)
        elements.append(Spacer(1, 15))

        # ── Footer (mirrors private app: soft-copy QR + T&C side by side) ──
        custom_terms = clean_text_value(
            self.seller.get("custom_tc", ""),
            DEFAULT_TERMS_AND_CONDITIONS
        )
        custom_terms = "<br/>".join(xml_escape(line) for line in custom_terms.splitlines())

        footer_html = f"""
        <b>TERMS &amp; CONDITIONS:</b><br/>
        {custom_terms}<br/>
        <br/><b>Bank Details:</b> {pdf_safe(self.seller.get('bank_details', 'N/A'))}<br/>
        <font color="#94A3B8"><i>* {DIGITAL_SIGNATURE_NOTE}</i></font>
        """

        soft_qr, soft_url = self.generate_soft_copy_qr(
            doc_type, doc_number, customer_info.get("name", ""), issue_date_label
        )
        soft_copy_style = ParagraphStyle(
            "SoftCopyMeta", fontSize=7.5, leading=9,
            textColor=secondary_slate, alignment=1, fontName="Helvetica"
        )

        if soft_qr:
            soft_block = [
                soft_qr,
                Paragraph("<b>Scan for soft copy</b>", soft_copy_style),
                Paragraph(f"<font color='#64748B'>{doc_number}</font>", soft_copy_style),
            ]
            footer_table = Table(
                [[soft_block, Paragraph(footer_html, body_text_style)]],
                colWidths=[90, doc.width - 90]
            )
            footer_table.setStyle(TableStyle([
                ("VALIGN",       (0, 0), (-1, -1), "TOP"),
                ("ALIGN",        (0, 0), (0, 0),   "CENTER"),
                ("LEFTPADDING",  (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            elements.append(footer_table)
        else:
            elements.append(Paragraph(footer_html, body_text_style))

        doc.build(elements)
        return buffer.getvalue()


# =============================================================================
# 📡 SELLER PROFILE LOADER  (reads config/firm_profile from Firestore)
# =============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def load_seller_profile() -> dict:
    """Loads the firm profile synced by the private ERP app. Falls back to defaults."""
    fallback_profile = {
        "firm_name":        "SHRI NAMDEV MEDICOSE",
        "address":          "Panchkula, Haryana, India",
        "contact":          "+91 90507-52290 | namdevsushil905@gmail.com",
        "gstin":            "",
        "bank_details":     "Axis Bank | A/C: 917020055729535 | IFSC: UTIB0002078",
        "upi_id":           "+919050752290@axisbank",
        "state":            "Haryana",
        "dl_no":            "14-OB,414-BR",
        "pan_no":           "",
        "jurisdiction":     "Ambala",
        "custom_tc":        DEFAULT_TERMS_AND_CONDITIONS,
        "show_batch_invoice": True,
        "show_uom_invoice":   True,
    }
    try:
        snap = db.collection("config").document("firm_profile").get()
        if snap.exists:
            data = snap.to_dict()
            data.pop("_synced_at", None)
            profile = {**fallback_profile, **data}
            if not clean_text_value(profile.get("custom_tc", ""), ""):
                profile["custom_tc"] = DEFAULT_TERMS_AND_CONDITIONS
            return profile
    except Exception:
        pass
    # Fallback defaults — update these only if Firestore sync hasn't run yet.
    return fallback_profile


# =============================================================================
# 🌐 PUBLIC PORTAL — MAIN UI
# =============================================================================

def render_portal_header():
    seller = load_seller_profile()
    st.markdown(f"""
    <div style="
        background: linear-gradient(135deg, #0F766E 0%, #1E293B 100%);
        padding: 2rem 2.5rem 1.5rem;
        border-radius: 12px;
        margin-bottom: 1.5rem;
    ">
        <h2 style="color:#FFFFFF; margin:0; font-size:1.6rem; letter-spacing:-0.5px;">
            📄 {seller.get('firm_name', 'JB Corporation')}
        </h2>
        <p style="color:#94D5CF; margin:0.3rem 0 0; font-size:0.9rem;">
            Secure Document Download Portal
        </p>
    </div>
    """, unsafe_allow_html=True)


def render_not_found(doc_id: str):
    st.error(f"Document **{doc_id}** was not found in the system records.")
    st.info("If you believe this is an error, please contact the pharmacy or billing desk directly.")


def render_invalid_token():
    st.error("🔒 This document link is invalid or has expired.")
    st.warning("Each document link is uniquely secured. Please request a fresh link from the issuer.")


def fetch_sales_rows(doc_id: str) -> list[dict]:
    try:
        stream = db.collection("sales").where("sale_id", "==", doc_id).stream()
        return [d.to_dict() for d in stream]
    except Exception as e:
        st.error(f"Database fetch failed: {e}")
        return []


def fetch_inventory_df() -> pd.DataFrame:
    try:
        rows = [d.to_dict() for d in db.collection("inventory").stream()]
        return normalize_inventory_dataframe(pd.DataFrame(rows)) if rows else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def validate_token(rows: list[dict], doc_id: str, supplied_token: str) -> bool:
    """Two-path validation: stored token (primary) or computed fallback."""
    first = rows[0]

    # Path 1: stored token (written by private app since the Firestore sync update)
    stored_token = clean_text_value(first.get("public_share_token", ""), "")
    if stored_token:
        return hmac.compare_digest(stored_token, supplied_token)

    # Path 2: deterministic fallback for older records that pre-date token storage
    computed = build_public_share_token(
        doc_id,
        first.get("customer", ""),
        first.get("issue_date")
    )
    return hmac.compare_digest(computed, supplied_token)


def build_cart(rows: list[dict], inv_df: pd.DataFrame) -> list[dict]:
    cart = []
    for r in rows:
        cart.append({
            "item_name":        r.get("item_name", "N/A"),
            "batch_no":         r.get("batch_no", "N/A"),
            "expiry_date":      format_expiry_mmyy(r.get("expiry_date", "N/A")),
            "uom":              r.get("uom", "Units"),
            "hsn_code":         resolve_cart_line_hsn(r, inv_df),
            "qty":              coerce_float_value(r.get("qty", 1.0), 1.0),
            "mrp":              coerce_float_value(r.get("mrp", 0.0), 0.0),
            "discount_pct":     coerce_float_value(r.get("discount_pct", 0.0), 0.0),
            "gst_rate":         coerce_float_value(r.get("gst_rate", 12.0), 12.0),
            "inventory_doc_id": r.get("inventory_doc_id", ""),
        })
    return cart


def build_customer_info(first_row: dict) -> dict:
    return {
        "name":         first_row.get("customer", "Counter Retail Customer"),
        "contact":      first_row.get("customer_contact", "N/A"),
        "address":      first_row.get("customer_address", "N/A"),
        "gstin":        first_row.get("customer_gstin", "N/A"),
        "payment_mode": first_row.get("payment_mode", "Cash"),
    }


# --- MAIN ENTRY POINT ---

render_portal_header()

qp = st.query_params

if "share_doc" not in qp or not str(qp.get("share_doc", "")).strip():
    # Landing page — no document requested
    st.markdown("### Welcome to the Secure Document Portal")
    st.markdown(
        "This portal lets you download invoices and documents shared with you via QR code or link. "
        "Please use the link provided by the billing desk — it contains a secure one-time token."
    )
    st.info("📱 Scan the QR code on your invoice to access your document instantly.")
    st.stop()

# --- DOCUMENT DOWNLOAD FLOW ---
doc_id        = str(qp.get("share_doc", "")).strip().upper()
dtype         = str(qp.get("dtype", "Tax Invoice")).strip()
supplied_token = str(qp.get("token", "")).strip()

st.markdown(f"""
<div style="
    background:#F0FDF4; border:1px solid #86EFAC; border-radius:8px;
    padding:1rem 1.2rem; margin-bottom:1rem;
">
    <p style="margin:0; color:#166534; font-size:0.95rem;">
        🔍 Fetching document <b>{doc_id}</b> ({dtype}) …
    </p>
</div>
""", unsafe_allow_html=True)

with st.spinner("Connecting to secure document store…"):
    rows = fetch_sales_rows(doc_id)

if not rows:
    render_not_found(doc_id)
    st.stop()

if not validate_token(rows, doc_id, supplied_token):
    render_invalid_token()
    st.stop()

# Token valid — build and serve the PDF
with st.spinner("Generating PDF…"):
    inv_df        = fetch_inventory_df()
    cart          = build_cart(rows, inv_df)
    first_row     = rows[0]
    customer_info = build_customer_info(first_row)
    customer_state = first_row.get("customer_state", "Haryana")
    issue_date     = coerce_document_issue_date(first_row.get("issue_date"))

    seller = load_seller_profile()
    engine = GSTBillingEngine(cart, seller, customer_state)
    pdf_bytes = engine.generate_pdf(
        doc_type=dtype,
        doc_number=doc_id,
        customer_info=customer_info,
        issue_date=issue_date,
    )

# --- SUCCESS UI ---
st.success(f"✅ Document **{doc_id}** is ready.")

col_l, col_c, col_r = st.columns([1, 2, 1])
with col_c:
    st.download_button(
        label=f"📥 Download {doc_id}.pdf",
        data=pdf_bytes,
        file_name=f"{doc_id}.pdf",
        mime="application/pdf",
        use_container_width=True,
        type="primary",
    )
    st.caption(
        f"Issued to: **{customer_info['name']}** · "
        f"Date: **{issue_date.strftime('%d-%m-%Y')}** · "
        "DIGITALLY GENERATED DOCUMENT — NO PHYSICAL SIGNATURE REQUIRED."
    )

st.divider()
st.markdown(
    "<p style='text-align:center; color:#94A3B8; font-size:0.8rem;'>"
    "This document was issued by "
    f"{seller.get('firm_name', 'JB Corporation')} · "
    f"{seller.get('address', '')} · "
    f"GSTIN: {seller.get('gstin', 'N/A')}"
    "</p>",
    unsafe_allow_html=True,
)
