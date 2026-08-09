"""
Analyzer for the 'vmstat' section in Linux SystemPerformance files.

Expected format: one header line followed by timestamped data rows.
  MM/DD/YY HH:MM:SS  r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st

Key columns:
  r    = run queue length (processes waiting for CPU)
  b    = blocked processes (waiting for I/O)
  swpd = virtual memory used (swap in use, kB)
  si   = swap in (kB/s from disk to RAM)
  so   = swap out (kB/s from RAM to disk)
  bi   = block in (blocks/s read from disk)
  bo   = block out (blocks/s written to disk)
  us/sy/id/wa = CPU breakdown %
"""
import re
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_DATA_RE = re.compile(
    r'^\s*(\d{2}/\d{2}/\d{2,4})\s+(\d{2}:\d{2}:\d{2})\s+(.+)',
    re.MULTILINE,
)
_HDR_RE = re.compile(r'r\s+b\s+swpd', re.IGNORECASE)

_COLS = ['r', 'b', 'swpd', 'free', 'buff', 'cache', 'si', 'so',
         'bi', 'bo', 'in', 'cs', 'us', 'sy', 'id', 'wa', 'st']


def _parse_vmstat(text: str) -> pd.DataFrame | None:
    if not _HDR_RE.search(text):
        return None

    records = []
    for m in _DATA_RE.finditer(text):
        values = m.group(3).split()
        if len(values) < len(_COLS):
            continue
        if not values[0].lstrip('-').isdigit():
            continue  # skip the header row (first value is 'r', not a number)
        date_s = m.group(1)
        time_s = m.group(2)
        if len(date_s.split('/')[2]) == 2:
            p = date_s.split('/')
            date_s = f'{p[0]}/{p[1]}/20{p[2]}'
        try:
            ts = pd.to_datetime(f'{date_s} {time_s}', format='%m/%d/%Y %H:%M:%S')
        except Exception:
            continue
        row = {'dt': ts}
        for col, val in zip(_COLS, values):
            try:
                row[col] = float(val)
            except ValueError:
                pass
        records.append(row)

    if not records:
        return None
    df = pd.DataFrame(records).dropna(subset=['r']).sort_values('dt').reset_index(drop=True)
    if len(df) > 500:
        step = len(df) // 500
        df = df.iloc[::step].reset_index(drop=True)
    return df


def _flag(level: str, text: str) -> str:
    style = {
        'red':   ('#fef2f2', '#fca5a5', '#7f1d1d', '#ef4444'),
        'amber': ('#fffbeb', '#fcd34d', '#78350f', '#f59e0b'),
        'info':  ('#eff6ff', '#93c5fd', '#1e3a5f', '#3b82f6'),
        'green': ('#f0fdf4', '#bbf7d0', '#14532d', '#22c55e'),
    }[level]
    bg, border, fg, dot = style
    return (f'<div style="display:flex;align-items:flex-start;gap:8px;padding:7px 11px;'
            f'background:{bg};border:1px solid {border};border-radius:6px;'
            f'font-size:0.78rem;color:{fg};line-height:1.4;margin-bottom:5px">'
            f'<span style="color:{dot};flex-shrink:0;margin-top:1px">&#9679;</span>'
            f'<span>{text}</span></div>')


def _stat(label: str, value: str, unit: str = '') -> str:
    return (f'<div style="background:#f8f9fc;border:1px solid #dde3ee;border-radius:8px;'
            f'padding:10px 14px;min-width:100px">'
            f'<div style="font-size:0.68rem;color:#888;text-transform:uppercase;'
            f'letter-spacing:.05em;margin-bottom:3px">{label}</div>'
            f'<div style="font-size:1.1rem;font-weight:700;color:#1a1a2e">'
            f'{value}<span style="font-size:0.75rem;font-weight:400;color:#888;'
            f'margin-left:3px">{unit}</span></div></div>')


async def analyze(section_text: str) -> str:
    df = _parse_vmstat(section_text)
    if df is None or df.empty:
        return ''

    n    = len(df)
    dcols = set(df.columns)

    has_runq    = 'r'    in dcols
    has_blocked = 'b'    in dcols
    has_swap    = {'swpd', 'si', 'so'}.issubset(dcols)
    has_cpu     = {'us', 'sy', 'id', 'wa'}.issubset(dcols)
    has_io      = {'bi', 'bo'}.issubset(dcols)
    has_free    = 'free' in dcols

    # ── Insights ─────────────────────────────────────────────────────────────
    flags = []

    if has_swap:
        si_avg  = df['si'].mean()
        so_avg  = df['so'].mean()
        swpd_max = df['swpd'].max()
        if so_avg > 10 or si_avg > 10:
            flags.append(_flag('red',
                f'<b>Active swap I/O</b>: avg swap-in {si_avg:.1f} kB/s, swap-out {so_avg:.1f} kB/s — '
                f'system is under memory pressure. IRIS global buffer pool may be competing with OS for RAM.'))
        elif swpd_max > 0:
            flags.append(_flag('amber',
                f'<b>Swap in use</b>: up to {swpd_max/1024:.0f} MB committed, but swap I/O is low.'))

    if has_runq:
        r_avg = df['r'].mean()
        r_p99 = df['r'].quantile(0.99)
        if r_avg > 8:
            flags.append(_flag('red',
                f'<b>CPU run queue saturation</b>: avg {r_avg:.1f}, p99 peak {r_p99:.0f} — '
                f'threads are waiting for CPU. Correlate with mgstat Rourefs/Glorefs drops.'))
        elif r_avg > 4:
            flags.append(_flag('amber',
                f'<b>Elevated run queue</b>: avg {r_avg:.1f}, p99 peak {r_p99:.0f}.'))

    if has_blocked:
        b_avg = df['b'].mean()
        b_p99 = df['b'].quantile(0.99)
        b_max = df['b'].max()
        if b_avg > 2:
            flags.append(_flag('red',
                f'<b>Processes blocked on I/O</b>: avg {b_avg:.1f}, p99 peak {b_p99:.0f} (max {b_max:.0f}) — '
                f'storage cannot keep up. Cross-reference with iostat %util and sar -d await.'))
        elif b_p99 > 4:
            flags.append(_flag('amber',
                f'<b>Intermittent I/O blocking</b>: p99 peak {b_p99:.0f} blocked processes (single-sample max {b_max:.0f}).'))

    if has_cpu:
        wa_avg = df['wa'].mean()
        wa_p99 = df['wa'].quantile(0.99)
        if wa_avg > 20:
            flags.append(_flag('red',
                f'<b>High CPU iowait</b>: avg {wa_avg:.1f}%, p99 peak {wa_p99:.1f}% — storage bottleneck.'))
        elif wa_avg > 10:
            flags.append(_flag('amber',
                f'<b>Elevated CPU iowait</b>: avg {wa_avg:.1f}%, p99 peak {wa_p99:.1f}%.'))

    if not flags:
        flags.append(_flag('green', 'No memory pressure or run queue saturation detected.'))

    insights_html = (
        '<!--INS-->'
        '<div style="margin-bottom:14px">'
        '<div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;'
        'letter-spacing:.06em;margin-bottom:6px">Insights</div>'
        + ''.join(flags) + '</div>'
        + '<!--/INS-->'
    )

    # ── Stat cards ────────────────────────────────────────────────────────────
    stat_items = []
    if has_runq:
        stat_items.append(_stat('Run queue avg', f'{df["r"].mean():.1f}'))
    if has_blocked:
        stat_items.append(_stat('Blocked avg', f'{df["b"].mean():.1f}'))
    if has_swap:
        stat_items.append(_stat('Swap used peak', f'{df["swpd"].max()/1024:.0f}', 'MB'))
        stat_items.append(_stat('Swap-out avg', f'{df["so"].mean():.1f}', 'kB/s'))
    if has_cpu:
        stat_items.append(_stat('CPU iowait avg', f'{df["wa"].mean():.1f}', '%'))
    if has_free:
        stat_items.append(_stat('Free RAM min', f'{df["free"].min()/1024:.0f}', 'MB'))

    stats_html = (
        '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">'
        + ''.join(stat_items) + '</div>'
    ) if stat_items else ''

    # ── Charts ────────────────────────────────────────────────────────────────
    row_defs = []  # (title, list of (col, color, name))
    ref_lines: dict[int, list[tuple]] = {}  # row_idx -> [(y, color, label)]

    if has_runq or has_blocked:
        traces = []
        if has_runq:
            traces.append(('r', '#0055aa', 'run queue (r)'))
            ref_lines[len(row_defs) + 1] = [
                (8, '#dc2626', 'run queue saturated (8)'),
                (4, '#d97706', 'elevated run queue (4)'),
            ]
        if has_blocked:
            traces.append(('b', '#e74c3c', 'blocked (b)'))
        row_defs.append(('CPU Run Queue & Blocked Processes', 'processes', traces))

    if has_swap:
        row_defs.append(('Swap I/O (kB/s)', 'kB/s', [
            ('si', '#e74c3c', 'swap-in'),
            ('so', '#e67e22', 'swap-out'),
        ]))

    if has_cpu:
        ref_lines[len(row_defs) + 1] = [(80, '#d97706', '80% busy (20% idle)')]
        row_defs.append(('CPU Breakdown (%)', '%', [
            ('wa', '#e74c3c', '%iowait'),
            ('us', '#0055aa', '%user'),
            ('sy', '#e67e22', '%sys'),
            ('id', '#27ae60', '%idle'),
        ]))

    if has_io:
        row_defs.append(('Block I/O (blocks/s)', 'blocks/s', [
            ('bi', '#0055aa', 'blocks-in'),
            ('bo', '#e74c3c', 'blocks-out'),
        ]))

    if not row_defs:
        return ''

    nrows = len(row_defs)
    fig = make_subplots(
        rows=nrows, cols=1,
        subplot_titles=[rd[0] for rd in row_defs],
        vertical_spacing=0.08 if nrows > 2 else 0.12,
    )

    for row_idx, (title, ylabel, traces) in enumerate(row_defs, start=1):
        for col, color, name in traces:
            fig.add_trace(go.Scatter(
                x=df['dt'], y=df[col], name=name, mode='lines',
                line=dict(color=color, width=1.5),
                hovertemplate=f'<b>{name}</b><br>%{{x}}<br>%{{y:.1f}}<extra></extra>',
            ), row=row_idx, col=1)
        fig.update_yaxes(title_text=ylabel, showgrid=True,
                         gridcolor='#e8edf5', rangemode='tozero',
                         row=row_idx, col=1)

    for row_idx, lines in ref_lines.items():
        for y, color, label in lines:
            fig.add_hline(
                y=y, row=row_idx, col=1,
                line=dict(color=color, width=1, dash='dash'),
                annotation_text=label, annotation_position='top left',
                annotation_font=dict(size=9, color=color), opacity=0.7,
            )

    fig.update_layout(
        height=240 * nrows,
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor='white',
        plot_bgcolor='#f8f9fc',
        font=dict(family='-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', size=10),
        legend=dict(orientation='h', x=0, y=-0.04, font_size=10),
        hovermode='x unified',
    )
    fig.update_xaxes(showgrid=True, gridcolor='#e8edf5', tickangle=-30)

    chart_html = fig.to_html(
        full_html=False, include_plotlyjs=False,
        config={'displayModeBar': True, 'displaylogo': False,
                'modeBarButtonsToRemove': ['select2d', 'lasso2d']},
    )

    return (
        '<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif">'
        '<div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px;'
        'text-transform:uppercase;letter-spacing:.06em">vmstat Analysis</div>'
        f'<div style="font-size:0.72rem;color:#999;margin-bottom:12px">{n} intervals</div>'
        + insights_html
        + stats_html
        + chart_html
        + '</div>'
    )
