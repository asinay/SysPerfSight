# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A web tool for analyzing InterSystems IRIS SystemPerformance HTML reports (formerly known as pButtons). Users upload a report file, select which sections to include, and download a filtered HTML file with inline analysis. Sensitive sections are pre-deselected with explanations. Selected sections get charts, insights, and cross-section synthesis injected above their raw data.

## Environment

Always use the `venv` virtual environment — never global pip or python.

```bash
# First-time setup
python -m venv venv
./venv/Scripts/pip install -r requirements.txt

# Run the dev server (no --reload — it leaves unkillable stale processes on Windows)
# If a port is squatted by a stale process, increment the port number (8001, 8002, ...)
# A full reboot clears all stale processes.
./venv/Scripts/uvicorn app:app --host 127.0.0.1 --port 8002
```

The app is then available at http://127.0.0.1:8000.

**If the repo folder gets renamed or moved**, every console-script shim in `venv/Scripts/` (`uvicorn.exe`, `pip.exe`, etc.) breaks silently — pip bakes the absolute path to `python.exe` into the shim at creation time, and an invalid path makes the `.exe` exit 1 with **no stdout/stderr at all**, even from `cmd.exe`. Symptom: `restart.bat` or a direct `venv\Scripts\uvicorn.exe ...` call does nothing. Fix: use `venv/Scripts/python.exe -m uvicorn ...` instead of the `.exe` shim — it goes through the current interpreter directly and is immune to the stale path. `restart.bat` already does this; if you hit the same silent-failure symptom with `pip.exe` or another shim, either use `python.exe -m pip ...` the same way, or regenerate all shims with `pip install --force-reinstall --no-deps -r requirements.txt`.

## Architecture

The app has three layers that must stay in sync:

**[sysperfsight_parser.py](sysperfsight_parser.py)** — Pure parsing logic, no web framework dependency.
- `parse_sections(html)` → `(header_html, [Section])`: splits the file into a header block (nav table + debug comment) and a list of `Section` dataclasses. Uses two regex patterns: one for `Configuration`/`Profile` which use `<div id="...">` (quoted), and one for all other sections which use `<div id=...>` (unquoted). If the primary patterns find zero sections, falls back to `_normalize_reserialized_html(html)` and retries once — this undoes browser DOM-reserialization (saving the report via "Save Page As" instead of the raw source rewrites the section-heading markup into a structurally different but visually identical form: quoted attributes, explicitly-closed void elements, and the div/title split across three separate `<b><font>` blocks instead of one). The fallback only swaps in the normalized text when it actually produces matches, so it never runs — and can't regress — on files the primary patterns already handle.
- `build_output(header_html, sections, selected_ids, analysis, synthesis, mode)` → `str`: iterates **all** sections — selected ones keep their full content, excluded ones show a placeholder. Injects `analysis[section_id]` HTML above each section's raw data block when present. `mode` controls output depth: `'full'` (default) includes charts + insights + raw data + synthesis + sensitive banners; `'charts_raw'` strips insights and synthesis, keeps raw data collapsed; `'charts_only'` strips everything except charts, hides excluded panels entirely. The output's `.topbar` deliberately mirrors [static/index.html](static/index.html)'s app header — same `--header-bg`/`--header-border`/`--accent`/`--font-mono` CSS vars and values (light `#0d9488` / dark `#2dd4bf`), the `> SysPerfSight` monospace wordmark, and the same sun/moon-icon dark-toggle button markup+JS — so a downloaded report looks like it came from the same tool that generated it. Keep these two headers in sync if either one's palette or wordmark changes; the rest of the report body (sidebar, section panels, nav accent color) intentionally keeps its own separate blue-accent theme, unchanged by this.
- `_make_excluded_html(content_html)`: strips all `<pre>…</pre>` blocks then inserts `_EXCLUDED_PLACEHOLDER` at the position of the first `<pre>`. The strip-then-insert order is important — do not replace-then-strip or the placeholder itself gets removed.
- `SENSITIVE_SECTIONS` dict maps section **titles** to human-readable reasons — drives UI warnings and default deselection.
- `SECTION_DESCRIPTIONS` dict maps section **titles** to one-line descriptions shown in the output sidebar.
- `SECTION_GROUPS` list defines sidebar nav groups using section **IDs** (not titles).
- `COLLAPSED_BY_DEFAULT` set of section **IDs** whose raw data panel starts collapsed.

**[app.py](app.py)** — FastAPI backend with two endpoints:
- `POST /upload` — accepts a multipart `.html` file, calls `parse_sections`, stores result in the in-memory `sessions` dict keyed by UUID, returns section metadata (id, title, sensitive flag, reason, time_filterable). Raises 400 with a diagnostic hint (no `noshade` marker at all vs. markers present but unrecognized layout) when `parse_sections` returns zero sections, instead of silently succeeding with an empty list.

**[diagnose_report.py](diagnose_report.py)** — Standalone CLI for users hitting the "No sections found" 400. Runs the real `parse_sections` when importable, otherwise falls back to raw regex probes for `noshade` and `<div id=`. Every excerpt it prints is truncated before the next `<pre` tag (where sensitive report data lives), so its output is safe to paste into a bug report without needing the actual file shared.
- `POST /export` — accepts `{session_id, selected_ids, output_filename, time_from, time_to, mode}`, applies time filters, runs analyzers in parallel (skipping synthesis when `mode` is not `'full'`), strips `<!--INS-->…<!--/INS-->` insight blocks when `mode` is `'charts_only'` or `'charts_raw'`, calls `build_output`, returns the result as a file download.

Sessions are in-memory only — lost on server restart. The `uploads/` directory exists but files are not written there; only `outputs/` gets written.

**[static/index.html](static/index.html)** — Single-file vanilla JS frontend (no build step). Communicates with the backend via `fetch`. Key flow: drag-drop/select file → `POST /upload` → render section checklist → user toggles sections + optionally sets time range → one of three export buttons → `POST /export` → trigger browser download via blob URL. The file is never opened automatically after download.

Three export buttons call `exportFile(mode)`:
- **Full report** (`'full'`) — complete output: charts, insights, raw data, synthesis, sensitive banners.
- **Charts + Raw** (`'charts_raw'`) — charts + raw data (collapsed); no insights, no synthesis. Filename gets `_charts_raw` suffix.
- **Charts only** (`'charts_only'`) — charts only; no raw data, no insights, no synthesis, excluded sections hidden. Filename gets `_charts` suffix.

On upload, the output filename field is auto-filled as `{source_stem}_{YYYYMMDD_HHMM}.html` using the uploaded file's name and the current local time. The user can edit it freely; mode suffixes are appended to whatever name is in the field.

The header's `.header-links` (and the matching page footer) hold three outbound links: **[static/help.html](static/help.html)** (a standalone FAQ page — not part of the FastAPI app's `/upload`/`/export` flow, opened in a new tab — covering the three export modes, time-range filtering, sensitive-section handling, and the "No sections found" upload error; deliberately *not* a how-to for generating a SystemPerformance/pButtons report, since anyone reaching this app already has one), and GitHub-hosted **Report bug** / **Request feature** links (`.../issues/new?labels=...`, no issue templates). All internal links use paths relative to the current document (`static/help.html`, `upload`, `export`), not root-absolute (`/static/help.html`), so the app keeps working when reverse-proxied under a subpath — see the relative-path fix applied for [asinay/logilyzer#1](https://github.com/asinay/logilyzer/pull/1).

`uploadFile`/`exportFile` in [static/index.html](static/index.html) never assume a failed `fetch` response body is JSON — `describeFailedResponse(res, verb)` tries `res.json()` and falls back to `HTTP <status> <statusText>` (with a specific hint on 413) when it isn't. A reverse proxy or gateway in front of the app can reject a request (oversized upload, auth, routing) and return its own HTML error page before the request ever reaches FastAPI; calling `res.json()` unconditionally on that produces an opaque `Unexpected token '<'` browser error instead of something a user or admin can act on. Confirmed in production behind an internal reverse proxy that was rejecting large SystemPerformance report uploads with a 413.

## SystemPerformance HTML format

The file uses `iso-8859-1` encoding. Section boundaries are `<hr size="4" noshade>` followed by a bold font tag containing a `<div id=SECTIONID>` anchor. The first two sections (Configuration, Profile) use quoted IDs (`<div id="Configuration">`) while all subsequent sections use unquoted IDs (`<div id=mgstat>`). Each section ends with a "Back to top" link before the next `<hr>`. The HTML *structure* (hr/div/pre nesting) is identical whether generated by old Caché pButtons or the new IRIS SystemPerformance tool — but older Caché reports use **different literal section ids/titles** for a few functionally-identical sections (Windows Caché confirmed; the same is plausible on Caché/Linux since it's the same underlying tool):

| IRIS id / title | Caché id / title seen in the wild |
|---|---|
| `IRISALL` / "IRIS ALL" | `ccontrolall` / "ccontrol all" |
| `License` / "License" | `license` / "license" (lowercase) |
| `CPFfile` / "CPF file" | `cpffile` / "cpf file" (lowercase) |
| `irisstat-c1` / "irisstat -c1" | `cstat-c1` / "cstat -c1" |
| `irisstat-D` / "irisstat -D" | `cstat-D` / "cstat -D" |
| `irisstat-R` / "irisstat -R" | `cstat-R` / "cstat -R" (not yet seen, added by analogy) |

`CACHE_SECTION_ALIASES` + `_canonicalize()` in `sysperfsight_parser.py` map the Caché variant to the canonical IRIS id/title **at parse time**, keyed by the raw id lowercased — so every downstream id- or title-keyed dict below only needs the IRIS entry, and IRIS's own ids/titles pass through as a no-op (verify with `_canonicalize('IRISALL', 'IRIS ALL')` if extending this map — it must return the input unchanged). Without this, Caché reports silently lose sensitive-section flagging on `license`/`cpf file`/`ccontrol all`, and `cstat -D`/`cstat -c1` never reach their analyzer at all.

**Important key distinction across dicts:**
- `SENSITIVE_SECTIONS`, `SECTION_DESCRIPTIONS`, `TITLE_TIME_FILTERS` — keyed by section **title** (the heading text, e.g. `"sar -d"`)
- `SECTION_ANALYZERS`, `SECTION_GROUPS`, `COLLAPSED_BY_DEFAULT` — keyed by section **ID** (the div id value, e.g. `"sar-d"`)

When adding a new section, update both the title-keyed and ID-keyed dicts as appropriate. If it's a section known to vary between IRIS and Caché, add the Caché variant to `CACHE_SECTION_ALIASES` instead of duplicating entries across every dict.

## Sections reference

IDs/titles below are the canonical IRIS ones. Older Caché reports use different literal ids/titles for `IRIS ALL`/`License`/`CPF file`/`irisstat -c1`/`irisstat -D`/`irisstat -R` — see the alias table under [SystemPerformance HTML format](#systemperformance-html-format).

### Common to Windows and Linux

| Title | Section ID | Sensitive | Analyzer | Time-filterable | Notes |
|---|---|---|---|---|---|
| Configuration | Configuration | ✓ | — | — | quoted div id |
| Profile | Profile | ✓ | — | — | quoted div id |
| IRIS ALL | IRISALL | ✓ | — | — | |
| License | License | ✓ | — | — | |
| CPF file | CPFfile | ✓ | — | — | collapsed by default |
| mgstat | mgstat | — | ✓ | ✓ | 24-hour CSV timestamps |
| %SS | %SS | — | ✓ | — | per-process snapshots |
| irisstat -c1 | irisstat-c1 | — | — | — | collapsed by default |
| irisstat -D | irisstat-D | — | — | — | collapsed by default |
| irisstat -R | irisstat-R | — | — | — | collapsed by default |

### Linux-only

| Title | Section ID | Sensitive | Analyzer | Time-filterable | Notes |
|---|---|---|---|---|---|
| Linux info | Linuxinfo | — | — | — | |
| cpu | cpu | — | ✓ | — | |
| ipcs | ipcs | — | — | — | |
| fdisk -l | fdisk-l | — | — | — | root access only; partition tables for all block devices |
| mount | mount | — | — | — | |
| df -m | df-m | — | — | — | |
| ifconfig | ifconfig | — | — | — | |
| sysctl -a | sysctl-a | — | ✓ | — | |
| ps | ps | — | — | — | |
| vmstat | vmstat | — | ✓ | ✓ | MM/DD/YY HH:MM:SS timestamp; header row has same timestamp format as data rows — skip rows where first value is non-numeric |
| sar -u | sar-u | — | — | ✓ | AM/PM timestamps on some locales |
| free | free | — | ✓ | — | |
| iostat | iostat | — | ✓ | ✓ | AM/PM timestamps on some locales |
| sar -d | sar-d | — | ✓ | ✓ | AM/PM timestamps on some locales |

### Windows-only

| Title | Section ID | Sensitive | Analyzer | Time-filterable | Notes |
|---|---|---|---|---|---|
| Windows info | Windowsinfo | ✓ | ✓ | — | |
| tasklist | tasklist | ✓ | ✓ | — | collapsed by default |
| perfmon | perfmon | — | — | — | collapsed by default |

## Time filter system

**[analyzers/time_filter.py](analyzers/time_filter.py)** — filter functions keyed by section **title** in `TITLE_TIME_FILTERS`.

- UI sends `time_from` / `time_to` as `HH:MM` 24-hour strings from plain `<input type="text">` fields with auto-formatting (`timeInput` / `timeBlur` JS helpers). Values are persisted to `localStorage` and restored on page load. Clear button wipes both fields and storage.
- `_parse_hhmm(s)` accepts `HH:MM` only — never AM/PM.
- `_in_range(t, lo, hi)` supports open-ended ranges (one bound is `None`) and midnight-crossing ranges (when `lo > hi`).
- `filter_mgstat` — matches CSV rows by `MM/DD/YYYY, HH:MM:SS` at the start of each line. mgstat always uses 24-hour timestamps.
- `filter_iostat` — groups lines into timestamp blocks; each block starts with a standalone `MM/DD/YYYY HH:MM:SS [AM|PM]` line. Parses with `strptime` trying `%I:%M:%S %p` then `%H:%M:%S`.
- `filter_sar` — filters line-by-line using `_SAR_DATA_RE` (verbose regex with named group `(?P<ts>...)` that handles optional date prefix and optional AM/PM suffix). **Must use `m.group('ts')` not `m.group(1)`** — the named group is not group 1 in the verbose regex. `_parse_sar_ts()` detects AM/PM presence with `re.search(r'\b[AP]M\b')` and uses `strptime('%I:%M:%S %p')` for 12-hour or `strptime('%H:%M:%S')` for 24-hour. Real Linux sar -d output uses `HH:MM:SS AM/PM` format (e.g. `03:45:09 PM`) — always parse with strptime, never manual arithmetic.

**Critical AM/PM rule**: never convert AM/PM timestamps by hand (e.g. `h + 12`). Always use `strptime` with `%I:%M:%S %p`. Manual arithmetic silently fails on edge cases like `12:00 AM` (midnight) and was the root cause of the filter bug where `03:45 PM` was incorrectly matched by a `03:45` filter.

When adding a new time-filterable section, add a `filter_*` function and register it in `TITLE_TIME_FILTERS` using the section **title**. Do not use the section ID here — titles are more stable for sar/vmstat sections whose div ids vary across report versions.

## Analyzer system

**[analyzers/__init__.py](analyzers/__init__.py)** — `SECTION_ANALYZERS` maps section **ID** → `async analyze(section_text) -> str`.

Each analyzer module (`analyzers/*.py`) exposes an `async analyze(section_text: str) -> str` that:
1. Parses the plain text extracted from the section's `<pre>` block(s).
2. Returns an HTML fragment (charts + insight flags) to inject above the raw data, or `''` if the section can't be parsed.

**Insight markers**: every analyzer wraps its `insights_html` assignment in `<!--INS-->…<!--/INS-->` comment markers so that `app.py` can strip the insights block with a single regex when `mode` is `'charts_only'` or `'charts_raw'`. Stat cards and chart HTML are placed **outside** these markers and are always retained. Pattern at assignment site:
```python
insights_html = '<!--INS-->' + f'<div ...>...</div>' + '<!--/INS-->'
```
Never wrap the entire analyzer return value — only `insights_html`. Stat cards (`_stat(...)`) and chart HTML go outside the markers.

Current analyzers:

| Section ID | Module | What it produces |
|---|---|---|
| Windowsinfo | windows_info.py | OS/hardware summary cards |
| tasklist | tasklist.py | Top processes by memory |
| mgstat | mgstat.py | 4-row × 2-col chart grid: Glorefs, PhyRds/Wrs, Jrnwrts+WDQ, Rourefs, GblSz, BytSnt/Rcd, Gloupds, Jrnwrts (dedicated); stat cards; insights including NSeize/ASeize contention, WD phase saturation, routine cache misses |
| iostat | iostat.py | %util, CPU iowait, IOPS, throughput, latency charts; insights. %util and CPU-iowait charts carry dashed threshold reference lines at the same cutoffs as their insight flags; latency chart carries a 1 ms reference line |
| cpu | cpu.py | CPU topology summary |
| %SS | ss.py | Process type breakdown, TCP trend, top-CPU/Glob tables, processes-per-namespace table, top-5-routines-by-concurrent-count table; insights |
| sar-d | sar_d.py | %util, tps, throughput, r/w latency, queue depth charts; per-device summary table with latency baselines; insights. %util chart carries 70%/40% reference lines, latency chart a 1 ms reference line |
| sar-u | sar_u.py | Stacked area CPU chart (user/sys/iowait/steal + idle dotted); stat cards from Average: line; insights for saturation/steal/iowait. Falls back to fixed column order when header line absent. Chart carries 90%/75% "%used" reference lines matching the idle-based insight thresholds |
| vmstat | vmstat.py | Run queue + blocked, swap I/O, CPU breakdown, block I/O charts; stat cards; insights. Skips header row by checking first value is numeric. Run-queue chart carries 8/4 reference lines; CPU breakdown chart an 80%-busy reference line |
| irisstat-D | irisstat_d.py | Lock contention summary table (sortable) + per-second rates table (sortable); insights flagging Bseize>0 on Global/LockHTAB/LockLHB/TransCB; block collision flags. Handles Linux (4-col) and Windows (6-col) column variants |
| free | free.py | RAM usage chart (used/buffers/cached), adjusted free RAM trend, swap used trend; stat cards; insights on low adjfree % and swap usage. Parses CSV header dynamically — columns mapped by name, tolerates missing trailing swap columns |
| sysctl-a | sysctl.py | Compliance table (current vs. recommended) for IRIS-relevant kernel parameters; red/amber/green status per row; insights for critical misconfigs (swappiness, NUMA balancing, shmmax, semaphores, huge pages, file-max, TCP backlog) |
| ps | ps.py | Stat cards (total/user-space/IRIS process count, IRIS RSS, D-state count, snapshot count); RSS by user horizontal bar chart; top-15 processes by RSS table; IRIS job-type breakdown table; insights for D-state processes and IRIS process count changes across snapshots |
| df-m | df_m.py | Stacked used/available bar chart per mount point; full table sorted by Use% with colour-coded badge (green/amber/red); stat cards; insights flagging ≥90% (red) and 75–89% (amber) filesystems, noting IRIS paths explicitly; virtual filesystems excluded |
| irisstat-R | irisstat_r.py | Routine buffer pool snapshot: stat cards (buffers loaded, pool memory MB, in-use count, old/M/D counts, class descriptor inuse+LRU); pool config cards (buffer counts per size tier); currently in-use routines table; top-20 packages by buffer count with memory and in-use; buffer type breakdown (P/M/D); insights for class LRU evictions, pool near capacity, high old% |
| mount | mount.py | Stat cards (total/local/network/virtual/ro counts); filesystem type summary table; local FS table (mount point, device, type, notable options); network mounts table (mount point, source, type, version, soft/hard mode, rw/ro, block sizes, server IP); insights for soft network mounts on IRIS paths (amber), read-only IRIS paths, NFS sync option |
| perfmon | perfmon.py | CPU, processor queue, memory, paging, network charts (single system-wide series); disk %busy/queue/IOPS/throughput/latency charts **broken out per physical drive** (`0 C:`, `1 D:`, ...) — see note below; insights and reference lines to match. mgstat has the equivalent role on Linux/Caché for IRIS metrics; perfmon is Windows-only and purely OS-level |

`_parse_perfmon` returns **two** DataFrames, not one: `flat_df` (one row per timestamp, one column per system-wide counter — cpu_total, mem_avail, proc_queue, etc.) and `disk_df` (long format: `dt`/`instance`/`metric`/`value`, one row per timestamp per physical disk per counter). This split exists because a naive single flat frame can only hold one series per counter name — with multiple physical disks that meant silently keeping only the `_Total` instance if present, or otherwise just whichever disk happened to appear first in the CSV header, discarding every other drive's data. On a system with separate OS/DB/journal/WIJ drives (the common case for IRIS/Caché on Windows) that could mean charting and alerting on the wrong disk entirely. `_match_counter` returns `(logical_name, instance)` — `instance` is `''` for non-disk counters (collapse to one column as before) and the raw PDH instance string (e.g. `'1 d:'`) for `physicaldisk` counters, so each drive keeps its own series. When adding a new per-instance-capable counter (e.g. per-NIC network stats), follow the same `disk_df`-style long-format pattern rather than trying to force it into a flat column.

### Shared UI patterns

`_flag(level, text)` — renders a coloured insight pill. Levels: `'red'`, `'amber'`, `'info'`, `'green'`.

`_stat(label, value, unit)` — renders a summary stat card.

Charts use Plotly (loaded from CDN in the output HTML). Call `fig.to_html(full_html=False, include_plotlyjs=False, ...)` — the CDN script tag is already in the output template.

**Threshold reference lines**: `iostat.py`, `sar_d.py`, `sar_u.py`, `vmstat.py`, `perfmon.py` draw dashed horizontal reference lines on their charts via a `ref_lines: dict[int, list[tuple]]` mapping `row_idx -> [(y, color, label), ...]`, populated at `len(row_defs) + 1` (the row's future 1-based index) right before each conditional `row_defs.append(...)`, then drawn in one pass after all subplot traces are added:
```python
for row_idx, lines in ref_lines.items():
    for y, color, label in lines:
        fig.add_hline(y=y, row=row_idx, col=1, line=dict(color=color, width=1, dash='dash'),
                      annotation_text=label, annotation_position='top left',
                      annotation_font=dict(size=9, color=color), opacity=0.7)
```
Prefer reusing a chart's **own existing insight thresholds** for the line values (e.g. sar-d's %util lines sit at 70/40, matching its red/amber insight cutoffs) rather than inventing new numbers — keeps the chart and the insight text making the same claim. Only reach for an unconditional convention value (the 80%-CPU-busy line, the 1 ms latency reference) where the file has no existing threshold to mirror.

**p99 instead of raw max in insight text**: `mgstat.py`, `iostat.py`, `sar_d.py`, `sar_u.py`, `vmstat.py`, `perfmon.py` compute the "peak" shown in insight flag text with `.quantile(0.99)` rather than `.max()`, so one freak single-sample spike doesn't dominate the displayed number or (where the peak *drives* the flag's severity, not just its text) the trigger itself. Label it "p99 peak" in the text so it's clear this isn't the absolute max. Two deliberate exceptions keep `.max()`: `iostat.py`'s bursty-write detector (`d.max() / d.mean() > 5`) is specifically about spike ratios, so smoothing away the spike would defeat its purpose; `vmstat.py`'s swap-committed check (`swpd_max`) is an existence check on a slowly-changing value, not a jittery rate, and a brief-but-real swap event is exactly the kind of thing a percentile could mask. When a flag's severity used to be triggered directly off `.max()` (vmstat's blocked-process amber branch was), switch the *trigger* to the p99 value too, but keep the true max visible in the flag text alongside it so nothing is hidden.

### mgstat column names (normalised after CSV parse)

Key columns: `Glorefs`, `Gloupds`, `PhyRds`, `PhyWrs`, `Jrnwrts`, `WDQsz`, `WDphase`, `Wijwri`, `WDtmpq`, `Rourefs`, `RouLaS`, `RouCMs`, `RemGrefs`, `RemRrefs`, `GblSz`, `pGblNsz`, `pGblAsz`, `RouSz`, `pRouAsz`, `BDBSz`, `BytSnt`, `BytRcd`, `Rdratio`, `PPGrefs`, `PPGupds`.

- `pGblNsz` / `pGblAsz` — % of NSeizes / ASeizes on global resource. NSeizes = process hibernated (expensive OS context switch, burns %system). ASeizes = spin-waited and got resource (cheaper, burns %user). High NSeize % is the more serious signal.
- `WDphase` — write daemon phase: 0=idle, 5=WIJ write, 7=commit, 8=DB update. Sustained phase 8 means writes are maxing out the WD cycle.
- `RouLaS` / `RouCMs` — routine loads/saves and cache misses. High values indicate routine buffer pressure; increase routine buffer allocation.

### %SS snapshot format

Each snapshot starts with `<product> System Status: <time>` — the product prefix varies (`InterSystems IRIS` on IRIS, `Cache` on older Caché); `analyzers/ss.py`'s `_SNAP_HDR_RE` only matches the `System Status:` suffix, not the prefix, so both are recognized for free. (This was a real bug once: the regex used to require the `InterSystems IRIS` prefix literally, so Caché's `%SS` section silently produced zero snapshots and zero output — no error, just nothing.) Fixed column positions (0-based): pid=0–9, device=10–21, ns=22–36, routine=37–end (until cpu,glob numbers). CPU and Glob values are **cumulative since process start** — the analyzer computes deltas between consecutive snapshots for rates. Routine field: read from col 37 to the start of the cpu,glob pair on the same line (not hard-capped at col 53) to avoid truncating long class method names like `EnsLib.TCP.InboundAdapter`.

### irisstat -D / cstat -D resource stats format

Three blocks per sample: `RESOURCE STATS OVER A N-SECOND INTERVAL`, `RESOURCE % STATS`, `RESOURCE /sec STATS`. The count and % blocks put their `seize Nseize Aseize Bseize...` column header on its **own line** below the block title; the `/sec` block puts it **inline on the same line** as `RESOURCE /sec STATS`. `analyzers/irisstat_d.py`'s `_RATE_RE` must join the block title and column header with `\s+` (matches inline-with-spaces or on-a-separate-line-with-a-newline, either format) rather than a literal `.*?\n` — the latter requires a newline to exist between them, which silently never matches the `/sec` block's real layout. This was a real bug: the "Per-Second Rates" table had never rendered for anyone, IRIS or Caché, until this was loosened. `_ROW_RE` also can't match resource names containing a hyphen (e.g. `Per-BDB` — `\w+` stops at the `-`), so that one resource is silently dropped from every block; harmless today since it isn't in `_KEY_RESOURCES` and isn't summed anywhere, but worth knowing if a future insight needs it.

### sar -d column variations

Old sysstat: `tps rd_sec/s wr_sec/s avgrq-sz avgqu-sz await svctm %util` — `rd_sec/s`/`wr_sec/s` are 512-byte sectors, divided by 2 to get kB/s.
New sysstat: `tps rkB/s wkB/s areq-sz aqu-sz await r_await w_await svctm %util`.
The analyzer handles both formats transparently via `col_map`.

## Cross-section synthesis system

**[analyzers/synthesis.py](analyzers/synthesis.py)** — `synthesize(section_texts: dict[str, str]) -> str`, called once after all per-section analyzers finish, with the full `{section_id: raw_text}` dict for every *selected* section. Produces the "Performance Summary" panel injected at the top of the report (only when `mode == 'full'`; skipped otherwise, same as the rest of synthesis). Unlike every other analyzer, this one deliberately looks across sections — it's the only place that can correlate, say, mgstat physical reads with a `free` memory squeeze, or a CPF-declared database path with an iostat device.

Structure:
- A fixed list of `(title, sub_id, fn)` checks in `synthesize()` — `Memory`, `CPU`, `Disk & Storage`, `IRIS Health`, `Filesystem View`. Each `fn(texts) -> (level, html) | None` is independent and defensive — wrapped in try/except, returns `None` (silently omitted) if its required sections aren't present or don't parse. Needs ≥2 non-`None` results or the whole panel is skipped (a single lonely finding isn't worth a whole panel).
- Each check does its own **lightweight, local** line/regex parsing of the raw section text rather than importing the dedicated analyzer's parser (e.g. `_analyse_disk` has its own tiny mount-line regex, distinct from `mount.py`'s full `_parse_mounts`). This is deliberate — keeps each check's failure mode contained and avoids coupling this module to every other analyzer's internal parsing choices.
- `_pill`/`_pill_grouped` render one finding, with a `<details>` "Show evidence" block naming the source section and the exact threshold that fired — every synthesis finding must be traceable back to a concrete number, not just an assertion.
- A top-level `_signal_strip` (clickable red/amber/green chips) sits above the collapsible subsections; sections other than red-level ones start collapsed.

### IRIS-relevant disk cross-referencing

`_analyse_disk` used to scan iostat/sar-d/perfmon text for peak %util **indiscriminately across every device** — a spike on a completely unrelated disk could trigger a "disk saturation" warning that had nothing to do with IRIS. It now cross-references the CPF file's `[Databases]` directories against the actual disk layout, when both are present in the selected sections, and scopes the check to just the IRIS-relevant device(s):
- `_parse_cpf_database_dirs(cpf_text)` — pulls directory paths out of the CPF's `[Databases]` section only (works for both Windows and Linux paths; trailing comma-separated flags like `,,1,,1` in a Caché database entry are stripped by taking only the first comma-separated field).
- **Linux**: `_parse_mount_devices(mount_text)` gets (mountpoint, device) pairs from the `mount` section; `_resolve_iris_devices(cpf_dirs, mount_pairs)` maps each CPF directory to its backing device via longest-mountpoint-prefix match, giving a `set[str]` of device names (e.g. `{'dm-7'}`). `_is_cpf_iris_mountpoint` does the reverse lookup (mountpoint → is a CPF dir under it?) and is unioned with the pre-existing `_IRIS_TAGS`/`_IRIS_PATHS` keyword heuristic in `_analyse_disk` and `_analyse_filesystem_combined` — CPF match adds precision, the heuristic still catches cases CPF didn't cover (or wasn't selected for export, since `CPFfile` is sensitive-and-deselected-by-default).
- **Windows**: `_resolve_iris_drive_letters(cpf_dirs)` extracts the drive letter directly from a Windows CPF path (e.g. `D:\CacheSys\mgr\` → `d:`) — no `mount` section exists on Windows, so this doesn't need one. `_parse_perfmon_disk_busy(perfmon_text)` does its own minimal PDH-CSV column parse (mirrors `perfmon.py`'s `_match_counter`, but only for the `% Disk Time` counter) to get `(drive_instance, value)` pairs, matched against the resolved letters by checking if the letter appears as a token in the instance string (`'d:' in '1 d:'.split()`).
- `_parse_device_util(text, sid)` does the equivalent for Linux `iostat`/`sar-d` text. **Timestamp parsing gotcha**: sar -d's 12-hour timestamp is *two* space-separated tokens (`03:45:00 PM`), so a naive `line.split()[1]` grabs `PM`, not the device — `_parse_device_util` uses a regex matching the whole timestamp (`\d{2}:\d{2}:\d{2}(?:\s*[AP]M)?`) as one unit before capturing the device, and iostat's `avg-cpu:` values row (e.g. `10.00 0.00 2.00 1.00 0.00 87.00`) has a numeric first token that can look like a device name unless explicitly excluded.
- In all cases: when the IRIS-relevant device/drive set is non-empty, the peak-%util pill scopes to just those and says so (`— scoped to IRIS disk(s) dm-7` / `— scoped to IRIS drive(s) d:`); when it's empty (CPF or mount/perfmon not selected, or nothing cross-referenced), it falls back to the old indiscriminate-scan behavior rather than silently going quiet.

## .gitignore

`/*.html` (root-level only) excludes SystemPerformance data files while keeping `static/index.html` tracked. Never broaden this to `**/*.html`.

## Reusable skill

A global Claude Code slash command `/iris-report-parser` at `~/.claude/commands/iris-report-parser.md` captures the full recipe for building similar tools for other InterSystems IRIS HTML reports.
