"""
Analyzer for the 'sar -u' (CPU utilization) section in Linux SystemPerformance files.

Expected format:
  Linux ... (preamble line)
  HH:MM:SS AM     CPU     %user     %nice   %system   %iowait    %steal     %idle
  HH:MM:SS AM     all     31.28      0.00      5.10      1.31      0.00     62.31
  ...
  Average:        all     ...

Timestamp format varies: 12-hour with AM/PM (most locales) or 24-hour.
Reuses the same strptime strategy as time_filter.py sar parser.
"""
import re
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_DATE_RE  = re.compile(r'(\d{2}/\d{2}/\d{4})')
_HDR_RE   = re.compile(
    r'^(\d{2}:\d{2}:\d{2}(?:\s*[AP]M)?)\s+CPU\s+(%user.+)',
    re.IGNORECASE | re.MULTILINE,
)
_DATA_RE  = re.compile(
    r'^(\d{2}:\d{2}:\d{2}(?:\s*[AP]M)?)\s+(\S+)\s+(.+)',
    re.IGNORECASE | re.MULTILINE,
)
_AVG_RE   = re.compile(r'^Average:\s+(\S+)\s+(.+)', re.IGNORECASE | re.MULTILINE)


def _parse_ts(time_str: str, date_str: str) -> pd.Timestamp | None:
    s = re.sub(r'\s+', ' ', f'{date_str} {time_str}'.strip())
    for fmt in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %H:%M:%S'):
        try:
            return pd.to_datetime(s, format=fmt)
        except Exception:
            pass
    return None


def _parse_sar_u(text: str) -> tuple[pd.DataFrame | None, dict | None]:
    date_m   = _DATE_RE.search(text)
    date_str = date_m.group(1) if date_m else '01/01/1970'

    hdr_m = _HDR_RE.search(text)
    # Fall back to the standard sar -u column order when header line is absent
    raw_cols = hdr_m.group(2).split() if hdr_m else ['%user', '%nice', '%system', '%iowait', '%steal', '%idle']

    records = []
    for m in _DATA_RE.finditer(text):
        cpu_field = m.group(2)
        if cpu_field.upper() == 'CPU':
            continue
        ts = _parse_ts(m.group(1).strip(), date_str)
        values = m.group(3).split()
        row = {'dt': ts, 'cpu': cpu_field}
        for col, val in zip(raw_cols, values):
            try:
                row[col] = float(val)
            except ValueError:
                pass
        records.append(row)

    # Parse Average: line for summary
    avg = {}
    avg_m = _AVG_RE.search(text)
    if avg_m and avg_m.group(1).lower() != 'cpu':
        for col, val in zip(raw_cols, avg_m.group(2).split()):
            try:
                avg[col] = float(val)
            except ValueError:
                pass

    df = pd.DataFrame(records) if records else None
    if df is not None and not df.empty:
        df = df[df['cpu'] == 'all'].dropna(subset=['dt']).sort_values('dt').reset_index(drop=True)
    return df, avg if avg else None


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
    df, avg = _parse_sar_u(section_text)
    if df is None or df.empty:
        return ''

    n    = len(df)
    cols = set(df.columns) - {'dt', 'cpu'}

    has_iowait = '%iowait' in cols
    has_user   = '%user'   in cols
    has_sys    = '%system' in cols
    has_idle   = '%idle'   in cols
    has_steal  = '%steal'  in cols

    # ── Insights ─────────────────────────────────────────────────────────────
    flags = []

    if has_idle:
        idle_avg = df['%idle'].mean()
        idle_min = df['%idle'].min()
        if idle_avg < 10:
            flags.append(_flag('red',
                f'<b>CPU near saturation</b>: avg idle {idle_avg:.1f}%, min {idle_min:.1f}% — '
                f'very little headroom. IRIS processes may be starved for CPU.'))
        elif idle_avg < 25:
            flags.append(_flag('amber',
                f'<b>High CPU utilisation</b>: avg idle {idle_avg:.1f}% — '
                f'headroom is limited. Sudden load spikes may cause queuing.'))

    if has_iowait:
        wa_avg = df['%iowait'].mean()
        wa_p99 = df['%iowait'].quantile(0.99)
        if wa_avg > 20:
            flags.append(_flag('red',
                f'<b>High CPU iowait</b>: avg {wa_avg:.1f}%, p99 peak {wa_p99:.1f}% — '
                f'CPU is frequently blocked on I/O. Correlate with iostat/sar -d latency.'))
        elif wa_avg > 10:
            flags.append(_flag('amber',
                f'<b>Elevated CPU iowait</b>: avg {wa_avg:.1f}%, p99 peak {wa_p99:.1f}% — '
                f'I/O delays are consuming CPU wait cycles.'))

    if has_steal:
        steal_avg = df['%steal'].mean()
        steal_p99 = df['%steal'].quantile(0.99)
        if steal_avg > 5:
            flags.append(_flag('red',
                f'<b>CPU steal time</b>: avg {steal_avg:.1f}%, p99 peak {steal_p99:.1f}% — '
                f'hypervisor is scheduling away CPU time. This system is resource-contended '
                f'at the VM host level.'))
        elif steal_avg > 1:
            flags.append(_flag('amber',
                f'<b>CPU steal present</b>: avg {steal_avg:.1f}% — '
                f'hypervisor contention is visible. Monitor for increases.'))

    if has_sys and has_user:
        sys_avg  = df['%system'].mean()
        user_avg = df['%user'].mean()
        if sys_avg > user_avg * 0.5 and sys_avg > 10:
            flags.append(_flag('amber',
                f'<b>High kernel time</b>: avg %system {sys_avg:.1f}% vs %user {user_avg:.1f}% — '
                f'kernel overhead is significant. Check context switches (vmstat cs) and '
                f'interrupt rate.'))

    if not flags:
        flags.append(_flag('green', 'CPU utilisation is within normal range.'))

    insights_html = '<!--INS-->' + f'''
<div style="margin-bottom:14px">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Insights</div>
  {''.join(flags)}
</div>''' + '<!--/INS-->'

    # ── Stat cards ────────────────────────────────────────────────────────────
    src = avg if avg else {c: df[c].mean() for c in cols if c in df.columns}
    stat_items = []
    for col, label, unit in [
        ('%user',   'User avg',   '%'),
        ('%system', 'System avg', '%'),
        ('%iowait', 'IOWait avg', '%'),
        ('%steal',  'Steal avg',  '%'),
        ('%idle',   'Idle avg',   '%'),
    ]:
        if col in src:
            stat_items.append(_stat(label, f'{src[col]:.1f}', unit))

    stats_html = (f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">'
                  + ''.join(stat_items) + '</div>') if stat_items else ''

    # ── Chart — stacked area ──────────────────────────────────────────────────
    # Show user/sys/iowait/steal stacked; idle as a separate line for context
    chart_cols = [
        ('%iowait', '#e74c3c', 'iowait'),
        ('%system', '#e67e22', 'system'),
        ('%user',   '#0055aa', 'user'),
        ('%steal',  '#8e44ad', 'steal'),
    ]
    chart_cols = [(c, col, lbl) for c, col, lbl in chart_cols if c in cols]

    if not chart_cols:
        return ''

    nrows = 1
    fig = make_subplots(rows=1, cols=1, subplot_titles=['CPU Utilisation (%)'])

    for c, color, lbl in chart_cols:
        fig.add_trace(go.Scatter(
            x=df['dt'], y=df[c], name=lbl, mode='lines',
            line=dict(color=color, width=1.5),
            stackgroup='cpu',
            hovertemplate=f'<b>{lbl}</b><br>%{{x}}<br>%{{y:.1f}}%<extra></extra>',
        ), row=1, col=1)

    if has_idle:
        fig.add_trace(go.Scatter(
            x=df['dt'], y=df['%idle'], name='idle', mode='lines',
            line=dict(color='#27ae60', width=1.2, dash='dot'),
            hovertemplate='<b>idle</b><br>%{x}<br>%{y:.1f}%<extra></extra>',
        ), row=1, col=1)

    fig.update_yaxes(title_text='%', showgrid=True, gridcolor='#e8edf5',
                     range=[0, 100], row=1, col=1)
    if has_idle:
        # Matches the idle<10/idle<25 insight thresholds above, expressed as %used.
        for y, color, label in [(90, '#dc2626', 'near saturation (90% used)'),
                                 (75, '#d97706', 'high utilisation (75% used)')]:
            fig.add_hline(
                y=y, row=1, col=1,
                line=dict(color=color, width=1, dash='dash'),
                annotation_text=label, annotation_position='top left',
                annotation_font=dict(size=9, color=color), opacity=0.7,
            )
    fig.update_xaxes(showgrid=True, gridcolor='#e8edf5', tickangle=-30)
    fig.update_layout(
        height=280,
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor='white',
        plot_bgcolor='#f8f9fc',
        font=dict(family='-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', size=10),
        legend=dict(orientation='h', x=0, y=-0.1, font_size=10),
        hovermode='x unified',
    )

    chart_html = fig.to_html(
        full_html=False, include_plotlyjs=False,
        config={'displayModeBar': True, 'displaylogo': False,
                'modeBarButtonsToRemove': ['select2d', 'lasso2d']},
    )

    avg_note = ' (from sar Average: line)' if avg else ''
    return f'''
<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px;
              text-transform:uppercase;letter-spacing:.06em">sar -u Analysis</div>
  <div style="font-size:0.72rem;color:#999;margin-bottom:12px">
    {n} intervals{avg_note}
  </div>
  {insights_html}
  {stats_html}
  {chart_html}
</div>
'''
