"""
Analyzer for the '%SS' (IRIS System Status) section in SystemPerformance files.

Format notes (learned from real data):
- Each snapshot starts with '<product> System Status: <time>' — product prefix varies
  ('InterSystems IRIS' on IRIS, 'Cache' on older Caché); only the 'System Status:' suffix is matched
- Column header: ' Process  Device      Namespace      Routine         CPU,Glob  Pr User/Location'
- Fixed cols (0-based): pid=0-9, device=10-21, ns=22-36, routine=37-52, cpu_glob=53-62, pr=63-65, user=66+
- CPU and Glob are CUMULATIVE totals since process start — deltas between snapshots are the rates
- Entries can span 1-3 lines when device, routine, or cpu,glob overflow their columns
- Entry starts when a line has a digit within the first 8 columns (pid field)
- Summary line '739 user, 26 system ...' ends the process list
"""
import re
from collections import Counter
from datetime import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_SNAP_HDR_RE = re.compile(
    r'System Status:\s+([\d:]+\s*[ap]m\s+\d+\s+\w+\s+\d{4})',
    re.IGNORECASE,
)
_COL_HDR_RE = re.compile(
    r'^\s*Process\s+Device\s+Namespace\s+Routine\s+CPU',
    re.IGNORECASE | re.MULTILINE,
)
_SUMMARY_RE = re.compile(r'^\s*\d+\s+user,\s*\d+\s+system', re.IGNORECASE)
_ENTRY_START_RE = re.compile(r'^\s{0,8}\d')


TYPE_COLORS = {
    'Client (TCP)':  '#0055aa',
    'System (%SYS)': '#7c3aed',
    'Background':    '#64748b',
    'Application':   '#059669',
}


def _parse_ts(s: str) -> datetime | None:
    s = re.sub(r'\s+', ' ', s.strip())
    s = re.sub(r'\b(am|pm)\b', lambda m: m.group().upper(), s, flags=re.IGNORECASE)
    for fmt in ('%I:%M %p %d %b %Y', '%H:%M %d %b %Y'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _snap_label(ts: datetime | None, idx: int) -> str:
    if ts is None:
        return f'Snap {idx + 1}'
    h = ts.strftime('%I').lstrip('0') or '12'
    return h + ts.strftime(':%M %p')


def _extract_proc(group: list[str]) -> dict | None:
    """
    Extract one process record from a group of 1-3 lines.
    Strategy: use cpu,glob as the primary anchor, then infer fields
    from column positions and surrounding context.
    """
    first = group[0]
    all_text = '\n'.join(group)

    # PID
    pid_m = re.match(r'^\s{0,8}(\d+)', first)
    if not pid_m:
        return None
    pid = int(pid_m.group(1))

    # cpu,glob anchor — the two large numbers separated by comma
    # Take the LAST match (avoids grabbing port numbers like 18088 or 1972)
    cpu_glob_matches = list(re.finditer(r'(\d+),(\d+)', all_text))
    if not cpu_glob_matches:
        return None
    best = cpu_glob_matches[-1]
    cpu_total  = int(best.group(1))
    glob_total = int(best.group(2))

    # pr and user: everything after cpu,glob on the same logical line
    after = re.sub(r'\s+', ' ', all_text[best.end():]).strip()
    pr_m = re.match(r'(-?\d+)', after)
    pr   = int(pr_m.group(1)) if pr_m else None
    user = after[pr_m.end():].strip() if pr_m else ''

    # Device, Namespace, Routine: find the source line that contains ns/routine
    # (col 22+ must have non-numeric content that isn't a cpu,glob continuation)
    device = ns = routine = ''

    # The line that carries ns/routine is whichever has non-empty content at col 37
    # and that content doesn't look like a pure-numeric cpu,glob continuation
    src_line = None
    for line in group:
        if len(line) > 37:
            content_at_37 = line[37:53].strip()
            # Skip lines that are purely a cpu,glob continuation (starts with spaces then digits)
            if content_at_37 and not re.match(r'^\d+,', content_at_37):
                src_line = line
                break
    if src_line is None:
        src_line = first

    # Device: col 10 on FIRST line, up to the first 2-space gap
    device_raw = first[10:22].strip() if len(first) > 10 else ''
    # If the device overflows past col 22 on the first line, use the full token
    if len(first) > 22 and first[10:22].strip() and not first[22:23].isspace():
        # device is running long — grab until double space
        m = re.search(r'\S.*?(?=\s{2,}|$)', first[10:])
        device = m.group(0).strip() if m else device_raw
        # In overflow case src_line for ns/routine is a continuation, not first
        src_line = None
        for line in group[1:]:
            if len(line) > 37 and line[22:37].strip():
                src_line = line
                break
        if src_line is None:
            src_line = first
    else:
        device = device_raw

    ns = src_line[22:37].strip() if len(src_line) > 22 else ''
    # Routine runs from col 37 to wherever cpu,glob starts on this line.
    # If cpu,glob is on a continuation line, take the full remainder.
    rtn_field = src_line[37:] if len(src_line) > 37 else ''
    cpu_glob_on_src = re.search(r'\d+,\d+', rtn_field)
    if cpu_glob_on_src:
        rtn_raw = rtn_field[:cpu_glob_on_src.start()]
    else:
        rtn_raw = rtn_field
    # Strip any trailing numeric overflow (e.g. line wrapping artefacts)
    routine = re.sub(r'\d+$', '', rtn_raw).strip()

    # |TCP| detection — scan all lines
    is_tcp = '|TCP|' in all_text.upper() or '|TCP|' in device.upper()

    return {
        'pid':    pid,
        'device': device,
        'ns':     ns,
        'routine': routine,
        'cpu':    cpu_total,
        'glob':   glob_total,
        'pr':     pr,
        'user':   user,
        'is_tcp': is_tcp,
        'type':   _proc_type(device, ns, is_tcp),
    }


def _proc_type(device: str, ns: str, is_tcp: bool) -> str:
    if is_tcp:
        return 'Client (TCP)'
    if ns.upper() == '%SYS':
        return 'System (%SYS)'
    if not device.strip() or device.strip() in ('0', '-'):
        return 'Background'
    return 'Application'


def _parse_snapshots(text: str) -> list[dict]:
    snap_matches = list(_SNAP_HDR_RE.finditer(text))
    if not snap_matches:
        return []

    snapshots = []
    for i, m in enumerate(snap_matches):
        end = snap_matches[i + 1].start() if i + 1 < len(snap_matches) else len(text)
        block = text[m.start():end]

        # Skip past the column header line
        col_m = _COL_HDR_RE.search(block)
        if not col_m:
            continue
        body_start = block.find('\n', col_m.start())
        if body_start < 0:
            continue
        body = block[body_start + 1:]

        # Group lines into per-process entries
        lines = body.splitlines()
        groups: list[list[str]] = []
        for line in lines:
            if _SUMMARY_RE.match(line):
                break
            if _ENTRY_START_RE.match(line):
                groups.append([line])
            elif groups:
                groups[-1].append(line)

        processes = [p for g in groups if (p := _extract_proc(g)) is not None]

        # Summary line stats
        summary_m = re.search(r'(\d+)\s+user,\s*(\d+)\s+system', block, re.IGNORECASE)
        n_user   = int(summary_m.group(1)) if summary_m else None
        n_system = int(summary_m.group(2)) if summary_m else None

        snapshots.append({
            'ts':       _parse_ts(m.group(1)),
            'ts_str':   m.group(1).strip(),
            'processes': processes,
            'n_user':   n_user,
            'n_system': n_system,
        })

    return snapshots


# ── Shared helpers ────────────────────────────────────────────────────────────

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


def _stat(label, value, unit=''):
    return (f'<div style="background:#f8f9fc;border:1px solid #dde3ee;border-radius:6px;'
            f'padding:8px 14px;min-width:110px;flex:1">'
            f'<div style="font-size:0.7rem;color:#777;text-transform:uppercase;letter-spacing:.05em">{label}</div>'
            f'<div style="font-size:1.1rem;font-weight:700;color:#003366">{value}'
            f'<span style="font-size:0.7rem;font-weight:400;color:#888;margin-left:3px">{unit}</span>'
            f'</div></div>')


# ── Main analyzer ─────────────────────────────────────────────────────────────

async def analyze(section_text: str) -> str:
    snapshots = _parse_snapshots(section_text)
    snapshots = [s for s in snapshots if s['processes']]
    if not snapshots:
        return ''

    n_snaps = len(snapshots)

    # ── Flat DataFrame of all processes across all snapshots ──────────────────
    records = []
    for idx, snap in enumerate(snapshots):
        label = _snap_label(snap['ts'], idx)
        for p in snap['processes']:
            records.append({'snap_idx': idx, 'snap_label': label, **p})
    df = pd.DataFrame(records)

    # ── Per-snapshot counts (from summary line if available) ──────────────────
    proc_counts = [len(s['processes']) for s in snapshots]
    user_counts = [s['n_user']   if s['n_user']   is not None else sum(1 for p in s['processes'] if p['type'] == 'Client (TCP)') for s in snapshots]
    sys_counts  = [s['n_system'] if s['n_system'] is not None else sum(1 for p in s['processes'] if p['type'] == 'System (%SYS)') for s in snapshots]
    tcp_counts  = [sum(1 for p in s['processes'] if p['is_tcp']) for s in snapshots]
    snap_labels = [_snap_label(s['ts'], i) for i, s in enumerate(snapshots)]

    avg_proc  = sum(proc_counts) / n_snaps
    avg_tcp   = sum(tcp_counts)  / n_snaps

    # ── Delta analysis across snapshots (cpu/glob are cumulative) ─────────────
    # For each PID seen in consecutive snapshots, compute the delta
    delta_records = []
    if n_snaps > 1:
        for snap_idx in range(1, n_snaps):
            prev_procs = {p['pid']: p for p in snapshots[snap_idx - 1]['processes']}
            curr_procs = {p['pid']: p for p in snapshots[snap_idx    ]['processes']}
            for pid, curr in curr_procs.items():
                if pid in prev_procs:
                    prev = prev_procs[pid]
                    delta_cpu  = curr['cpu']  - prev['cpu']
                    delta_glob = curr['glob'] - prev['glob']
                    if delta_cpu >= 0 and delta_glob >= 0:
                        delta_records.append({
                            'pid':     pid,
                            'routine': curr['routine'] or prev['routine'],
                            'ns':      curr['ns']      or prev['ns'],
                            'type':    curr['type'],
                            'is_tcp':  curr['is_tcp'],
                            'snap_idx': snap_idx,
                            'snap_label': snap_labels[snap_idx],
                            'delta_cpu':  delta_cpu,
                            'delta_glob': delta_glob,
                        })
    delta_df = pd.DataFrame(delta_records) if delta_records else None

    # Use deltas for top-consumers if available, otherwise fall back to totals
    if delta_df is not None and not delta_df.empty:
        top_cpu_src  = delta_df.groupby(['pid','routine','ns','type'])[['delta_cpu','delta_glob']].sum().reset_index()
        top_cpu_src  = top_cpu_src.rename(columns={'delta_cpu':'cpu_val','delta_glob':'glob_val'})
        value_label  = 'CPU ticks (delta)'
        glob_label   = 'Glob refs (delta)'
    else:
        top_cpu_src = df.groupby(['pid','routine','ns','type'])[['cpu','glob']].max().reset_index()
        top_cpu_src = top_cpu_src.rename(columns={'cpu':'cpu_val','glob':'glob_val'})
        value_label  = 'CPU ticks (cumulative)'
        glob_label   = 'Glob refs (cumulative)'

    top_by_cpu  = top_cpu_src.sort_values('cpu_val',  ascending=False).head(10)
    top_by_glob = top_cpu_src.sort_values('glob_val', ascending=False).head(10)

    top_cpu_row  = top_by_cpu.iloc[0]  if not top_by_cpu.empty  else None
    top_glob_row = top_by_glob.iloc[0] if not top_by_glob.empty else None

    top_cpu_label  = f"{top_cpu_row['routine'] or top_cpu_row['ns']} ({top_cpu_row['cpu_val']:,.0f})"   if top_cpu_row  is not None else 'N/A'
    top_glob_label = f"{top_glob_row['routine'] or top_glob_row['ns']} ({top_glob_row['glob_val']:,.0f})" if top_glob_row is not None else 'N/A'

    # ── Summary cards ─────────────────────────────────────────────────────────
    stats_html = (
        _stat('Snapshots', str(n_snaps)) +
        _stat('Avg processes', f'{avg_proc:.0f}') +
        _stat('Avg TCP clients', f'{avg_tcp:.0f}') +
        _stat('Peak count', str(max(proc_counts))) +
        _stat('Top CPU', (top_cpu_label[:26] + '…') if len(top_cpu_label) > 26 else top_cpu_label) +
        _stat('Top Glob', (top_glob_label[:26] + '…') if len(top_glob_label) > 26 else top_glob_label)
    )

    # ── Insights ──────────────────────────────────────────────────────────────
    flags = []

    # Top CPU consumer across deltas
    if top_cpu_row is not None and top_cpu_row['cpu_val'] > 0:
        rtn = top_cpu_row['routine'] or top_cpu_row['ns']
        ns  = top_cpu_row['ns']
        pid_rows = df[df['pid'] == top_cpu_row['pid']]
        seen_in  = pid_rows['snap_idx'].nunique()
        level = 'red' if top_cpu_row['cpu_val'] > 5_000_000 else 'amber' if top_cpu_row['cpu_val'] > 500_000 else 'info'
        flags.append(_flag(level,
            f'<b>Top CPU process: {rtn} (PID {int(top_cpu_row["pid"])}, ns {ns})</b> — '
            f'{top_cpu_row["cpu_val"]:,.0f} CPU ticks {"delta across snapshots" if delta_df is not None and not delta_df.empty else "cumulative"}. '
            f'Seen in {seen_in}/{n_snaps} snapshots.'))

    # TCP connection growth
    if n_snaps > 1 and tcp_counts[-1] != tcp_counts[0]:
        delta_tcp = tcp_counts[-1] - tcp_counts[0]
        if delta_tcp > 0:
            flags.append(_flag('amber',
                f'<b>TCP connections grew by {delta_tcp}</b> during capture '
                f'({tcp_counts[0]} → {tcp_counts[-1]}). '
                f'Sustained growth may indicate a connection leak or rising load.'))
        else:
            flags.append(_flag('info',
                f'<b>TCP connections fell by {abs(delta_tcp)}</b> during capture '
                f'({tcp_counts[0]} → {tcp_counts[-1]}).'))

    # High absolute TCP count
    if avg_tcp > 100:
        flags.append(_flag('info',
            f'<b>{avg_tcp:.0f} average TCP client connections</b> — high concurrency load.'))

    # Persistent high-delta-cpu processes (across ALL snapshot pairs)
    if delta_df is not None and not delta_df.empty:
        pid_snaps = df.groupby('pid')['snap_idx'].nunique()
        persistent = pid_snaps[pid_snaps == n_snaps].index
        persistent_delta = delta_df[delta_df['pid'].isin(persistent)]
        if not persistent_delta.empty:
            avg_delta_cpu = persistent_delta.groupby('pid')['delta_cpu'].mean()
            high = avg_delta_cpu[avg_delta_cpu > 1_000_000].sort_values(ascending=False).head(3)
            for pid, val in high.items():
                rtn = df[df['pid'] == pid]['routine'].iloc[0]
                ns  = df[df['pid'] == pid]['ns'].iloc[0]
                if top_cpu_row is not None and pid == top_cpu_row['pid']:
                    continue  # already flagged above
                flags.append(_flag('amber',
                    f'<b>Persistent CPU consumer: {rtn or ns} (PID {pid})</b> — '
                    f'avg {val:,.0f} CPU ticks/interval across all {n_snaps} snapshots.'))

    # %SYS overhead share
    if delta_df is not None and not delta_df.empty:
        total_cpu   = delta_df['delta_cpu'].sum()
        sys_cpu     = delta_df[delta_df['type'] == 'System (%SYS)']['delta_cpu'].sum()
        if total_cpu > 0 and sys_cpu / total_cpu > 0.4:
            flags.append(_flag('amber',
                f'<b>System (%SYS) processes account for {sys_cpu/total_cpu*100:.0f}% of CPU delta.</b> '
                f'High system overhead may indicate background maintenance, mirroring, or CSP gateway activity.'))

    # Top glob namespace
    if delta_df is not None and not delta_df.empty and not delta_df['delta_glob'].empty:
        top_ns = delta_df.groupby('ns')['delta_glob'].sum().sort_values(ascending=False)
        if not top_ns.empty and top_ns.index[0]:
            flags.append(_flag('info',
                f'<b>Highest global reference namespace: {top_ns.index[0]}</b> '
                f'({top_ns.iloc[0]:,.0f} glob delta) — most database I/O originates here.'))

    if not flags:
        flags.append(_flag('green', 'No significant anomalies detected across these snapshots.'))

    insights_html = '<!--INS-->' + f'''
<div style="margin-bottom:14px">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Insights</div>
  {''.join(flags)}
</div>''' + '<!--/INS-->'

    # ── Charts ────────────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=(
            'Process count by type per snapshot',
            'TCP client count trend',
            f'Top 10 by {value_label}',
            f'Top 10 by {glob_label}',
        ),
        vertical_spacing=0.20,
        horizontal_spacing=0.12,
    )

    # Row 1 Col 1: Stacked bar — process types per snapshot
    for ptype, color in TYPE_COLORS.items():
        counts = [sum(1 for p in s['processes'] if p['type'] == ptype) for s in snapshots]
        if not any(counts):
            continue
        fig.add_trace(go.Bar(
            x=snap_labels, y=counts, name=ptype, marker_color=color,
            hovertemplate=f'<b>{ptype}</b><br>%{{x}}<br>%{{y}}<extra></extra>',
        ), row=1, col=1)
    fig.update_layout(barmode='stack')
    fig.update_yaxes(title_text='processes', row=1, col=1)

    # Row 1 Col 2: TCP + total count line
    fig.add_trace(go.Scatter(
        x=snap_labels, y=proc_counts, name='Total',
        mode='lines+markers', line=dict(color='#334155', width=2),
        hovertemplate='<b>Total</b> %{y}<extra></extra>',
    ), row=1, col=2)
    fig.add_trace(go.Scatter(
        x=snap_labels, y=tcp_counts, name='TCP clients',
        mode='lines+markers', line=dict(color='#0055aa', width=2),
        hovertemplate='<b>TCP</b> %{y}<extra></extra>',
    ), row=1, col=2)
    fig.update_yaxes(title_text='count', rangemode='tozero', row=1, col=2)

    # Row 2 Col 1: Top 10 by CPU
    if not top_by_cpu.empty:
        labels = [f"PID {int(r['pid'])}: {r['routine'] or r['ns']}" for _, r in top_by_cpu.iterrows()]
        colors = [TYPE_COLORS.get(r['type'], '#888') for _, r in top_by_cpu.iterrows()]
        fig.add_trace(go.Bar(
            x=top_by_cpu['cpu_val'], y=labels,
            orientation='h', marker_color=colors, showlegend=False,
            hovertemplate='<b>%{y}</b><br>CPU: %{x:,.0f}<extra></extra>',
        ), row=2, col=1)
        fig.update_xaxes(title_text='CPU ticks', row=2, col=1)

    # Row 2 Col 2: Top 10 by Glob
    if not top_by_glob.empty:
        labels = [f"PID {int(r['pid'])}: {r['routine'] or r['ns']}" for _, r in top_by_glob.iterrows()]
        colors = [TYPE_COLORS.get(r['type'], '#888') for _, r in top_by_glob.iterrows()]
        fig.add_trace(go.Bar(
            x=top_by_glob['glob_val'], y=labels,
            orientation='h', marker_color=colors, showlegend=False,
            hovertemplate='<b>%{y}</b><br>Glob: %{x:,.0f}<extra></extra>',
        ), row=2, col=2)
        fig.update_xaxes(title_text='Glob refs', row=2, col=2)

    fig.update_layout(
        height=620,
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor='white',
        plot_bgcolor='#f8f9fc',
        font=dict(family='-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', size=10),
        legend=dict(orientation='h', x=0, y=-0.04, font_size=10),
        hovermode='closest',
    )
    fig.update_xaxes(showgrid=True, gridcolor='#e8edf5')
    fig.update_yaxes(showgrid=True, gridcolor='#e8edf5', automargin=True)

    chart_html = fig.to_html(
        full_html=False, include_plotlyjs=False,
        config={'displayModeBar': True, 'displaylogo': False,
                'modeBarButtonsToRemove': ['select2d', 'lasso2d']},
    )

    # ── Process stability table ───────────────────────────────────────────────
    diff_html = ''
    if n_snaps > 1 and delta_df is not None and not delta_df.empty:
        pid_snap_count = df.groupby('pid')['snap_idx'].nunique()
        summary = (delta_df.groupby('pid')
                   .agg(routine=('routine', 'first'), ns=('ns', 'first'),
                        type=('type', 'first'),
                        cpu_sum=('delta_cpu', 'sum'),
                        glob_sum=('delta_glob', 'sum'))
                   .reset_index())
        summary['snaps'] = summary['pid'].map(pid_snap_count)
        summary = summary.sort_values('cpu_sum', ascending=False).head(20)

        def badge(n):
            if n == n_snaps:
                return f'<span style="background:#dcfce7;color:#166534;border-radius:3px;padding:1px 5px;font-size:0.7rem">all {n}</span>'
            return f'<span style="background:#fef9c3;color:#713f12;border-radius:3px;padding:1px 5px;font-size:0.7rem">{n}/{n_snaps}</span>'

        rows_html = ''.join(
            f'<tr style="border-bottom:1px solid #f0f2f5">'
            f'<td style="padding:5px 10px;font-size:0.77rem;font-family:monospace">{int(r["pid"])}</td>'
            f'<td style="padding:5px 10px;font-size:0.77rem">{r["routine"] or "—"}</td>'
            f'<td style="padding:5px 10px;font-size:0.77rem">{r["ns"] or "—"}</td>'
            f'<td style="padding:5px 10px">'
            f'  <span style="background:{TYPE_COLORS.get(r["type"],"#eee")};color:#fff;'
            f'border-radius:3px;padding:1px 6px;font-size:0.7rem">{r["type"]}</span>'
            f'</td>'
            f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right;'
            f'color:{"#dc2626" if r["cpu_sum"]>5_000_000 else "#d97706" if r["cpu_sum"]>500_000 else "#334"}">'
            f'{r["cpu_sum"]:,.0f}</td>'
            f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right">{r["glob_sum"]:,.0f}</td>'
            f'<td style="padding:5px 10px">{badge(int(r["snaps"]))}</td>'
            f'</tr>'
            for _, r in summary.iterrows()
        )

        diff_html = f'''
<div style="margin-top:16px">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:8px;
              text-transform:uppercase;letter-spacing:.06em">Top processes — CPU delta across snapshots</div>
  <div style="overflow-x:auto">
    <table id="ss-top-cpu" style="border-collapse:collapse;width:100%;background:#f8f9fc;
                  border:1px solid #dde3ee;border-radius:8px;overflow:hidden">
      <thead>
        <tr style="background:#eef2f7;font-size:0.7rem;color:#667;text-transform:uppercase">
          <th onclick="sortTable('ss-top-cpu',0,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">PID ↕</th>
          <th onclick="sortTable('ss-top-cpu',1,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Routine ↕</th>
          <th onclick="sortTable('ss-top-cpu',2,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Namespace ↕</th>
          <th onclick="sortTable('ss-top-cpu',3,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Type ↕</th>
          <th onclick="sortTable('ss-top-cpu',4,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">CPU delta ↕</th>
          <th onclick="sortTable('ss-top-cpu',5,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Glob delta ↕</th>
          <th onclick="sortTable('ss-top-cpu',6,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Seen in ↕</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="font-size:0.72rem;color:#aaa;margin-top:6px">
    CPU and Glob are deltas between consecutive snapshots (cumulative totals subtracted).
    "Seen in" shows how many of {n_snaps} snapshots this PID appeared in.
  </div>
</div>'''

    # ── Processes per namespace ───────────────────────────────────────────────
    ns_counts_per_snap = []
    for snap in snapshots:
        ns_counts_per_snap.append(
            pd.Series([p['ns'] for p in snap['processes']]).value_counts()
        )
    ns_df = pd.DataFrame(ns_counts_per_snap).fillna(0).astype(int)
    ns_avg = ns_df.mean().sort_values(ascending=False)
    ns_peak = ns_df.max()

    ns_rows_html = ''.join(
        f'<tr style="border-bottom:1px solid #f0f2f5">'
        f'<td style="padding:5px 10px;font-size:0.77rem">{ns if ns else "—"}</td>'
        f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right">{avg:.1f}</td>'
        f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right">{int(ns_peak[ns])}</td>'
        f'</tr>'
        for ns, avg in ns_avg.items()
    )

    ns_table_html = f'''
<div style="margin-top:16px">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:8px;
              text-transform:uppercase;letter-spacing:.06em">Processes per namespace</div>
  <div style="overflow-x:auto">
    <table id="ss-ns" style="border-collapse:collapse;background:#f8f9fc;border:1px solid #dde3ee;
                  border-radius:8px;overflow:hidden">
      <thead>
        <tr style="background:#eef2f7;font-size:0.7rem;color:#667;text-transform:uppercase">
          <th onclick="sortTable('ss-ns',0,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Namespace ↕</th>
          <th onclick="sortTable('ss-ns',1,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Avg processes ↕</th>
          <th onclick="sortTable('ss-ns',2,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Peak processes ↕</th>
        </tr>
      </thead>
      <tbody>{ns_rows_html}</tbody>
    </table>
  </div>
</div>'''

    # ── Top 5 routines by concurrent process count ────────────────────────────
    # For each snapshot, count how many processes share the same routine, then
    # take the max observed count per routine across all snapshots.
    rtn_peak: dict[str, int] = {}
    rtn_ns: dict[str, set] = {}
    for snap in snapshots:
        rtn_series = pd.Series([p['routine'] for p in snap['processes'] if p['routine']])
        snap_counts = rtn_series.value_counts()
        for rtn, cnt in snap_counts.items():
            if cnt > rtn_peak.get(rtn, 0):
                rtn_peak[rtn] = cnt
            for p in snap['processes']:
                if p['routine'] == rtn and p['ns']:
                    rtn_ns.setdefault(rtn, set()).add(p['ns'])

    top_rtns = sorted(rtn_peak.items(), key=lambda x: x[1], reverse=True)[:5]

    rtn_rows_html = ''.join(
        f'<tr style="border-bottom:1px solid #f0f2f5">'
        f'<td style="padding:5px 10px;font-size:0.77rem;font-family:monospace">{rtn}</td>'
        f'<td style="padding:5px 10px;font-size:0.77rem">{", ".join(sorted(rtn_ns.get(rtn, {"—"}))) or "—"}</td>'
        f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right;font-weight:700;color:#003366">{cnt}</td>'
        f'</tr>'
        for rtn, cnt in top_rtns
    )

    top_rtn_html = f'''
<div style="margin-top:16px">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:8px;
              text-transform:uppercase;letter-spacing:.06em">Top 5 routines by concurrent process count</div>
  <div style="overflow-x:auto">
    <table id="ss-top-rtn" style="border-collapse:collapse;background:#f8f9fc;border:1px solid #dde3ee;
                  border-radius:8px;overflow:hidden">
      <thead>
        <tr style="background:#eef2f7;font-size:0.7rem;color:#667;text-transform:uppercase">
          <th onclick="sortTable('ss-top-rtn',0,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Routine ↕</th>
          <th onclick="sortTable('ss-top-rtn',1,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Namespace ↕</th>
          <th onclick="sortTable('ss-top-rtn',2,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Peak concurrent ↕</th>
        </tr>
      </thead>
      <tbody>{rtn_rows_html}</tbody>
    </table>
  </div>
  <div style="font-size:0.72rem;color:#aaa;margin-top:6px">
    Peak number of processes running the same routine simultaneously in any single snapshot.
  </div>
</div>''' if top_rtns else ''

    return f'''
<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:10px;
              text-transform:uppercase;letter-spacing:.06em">
    %SS Analysis — {n_snaps} snapshot{"s" if n_snaps != 1 else ""}
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px">{stats_html}</div>
  {insights_html}
  {chart_html}
  {diff_html}
  {ns_table_html}
  {top_rtn_html}
</div>
'''
