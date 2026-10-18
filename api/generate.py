"""
Vercel serverless function — draws the PDF and nothing else.

The browser does the parsing, column detection and aggregation, then POSTs a
small JSON summary here. That split is what makes this work on serverless:
no state is kept between requests, and the payload stays a few KB instead of
the whole spreadsheet.

Expects JSON:
{
  "title": "September Review",
  "client": "Al Noor Properties",
  "color": "#15614A",
  "currency": "AED",
  "template": "financial" | "marketing" | "operations",
  "period_label": "September 2026",
  "label_name": "agent",
  "logo": "data:image/png;base64,...."          (optional)
  "summary": {
    "total": 53330000.0,
    "count": 240,
    "average": 222208.3,
    "change": 1.8,                               (or null)
    "groups": 5,
    "best_period": "2026-06",                    (or null)
    "periods": [["2026-06", 14680000.0], ...],
    "labels": [["Leila K.", 50, 12330000.0], ...]
  }
}

Responds with application/pdf.
"""

import base64
import binascii
import io
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as pdfcanvas

INK = colors.HexColor("#15191F")
MUTED = colors.HexColor("#596273")
FAINT = colors.HexColor("#A4ADBC")
RULE = colors.HexColor("#E2E6EB")
PANEL = colors.HexColor("#F6F7F9")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm

MAX_BODY = 4_000_000  # stay under Vercel's 4.5 MB request cap

TEMPLATES = {
    "financial": {
        "chart_title": "Revenue by period",
        "table_title": "Breakdown by {label}",
        "mix": False,
        "kpis": ["total", "records", "average", "change"],
    },
    "marketing": {
        "chart_title": "Spend by period",
        "table_title": "Channel detail",
        "mix": True,
        "mix_title": "Share of spend by {label}",
        "kpis": ["total", "groups", "average_group", "change"],
    },
    "operations": {
        "chart_title": "Volume by period",
        "table_title": "Detail by {label}",
        "mix": True,
        "mix_title": "Distribution across {label}",
        "kpis": ["records", "groups", "busiest", "change"],
    },
}


# ---------------------------------------------------------------- helpers

def safe_color(value, fallback="#15614A"):
    try:
        return colors.HexColor(value)
    except Exception:
        return colors.HexColor(fallback)


def fmt_money(n, currency):
    try:
        n = float(n)
    except (TypeError, ValueError):
        n = 0.0
    if abs(n) >= 1_000_000:
        body = f"{n / 1_000_000:,.2f}M"
    elif abs(n) >= 10_000:
        body = f"{n / 1_000:,.1f}K"
    else:
        body = f"{n:,.2f}"
    return f"{currency} {body}".strip()


def tint(hex_color, amount):
    c = safe_color(hex_color)
    return colors.Color(c.red + (1 - c.red) * amount,
                        c.green + (1 - c.green) * amount,
                        c.blue + (1 - c.blue) * amount)


def truncate(text, font, size, max_w):
    text = str(text)
    if pdfmetrics.stringWidth(text, font, size) <= max_w:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > max_w:
        text = text[:-1]
    return text + "…"


def period_label_of(key):
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%b %y")
    except (ValueError, TypeError):
        return str(key)


# ---------------------------------------------------------------- drawing

def draw_header(c, cfg, y):
    accent = safe_color(cfg["color"])
    drawn = False

    if cfg.get("logo"):
        try:
            raw = cfg["logo"].split(",", 1)[-1]
            img = ImageReader(io.BytesIO(base64.b64decode(raw)))
            iw, ih = img.getSize()
            h = 13 * mm
            w = h * (iw / ih)
            max_w = 58 * mm
            if w > max_w:
                w, h = max_w, max_w * (ih / iw)
            c.drawImage(img, MARGIN, y - h, width=w, height=h,
                        mask="auto", preserveAspectRatio=True)
            drawn = True
        except (binascii.Error, ValueError, OSError):
            drawn = False

    if not drawn:
        c.setFillColor(accent)
        c.roundRect(MARGIN, y - 10 * mm, 10 * mm, 10 * mm, 2 * mm, stroke=0, fill=1)

    c.setFont("Helvetica", 8)
    c.setFillColor(MUTED)
    c.drawRightString(PAGE_W - MARGIN, y - 6 * mm,
                      str(cfg.get("period_label", "")).upper())

    y -= 16 * mm
    c.setStrokeColor(accent)
    c.setLineWidth(2.2)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    return y - 12 * mm


def draw_title(c, cfg, y):
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 21)
    c.drawString(MARGIN, y, truncate(cfg["title"], "Helvetica-Bold", 21,
                                     PAGE_W - 2 * MARGIN))
    y -= 7 * mm
    c.setFont("Helvetica", 9.5)
    c.setFillColor(MUTED)
    c.drawString(MARGIN, y,
                 f"Prepared for {cfg['client']}  ·  "
                 f"Generated {datetime.now():%d %B %Y}")
    return y - 12 * mm


def draw_kpis(c, cfg, s, y, label_name):
    accent = safe_color(cfg["color"])
    cur = cfg["currency"]
    tpl = TEMPLATES[cfg["template"]]

    change = s.get("change")
    change_txt = f"{change:+.1f}%" if isinstance(change, (int, float)) else "—"

    busiest = "—"
    if s.get("best_period"):
        try:
            busiest = datetime.strptime(s["best_period"], "%Y-%m").strftime("%b %Y")
        except (ValueError, TypeError):
            busiest = str(s["best_period"])

    groups = int(s.get("groups") or 0)
    avg_group = (s.get("total", 0.0) / groups) if groups else 0.0

    catalogue = {
        "total": ("TOTAL", fmt_money(s.get("total", 0), cur), "across all rows"),
        "records": ("RECORDS", f"{int(s.get('count', 0)):,}", "rows counted"),
        "average": ("AVERAGE", fmt_money(s.get("average", 0), cur), "per record"),
        "change": ("VS PREVIOUS", change_txt, "period on period"),
        "groups": (label_name.upper()[:14] or "GROUPS", f"{groups:,}", "distinct values"),
        "average_group": ("AVERAGE", fmt_money(avg_group, cur), f"per {label_name}"),
        "busiest": ("BUSIEST", busiest, "highest period"),
    }
    tiles = [catalogue[k] for k in tpl["kpis"] if k in catalogue]

    gap = 4 * mm
    tw = (PAGE_W - 2 * MARGIN - gap * (len(tiles) - 1)) / len(tiles)
    th = 22 * mm

    for i, (label, value, note) in enumerate(tiles):
        x = MARGIN + i * (tw + gap)
        c.setFillColor(PANEL)
        c.roundRect(x, y - th, tw, th, 2 * mm, stroke=0, fill=1)
        c.setFillColor(FAINT)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(x + 4 * mm, y - 6.5 * mm, label)
        c.setFillColor(accent if i == 0 else INK)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(x + 4 * mm, y - 13.5 * mm,
                     truncate(value, "Helvetica-Bold", 13, tw - 8 * mm))
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 6.5)
        c.drawString(x + 4 * mm, y - 18.5 * mm, note)

    return y - th - 12 * mm


def draw_chart(c, cfg, s, y):
    items = [(k, float(v)) for k, v in (s.get("periods") or [])][-12:]
    if not items:
        return y

    accent = safe_color(cfg["color"])
    soft = tint(cfg["color"], 0.62)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, y, TEMPLATES[cfg["template"]]["chart_title"])
    y -= 8 * mm

    chart_h = 42 * mm
    base = y - chart_h
    slot = (PAGE_W - 2 * MARGIN) / len(items)
    bar_w = min(slot * 0.58, 16 * mm)
    peak = max(abs(v) for _, v in items) or 1.0

    for i, (key, val) in enumerate(items):
        cx = MARGIN + slot * i + slot / 2
        h = (abs(val) / peak) * (chart_h - 8 * mm)
        c.setFillColor(accent if i == len(items) - 1 else soft)
        c.roundRect(cx - bar_w / 2, base, bar_w, max(h, 0.6), 1 * mm, stroke=0, fill=1)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 6)
        c.drawCentredString(cx, base + h + 2 * mm, fmt_money(val, "").strip())
        c.setFillColor(FAINT)
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(cx, base - 4.5 * mm, period_label_of(key))

    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(MARGIN, base, PAGE_W - MARGIN, base)
    return base - 14 * mm


def draw_mix(c, cfg, rows, y, label_name):
    if not rows:
        return y

    tpl = TEMPLATES[cfg["template"]]
    accent = safe_color(cfg["color"])
    soft = tint(cfg["color"], 0.6)
    cur = cfg["currency"]

    total = sum(abs(r[2]) for r in rows) or 1.0
    shown = rows[:6]
    peak = max(abs(r[2]) for r in shown) or 1.0

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, y, tpl.get("mix_title", "Share of total").format(label=label_name))
    y -= 9 * mm

    name_w, val_w = 42 * mm, 30 * mm
    track_x = MARGIN + name_w
    track_w = PAGE_W - MARGIN - val_w - track_x - 4 * mm

    for i, (name, _n, total_v) in enumerate(shown):
        share = abs(total_v) / total
        c.setFillColor(INK)
        c.setFont("Helvetica", 8.5)
        c.drawString(MARGIN, y - 4.6 * mm,
                     truncate(name, "Helvetica", 8.5, name_w - 4 * mm))
        c.setFillColor(PANEL)
        c.roundRect(track_x, y - 5.6 * mm, track_w, 3.4 * mm, 1.2 * mm, stroke=0, fill=1)
        c.setFillColor(accent if i == 0 else soft)
        c.roundRect(track_x, y - 5.6 * mm,
                    max(track_w * (abs(total_v) / peak), 1.2), 3.4 * mm,
                    1.2 * mm, stroke=0, fill=1)
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8)
        c.drawRightString(PAGE_W - MARGIN, y - 4.6 * mm,
                          f"{share * 100:.1f}%   {fmt_money(total_v, cur)}")
        y -= 7.5 * mm

    if len(rows) > len(shown):
        c.setFillColor(FAINT)
        c.setFont("Helvetica-Oblique", 7.5)
        c.drawString(MARGIN, y - 3 * mm,
                     f"and {len(rows) - len(shown)} more — full list in the table below")
        y -= 6 * mm

    return y - 8 * mm


def draw_table(c, cfg, rows, y, label_name, title=True):
    if not rows:
        return y, []

    cur = cfg["currency"]
    if title:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(MARGIN, y,
                     TEMPLATES[cfg["template"]]["table_title"].format(label=label_name))
        y -= 8 * mm

    col_n = PAGE_W - MARGIN - 62 * mm
    col_v = PAGE_W - MARGIN

    c.setFillColor(FAINT)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(MARGIN, y, label_name.upper())
    c.drawString(col_n, y, "COUNT")
    c.drawRightString(col_v, y, "VALUE")
    y -= 3 * mm
    c.setStrokeColor(RULE)
    c.setLineWidth(0.7)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    y -= 6 * mm

    drawn = 0
    for name, n, total_v in rows:
        if y < MARGIN + 18 * mm:
            break
        c.setFillColor(INK)
        c.setFont("Helvetica", 9)
        c.drawString(MARGIN, y, truncate(name, "Helvetica", 9, col_n - MARGIN - 6 * mm))
        c.setFillColor(MUTED)
        c.drawString(col_n, y, f"{int(n):,}")
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(col_v, y, fmt_money(total_v, cur))
        y -= 4 * mm
        c.setStrokeColor(colors.HexColor("#EDF0F4"))
        c.setLineWidth(0.5)
        c.line(MARGIN, y, PAGE_W - MARGIN, y)
        y -= 5.5 * mm
        drawn += 1

    return y - 4 * mm, rows[drawn:]


def draw_footer(c, cfg, page_no, total_pages=None):
    c.setFillColor(FAINT)
    c.setFont("Helvetica", 7)
    tail = f"page {page_no} of {total_pages}" if total_pages else f"page {page_no}"
    c.drawCentredString(PAGE_W / 2, MARGIN - 4 * mm, f"{cfg['title']}  ·  {tail}")


def build_pdf(payload):
    cfg = {
        "title": str(payload.get("title") or "Report")[:120],
        "client": str(payload.get("client") or "[CLIENT NAME]")[:120],
        "color": payload.get("color") or "#15614A",
        "currency": str(payload.get("currency") or "")[:8],
        "template": payload.get("template") if payload.get("template") in TEMPLATES else "financial",
        "period_label": str(payload.get("period_label") or "")[:40],
        "logo": payload.get("logo"),
    }
    s = payload.get("summary") or {}
    label_name = str(payload.get("label_name") or "category").replace("_", " ")[:40]
    rows = [(str(r[0])[:80], r[1], float(r[2])) for r in (s.get("labels") or [])]

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    c.setTitle(cfg["title"])
    c.setAuthor(cfg["client"])

    y = PAGE_H - MARGIN
    y = draw_header(c, cfg, y)
    y = draw_title(c, cfg, y)
    y = draw_kpis(c, cfg, s, y, label_name)
    y = draw_chart(c, cfg, s, y)
    if TEMPLATES[cfg["template"]].get("mix"):
        y = draw_mix(c, cfg, rows, y, label_name)
    y, leftover = draw_table(c, cfg, rows, y, label_name)

    page_no = 1
    while leftover and page_no < 40:
        draw_footer(c, cfg, page_no)
        c.showPage()
        page_no += 1
        y = PAGE_H - MARGIN
        y = draw_header(c, cfg, y)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(MARGIN, y,
                     TEMPLATES[cfg["template"]]["table_title"].format(label=label_name)
                     + " (continued)")
        y -= 8 * mm
        y, leftover = draw_table(c, cfg, leftover, y, label_name, title=False)

    draw_footer(c, cfg, page_no, page_no if page_no > 1 else None)
    c.showPage()
    c.save()
    return buf.getvalue(), cfg["title"]


# ---------------------------------------------------------------- handler

class handler(BaseHTTPRequestHandler):

    def _fail(self, code, message):
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "Bad Content-Length.")

        if length <= 0:
            return self._fail(400, "Empty request.")
        if length > MAX_BODY:
            return self._fail(413, "That summary is too large. Reduce the number "
                                   "of categories before generating.")

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._fail(400, "Body was not valid JSON.")

        if not isinstance(payload, dict) or not isinstance(payload.get("summary"), dict):
            return self._fail(400, "Expected an object with a 'summary' key.")

        try:
            pdf, title = build_pdf(payload)
        except Exception as exc:                      # noqa: BLE001 — report, don't crash
            return self._fail(500, f"Could not draw the report: {exc}")

        safe = "".join(ch for ch in title if ch.isalnum() or ch in " -_").strip() or "report"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{safe}.pdf"')
        self.send_header("Content-Length", str(len(pdf)))
        self.end_headers()
        self.wfile.write(pdf)

    def do_GET(self):
        self._fail(405, "POST a JSON summary to this endpoint.")
