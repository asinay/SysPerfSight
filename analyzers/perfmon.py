"""
Analyzer for the 'perfmon' section in Windows SystemPerformance files.

Expected format: PDH-CSV export from logman.
  Header: "(PDH-CSV 4.0) (timezone)(offset)","\\\\HOST\\Object(instance)\\counter",...
  Data:   "MM/DD/YYYY HH:MM:SS.mmm"," value",...

Returns '' when logman failed to collect data (access denied, etc.).
"""
import io
import re
import csv
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_PDH_RE = re.compile(r'\bPDH-CSV\b', re.IGNORECASE)

# (object_substring, counter_substring) → logical column name.
# Object match is against the object+instance part (e.g. "processor(_total)").
# First match wins; _Total instances are preferred over specific ones.
_COUNTER_MAP = [
    ('processor',        '% processor time',          'cpu_total'),
    ('processor',        '% privileged time',         'cpu_kernel'),
    ('processor',        '% user time',               'cpu_user'),
    ('processor',        '% interrupt time',          'cpu_interrupt'),
    ('memory',           'available mbytes',          'mem_avail'),
    ('memory',           'pages/sec',                 'mem_pages'),
    ('memory',           '% committed bytes in use',  'mem_commit_pct'),
    ('physicaldisk',     '% disk time',               'disk_busy'),
    ('physicaldisk',     'disk reads/sec',            'disk_rps'),
    ('physicaldisk',     'disk writes/sec',           'disk_wps'),
    ('physicaldisk',     'disk read bytes/sec',       'disk_rbytes'),
    ('physicaldisk',     'disk write bytes/sec',      'disk_wbytes'),
    ('physicaldisk',     'avg. disk queue length',    'disk_queue'),
    ('physicaldisk',     'avg. disk sec/read',        'disk_rlatency'),
    ('physicaldisk',     'avg. disk sec/write',       'disk_wlatency'),
    ('system',           'processor queue length',    'proc_queue'),
    ('network interface', 'bytes total/sec',          'net_bytes'),
    ('network interface', 'bytes sent/sec',           'net_sent'),
    ('network interface', 'bytes received/sec',       'net_recv'),
]


def _match_counter(path: str) -> tuple[str, str] | None:
    """Map a PDH counter path to (logical_name, instance).

    instance is '' for system-wide counters (CPU, memory, processor queue,
    network) and the raw PDH instance string (e.g. '1 d:') for physicaldisk
    counters, so each physical disk keeps its own series instead of being
    collapsed into one.
    """
    # Path: \\HOSTNAME\Object(Instance)\Counter  or  \\HOSTNAME\Object\Counter
    parts = [p for p in path.split('\\') if p]
    if len(parts) < 2:
        return None
    obj_part = parts[-2]
    counter  = parts[-1].lower()   # e.g. "% processor time"
    m = re.match(r'([^(]+)(?:\((.*)\))?', obj_part)
    if not m:
        return None
    obj_lower = m.group(1).strip().lower()
    instance  = (m.group(2) or '').strip()
    for obj_sub, ctr_sub, logical in _COUNTER_MAP:
        if obj_sub in obj_lower and ctr_sub in counter:
            return logical, (instance if logical.startswith('disk_') else '')
    return None


def _parse_perfmon(text: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Returns (flat_df, disk_df).

    flat_df: one row per timestamp, one column per system-wide counter
             (cpu_*, mem_*, proc_queue, net_*).
    disk_df: long format — columns dt/instance/metric/value — one row per
             (timestamp, physical disk, counter), so per-drive breakdowns
             and IRIS-drive cross-referencing stay possible.
    """
    lines = text.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if _PDH_RE.search(ln)), None)
    if header_idx is None:
        return None, None

    try:
        reader = csv.reader(io.StringIO('\n'.join(lines[header_idx:])))
        headers = next(reader)
    except Exception:
        return None, None

    if not headers:
        return None, None

    flat_assigned: dict[str, int] = {}          # logical -> csv_col_index (prefer _Total)
    disk_cols: list[tuple[int, str, str]] = []  # (csv_col_index, logical, instance)
    for i, h in enumerate(headers[1:], start=1):
        matched = _match_counter(h)
        if matched is None:
            continue
        logical, instance = matched
        if instance:
            disk_cols.append((i, logical, instance))
        else:
            is_total = '_total' in h.lower()
            if logical not in flat_assigned or is_total:
                flat_assigned[logical] = i

    idx_to_flat = {v: k for k, v in flat_assigned.items()}
    if not idx_to_flat and not disk_cols:
        return None, None

    flat_records, disk_records = [], []
    for row in reader:
        if not row or not row[0].strip():
            continue
        ts_raw = row[0].strip()
        for fmt in ('%m/%d/%Y %H:%M:%S.%f', '%m/%d/%Y %H:%M:%S'):
            try:
                ts = pd.to_datetime(ts_raw, format=fmt)
                break
            except Exception:
                pass
        else:
            continue

        rec: dict = {'dt': ts}
        for col_idx, logical in idx_to_flat.items():
            if col_idx < len(row):
                try:
                    rec[logical] = float(row[col_idx].strip())
                except (ValueError, AttributeError):
                    pass
        flat_records.append(rec)

        for col_idx, logical, instance in disk_cols:
            if col_idx >= len(row):
                continue
            try:
                v = float(row[col_idx].strip())
            except (ValueError, AttributeError):
                continue
            disk_records.append({'dt': ts, 'instance': instance, 'metric': logical, 'value': v})

    flat_df = None
    if len(flat_records) >= 2:
        flat_df = pd.DataFrame(flat_records).sort_values('dt').reset_index(drop=True)
        if len(flat_df) > 1000:
            step = len(flat_df) // 1000
            flat_df = flat_df.iloc[::step].reset_index(drop=True)

    disk_df = pd.DataFrame(disk_records) if disk_records else None
    if disk_df is not None and disk_df['dt'].nunique() > 1000:
        keep_ts = sorted(disk_df['dt'].unique())[::len(disk_df['dt'].unique()) // 1000]
        disk_df = disk_df[disk_df['dt'].isin(keep_ts)].reset_index(drop=True)

    return flat_df, disk_df


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
    df, disk_df = _parse_perfmon(section_text)
    if (df is None or df.empty) and (disk_df is None or disk_df.empty):
        return ''

    n     = len(df) if df is not None else disk_df['dt'].nunique()
    dcols = set(df.columns) if df is not None else set()
    has   = lambda c: c in dcols  # noqa: E731

    disk_instances = sorted(disk_df['instance'].unique()) if disk_df is not None else []
    phys_disks  = [d for d in disk_instances if d.lower() != '_total']
    chart_disks = phys_disks if phys_disks else disk_instances
    disk_metrics = set(disk_df['metric'].unique()) if disk_df is not None else set()

    # ── Insights ─────────────────────────────────────────────────────────────
    flags = []

    if has('cpu_total'):
        avg = df['cpu_total'].mean()
        pk  = df['cpu_total'].quantile(0.99)
        if avg > 80:
            flags.append(_flag('red',
                f'<b>CPU saturation</b>: avg {avg:.1f}%, p99 peak {pk:.1f}% — '
                f'processor is a bottleneck. Check Process\\% Processor Time for top consumers.'))
        elif avg > 60:
            flags.append(_flag('amber',
                f'<b>Elevated CPU utilization</b>: avg {avg:.1f}%, p99 peak {pk:.1f}%.'))

    if has('proc_queue'):
        avg = df['proc_queue'].mean()
        pk  = df['proc_queue'].quantile(0.99)
        if avg > 4:
            flags.append(_flag('red',
                f'<b>Processor queue backed up</b>: avg {avg:.1f}, p99 peak {pk:.0f} — '
                f'threads are waiting for CPU. Indicates CPU saturation.'))
        elif avg > 2:
            flags.append(_flag('amber',
                f'<b>Processor queue elevated</b>: avg {avg:.1f}, p99 peak {pk:.0f}.'))

    if has('mem_avail'):
        min_avail = df['mem_avail'].min()
        avg_avail = df['mem_avail'].mean()
        if min_avail < 500:
            flags.append(_flag('red',
                f'<b>Low available memory</b>: min {min_avail:.0f} MB — '
                f'risk of excessive paging. Consider increasing RAM or IRIS global buffer settings.'))
        elif min_avail < 1024:
            flags.append(_flag('amber',
                f'<b>Available memory tight</b>: min {min_avail:.0f} MB, avg {avg_avail:.0f} MB.'))

    if has('mem_pages'):
        avg = df['mem_pages'].mean()
        pk  = df['mem_pages'].quantile(0.99)
        if avg > 100:
            flags.append(_flag('red',
                f'<b>High page fault rate</b>: avg {avg:.0f} pages/sec, p99 peak {pk:.0f} — '
                f'system is paging heavily. Memory pressure confirmed.'))
        elif avg > 20:
            flags.append(_flag('amber',
                f'<b>Elevated paging</b>: avg {avg:.0f} pages/sec.'))

    def _disk_series(metric: str, dev: str) -> pd.Series:
        return disk_df[(disk_df['instance'] == dev) & (disk_df['metric'] == metric)]['value']

    if 'disk_busy' in disk_metrics:
        for dev in chart_disks:
            s = _disk_series('disk_busy', dev)
            if s.empty:
                continue
            avg, pk = s.mean(), s.quantile(0.99)
            if avg > 60:
                flags.append(_flag('red',
                    f'<b>{dev} %Disk Time avg {avg:.1f}%</b> (p99 peak {pk:.1f}%) — '
                    f'drive is heavily utilised. Concurrent I/O may queue behind it.'))
            elif avg > 30:
                flags.append(_flag('amber',
                    f'<b>{dev} %Disk Time avg {avg:.1f}%</b> (p99 peak {pk:.1f}%) — '
                    f'moderate utilisation. Monitor under heavier workload.'))

    if 'disk_queue' in disk_metrics:
        for dev in chart_disks:
            s = _disk_series('disk_queue', dev)
            if s.empty:
                continue
            avg, pk = s.mean(), s.quantile(0.99)
            if avg > 2:
                flags.append(_flag('red',
                    f'<b>{dev} queue saturation</b>: avg {avg:.1f}, p99 peak {pk:.1f} — '
                    f'I/O is queuing up. Storage cannot keep pace with demand.'))
            elif avg > 1:
                flags.append(_flag('amber',
                    f'<b>{dev} queue elevated</b>: avg {avg:.1f}, p99 peak {pk:.1f}.'))

    for metric, label in [('disk_rlatency', 'read latency'), ('disk_wlatency', 'write latency')]:
        if metric not in disk_metrics:
            continue
        for dev in chart_disks:
            s = _disk_series(metric, dev) * 1000
            if s.empty:
                continue
            avg_ms, pk_ms = s.mean(), s.quantile(0.99)
            if avg_ms > 20:
                flags.append(_flag('red',
                    f'<b>{dev} {label} avg {avg_ms:.1f} ms</b> (p99 peak {pk_ms:.1f} ms) — '
                    f'storage response is very slow.'))
            elif avg_ms > 10:
                flags.append(_flag('amber',
                    f'<b>{dev} {label} avg {avg_ms:.1f} ms</b> (p99 peak {pk_ms:.1f} ms).'))

    if not flags:
        flags.append(_flag('green', 'No significant CPU, memory, or disk pressure detected.'))

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
    if has('cpu_total'):
        stat_items.append(_stat('CPU avg', f'{df["cpu_total"].mean():.1f}', '%'))
        stat_items.append(_stat('CPU peak', f'{df["cpu_total"].max():.1f}', '%'))
    if has('mem_avail'):
        stat_items.append(_stat('Avail RAM min', f'{df["mem_avail"].min():.0f}', 'MB'))
    if has('mem_commit_pct'):
        stat_items.append(_stat('Mem commit avg', f'{df["mem_commit_pct"].mean():.1f}', '%'))
    if 'disk_queue' in disk_metrics:
        stat_items.append(_stat('Disk queue avg (all drives)', f'{disk_df[disk_df["metric"]=="disk_queue"]["value"].mean():.2f}'))
    if 'disk_rlatency' in disk_metrics:
        stat_items.append(_stat('Read latency avg (all drives)', f'{disk_df[disk_df["metric"]=="disk_rlatency"]["value"].mean()*1000:.1f}', 'ms'))
    if 'disk_wlatency' in disk_metrics:
        stat_items.append(_stat('Write latency avg (all drives)', f'{disk_df[disk_df["metric"]=="disk_wlatency"]["value"].mean()*1000:.1f}', 'ms'))

    stats_html = (
        '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">'
        + ''.join(stat_items) + '</div>'
    ) if stat_items else ''

    # ── Charts ────────────────────────────────────────────────────────────────
    row_defs = []  # (title, fn(fig, row))
    ref_lines: dict[int, list[tuple]] = {}  # row_idx -> [(y, color, label)]
    COLORS = ['#0055aa', '#e74c3c', '#27ae60', '#e67e22', '#8e44ad',
              '#16a085', '#f39c12', '#2c3e50', '#c0392b', '#2980b9']

    def _flat_row(traces, ylabel):
        def fn(fig, row):
            for col, color, name, transform in traces:
                y = df[col].apply(transform) if transform else df[col]
                fig.add_trace(go.Scatter(
                    x=df['dt'], y=y, name=name, mode='lines',
                    line=dict(color=color, width=1.5),
                    hovertemplate=f'<b>{name}</b><br>%{{x}}<br>%{{y:.2f}}<extra></extra>',
                ), row=row, col=1)
            if ylabel:
                fig.update_yaxes(title_text=ylabel, row=row, col=1)
            fig.update_yaxes(showgrid=True, gridcolor='#e8edf5', rangemode='tozero', row=row, col=1)
        return fn

    def _disk_row(metric_specs, ylabel):
        """metric_specs: list of (metric, dash_or_None, label_suffix, transform_fn)."""
        def fn(fig, row):
            for ci, dev in enumerate(chart_disks):
                for metric, dash, suffix, transform in metric_specs:
                    d = disk_df[(disk_df['instance'] == dev) & (disk_df['metric'] == metric)].sort_values('dt')
                    if d.empty:
                        continue
                    y = d['value'].apply(transform) if transform else d['value']
                    fig.add_trace(go.Scatter(
                        x=d['dt'], y=y, name=f'{dev} {suffix}', mode='lines',
                        line=dict(color=COLORS[ci % len(COLORS)], width=1.2,
                                  **(dict(dash=dash) if dash else {})),
                        legendgroup=f'{dev}_{metric}', showlegend=True,
                        hovertemplate=f'<b>{dev} {suffix}</b><br>%{{x}}<br>%{{y:.2f}}<extra></extra>',
                    ), row=row, col=1)
            if ylabel:
                fig.update_yaxes(title_text=ylabel, row=row, col=1)
            fig.update_yaxes(showgrid=True, gridcolor='#e8edf5', rangemode='tozero', row=row, col=1)
        return fn

    # CPU: single % Processor Time or stacked user+kernel if available
    cpu_traces = []
    if has('cpu_total'):
        cpu_traces.append(('cpu_total',   '#0055aa', '% Processor Time', None))
    if has('cpu_user'):
        cpu_traces.append(('cpu_user',    '#27ae60', '% User Time', None))
    if has('cpu_kernel'):
        cpu_traces.append(('cpu_kernel',  '#e67e22', '% Privileged Time', None))
    if has('cpu_interrupt'):
        cpu_traces.append(('cpu_interrupt', '#e74c3c', '% Interrupt Time', None))
    if cpu_traces:
        if has('cpu_total'):
            ref_lines[len(row_defs) + 1] = [(80, '#d97706', '80% busy')]
        row_defs.append(('CPU Utilization (%)', _flat_row(cpu_traces, '%')))

    if has('proc_queue'):
        ref_lines[len(row_defs) + 1] = [
            (4, '#dc2626', 'queue backed up (4)'),
            (2, '#d97706', 'elevated queue (2)'),
        ]
        row_defs.append(('Processor Queue Length', _flat_row(
            [('proc_queue', '#e74c3c', 'Processor Queue Length', None)], 'threads')))

    # Memory
    mem_traces = []
    if has('mem_avail'):
        mem_traces.append(('mem_avail', '#27ae60', 'Available MBytes', None))
    if mem_traces:
        row_defs.append(('Memory — Available MBytes', _flat_row(mem_traces, 'MB')))

    if has('mem_pages'):
        row_defs.append(('Paging Activity (pages/sec)', _flat_row(
            [('mem_pages', '#e74c3c', 'Pages/sec', None)], 'pages/sec')))

    # Disk rows — one trace per physical drive per metric, not collapsed
    if disk_df is not None:
        if 'disk_busy' in disk_metrics or 'disk_queue' in disk_metrics:
            specs = [m for m in [
                ('disk_busy', None, '%busy', None),
                ('disk_queue', 'dot', 'queue', None),
            ] if m[0] in disk_metrics]
            ref_lines[len(row_defs) + 1] = [(2, '#dc2626', 'queue saturated (2)')]
            row_defs.append(('Disk — % Busy & Queue Length (per drive)', _disk_row(specs, '')))

        if 'disk_rps' in disk_metrics or 'disk_wps' in disk_metrics:
            specs = [m for m in [
                ('disk_rps', None, 'reads/s', None),
                ('disk_wps', 'dot', 'writes/s', None),
            ] if m[0] in disk_metrics]
            row_defs.append(('Disk IOPS (per drive)', _disk_row(specs, 'ops/sec')))

        if 'disk_rbytes' in disk_metrics or 'disk_wbytes' in disk_metrics:
            specs = [m for m in [
                ('disk_rbytes', None, 'read MB/s', lambda v: v / 1048576),
                ('disk_wbytes', 'dot', 'write MB/s', lambda v: v / 1048576),
            ] if m[0] in disk_metrics]
            row_defs.append(('Disk Throughput (per drive)', _disk_row(specs, 'MB/s')))

        if 'disk_rlatency' in disk_metrics or 'disk_wlatency' in disk_metrics:
            specs = [m for m in [
                ('disk_rlatency', None, 'read ms', lambda v: v * 1000),
                ('disk_wlatency', 'dot', 'write ms', lambda v: v * 1000),
            ] if m[0] in disk_metrics]
            ref_lines[len(row_defs) + 1] = [(1, '#64748b', '1 ms reference (SSD/NVMe target)')]
            row_defs.append(('Disk Latency (per drive)', _disk_row(specs, 'ms')))

    # Network
    net_traces = []
    if has('net_bytes'):
        net_traces.append(('net_bytes', '#2980b9', 'Total Bytes/sec', None))
    elif has('net_sent') or has('net_recv'):
        if has('net_sent'):
            net_traces.append(('net_sent', '#27ae60', 'Sent Bytes/sec', None))
        if has('net_recv'):
            net_traces.append(('net_recv', '#e74c3c', 'Recv Bytes/sec', None))
    if net_traces:
        row_defs.append(('Network Throughput (bytes/sec)', _flat_row(net_traces, 'bytes/sec')))

    if not row_defs:
        return ''

    nrows = len(row_defs)
    fig = make_subplots(
        rows=nrows, cols=1,
        subplot_titles=[rd[0] for rd in row_defs],
        vertical_spacing=0.06 if nrows > 3 else 0.10,
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
        height=max(280 * nrows, 400),
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor='white',
        plot_bgcolor='#f8f9fc',
        font=dict(family='-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', size=10),
        legend=dict(orientation='h', x=0, y=-0.03, font_size=10),
        hovermode='x unified',
    )
    fig.update_xaxes(showgrid=True, gridcolor='#e8edf5', tickangle=-30)

    chart_html = fig.to_html(
        full_html=False, include_plotlyjs=False,
        config={'displayModeBar': True, 'displaylogo': False,
                'modeBarButtonsToRemove': ['select2d', 'lasso2d']},
    )

    dt_series = df['dt'] if df is not None else disk_df['dt']
    duration = dt_series.max() - dt_series.min()
    h, rem = divmod(int(duration.total_seconds()), 3600)
    duration_str = f'{h}h {rem // 60}m' if h else f'{rem // 60}m'

    return (
        '<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif">'
        '<div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px;'
        'text-transform:uppercase;letter-spacing:.06em">perfmon Analysis</div>'
        f'<div style="font-size:0.72rem;color:#999;margin-bottom:12px">{n} samples · {duration_str}</div>'
        + insights_html
        + stats_html
        + chart_html
        + '</div>'
    )
