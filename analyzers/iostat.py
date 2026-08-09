"""
Analyzer for the 'iostat' section found in Linux SystemPerformance files.
Windows SystemPerformance files do not contain this section; returns '' silently.

Expected format: iostat -xmt (repeated intervals with timestamps)
  Linux ... (preamble)
  MM/DD/YYYY HH:MM:SS AM
  avg-cpu:  %user  %nice  %system  %iowait  %steal  %idle
            13.61   1.06     2.32     5.50    0.01  77.49
  Device   r/s  w/s  rkB/s  wkB/s  ... %util
  sda      ...
  (repeat)
"""
import re
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_TS_LINE_RE = re.compile(
    r'^\s*(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:[AP]M))\s*$',
    re.IGNORECASE | re.MULTILINE,
)
_TS_FORMAT = '%m/%d/%Y %I:%M:%S %p'
_CPU_VALUES_RE = re.compile(r'avg-cpu:.*?\n\s*([\d.\s]+)', re.DOTALL)
_CPU_HEADER_RE = re.compile(r'avg-cpu:\s+(%\S+.*)', re.IGNORECASE)
_DEV_HEADER_RE = re.compile(r'^\s*Device\s+(.+)', re.IGNORECASE | re.MULTILINE)


def _parse_iostat(text: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    boundaries = list(_TS_LINE_RE.finditer(text))
    if len(boundaries) < 2:
        return None, None

    dev_records, cpu_records = [], []
    for i, m in enumerate(boundaries):
        ts_str = re.sub(r'\s+([AP]M)$', r' \1', m.group(1).strip(), flags=re.IGNORECASE)
        try:
            ts = pd.to_datetime(ts_str, format=_TS_FORMAT)
        except Exception:
            continue

        block = text[m.end(): boundaries[i + 1].start() if i + 1 < len(boundaries) else len(text)]

        cpu_hdr = _CPU_HEADER_RE.search(block)
        cpu_vals_m = _CPU_VALUES_RE.search(block)
        if cpu_hdr and cpu_vals_m:
            for col, val in zip(cpu_hdr.group(1).split(), cpu_vals_m.group(1).split()):
                try:
                    cpu_records.append({'dt': ts, 'metric': col, 'value': float(val)})
                except ValueError:
                    pass

        dev_hdr_m = _DEV_HEADER_RE.search(block)
        if not dev_hdr_m:
            continue
        dev_cols = ['Device'] + dev_hdr_m.group(1).split()
        for line in block[dev_hdr_m.end():].splitlines():
            if not line.strip() or re.match(r'\s*(avg-cpu|Device)', line):
                continue
            parts = line.split()
            if not parts:
                continue
            device = parts[0]
            for col, val in zip(dev_cols[1:], parts[1:]):
                try:
                    dev_records.append({'dt': ts, 'device': device,
                                        'metric': col, 'value': float(val)})
                except ValueError:
                    pass

    return (pd.DataFrame(dev_records) if dev_records else None,
            pd.DataFrame(cpu_records) if cpu_records else None)


def _flag(level: str, text: str) -> str:
    """Render a single insight callout pill."""
    style = {
        'red':   ('#fef2f2', '#fca5a5', '#7f1d1d', '#ef4444'),
        'amber': ('#fffbeb', '#fcd34d', '#78350f', '#f59e0b'),
        'info':  ('#eff6ff', '#93c5fd', '#1e3a5f', '#3b82f6'),
    }[level]
    bg, border, fg, dot = style
    return (f'<div style="display:flex;align-items:flex-start;gap:8px;padding:7px 11px;'
            f'background:{bg};border:1px solid {border};border-radius:6px;'
            f'font-size:0.78rem;color:{fg};line-height:1.4">'
            f'<span style="color:{dot};flex-shrink:0;margin-top:1px">&#9679;</span>'
            f'<span>{text}</span></div>')


def _insights_panel(flags: list[str]) -> str:
    if not flags:
        return ''
    items = '\n'.join(flags)
    return f'''
<div style="margin-bottom:14px">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Insights</div>
  <div style="display:flex;flex-direction:column;gap:5px">{items}</div>
</div>'''


async def analyze(section_text: str) -> str:
    dev_df, cpu_df = _parse_iostat(section_text)
    if (dev_df is None or dev_df.empty) and (cpu_df is None or cpu_df.empty):
        return ''

    n_intervals  = dev_df['dt'].nunique() if dev_df is not None else 0
    devices_all  = dev_df['device'].unique().tolist() if dev_df is not None else []
    phys_devs    = [d for d in devices_all if not d.startswith('dm-')]
    dm_devs      = [d for d in devices_all if d.startswith('dm-')]
    chart_devs   = phys_devs if phys_devs else devices_all
    dev_metrics  = set(dev_df['metric'].unique()) if dev_df is not None else set()
    cpu_metrics  = set(cpu_df['metric'].unique()) if cpu_df is not None else set()

    # ── Insights ──────────────────────────────────────────────────────────────
    flags = []

    # CPU %iowait
    if cpu_df is not None and '%iowait' in cpu_metrics:
        iowait_avg = cpu_df[cpu_df['metric'] == '%iowait']['value'].mean()
        iowait_p99 = cpu_df[cpu_df['metric'] == '%iowait']['value'].quantile(0.99)
        if iowait_avg > 20:
            flags.append(_flag('red',
                f'<b>High CPU %iowait</b>: avg {iowait_avg:.1f}%, p99 peak {iowait_p99:.1f}% — '
                f'CPU is frequently blocked waiting for I/O. Investigate disk latency or throughput saturation.'))
        elif iowait_avg > 10:
            flags.append(_flag('amber',
                f'<b>Elevated CPU %iowait</b>: avg {iowait_avg:.1f}%, p99 peak {iowait_p99:.1f}% — '
                f'I/O is delaying the CPU. Worth correlating with disk %util and latency.'))

    # Per-device %util
    if dev_df is not None and '%util' in dev_metrics:
        util_avg = (dev_df[dev_df['metric'] == '%util']
                    .groupby('device')['value'].mean())
        util_p99 = (dev_df[dev_df['metric'] == '%util']
                    .groupby('device')['value'].quantile(0.99))
        for dev in chart_devs:
            avg_v = util_avg.get(dev, 0)
            max_v = util_p99.get(dev, 0)
            if avg_v > 60:
                flags.append(_flag('red',
                    f'<b>{dev} %util avg {avg_v:.1f}%</b> (p99 peak {max_v:.1f}%) — '
                    f'device is heavily utilised. Concurrent I/O may queue behind it.'))
            elif avg_v > 30:
                flags.append(_flag('amber',
                    f'<b>{dev} %util avg {avg_v:.1f}%</b> (p99 peak {max_v:.1f}%) — '
                    f'moderate utilisation. Monitor under heavier workload.'))

    # Per-device latency
    if dev_df is not None:
        for lat_col, label in [('r_await', 'read latency'), ('w_await', 'write latency'), ('await', 'latency')]:
            if lat_col not in dev_metrics:
                continue
            lat_avg = (dev_df[dev_df['metric'] == lat_col]
                       .groupby('device')['value'].mean())
            for dev in chart_devs:
                v = lat_avg.get(dev, 0)
                if v > 20:
                    flags.append(_flag('red',
                        f'<b>{dev} {label} avg {v:.1f} ms</b> — severely elevated. '
                        f'Suggests saturation or underlying storage performance issue.'))
                elif v > 5:
                    flags.append(_flag('amber',
                        f'<b>{dev} {label} avg {v:.1f} ms</b> — moderately elevated. '
                        f'NVMe typically &lt;1 ms; spinning disk typically &lt;10 ms.'))

    # Bursty writes (peak wkB/s > 5× avg)
    if dev_df is not None and 'wkB/s' in dev_metrics:
        for dev in chart_devs:
            d = dev_df[(dev_df['device'] == dev) & (dev_df['metric'] == 'wkB/s')]['value']
            if d.mean() > 0 and d.max() / d.mean() > 5:
                flags.append(_flag('info',
                    f'<b>{dev} bursty writes</b>: peak {d.max():.0f} kB/s vs avg {d.mean():.0f} kB/s '
                    f'({d.max()/d.mean():.0f}× ratio) — write pattern is spiky, not sustained.'))

    if not flags:
        flags.append(_flag('info', 'No significant anomalies detected in this sample window.'))

    insights_html = '<!--INS-->' + _insights_panel(flags) + '<!--/INS-->'

    # ── Charts ────────────────────────────────────────────────────────────────
    COLORS = ['#0055aa', '#e74c3c', '#27ae60', '#e67e22', '#8e44ad',
              '#16a085', '#f39c12', '#2c3e50', '#c0392b', '#2980b9']

    has_util    = '%util'   in dev_metrics
    has_iops    = 'r/s'     in dev_metrics or 'w/s'     in dev_metrics
    has_thru_kb = 'rkB/s'  in dev_metrics or 'wkB/s'   in dev_metrics
    has_await   = 'r_await' in dev_metrics or 'w_await' in dev_metrics or 'await' in dev_metrics
    has_iowait  = '%iowait' in cpu_metrics

    row_defs = []  # (title, fn(fig, row))
    ref_lines: dict[int, list[tuple]] = {}  # row_idx -> [(y, color, label)]

    if has_util:
        ref_lines[len(row_defs) + 1] = [
            (60, '#dc2626', 'heavily utilised (60%)'),
            (30, '#d97706', 'moderate (30%)'),
        ]
        def _add_util(fig, r, devs=chart_devs):
            for ci, dev in enumerate(devs):
                d = dev_df[(dev_df['device'] == dev) & (dev_df['metric'] == '%util')].sort_values('dt')
                if d.empty:
                    continue
                fig.add_trace(go.Scatter(
                    x=d['dt'], y=d['value'], name=dev, mode='lines',
                    line=dict(color=COLORS[ci % len(COLORS)], width=1.5),
                    hovertemplate=f'<b>{dev}</b><br>%{{x}}<br>%{{y:.1f}}%<extra></extra>',
                    legendgroup=dev, showlegend=True,
                ), row=r, col=1)
            fig.update_yaxes(title_text='%util', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=r, col=1)
        row_defs.append(('Disk %util', _add_util))

    if has_iowait and cpu_df is not None:
        ref_lines[len(row_defs) + 1] = [
            (20, '#dc2626', 'high iowait (20%)'),
            (10, '#d97706', 'elevated iowait (10%)'),
        ]
        def _add_cpu(fig, r, cpu_df=cpu_df):
            for mi, (metric, color) in enumerate(
                    [('%iowait', '#e74c3c'), ('%user', '#0055aa'), ('%system', '#e67e22')]):
                d = cpu_df[cpu_df['metric'] == metric].sort_values('dt')
                if d.empty:
                    continue
                fig.add_trace(go.Scatter(
                    x=d['dt'], y=d['value'], name=metric, mode='lines',
                    line=dict(color=color, width=1.5),
                    hovertemplate=f'<b>{metric}</b><br>%{{x}}<br>%{{y:.1f}}%<extra></extra>',
                ), row=r, col=1)
            fig.update_yaxes(title_text='%', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=r, col=1)
        row_defs.append(('CPU %iowait / %user / %system', _add_cpu))

    if has_iops:
        def _add_iops(fig, r):
            for ci, dev in enumerate(chart_devs):
                for metric, dash in [('r/s', None), ('w/s', 'dot')]:
                    if metric not in dev_metrics:
                        continue
                    d = dev_df[(dev_df['device'] == dev) & (dev_df['metric'] == metric)].sort_values('dt')
                    if d.empty:
                        continue
                    fig.add_trace(go.Scatter(
                        x=d['dt'], y=d['value'], name=f'{dev} {metric}', mode='lines',
                        line=dict(color=COLORS[ci % len(COLORS)], width=1.2,
                                  **(dict(dash=dash) if dash else {})),
                        hovertemplate=f'<b>{dev} {metric}</b><br>%{{x}}<br>%{{y:.1f}}<extra></extra>',
                        legendgroup=f'{dev}_{metric}', showlegend=True,
                    ), row=r, col=1)
            fig.update_yaxes(title_text='ops/s', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=r, col=1)
        row_defs.append(('IOPS (r/s solid, w/s dotted)', _add_iops))

    if has_thru_kb:
        def _add_thru(fig, r):
            for ci, dev in enumerate(chart_devs):
                for metric, dash in [('rkB/s', None), ('wkB/s', 'dot')]:
                    if metric not in dev_metrics:
                        continue
                    d = dev_df[(dev_df['device'] == dev) & (dev_df['metric'] == metric)].sort_values('dt')
                    if d.empty:
                        continue
                    fig.add_trace(go.Scatter(
                        x=d['dt'], y=d['value'], name=f'{dev} {metric}', mode='lines',
                        line=dict(color=COLORS[ci % len(COLORS)], width=1.2,
                                  **(dict(dash=dash) if dash else {})),
                        hovertemplate=f'<b>{dev} {metric}</b><br>%{{x}}<br>%{{y:.1f}} kB/s<extra></extra>',
                        legendgroup=f'{dev}_{metric}', showlegend=True,
                    ), row=r, col=1)
            fig.update_yaxes(title_text='kB/s', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=r, col=1)
        row_defs.append(('Throughput kB/s (read solid, write dotted)', _add_thru))

    if has_await:
        ref_lines[len(row_defs) + 1] = [(1, '#64748b', '1 ms reference (SSD/NVMe target)')]
        def _add_await(fig, r):
            for ci, dev in enumerate(chart_devs):
                for metric, dash in [('r_await', None), ('w_await', 'dot'), ('await', None)]:
                    if metric not in dev_metrics:
                        continue
                    d = dev_df[(dev_df['device'] == dev) & (dev_df['metric'] == metric)].sort_values('dt')
                    if d.empty:
                        continue
                    fig.add_trace(go.Scatter(
                        x=d['dt'], y=d['value'], name=f'{dev} {metric}', mode='lines',
                        line=dict(color=COLORS[ci % len(COLORS)], width=1.2,
                                  **(dict(dash=dash) if dash else {})),
                        hovertemplate=f'<b>{dev} {metric}</b><br>%{{x}}<br>%{{y:.2f}} ms<extra></extra>',
                        legendgroup=f'{dev}_{metric}', showlegend=True,
                    ), row=r, col=1)
            fig.update_yaxes(title_text='ms', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=r, col=1)
        row_defs.append(('Latency ms (r_await solid, w_await dotted)', _add_await))

    if not row_defs:
        return ''

    nrows = len(row_defs)
    fig = make_subplots(
        rows=nrows, cols=1,
        subplot_titles=[r[0] for r in row_defs],
        vertical_spacing=0.08 if nrows > 2 else 0.12,
    )
    for row_idx, (_, fn) in enumerate(row_defs, start=1):
        fn(fig, row_idx)

    for row_idx, lines in ref_lines.items():
        for y, color, label in lines:
            fig.add_hline(
                y=y, row=row_idx, col=1,
                line=dict(color=color, width=1, dash='dash'),
                annotation_text=label, annotation_position='top left',
                annotation_font=dict(size=9, color=color), opacity=0.7,
            )

    fig.update_layout(
        height=250 * nrows,
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

    # %util table — physical devices only
    util_table = ''
    if has_util and chart_devs:
        util_avg_all = (dev_df[dev_df['metric'] == '%util']
                        .groupby('device')['value'].mean()
                        .reindex(chart_devs)
                        .sort_values(ascending=False))
        rows_html = ''.join(
            f'<tr>'
            f'<td style="padding:3px 16px 3px 0">{dev}</td>'
            f'<td style="text-align:right;font-weight:600;'
            f'color:{"#c0392b" if v > 60 else "#e67e22" if v > 30 else "#27ae60"}">'
            f'{v:.1f}%</td>'
            f'</tr>'
            for dev, v in util_avg_all.items() if not pd.isna(v)
        )
        util_table = f'''
<table id="iostat-util" style="font-size:0.78rem;border-collapse:collapse;margin:0 0 14px 0">
  <thead><tr style="color:#888;font-size:0.72rem;font-weight:600;text-transform:uppercase">
    <th onclick="sortTable('iostat-util',0,this)" style="text-align:left;padding-bottom:4px;cursor:pointer;user-select:none">Device (physical) ↕</th>
    <th onclick="sortTable('iostat-util',1,this)" style="text-align:right;padding-bottom:4px;cursor:pointer;user-select:none">Avg %util ↕</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>'''

    n_phys = len(phys_devs)
    n_dm   = len(dm_devs)
    dev_note = (f'{n_phys} physical device(s)'
                + (f' &mdash; {n_dm} dm device(s) hidden from charts' if n_dm else ''))

    return f'''
<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px;
              text-transform:uppercase;letter-spacing:.06em">iostat Analysis</div>
  <div style="font-size:0.72rem;color:#999;margin-bottom:12px">
    {n_intervals} intervals &nbsp;|&nbsp; {dev_note}
  </div>
  {insights_html}
  {util_table}
  {chart_html}
</div>
'''
