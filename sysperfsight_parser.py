import re
from dataclasses import dataclass, field
from typing import Optional

# Sections known to contain potentially sensitive information
SENSITIVE_SECTIONS = {
    "Configuration": "Contains instance name, machine name, GUID, license number, and product version.",
    "Profile": "Contains username/email of the person who ran the report and directory paths.",
    "License": "Contains license type, user counts, and feature codes.",
    "CPF file": "Contains full filesystem paths for all databases and namespaces.",
    "IRIS ALL": "Lists all IRIS instances on the machine with their ports and directories.",
    "Windows info": "Contains detailed OS, hardware, and network configuration.",
    "tasklist": "Lists all running processes on the machine.",
}

# Older Caché SystemPerformance reports use different section ids/titles for
# functionally identical sections. Mapping them to the canonical IRIS id/title here
# means every downstream id- or title-keyed lookup (SENSITIVE_SECTIONS, SECTION_DESCRIPTIONS,
# SECTION_ANALYZERS, SECTION_GROUPS, COLLAPSED_BY_DEFAULT, TITLE_TIME_FILTERS) needs only
# one entry, shared by both products. Keyed by the raw id lowercased; IRIS's own ids
# lowercase to strings not present here, so this is a no-op on IRIS reports.
CACHE_SECTION_ALIASES: dict[str, tuple[str, str]] = {
    "ccontrolall": ("IRISALL", "IRIS ALL"),
    "license":     ("License", "License"),
    "cpffile":     ("CPFfile", "CPF file"),
    "cstat-c1":    ("irisstat-c1", "irisstat -c1"),
    "cstat-d":     ("irisstat-D", "irisstat -D"),
    "cstat-r":     ("irisstat-R", "irisstat -R"),
}


def _canonicalize(section_id: str, title: str) -> tuple[str, str]:
    return CACHE_SECTION_ALIASES.get(section_id.lower(), (section_id, title))


SECTION_DESCRIPTIONS: dict[str, str] = {
    # Common sections
    "IRIS ALL":      "Lists all IRIS instances on this machine with their ports and installation paths.",
    "License":       "License type, user capacity, and feature codes.",
    "CPF file":      "Full IRIS configuration: databases, namespaces, journal settings, and memory.",
    "mgstat":        "Core IRIS performance counters sampled over time — globals/sec, routine calls, lock table, journal writes.",
    "%SS":           "Per-process snapshot of IRIS activity — CPU, memory, lock state, and open devices.",
    "irisstat -c1":  "Shared memory and cache statistics — buffer hits, evictions, and memory pressure.",
    "irisstat -D":   "Database file I/O statistics — reads, writes, and physical vs. logical ratios per database.",
    "irisstat -R":   "Routine cache statistics — hits, misses, and cache utilization.",
    # Linux sections
    "Linux info":    "OS version, hostname, kernel version, and hardware summary.",
    "cpu":           "CPU topology, core count, clock speeds, and processor capabilities.",
    "ipcs":          "IPC shared memory segments — useful for verifying IRIS shared memory allocation.",
    "fdisk-l":       "Partition tables for all block devices — disks, LUNs, and their sizes.",
    "mount":         "Mounted filesystems with options — check for noatime and barrier settings.",
    "df -m":         "Disk space usage by filesystem — spot full or near-full volumes.",
    "ifconfig":      "Network interface configuration — IP addresses, MTU, and interface flags.",
    "sysctl -a":     "Kernel tuning parameters — hugepages, semaphores, file handles, and TCP settings.",
    "ps":            "Snapshot of all running processes with CPU and memory usage.",
    "vmstat":        "Virtual memory, swap, CPU run queue, and context switches over time.",
    "sar -u":        "CPU utilization history — user, system, iowait, and idle percentages.",
    "free":          "RAM and swap usage — check for swap activity indicating memory pressure.",
    "iostat":        "Disk I/O throughput and latency per device — identify storage bottlenecks.",
    "sar -d":        "Historical disk I/O rates per device — correlate with mgstat spikes.",
    # Windows sections
    "Windows info":  "OS version, hostname, hardware summary, and Windows configuration.",
    "tasklist":      "Snapshot of all running processes with memory usage.",
    "perfmon":       "Windows Performance Monitor counters — CPU, disk, memory, and network over time.",
}


@dataclass
class Section:
    id: str
    title: str
    content_html: str
    sensitive: bool = False
    sensitive_reason: Optional[str] = None


# Browsers re-serialize their parsed DOM when a report is saved via "Save Page As"
# instead of saving/copying the raw source. The result looks identical on screen
# but restructures the section-heading markup: quotes every attribute, explicitly
# closes void elements (`noshade=""></hr>`, `<br />`), and splits the single
# `<b><font><div id=...></div>TITLE</font></b>` heading into three separate
# `<b><font>` blocks (empty one before the div, empty one nested inside it, and a
# third one after `</div>` holding the actual title text). These patterns undo
# that reshaping back into the form `anchor_pattern`/`config_pattern` expect.
_RESERIALIZED_HR_RE = re.compile(
    r'<hr\s+size="4"\s+noshade(?:="")?\s*/?>(?:\s*</hr>)?', re.IGNORECASE
)
_RESERIALIZED_HEAD_RE = re.compile(
    r'(?P<popen><p>)?\s*<b>\s*<font(?P<attrs>[^>]*)>\s*</font>\s*</b>\s*(?P<pclose></p>)?'
    r'\s*<div id=(?P<q>["\']?)(?P<id>[^"\'>\s]+)(?P=q)>'
    r'\s*(?:<b>\s*<font[^>]*>\s*</font>\s*</b>)?'
    r'\s*</div>'
    r'\s*(?:<b>\s*<font[^>]*>)?'
    r'\s*(?P<title>[^<]+?)\s*'
    r'</font>\s*</b>',
    re.IGNORECASE
)


def _fix_reserialized_head(m: re.Match) -> str:
    popen = m.group('popen') or ''
    pclose = m.group('pclose') or ''
    # Always quote the id: anchor_pattern tolerates quoted or bare, but
    # config_pattern (Configuration/Profile) requires quotes.
    return f'{popen}<b><font{m.group("attrs")}><div id="{m.group("id")}"></div>{m.group("title")}</font></b>{pclose}'


def _normalize_reserialized_html(html: str) -> str:
    """Undo browser DOM-reserialization of the section-heading markup (see above)."""
    html = _RESERIALIZED_HR_RE.sub('<hr size="4" noshade>', html)
    html = _RESERIALIZED_HEAD_RE.sub(_fix_reserialized_head, html)
    return html


def parse_sections(html: str) -> tuple[str, list[Section]]:
    """
    Parse a SystemPerformance HTML file into its header and individual sections.
    Returns (header_html, [Section, ...]).
    """
    # The header is everything before the first <hr> separator that precedes a section
    # Sections are delimited by <hr size="4" noshade> followed by a div id=...
    # Pattern: find each section start
    section_pattern = re.compile(
        r'(<hr size="4" noshade>|<hr size="4" noshade/>)'
        r'(<b>.*?<div id=["\']?([^"\'>\s]+)["\']?></div>'
        r'(.*?)</font></b>.*?<pre>(.*?)</pre>)'
        r'(?=<p align="right">.*?Back to top|$)',
        re.DOTALL | re.IGNORECASE
    )

    # Simpler approach: split on the hr+section pattern
    # Find all section anchors with their positions.
    # Handles three hr-delimited heading variants:
    #   IRIS:  <hr>\n<b><font...>          (no wrapper tag)
    #   Caché: <hr><br>\n<b><font...>      (br wrapper)
    #   Caché: <hr>\n<p> <b><font...></p>  (p wrapper, used for Profile)
    anchor_pattern = re.compile(
        r'<hr size="4" noshade>\s*(?:<br>|<p>)?\s*<b><font[^>]*>'
        r'<div id=["\']?([^"\'>\s]+)["\']?></div>'
        r'([^<]+)</font></b>',
        re.IGNORECASE
    )

    matches = list(anchor_pattern.finditer(html))

    if not matches:
        # Fall back to undoing a browser DOM-reserialization before giving up.
        # Only ever swaps `html` for the normalized version if that actually
        # produces matches, so a genuinely unrecognized file still returns [].
        normalized = _normalize_reserialized_html(html)
        if normalized != html:
            normalized_matches = list(anchor_pattern.finditer(normalized))
            if normalized_matches:
                html = normalized
                matches = normalized_matches

    if not matches:
        return html, []

    # Everything before the first section anchor is the header
    # (includes the nav table and debug comment and Configuration/Profile sections
    #  which use a slightly different pattern)
    header_end = matches[0].start()

    # Also grab Configuration and Profile which use <p>...<div id="..."> pattern
    config_pattern = re.compile(
        r'(<p>\s*<b><font[^>]*><div id=["\']([^"\'>\s]+)["\']></div>'
        r'([^<]+)</font></b></p>)(.*?)'
        r'(?=<hr size="4" noshade>)',
        re.DOTALL | re.IGNORECASE
    )

    sections: list[Section] = []

    # IDs already handled by anchor_pattern (e.g. Profile in Caché format which
    # is preceded by <hr> so it appears in both patterns — skip the duplicate)
    anchor_ids = {m.group(1) for m in matches}

    # Parse Configuration / Profile (pre-section area)
    for m in config_pattern.finditer(html[:header_end + 5000]):
        section_id = m.group(2)
        if section_id in anchor_ids:
            continue
        title = m.group(3).strip()
        section_id, title = _canonicalize(section_id, title)
        # grab pre content
        pre_match = re.search(r'<pre>(.*?)</pre>', m.group(0), re.DOTALL)
        content = m.group(0)
        sections.append(Section(
            id=section_id,
            title=title,
            content_html=content,
            sensitive=title in SENSITIVE_SECTIONS,
            sensitive_reason=SENSITIVE_SECTIONS.get(title),
        ))

    # Parse main sections
    for i, m in enumerate(matches):
        section_id = m.group(1)
        title = m.group(2).strip()
        section_id, title = _canonicalize(section_id, title)

        # Content runs from this match to the next match (or end)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        content_html = html[start:end]

        sections.append(Section(
            id=section_id,
            title=title,
            content_html=content_html,
            sensitive=title in SENSITIVE_SECTIONS,
            sensitive_reason=SENSITIVE_SECTIONS.get(title),
        ))

    # Build header: nav table + debug comment (before first config section or first hr section)
    # Use the raw html up to the first <p> config block or first hr section
    first_config = config_pattern.search(html)
    if first_config:
        header_html = html[:first_config.start()]
    else:
        header_html = html[:header_end]

    return header_html, sections


_EXCLUDED_PLACEHOLDER = (
    '<pre>\n'
    '    [ This section was excluded by the report author. ]\n'
    '    [ Filtered using SysPerfSight - a free tool that redacts InterSystems IRIS\n'
    '      SystemPerformance files locally, without uploading any data to the cloud. ]\n'
    '</pre>'
)


def _make_excluded_html(content_html: str) -> str:
    """Replace all <pre> content blocks with a single exclusion placeholder."""
    # Remove all pre blocks first, then insert one placeholder at the first position
    first_pre = re.search(r'<pre>', content_html, re.IGNORECASE)
    if not first_pre:
        return content_html
    stripped = re.sub(r'<pre>.*?</pre>', '', content_html, flags=re.DOTALL | re.IGNORECASE)
    return stripped[:first_pre.start()] + _EXCLUDED_PLACEHOLDER + stripped[first_pre.start():]


# Groups define sidebar structure. Sections not listed here appear under "Other".
SECTION_GROUPS = [
    ("System Info",    ["Configuration", "Profile", "IRIS ALL", "License"]),
    ("Configuration",  ["CPFfile"]),
    ("OS",             ["Windowsinfo", "tasklist", "perfmon"]),
    ("IRIS Metrics",   ["mgstat", "%SS"]),
    ("irisstat",       ["irisstat-c1", "irisstat-D", "irisstat-R"]),
]

# Sections collapsed by default (raw data, low immediate value)
COLLAPSED_BY_DEFAULT = {"CPFfile", "irisstat-c1", "irisstat-D", "irisstat-R", "tasklist", "perfmon"}


def _extract_pre_text(content_html: str) -> str:
    """Return plain text from all <pre> blocks in a section's HTML."""
    return '\n'.join(re.findall(r'<pre>(.*?)</pre>', content_html, re.DOTALL | re.IGNORECASE))


def build_output(
    header_html: str,
    sections: list[Section],
    selected_ids: list[str],
    analysis: dict[str, str] | None = None,
    synthesis: str = '',
    mode: str = 'full',
) -> str:
    """Build a redesigned HTML output with a fixed sidebar, grouped navigation,
    and collapsible sections. Selected sections show their data; excluded ones
    show a placeholder. Sections with analysis get charts injected above the raw data."""

    selected_set = set(selected_ids)
    analysis = analysis or {}

    # Map id -> Section for quick lookup
    sec_by_id = {s.id: s for s in sections}

    # --- extract metadata from header for the top bar ---
    instance = re.search(r'(\w+) on machine', header_html)
    machine  = re.search(r'on machine (\S+)', header_html)
    run_by   = re.search(r'started by user "([^"]+)"', header_html)
    run_at   = re.search(r'at ([\d:]+) on (.+?)\.', header_html)
    instance_name = instance.group(1) if instance else 'IRIS'
    machine_name  = machine.group(1)  if machine  else ''
    user_name     = run_by.group(1)   if run_by   else ''
    run_time      = f"{run_at.group(1)} {run_at.group(2)}" if run_at else ''

    # --- build sidebar nav ---
    # Assign each section to a group
    grouped: dict[str, list[Section]] = {g: [] for g, _ in SECTION_GROUPS}
    grouped['Other'] = []
    section_to_group: dict[str, str] = {}
    for group_name, ids in SECTION_GROUPS:
        for sid in ids:
            if sid in sec_by_id:
                grouped[group_name].append(sec_by_id[sid])
                section_to_group[sid] = group_name
    for s in sections:
        if s.id not in section_to_group:
            grouped['Other'].append(s)
            section_to_group[s.id] = 'Other'

    nav_html = ''
    if synthesis:
        nav_html += '''
<div class="nav-group">
  <div class="nav-group-label" style="cursor:default">Overview</div>
  <div class="nav-group-items">
    <a href="#synth-panel" class="nav-item" onclick="document.getElementById('synth-panel').scrollIntoView({behavior:'smooth',block:'start'});return false;">
      &#9781; Performance Summary
    </a>
  </div>
</div>'''
    for group_name, group_sections in grouped.items():
        if not group_sections:
            continue
        items = ''
        for s in group_sections:
            is_selected = s.id in selected_set
            has_analysis = s.id in analysis
            is_reduced = mode in ('charts_only', 'charts_raw')
            if is_reduced and not has_analysis:
                continue
            dot = '<span class="analysis-dot" title="Analysis available"></span>' if has_analysis and not is_reduced else ''
            excluded_label = '<span class="excl-badge">excluded</span>' if not is_selected and not is_reduced else ''
            items += f'''
<a href="#sec-{s.id}" class="nav-item {'excluded' if not is_selected and not is_reduced else ''}" onclick="showSection('{s.id}')">
  {dot}{s.title}{excluded_label}
</a>'''
        if not items:
            continue
        nav_html += f'''
<div class="nav-group">
  <div class="nav-group-label" onclick="toggleGroup(this)">{group_name}<span class="chevron">▾</span></div>
  <div class="nav-group-items">{items}</div>
</div>'''

    # --- build section panels ---
    panels_html = ''
    for s in sections:
        is_selected = s.id in selected_set
        collapsed = s.id in COLLAPSED_BY_DEFAULT and s.id not in analysis
        pre_text = _extract_pre_text(s.content_html)

        if is_selected:
            analysis_block = f'<div class="analysis-block">{analysis[s.id]}</div>' if s.id in analysis else ''
            if mode == 'charts_only':
                content_block = analysis_block
            else:
                raw_collapsed = mode == 'charts_raw' and bool(analysis_block)
                raw_block = f'''
<div class="raw-toggle" onclick="toggleRaw(this)">
  <span>Raw data</span><span class="chevron">{'▸' if raw_collapsed else '▾'}</span>
</div>
<div class="raw-content" {'style="display:none"' if raw_collapsed else ''}>
<pre>{pre_text}</pre>
</div>'''
                content_block = analysis_block + raw_block
            excl_banner = ''
        else:
            if mode in ('charts_only', 'charts_raw'):
                content_block = ''
            else:
                content_block = f'''<div class="excluded-block">
  <div class="excl-icon">⊘</div>
  <div>
    <div class="excl-title">Section excluded by report author</div>
    <div class="excl-sub">Filtered using SysPerfSight - a free tool that redacts InterSystems IRIS SystemPerformance files locally, without uploading any data to the cloud.</div>
  </div>
</div>'''
            excl_banner = ''

        sensitive_banner = ''
        if s.sensitive and is_selected and mode == 'full':
            sensitive_banner = f'<div class="sensitive-banner">&#9888; This section may contain sensitive data: {s.sensitive_reason}</div>'

        # In reduced modes skip panels with no content (excluded or no analyzer)
        if mode in ('charts_only', 'charts_raw') and not content_block:
            continue

        panel_hidden = 'style="display:none"' if collapsed and is_selected else ''
        desc = SECTION_DESCRIPTIONS.get(s.title, '')
        desc_html = f'<p class="section-desc">{desc}</p>' if desc else ''
        panels_html += f'''
<div class="section-panel" id="sec-{s.id}" {panel_hidden}>
  <div class="section-header" onclick="toggleSection('{s.id}')">
    <div class="section-title-block"><h2>{s.title}</h2>{desc_html}</div>
    <span class="section-chevron" id="chev-{s.id}">{'▸' if collapsed and is_selected else '▾'}</span>
  </div>
  {sensitive_banner}
  <div class="section-body" id="body-{s.id}">
    {content_block}
  </div>
</div>'''

    # --- assemble full HTML ---
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SysPerfSight - {instance_name} {run_time}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}

:root{{
  --bg:#f0f2f5;--card:#fff;--text:#1a1a2e;--text-dim:#778;--text-muted:#999;
  --sidebar-bg:#fff;--sidebar-border:#dde3ee;
  --nav-item:#334;--nav-hover-bg:#f0f6ff;--nav-active-bg:#e8f0ff;--nav-group-color:#889;
  --excl-badge-bg:#f3f4f6;--excl-badge-color:#aaa;
  --section-border:#f0f2f5;--section-hover:#fafbff;
  --raw-bg:#f8f9fc;--raw-border:#e8edf5;
  --excl-block-bg:#fafafa;--excl-block-border:#ddd;--excl-icon:#ccc;--excl-title:#bbb;--excl-sub:#ccc;
  --sens-bg:#fffbeb;--sens-border:#f59e0b;--sens-color:#92400e;
  --scrollbar:#ddd;
  --topbar-badge:rgba(255,255,255,.15);
}}
body.dark{{
  --bg:#0f1117;--card:#1a1d27;--text:#e2e4ed;--text-dim:#8896b0;--text-muted:#6b7280;
  --sidebar-bg:#13161f;--sidebar-border:#252840;
  --nav-item:#b0b8d0;--nav-hover-bg:#1e2235;--nav-active-bg:#1a2540;--nav-group-color:#5a6480;
  --excl-badge-bg:#252840;--excl-badge-color:#6b7280;
  --section-border:#252840;--section-hover:#1e2235;
  --raw-bg:#13161f;--raw-border:#2d3148;
  --excl-block-bg:#13161f;--excl-block-border:#2d3148;--excl-icon:#3a4060;--excl-title:#4a5280;--excl-sub:#3a4060;
  --sens-bg:#2a1f00;--sens-border:#92400e;--sens-color:#fcd34d;
  --scrollbar:#2d3148;
  --topbar-badge:rgba(255,255,255,.1);
}}

body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column;height:100vh;overflow:hidden;transition:background .2s,color .2s}}

/* Top bar */
.topbar{{background:#003366;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0;z-index:10;box-shadow:0 2px 8px rgba(0,0,0,.3)}}
.topbar h1{{font-size:1rem;font-weight:700;white-space:nowrap}}
.topbar .meta{{font-size:0.75rem;opacity:.75;display:flex;gap:12px;flex-wrap:wrap}}
.topbar .meta span::before{{content:"·";margin-right:8px;opacity:.5}}
.topbar .badge{{background:var(--topbar-badge);border-radius:4px;padding:2px 8px;font-size:0.7rem;margin-left:auto;white-space:nowrap}}
.dark-toggle{{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);color:#fff;border-radius:5px;padding:4px 10px;font-size:0.75rem;cursor:pointer;white-space:nowrap}}
.dark-toggle:hover{{background:rgba(255,255,255,.22)}}

/* Layout */
.layout{{display:flex;flex:1;overflow:hidden}}

/* Sidebar */
.sidebar{{width:220px;background:var(--sidebar-bg);border-right:1px solid var(--sidebar-border);overflow-y:auto;flex-shrink:0;padding:12px 0;transition:background .2s}}
.nav-group{{margin-bottom:4px}}
.nav-group-label{{padding:6px 16px;font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--nav-group-color);cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none}}
.nav-group-label:hover{{color:#4d9fff}}
.nav-group-items{{padding:0 0 4px 0}}
.nav-item{{display:flex;align-items:center;gap:6px;padding:5px 16px 5px 24px;font-size:0.82rem;color:var(--nav-item);text-decoration:none;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:background .1s}}
.nav-item:hover{{background:var(--nav-hover-bg);color:#4d9fff}}
.nav-item.active{{background:var(--nav-active-bg);color:#003366;font-weight:600;border-right:3px solid #003366}}
body.dark .nav-item.active{{color:#4d9fff;border-right-color:#4d9fff}}
.nav-item.excluded{{color:var(--text-muted);font-style:italic}}
.excl-badge{{font-size:0.65rem;background:var(--excl-badge-bg);color:var(--excl-badge-color);border-radius:3px;padding:1px 4px;margin-left:auto;flex-shrink:0}}
.analysis-dot{{width:6px;height:6px;background:#0055aa;border-radius:50%;flex-shrink:0}}
.chevron{{font-size:0.7rem;transition:transform .2s}}
.chevron.open{{transform:rotate(180deg)}}

/* Main content */
.main{{flex:1;overflow-y:auto;padding:24px}}

/* Section panels */
.section-panel{{background:var(--card);border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.07);margin-bottom:16px;overflow:hidden;transition:background .2s}}
.section-header{{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;cursor:pointer;border-bottom:1px solid var(--section-border);user-select:none}}
.section-header:hover{{background:var(--section-hover)}}
.section-title-block{{display:flex;flex-direction:column;gap:2px}}
.section-header h2{{font-size:1rem;font-weight:600;color:#003366}}
body.dark .section-header h2{{color:#4d9fff}}
.section-desc{{font-size:0.75rem;color:var(--text-dim);font-weight:400;margin:0}}
.section-chevron{{font-size:0.85rem;color:var(--text-muted);transition:transform .2s}}
.section-body{{padding:16px 20px}}

/* Analysis block */
.analysis-block{{margin-bottom:8px}}

/* Raw data toggle */
.raw-toggle{{display:inline-flex;align-items:center;gap:6px;font-size:0.78rem;color:#0055aa;cursor:pointer;padding:4px 0;margin-bottom:6px;user-select:none}}
body.dark .raw-toggle{{color:#4d9fff}}
.raw-toggle:hover{{text-decoration:underline}}
.raw-content pre{{background:var(--raw-bg);border:1px solid var(--raw-border);border-radius:6px;padding:12px;font-size:0.75rem;line-height:1.5;overflow-x:auto;white-space:pre;max-height:500px;overflow-y:auto;color:var(--text)}}

/* Excluded */
.excluded-block{{display:flex;align-items:flex-start;gap:12px;padding:16px;background:var(--excl-block-bg);border:1px dashed var(--excl-block-border);border-radius:6px;color:var(--text-muted)}}
.excl-icon{{font-size:1.4rem;line-height:1;color:var(--excl-icon)}}
.excl-title{{font-weight:500;color:var(--excl-title);font-size:0.85rem}}
.excl-sub{{font-size:0.75rem;color:var(--excl-sub);margin-top:3px}}

/* Sensitive banner */
.sensitive-banner{{background:var(--sens-bg);border:1px solid var(--sens-border);border-radius:6px;padding:8px 14px;font-size:0.78rem;color:var(--sens-color);margin-bottom:12px}}

/* Scrollbars */
.sidebar::-webkit-scrollbar,.main::-webkit-scrollbar{{width:5px}}
.sidebar::-webkit-scrollbar-thumb,.main::-webkit-scrollbar-thumb{{background:var(--scrollbar);border-radius:3px}}
</style>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
</head>
<body>

<div class="topbar">
  <h1>SysPerfSight</h1>
  <div class="meta">
    {f'<span>{instance_name}</span>' if instance_name else ''}
    {f'<span>{machine_name}</span>' if machine_name else ''}
    {f'<span>{run_time}</span>' if run_time else ''}
    {f'<span>{user_name}</span>' if user_name else ''}
  </div>
  <span class="badge">SysPerfSight</span>
  <button class="dark-toggle" onclick="toggleDark()" id="dark-toggle">🌙 Dark</button>
</div>

<div class="layout">
  <nav class="sidebar">{nav_html}</nav>
  <div class="main" id="main-content">
    {synthesis}
    {panels_html}
  </div>
</div>

<script>
// Dark mode
(function(){{
  const saved = localStorage.getItem('pbtns_dark');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  if (saved === 'true' || (saved === null && prefersDark)) applyDark(true);
}})();
function applyDark(on){{
  document.body.classList.toggle('dark', on);
  const btn = document.getElementById('dark-toggle');
  if (btn) btn.textContent = on ? '☀️ Light' : '🌙 Dark';
}}
function toggleDark(){{
  const on = !document.body.classList.contains('dark');
  applyDark(on);
  localStorage.setItem('pbtns_dark', on);
}}

// Show only clicked section (scroll into view + highlight nav)
function showSection(id) {{
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const link = document.querySelector(`.nav-item[onclick*="${{id}}"]`);
  if (link) link.classList.add('active');

  const panel = document.getElementById('sec-' + id);
  if (!panel) return;
  panel.style.display = 'block';

  // Expand if collapsed
  const body = document.getElementById('body-' + id);
  const chev = document.getElementById('chev-' + id);
  if (body && body.style.display === 'none') {{
    body.style.display = 'block';
    if (chev) chev.style.transform = 'rotate(0deg)';
  }}

  panel.scrollIntoView({{behavior: 'smooth', block: 'start'}});
}}

// Toggle section collapse
function toggleSection(id) {{
  const body = document.getElementById('body-' + id);
  const chev = document.getElementById('chev-' + id);
  if (!body) return;
  const collapsed = body.style.display === 'none';
  body.style.display = collapsed ? 'block' : 'none';
  if (chev) chev.style.transform = collapsed ? 'rotate(0deg)' : 'rotate(-90deg)';
}}

// Toggle nav group open/closed
function toggleGroup(el) {{
  const items = el.nextElementSibling;
  const chev = el.querySelector('.chevron');
  const hidden = items.style.display === 'none';
  items.style.display = hidden ? 'block' : 'none';
  if (chev) chev.classList.toggle('open', hidden);
}}

// Toggle raw data visibility
function toggleRaw(el) {{
  const raw = el.nextElementSibling;
  const chev = el.querySelector('.chevron');
  const hidden = raw.style.display === 'none';
  raw.style.display = hidden ? 'block' : 'none';
  chev.textContent = hidden ? '▾' : '▸';
}}

// Activate first visible section on load
document.addEventListener('DOMContentLoaded', () => {{
  const first = document.querySelector('.nav-item:not(.excluded)');
  if (first) first.click();
}});

// Shared sortable-table utility used by all analyzer tables
(function() {{
  const _state = {{}};
  window.sortTable = function(tblId, col, th) {{
    const tbl = document.getElementById(tblId);
    if (!tbl) return;
    const tbody = tbl.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const key = tblId + ':' + col;
    const asc = !_state[key];
    _state[key] = asc;
    rows.sort((a, b) => {{
      const av = a.cells[col] ? a.cells[col].textContent.trim() : '';
      const bv = b.cells[col] ? b.cells[col].textContent.trim() : '';
      const an = parseFloat(av.replace(/[^0-9.+]/g, ''));
      const bn = parseFloat(bv.replace(/[^0-9.+]/g, ''));
      const cmp = isNaN(an) || isNaN(bn) ? av.localeCompare(bv) : an - bn;
      return asc ? cmp : -cmp;
    }});
    rows.forEach(r => tbody.appendChild(r));
    tbl.querySelectorAll('th').forEach(h => {{
      h.style.color = '';
      h.textContent = h.textContent.replace(/ [▲▼]$/, '') + ' ↕';
    }});
    th.textContent = th.textContent.replace(/ [▲▼↕]$/, '') + (asc ? ' ▲' : ' ▼');
    th.style.color = '#003366';
  }};
}})();
</script>
</body>
</html>'''
