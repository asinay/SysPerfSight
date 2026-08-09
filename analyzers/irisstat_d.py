"""
Analyzer for the 'irisstat -D' (database/resource lock stats) section in SystemPerformance files.

irisstat -D reports resource lock contention across multiple samples. Each sample contains:
  - RESOURCE STATS OVER A N-SECOND INTERVAL
      seize   Nseize   Aseize   Bseize BusySet Wakeups
   3 - Global    152     0        1        0       0       0
  - RESOURCE % STATS (same resources, values as %)
  - RESOURCE /sec STATS (per-second rates)
  - Block collision counts per database file

Key metrics:
  Nseize  = non-blocking seize failures (had to spin/wait)
  Aseize  = seize succeeded after waiting (adaptive spin)
  Bseize  = blocked seize (had to sleep — most expensive)
  BusySet = resource was already busy when seized
  Wakeups = number of times a waiter was woken

High Bseize or Nseize on Global, LockHTAB, LockLHB, or TransCB resources
indicates lock contention that can cause IRIS performance degradation.
Block collisions indicate concurrent access to the same database block.
"""
import re
import pandas as pd

# One sample starts at "RESOURCE STATS OVER A"
_SAMPLE_RE = re.compile(
    r'RESOURCE STATS OVER A (\d+)-SECOND INTERVAL\s*\n'
    r'\s*seize\s+Nseize\s+Aseize\s+Bseize.*?\n'  # BusySet/Wakeups optional
    r'(.*?)'
    r'(?=RESOURCE|$)',
    re.DOTALL | re.IGNORECASE,
)
_ROW_RE = re.compile(
    r'^\s*(\d+)\s*-\s*(\w+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(\d+)\s+(\d+))?',
    re.MULTILINE,
)
# Per-second stats block — the column header sits on the same physical line as
# 'RESOURCE /sec STATS' (unlike the count/% blocks, which put it on the next line);
# \s+ matches either layout since it swallows newlines too.
_RATE_RE = re.compile(
    r'RESOURCE /sec STATS\s+seize\s+Nseize\s+Aseize\s+Bseize\b.*?\n'
    r'(.*?)'
    r'(?=\n\s*\n|Total|$)',
    re.DOTALL | re.IGNORECASE,
)
_RATE_ROW_RE = re.compile(
    r'^\s*(\d+)\s*-\s*(\w+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
    re.MULTILINE,
)
_BLOCK_COLL_RE = re.compile(
    r'Total of (\d+) block collision[s]? in (\d+) samples?',
    re.IGNORECASE,
)
_DB_COLL_RE = re.compile(
    r'For blocks in:\s*(.+?)\s*\n.*?Block #\s+Counts\s*\n(.*?)(?=\n\s*\n|For blocks|$)',
    re.DOTALL | re.IGNORECASE,
)


# Resources to highlight (most relevant to IRIS performance)
_KEY_RESOURCES = {'Global', 'LockHTAB', 'LockLHB', 'TransCB', 'EVT', 'GfileTab',
                  'SEM_WaitQ', 'BlkSrch_ENQ', 'BlkSrch_DEQ', 'LRU'}


def _parse_irisstat_d(text: str) -> dict:
    """
    Returns dict with:
      samples: list of {interval_sec, rows: [{id, name, seize, nseize, aseize, bseize, busyset, wakeups}]}
      rates:   list of {name, seize_s, nseize_s, aseize_s, bseize_s} (per-second from last sample)
      block_collisions: list of {total, samples, db_path, hot_blocks}
    """
    result = {'samples': [], 'rates': [], 'block_collisions': []}

    for m in _SAMPLE_RE.finditer(text):
        interval = int(m.group(1))
        rows = []
        for rm in _ROW_RE.finditer(m.group(2)):
            rows.append({
                'id':      int(rm.group(1)),
                'name':    rm.group(2),
                'seize':   int(rm.group(3)),
                'nseize':  int(rm.group(4)),
                'aseize':  int(rm.group(5)),
                'bseize':  int(rm.group(6)),
                'busyset': int(rm.group(7)) if rm.group(7) else 0,
                'wakeups': int(rm.group(8)) if rm.group(8) else 0,
            })
        if rows:
            result['samples'].append({'interval_sec': interval, 'rows': rows})

    # Per-second rates (from last occurrence)
    for rm in _RATE_RE.finditer(text):
        rates = []
        for row in _RATE_ROW_RE.finditer(rm.group(1)):
            rates.append({
                'name':     row.group(2),
                'seize_s':  float(row.group(3)),
                'nseize_s': float(row.group(4)),
                'aseize_s': float(row.group(5)),
                'bseize_s': float(row.group(6)),
            })
        if rates:
            result['rates'] = rates  # keep last

    # Block collisions
    for cm in _BLOCK_COLL_RE.finditer(text):
        result['block_collisions'].append({
            'total': int(cm.group(1)),
            'samples': int(cm.group(2)),
        })

    return result


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
    parsed = _parse_irisstat_d(section_text)
    samples = parsed['samples']
    rates   = parsed['rates']
    collisions = parsed['block_collisions']

    if not samples and not rates:
        return ''

    n_samples = len(samples)

    # ── Aggregate across samples: sum seizes, sum bseizes per resource ────────
    agg: dict[str, dict] = {}
    for s in samples:
        for row in s['rows']:
            name = row['name']
            if name not in agg:
                agg[name] = {'seize': 0, 'nseize': 0, 'aseize': 0,
                             'bseize': 0, 'busyset': 0, 'wakeups': 0, 'count': 0}
            for k in ('seize', 'nseize', 'aseize', 'bseize', 'busyset', 'wakeups'):
                agg[name][k] += row[k]
            agg[name]['count'] += 1

    # ── Insights ─────────────────────────────────────────────────────────────
    flags = []

    # Block collisions are the most actionable signal
    total_collisions = sum(c['total'] for c in collisions)
    if total_collisions > 0:
        coll_per_sample = total_collisions / max(n_samples, 1)
        if coll_per_sample > 5:
            flags.append(_flag('red',
                f'<b>Block collisions detected</b>: {total_collisions} total across '
                f'{n_samples} sample(s) ({coll_per_sample:.1f}/sample) — multiple processes '
                f'are competing for the same database blocks. This causes write serialization '
                f'and can severely impact throughput under concurrent load.'))
        else:
            flags.append(_flag('amber',
                f'<b>Block collisions present</b>: {total_collisions} total — '
                f'low level, but worth monitoring if workload increases.'))

    # Bseize (sleeping waits) on critical resources
    CRIT = ['Global', 'LockHTAB', 'LockLHB', 'TransCB']
    for name in CRIT:
        if name not in agg:
            continue
        a = agg[name]
        if a['seize'] == 0:
            continue
        bseize_pct = a['bseize'] / a['seize'] * 100
        nseize_pct = a['nseize'] / a['seize'] * 100
        if a['bseize'] > 0:
            flags.append(_flag('red' if bseize_pct > 1 else 'amber',
                f'<b>{name} lock contention</b>: {a["bseize"]} blocking seize(s) '
                f'({bseize_pct:.2f}% of {a["seize"]} total seizes) — '
                f'processes had to sleep waiting for this resource. '
                + ('High contention on the Global resource indicates concurrent global access bottleneck.' if name == 'Global'
                   else 'High contention on LockHTAB/LockLHB indicates lock table pressure.'
                   if name in ('LockHTAB', 'LockLHB')
                   else 'High contention on TransCB indicates transaction overhead.')))
        elif nseize_pct > 5:
            flags.append(_flag('info',
                f'<b>{name} spin contention</b>: {nseize_pct:.1f}% non-blocking misses — '
                f'resource was busy but no sleeping required. Low risk but indicates activity.'))

    if not flags:
        flags.append(_flag('green',
            f'No significant lock contention or block collisions detected across '
            f'{n_samples} sample(s).'))

    insights_html = '<!--INS-->' + f'''
<div style="margin-bottom:14px">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Insights</div>
  {''.join(flags)}
</div>''' + '<!--/INS-->'

    # ── Summary table — key resources, aggregated ─────────────────────────────
    table_rows = ''
    # Sort by bseize descending, show key resources first
    sorted_names = sorted(
        [n for n in agg if n in _KEY_RESOURCES],
        key=lambda n: agg[n]['bseize'],
        reverse=True,
    )
    # Include any non-key resource with bseize > 0
    extras = sorted(
        [n for n in agg if n not in _KEY_RESOURCES and agg[n]['bseize'] > 0],
        key=lambda n: agg[n]['bseize'],
        reverse=True,
    )
    display_names = sorted_names + extras

    for name in display_names:
        a = agg[name]
        if a['seize'] == 0:
            continue
        bseize_pct = a['bseize'] / a['seize'] * 100
        color = '#dc2626' if a['bseize'] > 0 else '#64748b'
        table_rows += f'''
<tr style="border-bottom:1px solid #f0f2f5">
  <td style="padding:5px 10px;font-size:0.77rem;font-family:monospace">{name}</td>
  <td style="padding:5px 10px;font-size:0.77rem;text-align:right">{a["seize"]}</td>
  <td style="padding:5px 10px;font-size:0.77rem;text-align:right">{a["nseize"]}</td>
  <td style="padding:5px 10px;font-size:0.77rem;text-align:right">{a["aseize"]}</td>
  <td style="padding:5px 10px;font-size:0.77rem;text-align:right;font-weight:700;color:{color}">{a["bseize"]}</td>
  <td style="padding:5px 10px;font-size:0.77rem;text-align:right">{a["wakeups"]}</td>
</tr>'''

    summary_table = ''
    if table_rows:
        summary_table = f'''
<div style="margin-bottom:14px;overflow-x:auto">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Resource Lock Summary
    <span style="font-weight:400;text-transform:none;color:#999">
      — {n_samples} sample(s) aggregated &nbsp;·&nbsp;
      Bseize = blocking waits (most expensive) &nbsp;·&nbsp;
      <span style="font-style:italic">click a column header to sort</span>
    </span>
  </div>
  <table id="irisd-lock-tbl" style="border-collapse:collapse;width:100%;background:#f8f9fc;
                border:1px solid #dde3ee;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#eef2f7;font-size:0.7rem;color:#667;text-transform:uppercase">
        <th onclick="sortTable('irisd-lock-tbl',0,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Resource ↕</th>
        <th onclick="sortTable('irisd-lock-tbl',1,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Seizes ↕</th>
        <th onclick="sortTable('irisd-lock-tbl',2,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Nseize ↕</th>
        <th onclick="sortTable('irisd-lock-tbl',3,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Aseize ↕</th>
        <th onclick="sortTable('irisd-lock-tbl',4,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Bseize ⚠ ↕</th>
        <th onclick="sortTable('irisd-lock-tbl',5,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">Wakeups ↕</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>
'''

    # ── Per-second rates table (from latest sample) ───────────────────────────
    rate_table = ''
    if rates:
        key_rates = [r for r in rates if r['name'] in _KEY_RESOURCES or r['bseize_s'] > 0]
        if key_rates:
            rate_rows = ''.join(
                f'<tr style="border-bottom:1px solid #f0f2f5">'
                f'<td style="padding:5px 10px;font-size:0.77rem;font-family:monospace">{r["name"]}</td>'
                f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right">{r["seize_s"]:.1f}</td>'
                f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right">{r["nseize_s"]:.2f}</td>'
                f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right">{r["aseize_s"]:.2f}</td>'
                f'<td style="padding:5px 10px;font-size:0.77rem;text-align:right;'
                f'font-weight:700;color:{"#dc2626" if r["bseize_s"] > 0 else "#64748b"}">'
                f'{r["bseize_s"]:.2f}</td>'
                f'</tr>'
                for r in sorted(key_rates, key=lambda x: x["bseize_s"], reverse=True)
            )
            rate_table = f'''
<div style="margin-bottom:14px;overflow-x:auto">
  <div style="font-size:0.72rem;font-weight:700;color:#555;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px">Per-Second Rates (last sample)</div>
  <table id="irisd-rates" style="border-collapse:collapse;background:#f8f9fc;border:1px solid #dde3ee;
                border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#eef2f7;font-size:0.7rem;color:#667;text-transform:uppercase">
        <th onclick="sortTable('irisd-rates',0,this)" style="padding:7px 10px;text-align:left;cursor:pointer;user-select:none">Resource ↕</th>
        <th onclick="sortTable('irisd-rates',1,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">seize/s ↕</th>
        <th onclick="sortTable('irisd-rates',2,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">nseize/s ↕</th>
        <th onclick="sortTable('irisd-rates',3,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">aseize/s ↕</th>
        <th onclick="sortTable('irisd-rates',4,this)" style="padding:7px 10px;text-align:right;cursor:pointer;user-select:none">bseize/s ↕</th>
      </tr>
    </thead>
    <tbody>{rate_rows}</tbody>
  </table>
</div>'''

    return f'''
<div style="margin:16px 0 24px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:0.8rem;font-weight:600;color:#555;margin-bottom:4px;
              text-transform:uppercase;letter-spacing:.06em">irisstat -D Analysis</div>
  <div style="font-size:0.72rem;color:#999;margin-bottom:12px">
    {n_samples} sample(s) &nbsp;|&nbsp; Resource lock contention &amp; block collisions
  </div>
  {insights_html}
  {summary_table}
  {rate_table}
</div>
'''
