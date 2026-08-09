import io
import re
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def _parse_mgstat(text: str) -> pd.DataFrame | None:
    """Parse mgstat CSV text into a DataFrame with a datetime index."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None

    # Find the header row — contains 'Date' and 'Time' as the first two CSV fields
    # The actual header may have extra spaces: 'Date,       Time    ,...'
    header_idx = next(
        (i for i, l in enumerate(lines)
         if re.match(r'Date\s*,\s*Time\s*,', l)),
        None,
    )
    if header_idx is None:
        return None

    csv_text = '\n'.join(lines[header_idx:])
    try:
        df = pd.read_csv(io.StringIO(csv_text), skipinitialspace=True)
    except Exception:
        return None

    # Normalise column names (strip whitespace)
    df.columns = [c.strip() for c in df.columns]

    if 'Date' not in df.columns or 'Time' not in df.columns:
        return None

    try:
        df['dt'] = pd.to_datetime(
            df['Date'].str.strip() + ' ' + df['Time'].str.strip(),
            format='%m/%d/%Y %H:%M:%S',
            errors='coerce',
        )
        df = df.dropna(subset=['dt']).sort_values('dt').reset_index(drop=True)
    except Exception:
        return None

    # Coerce all numeric columns
    for col in df.columns:
        if col not in ('Date', 'Time', 'dt'):
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    return df


def _ts(fig, df, col, name, row, col_idx, color, yaxis_title='', dash=None):
    """Add a time-series line trace."""
    kwargs = dict(line=dict(color=color, width=1.5))
    if dash:
        kwargs['line']['dash'] = dash
    fig.add_trace(go.Scatter(
        x=df['dt'], y=df[col],
        name=name,
        mode='lines',
        hovertemplate=f'<b>{name}</b><br>%{{x}}<br>%{{y:,.0f}}<extra></extra>',
        **kwargs,
    ), row=row, col=col_idx)
    if yaxis_title:
        fig.update_yaxes(title_text=yaxis_title, row=row, col=col_idx)


def _flag(level: str, text: str) -> str:
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


def _mgstat_insights(df: pd.DataFrame) -> str:
    has = lambda c: c in df.columns  # noqa: E731
    flags = []

    # Physical reads sustained high → cache miss pressure
    if has('PhyRds'):
        avg = df['PhyRds'].mean()
        pk  = df['PhyRds'].quantile(0.99)
        if avg > 500:
            flags.append(_flag('red',
                f'<b>High physical reads</b>: avg {avg:,.0f}/s, p99 peak {pk:,.0f}/s — '
                f'sustained cache misses. Consider increasing global buffers.'))
        elif avg > 100:
            flags.append(_flag('amber',
                f'<b>Elevated physical reads</b>: avg {avg:,.0f}/s — '
                f'some cache pressure. Check Rdratio (cache-hit ratio).'))

    # Low read cache hit ratio
    if has('Rdratio') and has('PhyRds'):
        # Rdratio = PhyRds/Glorefs*100; low means most refs go to disk
        mean_ratio = df.loc[df['Glorefs'] > 0, 'Rdratio'].mean() if has('Glorefs') else None
        if mean_ratio is not None and mean_ratio > 10:
            flags.append(_flag('amber',
                f'<b>Low database cache-hit ratio</b>: avg Rdratio {mean_ratio:.1f}% '
                f'(physical reads / global refs) — significant portion of global refs hit disk.'))

    # WD queue growing (write daemon backlog)
    if has('WDQsz'):
        avg = df['WDQsz'].mean()
        pk  = df['WDQsz'].quantile(0.99)
        if avg > 50:
            flags.append(_flag('red',
                f'<b>Write Daemon queue backed up</b>: avg {avg:.0f}, p99 peak {pk:.0f} — '
                f'writes are outpacing the WD cycle. Check disk write throughput.'))
        elif avg > 10:
            flags.append(_flag('amber',
                f'<b>Write Daemon queue elevated</b>: avg {avg:.0f}, p99 peak {pk:.0f}.'))

    # Journal writes high
    if has('Jrnwrts'):
        avg = df['Jrnwrts'].mean()
        pk  = df['Jrnwrts'].quantile(0.99)
        if avg > 1000:
            flags.append(_flag('amber',
                f'<b>High journal write rate</b>: avg {avg:,.0f}/s, p99 peak {pk:,.0f}/s — '
                f'heavy transactional workload. Ensure journal disk I/O is not a bottleneck.'))

    # Global resource NSeizes (expensive OS context switches)
    if has('pGblNsz'):
        avg_nsz = df['pGblNsz'].mean()
        pk_nsz  = df['pGblNsz'].quantile(0.99)
        if avg_nsz > 5:
            flags.append(_flag('red',
                f'<b>High global NSeize rate</b>: avg {avg_nsz:.1f}%, p99 peak {pk_nsz:.1f}% — '
                f'processes are hibernating waiting for the global resource. NSeizes incur kernel '
                f'context-switch overhead. Correlate with high Glorefs and CPU %system.'))
        elif avg_nsz > 1:
            flags.append(_flag('amber',
                f'<b>Elevated global NSeizes</b>: avg {avg_nsz:.1f}% — '
                f'some lock contention on the global resource. Monitor for increases under load.'))

    # Global resource ASeizes (spin-wait, cheaper but burns CPU user time)
    if has('pGblAsz'):
        avg_asz = df['pGblAsz'].mean()
        if avg_asz > 10:
            flags.append(_flag('amber',
                f'<b>Elevated global ASeizes</b>: avg {avg_asz:.1f}% — '
                f'processes are spin-waiting on the global resource (user CPU overhead). '
                f'Normal on busy multi-CPU systems but high values indicate lock pressure.'))

    # Write Daemon phase — sustained non-zero means WD is always active
    if has('WDphase'):
        nonzero_pct = (df['WDphase'] > 0).mean() * 100
        phase_8_pct = (df['WDphase'] == 8).mean() * 100
        if phase_8_pct > 80:
            flags.append(_flag('amber',
                f'<b>Write Daemon continuously updating databases</b>: phase 8 (DB update) '
                f'observed {phase_8_pct:.0f}% of the time — write throughput is at its limit. '
                f'Check disk write latency with sar -d / iostat.'))
        elif nonzero_pct > 95:
            flags.append(_flag('info',
                f'<b>Write Daemon rarely idle</b>: active {nonzero_pct:.0f}% of samples — '
                f'system has a sustained write workload.'))

    # Routine cache misses / routine loads-saves
    if has('RouCMs'):
        avg_rcm = df['RouCMs'].mean()
        if avg_rcm > 50:
            flags.append(_flag('amber',
                f'<b>Routine cache misses elevated</b>: avg {avg_rcm:.0f}/s — '
                f'IRIS is fetching routines from disk frequently. '
                f'Consider increasing routine buffer allocation.'))

    if has('RouLaS'):
        avg_rls = df['RouLaS'].mean()
        if avg_rls > 20:
            flags.append(_flag('amber',
                f'<b>High routine loads/saves</b>: avg {avg_rls:.0f}/s — '
                f'routine buffer pressure. Increase routine buffers to reduce disk activity.'))

    # Remote global refs (ECP traffic)
    if has('RemGrefs') and has('Glorefs'):
        pct = (df['RemGrefs'].sum() / df['Glorefs'].sum() * 100) if df['Glorefs'].sum() > 0 else 0
        if pct > 20:
            flags.append(_flag('info',
                f'<b>Significant ECP traffic</b>: {pct:.1f}% of global refs are remote — '
                f'this instance has notable client-server (ECP) activity.'))

    if not flags:
        flags.append(_flag('info', 'No significant anomalies detected in this sample window.'))

    return _insights_panel(flags)


async def analyze(section_text: str) -> str:
    df = _parse_mgstat(section_text)
    if df is None or df.empty or len(df) < 2:
        return ''

    cols = df.columns.tolist()

    # Check which optional columns exist
    has = lambda c: c in cols  # noqa: E731

    fig = make_subplots(
        rows=4, cols=2,
        subplot_titles=(
            'Global References / sec',
            'Physical Reads & Writes / sec',
            'Journal Writes / sec & Write Daemon Queue',
            'Routine References / sec',
            'Database Cache (Global Blocks)',
            'Bytes Sent & Received',
            'Gloupds / sec (Global Updates)',
            'Jrnwrts / sec (Journal Writes)',
        ),
        vertical_spacing=0.10,
        horizontal_spacing=0.10,
    )

    common_layout = dict(
        height=1040,
        margin=dict(l=10, r=10, t=50, b=10),
        paper_bgcolor='white',
        plot_bgcolor='#f8f9fc',
        font=dict(family='-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', size=10),
        legend=dict(orientation='h', x=0, y=-0.05, font_size=10),
        hovermode='x unified',
    )

    # Row 1, Col 1: Global refs
    if has('Glorefs'):
        _ts(fig, df, 'Glorefs', 'Glorefs', 1, 1, '#0055aa', 'refs/s')
    if has('RemGrefs'):
        _ts(fig, df, 'RemGrefs', 'RemGrefs', 1, 1, '#66aaff', dash='dot')

    # Row 1, Col 2: Physical I/O
    if has('PhyRds'):
        _ts(fig, df, 'PhyRds', 'PhyRds', 1, 2, '#e74c3c', 'ops/s')
    if has('PhyWrs'):
        _ts(fig, df, 'PhyWrs', 'PhyWrs', 1, 2, '#f39c12', dash='dot')

    # Row 2, Col 1: Journal writes + WD queue
    if has('Jrnwrts'):
        _ts(fig, df, 'Jrnwrts', 'Jrnwrts', 2, 1, '#8e44ad', 'writes/s')
    if has('WDQsz'):
        _ts(fig, df, 'WDQsz', 'WDQsz', 2, 1, '#2ecc71', dash='dot')

    # Row 2, Col 2: Routine refs
    if has('Rourefs'):
        _ts(fig, df, 'Rourefs', 'Rourefs', 2, 2, '#16a085', 'refs/s')
    if has('RemRrefs'):
        _ts(fig, df, 'RemRrefs', 'RemRrefs', 2, 2, '#1abc9c', dash='dot')

    # Row 3, Col 1: Global block cache size
    if has('GblSz'):
        _ts(fig, df, 'GblSz', 'GblSz', 3, 1, '#2980b9', 'blocks')
    if has('BDBSz'):
        _ts(fig, df, 'BDBSz', 'BDBSz', 3, 1, '#3498db', dash='dot')

    # Row 3, Col 2: Network bytes
    if has('BytSnt'):
        _ts(fig, df, 'BytSnt', 'BytSnt', 3, 2, '#27ae60', 'bytes/s')
    if has('BytRcd'):
        _ts(fig, df, 'BytRcd', 'BytRcd', 3, 2, '#2ecc71', dash='dot')

    # Row 4, Col 1: Gloupds dedicated
    if has('Gloupds'):
        _ts(fig, df, 'Gloupds', 'Gloupds', 4, 1, '#0055aa', 'updates/s')

    # Row 4, Col 2: Jrnwrts dedicated
    if has('Jrnwrts'):
        _ts(fig, df, 'Jrnwrts', 'Jrnwrts', 4, 2, '#8e44ad', 'writes/s')

    fig.update_layout(**common_layout)
    fig.update_xaxes(showgrid=True, gridcolor='#e8edf5', tickangle=-30)
    fig.update_yaxes(showgrid=True, gridcolor='#e8edf5', rangemode='tozero')

    duration = df['dt'].iloc[-1] - df['dt'].iloc[0]
    hours, rem = divmod(int(duration.total_seconds()), 3600)
    minutes = rem // 60
    duration_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    chart_html = fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config={'displayModeBar': True, 'displaylogo': False,
                'modeBarButtonsToRemove': ['select2d', 'lasso2d']},
    )

    # Summary stat cards
    def _stat(label, value, unit=''):
        return (f'<div style="background:#f8f9fc;border:1px solid #dde3ee;border-radius:6px;'
                f'padding:8px 14px;min-width:120px;flex:1">'
                f'<div style="font-size:0.7rem;color:#777;text-transform:uppercase;letter-spacing:.05em">{label}</div>'
                f'<div style="font-size:1.1rem;font-weight:700;color:#003366">{value}'
                f'<span style="font-size:0.7rem;font-weight:400;color:#888;margin-left:3px">{unit}</span></div></div>')

    n = len(df)
    glorefs_avg = int(df['Glorefs'].mean()) if has('Glorefs') else None
    phyrds_avg  = int(df['PhyRds'].mean())  if has('PhyRds')  else None
    phywrs_avg  = int(df['PhyWrs'].mean())  if has('PhyWrs')  else None
    jrn_avg     = int(df['Jrnwrts'].mean()) if has('Jrnwrts') else None
    nsz_avg     = df['pGblNsz'].mean()      if has('pGblNsz') else None
    roucm_avg   = df['RouCMs'].mean()       if has('RouCMs')  else None

    stat_items = [_stat('Samples', f'{n:,}'), _stat('Duration', duration_str)]
    if glorefs_avg is not None: stat_items.append(_stat('Glorefs avg', f'{glorefs_avg:,}', '/s'))
    if phyrds_avg  is not None: stat_items.append(_stat('PhyRds avg',  f'{phyrds_avg:,}',  '/s'))
    if phywrs_avg  is not None: stat_items.append(_stat('PhyWrs avg',  f'{phywrs_avg:,}',  '/s'))
    if jrn_avg     is not None: stat_items.append(_stat('Jrnwrts avg', f'{jrn_avg:,}',     '/s'))
    if nsz_avg     is not None: stat_items.append(_stat('GblNSeize avg', f'{nsz_avg:.1f}', '%'))
    if roucm_avg   is not None: stat_items.append(_stat('RouCacheMiss avg', f'{roucm_avg:.0f}', '/s'))

    stats_html = ''.join(stat_items)

    insights_html = '<!--INS-->' + _mgstat_insights(df) + '<!--/INS-->'

    return f'''
<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:10px;
              text-transform:uppercase;letter-spacing:.06em">mgstat Analysis</div>
  <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px">{stats_html}</div>
  {insights_html}
  {chart_html}
</div>
'''
