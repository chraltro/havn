"""Rendering scheduled reports without a browser.

Every report gets an HTML body (KPI tiles plus a table of each widget's
rows, inline CSS only so mail clients render it), a plain-text alternative
and one CSV per widget. Images degrade gracefully:

* With matplotlib installed (``pip install "havn[reports]"``), chart widgets
  are drawn server-side: one PNG per chart (embedded in the email) and a
  composite PNG of the dashboard, and the PDF has a page per chart.
* Without it, the PDF is still produced by the small writer at the bottom
  of this module (text only: KPI values and tables), and PNG output is
  skipped with a note in the report.

All values from the warehouse are escaped before they reach HTML.
"""

from __future__ import annotations

import csv
import datetime as _dt
import html
import io
import logging
import math
from typing import Any

logger = logging.getLogger("havn.engine.report_render")

# Categorical slots, light surface (validated reference palette; reports are
# read on white mail and paper backgrounds). Assigned in order, never cycled:
# series past the eighth fold into the last slot's table instead.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#8a8984"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

HTML_TABLE_ROWS = 25
PDF_TABLE_ROWS = 40

_LINE_TYPES = {"line", "area", "stacked_area", "sparkline"}
_BAR_TYPES = {"bar", "column", "stacked_bar", "grouped_bar", "horizontal_bar", "histogram", "waterfall", "combo"}
_PIE_TYPES = {"pie", "donut"}
_SCATTER_TYPES = {"scatter", "bubble"}


def charts_available(setting: str = "auto") -> bool:
    """True when server-side charts can be drawn (matplotlib importable and not switched off)."""
    if setting == "off":
        return False
    return _matplotlib_usable()


_MPL_USABLE: bool | None = None


def _matplotlib_usable() -> bool:
    """Import the pieces rendering needs; a half-installed matplotlib counts as absent."""
    global _MPL_USABLE
    if _MPL_USABLE is None:
        try:
            from matplotlib.backends import backend_pdf  # noqa: F401
            from matplotlib.figure import Figure

            # A trial render: a broken install can import and still fail on first use.
            fig = Figure(figsize=(1, 1))
            fig.add_subplot(1, 1, 1).bar([0], [1])
            fig.savefig(io.BytesIO(), format="png")
        except Exception as e:
            logger.info("Server-side charts unavailable (%s); reports use tables and text-only PDF", e)
            _MPL_USABLE = False
        else:
            _MPL_USABLE = True
    return _MPL_USABLE


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def format_number(v: Any) -> str:
    """Compact display of a headline number (1.2K, 3.4M), like the dashboard KPI tile."""
    n = _num(v)
    if n is None:
        return "—" if v is None else str(v)
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.1f}B"
    if a >= 1e6:
        return f"{n / 1e6:.1f}M"
    if a >= 1e4:
        return f"{n / 1e3:.1f}K"
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}"


def format_cell(v: Any) -> str:
    if v is None:
        return ""
    n = _num(v) if not isinstance(v, str) else None
    if n is not None and not float(n).is_integer():
        return f"{n:,.4g}" if abs(n) < 1 else f"{n:,.2f}"
    return str(v)


def kpi_display(widget: dict) -> dict | None:
    """Headline value of a KPI widget: {label, value, display, comparison, delta_pct}."""
    columns, rows = widget.get("columns") or [], widget.get("rows") or []
    if not columns or not rows:
        return None
    cfg = widget.get("config") or {}
    vcol = cfg.get("value_column") if cfg.get("value_column") in columns else columns[0]
    value = rows[0][columns.index(vcol)]
    ccol = cfg.get("comparison_column") if cfg.get("comparison_column") in columns else None
    comparison = rows[0][columns.index(ccol)] if ccol else None
    delta = None
    nv, nc = _num(value), _num(comparison)
    if nv is not None and nc not in (None, 0):
        delta = (nv - nc) / abs(nc) * 100
    return {
        "label": widget.get("title") or vcol,
        "column": vcol,
        "value": value,
        "display": f"{cfg.get('prefix', '')}{format_number(value)}{cfg.get('suffix', '')}",
        "comparison_column": ccol,
        "comparison": comparison,
        "delta_pct": delta,
    }


def widget_csv(widget: dict) -> bytes:
    """CSV of a widget's rows (UTF-8 with BOM so spreadsheet apps detect the encoding)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(widget.get("columns") or [])
    for row in widget.get("rows") or []:
        w.writerow(["" if v is None else v for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def safe_filename(name: str, default: str = "widget") -> str:
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (name or ""))
    keep = "_".join(keep.split())[:60].strip("_")
    return keep or default


# ---------------------------------------------------------------------------
# HTML and text
# ---------------------------------------------------------------------------

_e = html.escape


def _fmt_time(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        return _dt.datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def render_html(data: dict, image_cids: dict[str, str] | None = None) -> str:
    """The email body: header, KPI tiles, then each widget (chart image or table)."""
    image_cids = image_cids or {}
    out: list[str] = []
    out.append(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:#f3f2ef;color:{INK};'
        "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif\">"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f3f2ef"><tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:680px;background:{SURFACE};border:1px solid {GRID};border-radius:8px">'
    )
    # Header
    out.append(f'<tr><td style="padding:24px 24px 8px">')
    out.append(f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{INK_MUTED}">havn report</div>')
    out.append(f'<div style="font-size:22px;font-weight:600;margin-top:4px">{_e(data.get("title") or "")}</div>')
    if data.get("dashboard_name") and data.get("dashboard_name") != data.get("title"):
        out.append(f'<div style="font-size:14px;color:{INK_SECONDARY};margin-top:2px">{_e(data["dashboard_name"])}</div>')
    if data.get("message"):
        out.append(f'<p style="font-size:14px;line-height:1.5;margin:12px 0 0">{_e(data["message"])}</p>')
    meta = [f"Generated {_fmt_time(data.get('generated_at'))}"]
    fresh = (data.get("freshness") or {}).get("as_of")
    if fresh:
        meta.append(f"data as of {_fmt_time(fresh)}")
    out.append(f'<div style="font-size:12px;color:{INK_MUTED};margin-top:10px">{_e(" · ".join(meta))}</div>')
    cond = data.get("condition")
    if cond and cond.get("description"):
        out.append(
            f'<div style="font-size:13px;margin-top:12px;padding:8px 12px;border-left:3px solid {SERIES_COLORS[1]};'
            f'background:#fbf1ec">Sent because {_e(cond["description"])}</div>'
        )
    out.append("</td></tr>")

    # KPI tiles
    kpis = [w["kpi"] for w in data.get("widgets", []) if w.get("kpi")]
    if kpis:
        out.append('<tr><td style="padding:8px 18px">')
        out.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="6"><tr>')
        for i, k in enumerate(kpis):
            if i and i % 3 == 0:
                out.append("</tr><tr>")
            delta = ""
            if k.get("delta_pct") is not None:
                arrow = "▲" if k["delta_pct"] > 0 else ("▼" if k["delta_pct"] < 0 else "–")
                delta = (
                    f'<div style="font-size:12px;color:{INK_SECONDARY};margin-top:2px">'
                    f'{arrow} {abs(k["delta_pct"]):.1f}% vs {_e(str(k.get("comparison_column") or ""))}</div>'
                )
            out.append(
                f'<td valign="top" style="border:1px solid {GRID};border-radius:6px;padding:12px;width:33%">'
                f'<div style="font-size:12px;color:{INK_SECONDARY}">{_e(str(k["label"]))}</div>'
                f'<div style="font-size:26px;font-weight:600;margin-top:4px">{_e(k["display"])}</div>'
                f"{delta}</td>"
            )
        out.append("</tr></table></td></tr>")

    # Widgets
    for w in data.get("widgets", []):
        if w.get("kpi") or w.get("widget_type") in ("divider", "image"):
            continue
        out.append(f'<tr><td style="padding:16px 24px 4px">')
        out.append(f'<div style="font-size:15px;font-weight:600">{_e(w.get("title") or "Untitled")}</div>')
        if w.get("widget_type") == "text":
            text = (w.get("config") or {}).get("content") or ""
            out.append(f'<p style="font-size:14px;line-height:1.5;white-space:pre-wrap;margin:6px 0">{_e(text)}</p>')
        elif w.get("error"):
            out.append(f'<div style="font-size:13px;color:#b42318;margin-top:6px">Could not load: {_e(w["error"])}</div>')
        elif w["id"] in image_cids:
            out.append(
                f'<img src="cid:{_e(image_cids[w["id"]])}" alt="{_e(w.get("title") or "chart")}" '
                'width="632" style="width:100%;max-width:632px;height:auto;margin-top:8px;display:block">'
            )
        else:
            out.append(_html_table(w))
        out.append("</td></tr>")

    if data.get("notes"):
        out.append(f'<tr><td style="padding:12px 24px;font-size:12px;color:{INK_MUTED}">')
        out.append("<br>".join(_e(n) for n in data["notes"]))
        out.append("</td></tr>")
    footer = []
    if data.get("link_url"):
        footer.append(
            f'<a href="{_e(data["link_url"], quote=True)}" style="color:{SERIES_COLORS[0]}">Open the dashboard</a>'
        )
    if data.get("attachments_note"):
        footer.append(_e(data["attachments_note"]))
    out.append(
        f'<tr><td style="padding:16px 24px 24px;font-size:12px;color:{INK_MUTED};border-top:1px solid {GRID}">'
        f'{" · ".join(footer) or "Sent by havn"}</td></tr>'
    )
    out.append("</table></td></tr></table></body></html>")
    return "".join(out)


def _html_table(w: dict) -> str:
    columns, rows = w.get("columns") or [], w.get("rows") or []
    if not rows:
        return f'<div style="font-size:13px;color:{INK_MUTED};margin-top:6px">No rows</div>'
    numeric = [all(_num(r[i]) is not None or r[i] is None for r in rows[:50]) for i in range(len(columns))]
    parts = [
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;font-size:13px;margin-top:8px">',
        "<tr>",
    ]
    for i, c in enumerate(columns):
        align = "right" if numeric[i] else "left"
        parts.append(
            f'<th align="{align}" style="padding:6px 8px;border-bottom:1px solid {INK_MUTED};'
            f'color:{INK_SECONDARY};font-weight:600">{_e(str(c))}</th>'
        )
    parts.append("</tr>")
    for r in rows[:HTML_TABLE_ROWS]:
        parts.append("<tr>")
        for i, v in enumerate(r):
            align = "right" if numeric[i] else "left"
            parts.append(
                f'<td align="{align}" style="padding:5px 8px;border-bottom:1px solid {GRID}">{_e(format_cell(v))}</td>'
            )
        parts.append("</tr>")
    parts.append("</table>")
    total = w.get("row_count", len(rows))
    if total > HTML_TABLE_ROWS:
        parts.append(
            f'<div style="font-size:12px;color:{INK_MUTED};margin-top:4px">'
            f"Showing {HTML_TABLE_ROWS} of {total}{'+' if w.get('truncated') else ''} rows (all rows in the CSV attachment)</div>"
        )
    return "".join(parts)


def render_text(data: dict) -> str:
    """Plain-text alternative (and Slack/CLI summary)."""
    lines = [data.get("title") or "havn report"]
    if data.get("message"):
        lines += ["", data["message"]]
    fresh = (data.get("freshness") or {}).get("as_of")
    lines.append(f"Generated {_fmt_time(data.get('generated_at'))}" + (f", data as of {_fmt_time(fresh)}" if fresh else ""))
    cond = data.get("condition")
    if cond and cond.get("description"):
        lines.append(f"Sent because {cond['description']}")
    lines.append("")
    for w in data.get("widgets", []):
        if w.get("kpi"):
            k = w["kpi"]
            delta = f" ({k['delta_pct']:+.1f}%)" if k.get("delta_pct") is not None else ""
            lines.append(f"{k['label']}: {k['display']}{delta}")
    for w in data.get("widgets", []):
        if w.get("kpi") or w.get("widget_type") in ("divider", "image", "text"):
            continue
        lines.append("")
        lines.append(w.get("title") or "Untitled")
        if w.get("error"):
            lines.append(f"  could not load: {w['error']}")
            continue
        cols = w.get("columns") or []
        lines.append("  " + " | ".join(str(c) for c in cols))
        for r in (w.get("rows") or [])[:10]:
            lines.append("  " + " | ".join(format_cell(v) for v in r))
        if w.get("row_count", 0) > 10:
            lines.append(f"  … {w['row_count']} rows in total")
    if data.get("link_url"):
        lines += ["", data["link_url"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Charts (matplotlib, optional)
# ---------------------------------------------------------------------------


def _chart_series(w: dict) -> tuple[list[str], list[tuple[str, list[float]]]] | None:
    """(x labels, [(series name, values)]) from a widget result: first column is x, numeric columns are series."""
    columns, rows = w.get("columns") or [], w.get("rows") or []
    if len(columns) < 2 or not rows:
        return None
    numeric_idx = [i for i in range(1, len(columns)) if any(_num(r[i]) is not None for r in rows)]
    if not numeric_idx:
        return None
    labels = [format_cell(r[0]) for r in rows]
    series = [(str(columns[i]), [(_num(r[i]) or 0.0) for r in rows]) for i in numeric_idx[: len(SERIES_COLORS)]]
    return labels, series


def is_chartable(w: dict) -> bool:
    return w.get("widget_type") == "chart" and not w.get("error") and _chart_series(w) is not None


def _style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SECONDARY, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _draw_chart(ax, w: dict) -> bool:
    parsed = _chart_series(w)
    if parsed is None:
        return False
    labels, series = parsed
    ctype = (w.get("chart_type") or "bar").lower()
    max_points = 60
    if len(labels) > max_points and ctype not in _LINE_TYPES:
        labels = labels[:max_points]
        series = [(n, v[:max_points]) for n, v in series]
    x = list(range(len(labels)))
    if ctype in _PIE_TYPES:
        name, values = series[0]
        vals = [max(v, 0.0) for v in values[: len(SERIES_COLORS)]]
        if not any(vals):
            return False
        ax.pie(
            vals, labels=labels[: len(vals)], colors=SERIES_COLORS[: len(vals)],
            wedgeprops={"linewidth": 2, "edgecolor": SURFACE, "width": 0.45 if ctype == "donut" else 1},
            textprops={"fontsize": 8, "color": INK_SECONDARY},
        )
        ax.set_aspect("equal")
        return True
    _style_axes(ax)
    if ctype in _LINE_TYPES:
        for i, (name, values) in enumerate(series):
            ax.plot(x, values, color=SERIES_COLORS[i], linewidth=2, label=name)
            if ctype in ("area", "stacked_area"):
                ax.fill_between(x, values, color=SERIES_COLORS[i], alpha=0.12)
    elif ctype in _SCATTER_TYPES:
        if len(series) >= 1 and all(_num(l) is not None for l in labels):
            xs = [float(_num(l)) for l in labels]
            for i, (name, values) in enumerate(series[:3]):
                ax.scatter(xs, values, s=24, color=SERIES_COLORS[i], edgecolors=SURFACE, linewidths=1, label=name)
        else:
            return False
    else:
        horizontal = ctype == "horizontal_bar"
        n = len(series)
        width = 0.8 / n
        for i, (name, values) in enumerate(series):
            pos = [xi - 0.4 + width * (i + 0.5) for xi in x]
            if horizontal:
                ax.barh(pos, values, height=width * 0.92, color=SERIES_COLORS[i], label=name)
            else:
                ax.bar(pos, values, width=width * 0.92, color=SERIES_COLORS[i], label=name)
    if ctype != "horizontal_bar":
        step = max(1, len(labels) // 12)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([l[:14] for l in labels[::step]], rotation=30 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")
    else:
        ax.set_yticks(x)
        ax.set_yticklabels([l[:18] for l in labels])
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.grid(axis="y", visible=False)
    if len(series) >= 2:
        ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, loc="upper left", bbox_to_anchor=(0, 1.12), ncol=min(4, len(series)))
    return True


def _figure(width_in: float, height_in: float):
    # A bare Figure renders through its own Agg canvas: no pyplot, no GUI
    # backend and no global state shared with other threads.
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width_in, height_in), dpi=110, facecolor=SURFACE)
    return fig


def render_widget_png(w: dict) -> bytes | None:
    """One chart widget as a PNG, or None when it cannot be drawn."""
    if not is_chartable(w):
        return None
    try:
        fig = _figure(6.4, 3.2)
        ax = fig.add_subplot(1, 1, 1)
        if not _draw_chart(ax, w):
            return None
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=SURFACE)
        return buf.getvalue()
    except Exception:
        logger.warning("Chart rendering failed for widget %s", w.get("id"), exc_info=True)
        return None


def render_dashboard_png(data: dict) -> bytes | None:
    """A single image of the report: KPI row on top, then charts two per row."""
    charts = [w for w in data.get("widgets", []) if is_chartable(w)]
    kpis = [w["kpi"] for w in data.get("widgets", []) if w.get("kpi")]
    if not charts and not kpis:
        return None
    try:
        rows_of_charts = math.ceil(len(charts) / 2)
        kpi_h = 1.1 if kpis else 0
        height = 0.9 + kpi_h + rows_of_charts * 3.0
        fig = _figure(10, height)
        fig.text(0.03, 1 - 0.35 / height, data.get("title") or "", fontsize=15, fontweight="bold", color=INK, va="top")
        sub = f"Generated {_fmt_time(data.get('generated_at'))}"
        if (data.get("freshness") or {}).get("as_of"):
            sub += f" · data as of {_fmt_time(data['freshness']['as_of'])}"
        fig.text(0.03, 1 - 0.68 / height, sub, fontsize=8, color=INK_MUTED, va="top")
        top = 0.9
        if kpis:
            n = min(len(kpis), 4)
            for i, k in enumerate(kpis[:4]):
                xk = 0.03 + i * (0.94 / n)
                fig.text(xk, 1 - (top + 0.25) / height, str(k["label"])[:28], fontsize=8, color=INK_SECONDARY, va="top")
                fig.text(xk, 1 - (top + 0.5) / height, k["display"], fontsize=18, fontweight="bold", color=INK, va="top")
            top += kpi_h
        for idx, w in enumerate(charts):
            r, c = divmod(idx, 2)
            left = 0.07 + c * 0.5
            bottom_in = height - (top + (r + 1) * 3.0) + 0.45
            ax = fig.add_axes([left, bottom_in / height, 0.4, 2.1 / height])
            _draw_chart(ax, w)
            ax.set_title((w.get("title") or "")[:48], fontsize=9, color=INK, loc="left", pad=14)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=SURFACE)
        return buf.getvalue()
    except Exception:
        logger.warning("Dashboard image rendering failed", exc_info=True)
        return None


def render_pdf(data: dict, use_charts: bool = True) -> bytes:
    """The report as a PDF: charts via matplotlib when available, text-only otherwise."""
    if use_charts and charts_available():
        try:
            return _render_pdf_matplotlib(data)
        except Exception:
            logger.warning("matplotlib PDF failed; falling back to text PDF", exc_info=True)
    return _render_pdf_text(data)


def _render_pdf_matplotlib(data: dict) -> bytes:
    from matplotlib.backends.backend_pdf import PdfPages

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        # Cover: title, freshness, KPIs
        fig = _figure(8.27, 11.69)
        y = 0.94
        fig.text(0.08, y, data.get("title") or "havn report", fontsize=18, fontweight="bold", color=INK)
        y -= 0.03
        meta = f"Generated {_fmt_time(data.get('generated_at'))}"
        if (data.get("freshness") or {}).get("as_of"):
            meta += f" · data as of {_fmt_time(data['freshness']['as_of'])}"
        fig.text(0.08, y, meta, fontsize=9, color=INK_MUTED)
        y -= 0.04
        if (data.get("condition") or {}).get("description"):
            fig.text(0.08, y, "Sent because " + data["condition"]["description"], fontsize=10, color=INK)
            y -= 0.04
        for k in [w["kpi"] for w in data.get("widgets", []) if w.get("kpi")]:
            fig.text(0.08, y, str(k["label"])[:60], fontsize=10, color=INK_SECONDARY)
            fig.text(0.62, y, k["display"], fontsize=14, fontweight="bold", color=INK)
            y -= 0.04
            if y < 0.1:
                break
        pdf.savefig(fig)
        for w in data.get("widgets", []):
            if w.get("kpi") or w.get("widget_type") in ("divider", "image", "text"):
                continue
            fig = _figure(8.27, 11.69)
            fig.text(0.08, 0.95, (w.get("title") or "Untitled")[:80], fontsize=13, fontweight="bold", color=INK)
            if w.get("error"):
                fig.text(0.08, 0.91, "Could not load: " + str(w["error"])[:200], fontsize=9, color="#b42318")
            elif is_chartable(w):
                ax = fig.add_axes([0.1, 0.55, 0.82, 0.33])
                _draw_chart(ax, w)
                _pdf_table(fig, w, top=0.47, max_rows=18)
            else:
                _pdf_table(fig, w, top=0.92, max_rows=PDF_TABLE_ROWS)
            pdf.savefig(fig)
    return buf.getvalue()


def _pdf_table(fig, w: dict, top: float, max_rows: int) -> None:
    columns, rows = w.get("columns") or [], (w.get("rows") or [])[:max_rows]
    if not columns:
        return
    line_h = 0.018
    ncol = min(len(columns), 6)
    col_w = 0.84 / ncol
    y = top
    for i, c in enumerate(columns[:ncol]):
        fig.text(0.08 + i * col_w, y, str(c)[:22], fontsize=8, fontweight="bold", color=INK_SECONDARY)
    y -= line_h
    for r in rows:
        for i, v in enumerate(r[:ncol]):
            fig.text(0.08 + i * col_w, y, format_cell(v)[:24], fontsize=8, color=INK)
        y -= line_h
        if y < 0.05:
            break
    total = w.get("row_count", len(rows))
    if total > len(rows):
        fig.text(0.08, max(y, 0.03), f"{len(rows)} of {total} rows shown; see the CSV attachment", fontsize=7, color=INK_MUTED)


# ---------------------------------------------------------------------------
# Minimal text-only PDF writer (no dependencies)
# ---------------------------------------------------------------------------


def _pdf_escape(text: str) -> str:
    text = text.encode("cp1252", errors="replace").decode("cp1252")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _render_pdf_text(data: dict) -> bytes:
    """A plain A4 PDF of the report's text (Helvetica, cp1252), one or more pages."""
    lines: list[tuple[int, bool, str]] = []  # (size, bold, text)
    lines.append((16, True, data.get("title") or "havn report"))
    meta = f"Generated {_fmt_time(data.get('generated_at'))}"
    if (data.get("freshness") or {}).get("as_of"):
        meta += f" - data as of {_fmt_time(data['freshness']['as_of'])}"
    lines.append((9, False, meta))
    if (data.get("condition") or {}).get("description"):
        lines.append((10, False, "Sent because " + data["condition"]["description"]))
    lines.append((9, False, ""))
    for w in data.get("widgets", []):
        if w.get("kpi"):
            k = w["kpi"]
            lines.append((11, False, f"{k['label']}: {k['display']}"))
    for w in data.get("widgets", []):
        if w.get("kpi") or w.get("widget_type") in ("divider", "image"):
            continue
        lines.append((9, False, ""))
        lines.append((12, True, w.get("title") or "Untitled"))
        if w.get("widget_type") == "text":
            for para in ((w.get("config") or {}).get("content") or "").splitlines():
                lines.append((9, False, para[:110]))
            continue
        if w.get("error"):
            lines.append((9, False, f"Could not load: {w['error']}"[:110]))
            continue
        cols = w.get("columns") or []
        widths = [max([len(str(c))] + [len(format_cell(r[i])) for r in (w.get("rows") or [])[:PDF_TABLE_ROWS]]) for i, c in enumerate(cols)]
        widths = [min(max(wd, 4), 24) for wd in widths]

        def _row(vals):
            return "  ".join(str(v)[: widths[i]].ljust(widths[i]) for i, v in enumerate(vals))[:110]

        lines.append((8, True, _row([str(c) for c in cols])))
        for r in (w.get("rows") or [])[:PDF_TABLE_ROWS]:
            lines.append((8, False, _row([format_cell(v) for v in r])))
        if w.get("row_count", 0) > PDF_TABLE_ROWS:
            lines.append((8, False, f"{PDF_TABLE_ROWS} of {w['row_count']} rows shown; see the CSV attachment"))

    page_w, page_h, margin = 595, 842, 50
    pages: list[list[str]] = [[]]
    y = page_h - margin
    for size, bold, text in lines:
        lead = size + 4
        if y - lead < margin:
            pages.append([])
            y = page_h - margin
        y -= lead
        font = "F2" if bold else "F1"
        # Courier for tables keeps columns aligned
        if size == 8:
            font = "F4" if bold else "F3"
        pages[-1].append(f"BT /{font} {size} Tf {margin} {y} Td ({_pdf_escape(text)}) Tj ET")

    objects: list[bytes] = []

    def add(obj: str | bytes) -> int:
        objects.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
        return len(objects)

    catalog = add("")  # placeholder 1
    pages_obj = add("")  # placeholder 2
    fonts = [
        add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
        add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"),
        add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"),
        add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"),
    ]
    font_res = " ".join(f"/F{i + 1} {n} 0 R" for i, n in enumerate(fonts))
    page_ids = []
    for content in pages:
        stream = "\n".join(content).encode("cp1252", errors="replace")
        cid = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {page_w} {page_h}] "
            f"/Resources << /Font << {font_res} >> >> /Contents {cid} 0 R >>"
        ))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objects[pages_obj - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>"
    ).encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
