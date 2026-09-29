# Performance notes

Lessons from diagnosing a report export that went from "never finishes" to ~2 minutes to ~15s (see `dev-logs/2026-09-28.md` for the full narrative). Kept here so the next slow analyzer gets fixed in one pass instead of rediscovering these.

## Diagnosing "why is this slow"

`app.py` prints `[timing] ...` lines around each major export stage (`prepare_sections`, each analyzer, `synthesis`, `build_output`), and `analyzers/iostat.py` has finer-grained ones inside `_parse_iostat` and `analyze()`. These are unconditional `print(..., flush=True)` calls, not behind a log-level flag — cheap enough to leave in production, and they're what let a real 2-minute slowdown get diagnosed from `docker logs` alone, without ever needing the user's actual (large, sensitive) report file.

When a new analyzer or a specific report is slow, add the same style of timing print around suspected stages before reaching for a profiler. It works inside `ProcessPoolExecutor` workers too — child process stdout is inherited by the parent on both Linux (`fork`) and Windows (`spawn`), so `docker logs`/console output still shows it.

**Signal to watch for:** if a 2x bigger input causes a 30x+ slowdown, that's not "bigger file, proportionally slower" — it's evidence of an algorithmic anti-pattern (see below), worth digging into rather than accepting.

## Anti-patterns found so far (check new/edited analyzers for these)

Both of these were found in `analyzers/iostat.py` on a report with 75 disk devices — 528 chart traces and repeated full-dataframe scans turned a few seconds into two minutes. Neither is `iostat`-specific; any analyzer that loops over a per-device/per-instance list (that could grow large on a real system) is a candidate.

**1. `fig.add_trace()` called in a loop.** Plotly's `add_trace()` revalidates the whole figure on every call, so N calls scales worse than O(N) — building 528 traces one at a time took 94s; batching them into one `fig.add_traces([...])` call took 0.1s for the same traces. If a chart loops over devices/instances, build a list of `(trace, row)` tuples and call `fig.add_traces()` once at the end instead of `fig.add_trace()` inside the loop.

Also consider capping how many series get charted (e.g. top-N by the metric that matters) — a chart with 75 overlapping lines isn't readable anyway, so a cap is a UX improvement, not just a performance one. Keep any summary table/insights covering the *full* device list even when the chart itself is capped — see `iostat.py`'s `top_chart_devs` (capped, used for chart traces) vs `chart_devs` (uncapped, used for the table and per-device insight flags).

**2. Filtering the same dataframe in a loop.** `dev_df[(dev_df['device'] == dev) & (dev_df['metric'] == metric)]` called once per (device, metric) pair inside a loop is O(pairs × rows) — each call rescans the whole frame. Group once instead: `{k: v for k, v in dev_df.groupby(['device', 'metric'])}`, then look up by key inside the loop (O(1) per lookup). This exact pattern showed up twice in the same file — once building chart traces, once in an insight check ("bursty writes") that iterated the same device list — so check every loop over a device/instance list in a module, not just the obvious chart-building one.

## Concurrency model (why threads *and* processes)

`app.py` uses two different executors for a reason:

- **`_EXECUTOR` (`ThreadPoolExecutor`)** — for `parse_sections`, `_prepare_sections_sync`, `synthesis`, and `build_output`. These are single sequential tasks with nothing to parallelize against; a thread just keeps the event loop free to serve other requests while they run.
- **`_ANALYZER_POOL` (`ProcessPoolExecutor`)** — for the per-section analyzers specifically, because there are multiple of them that can genuinely run at the same time. Analyzers are pure CPU work (pandas/regex/Plotly) with nothing to `await`, so N threads mostly serialize on the GIL regardless of core count; N separate processes don't, since each has its own interpreter/GIL.

`ProcessPoolExecutor` created at module level is safe here on both Windows (`spawn`) and Linux (`fork`) — constructing the pool object doesn't start any worker processes (workers start lazily on the first `.submit()`), so a spawned child re-importing `app.py` to resolve a pickled function reference doesn't recursively spawn more processes.

Worker count defaults to 4 (`SYSPERFSIGHT_ANALYZER_WORKERS` env var to override) to match `docker-compose.yml`'s CPU limit — keep these in sync if one changes.

## Not yet audited

`sar_d.py`, `perfmon.py`, and any other analyzer with a per-device/per-instance charting loop haven't been checked for the same two anti-patterns. They're plausible candidates if a report with an unusually large device/instance count surfaces slowness there — same diagnostic approach (timing prints, then check for `add_trace`-in-a-loop or `df[mask]`-in-a-loop) should find it quickly.
