"""
Analyzer for the 'sar -d' (disk activity) section in SystemPerformance files.
Linux only; returns '' silently if the section can't be parsed.

Column variations across sysstat versions:
  Old:  tps  rd_sec/s  wr_sec/s  avgrq-sz  avgqu-sz  await  svctm  %util
  New:  tps  rkB/s     wkB/s     areq-sz   aqu-sz    await  r_await  w_await  svctm  %util

rd_sec/s and wr_sec/s are 512-byte sectors; divide by 2 to get kB/s.
"""
import re
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_DATE_RE = re.compile(r'(\d{2}/\d{2}/\d{4})')
_COL_HDR_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2}(?:\s*[AP]M)?)\s+DEV\s+(.+)',
    re.IGNORECASE | re.MULTILINE,
)
_DATA_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2}(?:\s*[AP]M)?)\s+(\S+)\s+(.+)',
    re.IGNORECASE | re.MULTILINE,
)
_AVERAGE_RE = re.compile(r'^Average:\s+(\S+)\s+(.+)', re.IGNORECASE | re.MULTILINE)


def _parse_ts(time_str: str, date_str: str) -> pd.Timestamp | None:
    s = re.sub(r'\s+', ' ', f'{date_str} {time_str}'.strip())
    for fmt in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %H:%M:%S'):
        try:
            return pd.to_datetime(s, format=fmt)
        except Exception:
            pass
    return None


def _parse_sar_d(text: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    date_m = _DATE_RE.search(text)
    date_str = date_m.group(1) if date_m else '01/01/1970'

    hdr_m = _COL_HDR_RE.search(text)
    if not hdr_m:
        return None, None

    raw_cols = hdr_m.group(2).split()
    # Normalise column names to a stable internal set
    col_map = {
        'rd_sec/s': 'read_kb_s',  # will divide by 2
        'wr_sec/s': 'write_kb_s', # will divide by 2
        'rkB/s':    'read_kb_s',
        'wkB/s':    'write_kb_s',
        'avgqu-sz': 'aqu_sz',
        'aqu-sz':   'aqu_sz',
        'avgrq-sz': 'areq_sz',
        'areq-sz':  'areq_sz',
        'r_await':  'r_await',
        'w_await':  'w_await',
        'await':    'await',
        'svctm':    'svctm',
        '%util':    'pct_util',
        'tps':      'tps',
    }
    # Which original names need /2 conversion
    sector_cols = {'rd_sec/s', 'wr_sec/s'}
    cols = [col_map.get(c, c) for c in raw_cols]

    records = []
    for m in _DATA_RE.finditer(text):
        ts = _parse_ts(m.group(1).strip(), date_str)
        device = m.group(2)
        if device.upper() == 'DEV':
            continue
        values = m.group(3).split()
        row = {'dt': ts, 'device': device}
        for i, (orig, norm) in enumerate(zip(raw_cols, cols)):
            if i >= len(values):
                break
            try:
                v = float(values[i])
                if orig in sector_cols:
                    v /= 2.0  # sectors → kB
                row[norm] = v
            except ValueError:
                pass
        records.append(row)

    avg_records = []
    for m in _AVERAGE_RE.finditer(text):
        device = m.group(1)
        if device.upper() == 'DEV':
            continue
        values = m.group(2).split()
        row = {'device': device}
        for i, (orig, norm) in enumerate(zip(raw_cols, cols)):
            if i >= len(values):
                break
            try:
                v = float(values[i])
                if orig in sector_cols:
                    v /= 2.0
                row[norm] = v
            except ValueError:
                pass
        avg_records.append(row)

    data_df = pd.DataFrame(records) if records else None
    avg_df  = pd.DataFrame(avg_records) if avg_records else None
    return data_df, avg_df


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


async def analyze(section_text: str) -> str:
    data_df, avg_df = _parse_sar_d(section_text)
    if data_df is None or data_df.empty:
        return ''

    cols       = set(data_df.columns) - {'dt', 'device'}
    devices    = data_df['device'].unique().tolist()
    phys_devs  = [d for d in devices if not d.startswith('dm-')]
    dm_devs    = [d for d in devices if d.startswith('dm-')]
    chart_devs = phys_devs if phys_devs else devices
    n_intervals = data_df['dt'].nunique()

    has_util  = 'pct_util'    in cols
    has_tps   = 'tps'         in cols
    has_read  = 'read_kb_s'   in cols
    has_write = 'write_kb_s'  in cols
    has_await = 'await'       in cols
    has_rw_await = 'r_await'  in cols and 'w_await' in cols
    has_aqu   = 'aqu_sz'      in cols

    # ── Insights ─────────────────────────────────────────────────────────────
    flags = []

    if has_util:
        util_avg = data_df[data_df['device'].isin(chart_devs)].groupby('device')['pct_util'].mean()
        util_p99 = data_df[data_df['device'].isin(chart_devs)].groupby('device')['pct_util'].quantile(0.99)
        for dev in chart_devs:
            avg_v = util_avg.get(dev, 0)
            max_v = util_p99.get(dev, 0)
            if avg_v > 70:
                flags.append(_flag('red',
                    f'<b>{dev} avg %util {avg_v:.1f}%</b> (p99 peak {max_v:.1f}%) — '
                    f'device is heavily saturated. I/O requests are queuing.'))
            elif avg_v > 40:
                flags.append(_flag('amber',
                    f'<b>{dev} avg %util {avg_v:.1f}%</b> (p99 peak {max_v:.1f}%) — '
                    f'moderate utilisation. Monitor for growth under load.'))

    _BASELINES = 'Baselines: NVMe &lt;1 ms · SAS/SATA SSD &lt;2 ms · SAN/flash array &lt;5 ms · spinning disk &lt;10 ms.'

    if has_rw_await:
        for metric, label in [('r_await', 'read await'), ('w_await', 'write await')]:
            avg_lat = data_df[data_df['device'].isin(chart_devs)].groupby('device')[metric].mean()
            for dev in chart_devs:
                v = avg_lat.get(dev, 0)
                if v > 20:
                    flags.append(_flag('red',
                        f'<b>{dev} avg {label} {v:.1f} ms</b> — severely elevated. '
                        f'{_BASELINES} This device is well outside normal range for any storage type.'))
                elif v > 5:
                    flags.append(_flag('amber',
                        f'<b>{dev} avg {label} {v:.1f} ms</b> — elevated. {_BASELINES}'))
                elif v > 0:
                    flags.append(_flag('green',
                        f'<b>{dev} avg {label} {v:.1f} ms</b> — within normal range. {_BASELINES}'))

    if has_await and not has_rw_await:
        await_avg = data_df[data_df['device'].isin(chart_devs)].groupby('device')['await'].mean()
        for dev in chart_devs:
            v = await_avg.get(dev, 0)
            if v > 20:
                flags.append(_flag('red',
                    f'<b>{dev} avg I/O await {v:.1f} ms</b> — severely elevated. '
                    f'{_BASELINES} This device is well outside normal range for any storage type.'))
            elif v > 5:
                flags.append(_flag('amber',
                    f'<b>{dev} avg I/O await {v:.1f} ms</b> — elevated. {_BASELINES}'))
            elif v > 0:
                flags.append(_flag('green',
                    f'<b>{dev} avg I/O await {v:.1f} ms</b> — within normal range. {_BASELINES}'))

    if has_aqu:
        aqu_avg = data_df[data_df['device'].isin(chart_devs)].groupby('device')['aqu_sz'].mean()
        for dev in chart_devs:
            v = aqu_avg.get(dev, 0)
            if v > 4:
                flags.append(_flag('red',
                    f'<b>{dev} avg queue depth {v:.1f}</b> — deep queue indicates saturation. '
                    f'The device cannot drain requests as fast as they arrive.'))
            elif v > 1:
                flags.append(_flag('amber',
                    f'<b>{dev} avg queue depth {v:.1f}</b> — queue depth &gt;1 means requests '
                    f'are waiting behind each other. Consider checking %util.'))

    if has_read and has_write:
        read_total  = data_df[data_df['device'].isin(chart_devs)]['read_kb_s'].sum()
        write_total = data_df[data_df['device'].isin(chart_devs)]['write_kb_s'].sum()
        total = read_total + write_total
        if total > 0:
            read_pct = read_total / total * 100
            if read_pct > 80:
                flags.append(_flag('info',
                    f'<b>Read-heavy workload</b>: {read_pct:.0f}% of I/O is reads. '
                    f'Buffer cache hit rate (see mgstat) directly impacts this load.'))
            elif read_pct < 20:
                flags.append(_flag('info',
                    f'<b>Write-heavy workload</b>: {100-read_pct:.0f}% of I/O is writes. '
                    f'Journal write performance (WIJwri/PhyWrs in mgstat) is the key metric.'))

    if not flags:
        flags.append(_flag('green', 'No significant disk I/O anomalies detected.'))

    insights_html = '<!--INS-->' + f'''
<div style="margin-bottom:14px">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Insights</div>
  {''.join(flags)}
</div>''' + '<!--/INS-->'

    # ── Summary table (from Average: lines, fall back to computed averages) ──
    if avg_df is not None and not avg_df.empty:
        sum_src = avg_df[avg_df['device'].isin(chart_devs)].copy()
    else:
        agg = {c: 'mean' for c in cols}
        sum_src = (data_df[data_df['device'].isin(chart_devs)]
                   .groupby('device').agg(agg).reset_index())

    sum_cols = [c for c in ['tps', 'read_kb_s', 'write_kb_s', 'await', 'r_await', 'w_await', 'aqu_sz', 'pct_util'] if c in sum_src.columns]
    label_map = {
        'tps': 'tps', 'read_kb_s': 'read kB/s', 'write_kb_s': 'write kB/s',
        'await': 'await ms', 'r_await': 'r_await ms', 'w_await': 'w_await ms',
        'aqu_sz': 'queue', 'pct_util': '%util',
    }

    def _util_color(v):
        if v > 70: return '#dc2626'
        if v > 40: return '#d97706'
        return '#059669'

    th_cells = ''.join(f'<th style="padding:7px 10px;text-align:right">{label_map[c]}</th>' for c in sum_cols)
    sum_rows = ''
    for _, row in sum_src.sort_values('pct_util', ascending=False).iterrows() if 'pct_util' in sum_src.columns else sum_src.iterrows():
        cells = ''.join(
            f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right;'
            f'{"font-weight:700;color:" + _util_color(row[c]) if c == "pct_util" else "color:#334"}">'
            f'{row[c]:.1f}</td>'
            for c in sum_cols if c in row.index
        )
        sum_rows += (f'<tr style="border-bottom:1px solid #f0f2f5">'
                     f'<td style="padding:5px 10px;font-size:0.77rem;font-family:monospace">{row["device"]}</td>'
                     f'{cells}</tr>')

    th_sort_cells = ''.join(
        f'<th onclick="sortTable(\'sard-sum\',{i+1},this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">{label_map[c]} ↕</th>'
        for i, c in enumerate(sum_cols)
    )
    summary_table = f'''
<div style="margin-bottom:14px;overflow-x:auto">
  <table id="sard-sum" style="border-collapse:collapse;width:100%;background:#f8f9fc;
                border:1px solid #dde3ee;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#eef2f7;font-size:0.7rem;color:#667;text-transform:uppercase">
        <th onclick="sortTable('sard-sum',0,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Device ↕</th>
        {th_sort_cells}
      </tr>
    </thead>
    <tbody>{sum_rows}</tbody>
  </table>
  <div style="font-size:0.72rem;color:#aaa;margin-top:5px">
    Averages over all {n_intervals} intervals{"  (from sar Average: line)" if avg_df is not None and not avg_df.empty else ""}.
  </div>
</div>'''

    # ── Charts ────────────────────────────────────────────────────────────────
    COLORS = ['#0055aa', '#e74c3c', '#27ae60', '#e67e22', '#8e44ad',
              '#16a085', '#f39c12', '#2c3e50', '#c0392b', '#2980b9']

    row_defs = []
    ref_lines: dict[int, list[tuple]] = {}  # row_idx -> [(y, color, label)]

    def _line_traces(fig, row, metric, y_label, unit='', devs=chart_devs):
        for ci, dev in enumerate(devs):
            d = data_df[(data_df['device'] == dev) & data_df[metric].notna()].sort_values('dt')
            if d.empty:
                continue
            fig.add_trace(go.Scatter(
                x=d['dt'], y=d[metric], name=dev, mode='lines',
                line=dict(color=COLORS[ci % len(COLORS)], width=1.5),
                legendgroup=dev, showlegend=(row == 1),
                hovertemplate=f'<b>{dev}</b><br>%{{x}}<br>%{{y:.2f}}{unit}<extra></extra>',
            ), row=row, col=1)
        fig.update_yaxes(title_text=y_label, showgrid=True, gridcolor='#e8edf5',
                         rangemode='tozero', row=row, col=1)

    if has_util:
        ref_lines[len(row_defs) + 1] = [
            (70, '#dc2626', 'saturated (70%)'),
            (40, '#d97706', 'elevated (40%)'),
        ]
        row_defs.append(('%util per device', lambda fig, r: _line_traces(fig, r, 'pct_util', '%util', '%')))

    if has_tps:
        row_defs.append(('IOPS (tps)', lambda fig, r: _line_traces(fig, r, 'tps', 'tps')))

    if has_read or has_write:
        def _add_thru(fig, row):
            for ci, dev in enumerate(chart_devs):
                for metric, dash, label in [('read_kb_s', None, 'read'), ('write_kb_s', 'dot', 'write')]:
                    if metric not in cols:
                        continue
                    d = data_df[(data_df['device'] == dev) & data_df[metric].notna()].sort_values('dt')
                    if d.empty:
                        continue
                    fig.add_trace(go.Scatter(
                        x=d['dt'], y=d[metric], name=f'{dev} {label}', mode='lines',
                        line=dict(color=COLORS[ci % len(COLORS)], width=1.2,
                                  **(dict(dash=dash) if dash else {})),
                        legendgroup=f'{dev}_{label}',
                        showlegend=(row == (2 if has_util and has_tps else 1)),
                        hovertemplate=f'<b>{dev} {label}</b><br>%{{x}}<br>%{{y:.0f}} kB/s<extra></extra>',
                    ), row=row, col=1)
            fig.update_yaxes(title_text='kB/s', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=row, col=1)
        row_defs.append(('Throughput kB/s (read solid, write dotted)', _add_thru))

    if has_rw_await:
        def _add_rw_await(fig, row):
            for ci, dev in enumerate(chart_devs):
                for metric, dash, label in [('r_await', None, 'r_await'), ('w_await', 'dot', 'w_await')]:
                    d = data_df[(data_df['device'] == dev) & data_df[metric].notna()].sort_values('dt')
                    if d.empty:
                        continue
                    fig.add_trace(go.Scatter(
                        x=d['dt'], y=d[metric], name=f'{dev} {label}', mode='lines',
                        line=dict(color=COLORS[ci % len(COLORS)], width=1.2,
                                  **(dict(dash=dash) if dash else {})),
                        legendgroup=f'{dev}_{label}', showlegend=True,
                        hovertemplate=f'<b>{dev} {label}</b><br>%{{x}}<br>%{{y:.2f}} ms<extra></extra>',
                    ), row=row, col=1)
            fig.update_yaxes(title_text='ms', showgrid=True, gridcolor='#e8edf5',
                             rangemode='tozero', row=row, col=1)
        ref_lines[len(row_defs) + 1] = [(1, '#64748b', '1 ms reference (SSD/NVMe target)')]
        row_defs.append(('Latency ms (r_await solid, w_await dotted)', _add_rw_await))
    elif has_await:
        ref_lines[len(row_defs) + 1] = [(1, '#64748b', '1 ms reference (SSD/NVMe target)')]
        row_defs.append(('I/O await ms', lambda fig, r: _line_traces(fig, r, 'await', 'ms', ' ms')))

    if has_aqu:
        row_defs.append(('Queue depth (aqu-sz)', lambda fig, r: _line_traces(fig, r, 'aqu_sz', 'queue depth')))

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

    n_phys = len(phys_devs)
    n_dm   = len(dm_devs)
    dev_note = (f'{n_phys} physical device{"s" if n_phys != 1 else ""}'
                + (f' &mdash; {n_dm} dm device(s) hidden from charts' if n_dm else ''))

    return f'''
<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px;
              text-transform:uppercase;letter-spacing:.06em">sar -d Analysis</div>
  <div style="font-size:0.72rem;color:#999;margin-bottom:12px">
    {n_intervals} intervals &nbsp;|&nbsp; {dev_note}
  </div>
  {insights_html}
  {summary_table}
  {chart_html}
</div>
'''
