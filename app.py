import uuid
import asyncio
import copy
import os
import re as _re
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sysperfsight_parser import parse_sections, build_output
from analyzers import SECTION_ANALYZERS
from analyzers.time_filter import TITLE_TIME_FILTERS
from analyzers.synthesis import synthesize

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Parsing/analysis/build_output are all synchronous CPU-bound work. Running them
# directly in the async endpoint would block the entire single-process event loop
# for the whole duration on a large file — no other request (including a health
# check or a second browser tab) could be served, and there'd be no way to time
# out a request that's taking too long. Routing them through this thread pool
# keeps the event loop free and lets asyncio.wait_for enforce a deadline below.
_EXECUTOR = ThreadPoolExecutor(max_workers=max(4, os.cpu_count() or 4))

# Safety nets, not expected durations — normal files should finish in well under
# these. They exist so a pathological file fails fast with a clear message
# instead of hanging the request forever.
UPLOAD_TIMEOUT_S = 180
EXPORT_TIMEOUT_S = 900


@asynccontextmanager
async def lifespan(_app):
    yield
    _EXECUTOR.shutdown(wait=False)


app = FastAPI(title="SysPerfSight", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# In-memory store: session_id -> (header_html, sections)
sessions: dict = {}


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("static/index.html")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename.endswith(".html"):
        raise HTTPException(400, "Only .html SystemPerformance files are supported.")

    content = await file.read()
    html = content.decode("iso-8859-1", errors="replace")

    loop = asyncio.get_running_loop()
    try:
        header_html, sections = await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, parse_sections, html),
            timeout=UPLOAD_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            504,
            f"Parsing this file didn't finish within {UPLOAD_TIMEOUT_S}s. "
            "It may be unusually large or an unrecognized format — see diagnose_report.py.",
        )

    if not sections:
        hint = (
            "the file doesn't contain the expected '<hr size=\"4\" noshade>' section markers at all"
            if 'noshade' not in html.lower()
            else "the file has section markers but not in a layout this parser recognizes "
                 "(unrecognized SystemPerformance/pButtons HTML variant)"
        )
        raise HTTPException(
            400,
            f"No sections found — {hint}. Please confirm this is an unmodified "
            "InterSystems IRIS SystemPerformance (or Caché pButtons) HTML export.",
        )

    session_id = str(uuid.uuid4())
    sessions[session_id] = (header_html, sections)

    return {
        "session_id": session_id,
        "filename": file.filename,
        "sections": [
            {
                "id": s.id,
                "title": s.title,
                "sensitive": s.sensitive,
                "sensitive_reason": s.sensitive_reason,
                "time_filterable": s.title in TITLE_TIME_FILTERS,
            }
            for s in sections
        ],
    }


class ExportRequest(BaseModel):
    session_id: str
    selected_ids: list[str]
    output_filename: str = "sysperf_report.html"
    time_from: str = ""
    time_to: str = ""
    mode: str = "full"  # "full" | "charts_only" | "charts_raw"


def _extract_pre_text(content_html: str) -> str:
    return '\n'.join(_re.findall(r'<pre>(.*?)</pre>', content_html, _re.DOTALL | _re.IGNORECASE))


def _apply_time_filter(section, time_from: str, time_to: str):
    fn = TITLE_TIME_FILTERS.get(section.title)
    if fn is None or (not time_from and not time_to):
        return section
    text = _extract_pre_text(section.content_html)
    filtered_text = fn(text, time_from, time_to)
    filtered_html = _re.sub(
        r'(<pre>).*?(</pre>)',
        lambda m: m.group(1) + filtered_text + m.group(2),
        section.content_html,
        count=1,
        flags=_re.DOTALL | _re.IGNORECASE,
    )
    s = copy.copy(section)
    s.content_html = filtered_html
    return s


def _prepare_sections_sync(sections, selected_ids, time_from, time_to):
    """Apply time filters and extract each selected section's raw text once
    (instead of re-running the same <pre> regex separately for filtering,
    analysis, and synthesis). Runs in the thread pool — pure CPU/text work."""
    selected_set = set(selected_ids)
    filtered = [_apply_time_filter(s, time_from, time_to) for s in sections]
    section_texts = {s.id: _extract_pre_text(s.content_html) for s in filtered if s.id in selected_set}
    return filtered, section_texts


def _analyze_sync(fn, text: str) -> str:
    """Run an analyzer's `async def analyze()` synchronously in a worker thread.
    None of the analyzers actually await anything — they're synchronous
    pandas/regex/Plotly work wearing an async interface — so this just executes
    them without needing an event loop of their own beyond what asyncio.run sets up."""
    return asyncio.run(fn(text))


async def _run_analyzer(loop, section_id: str, fn, text: str):
    try:
        html = await loop.run_in_executor(_EXECUTOR, _analyze_sync, fn, text)
        return section_id, html
    except Exception:
        print(f'[analyzer] {section_id} EXCEPTION:\n{traceback.format_exc()}', flush=True)
        return section_id, ''


@app.post("/export")
async def export_file(req: ExportRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "Session not found. Please re-upload the file.")

    header_html, sections = sessions[req.session_id]
    loop = asyncio.get_running_loop()

    async def _with_timeout(coro):
        try:
            return await asyncio.wait_for(coro, timeout=EXPORT_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise HTTPException(
                504,
                f"Generating this report didn't finish within {EXPORT_TIMEOUT_S}s. "
                "Try selecting fewer sections, narrowing the time range, or exporting "
                "'Charts only' instead of the full report.",
            )

    sections, section_texts = await _with_timeout(
        loop.run_in_executor(_EXECUTOR, _prepare_sections_sync, sections, req.selected_ids, req.time_from, req.time_to)
    )

    # Run all applicable analyzers in parallel for selected sections. Each one runs
    # in its own worker thread — pandas/numpy release the GIL for their vectorized
    # work, so this gets real overlap between sections instead of the previous
    # asyncio.gather() over purely synchronous coroutines (which never yielded,
    # so it ran every analyzer back-to-back on the same thread regardless of "parallel").
    analyzable = [(s.id, SECTION_ANALYZERS[s.id]) for s in sections
                  if s.id in req.selected_ids and s.id in SECTION_ANALYZERS]
    results = await _with_timeout(asyncio.gather(*[
        _run_analyzer(loop, sid, fn, section_texts[sid]) for sid, fn in analyzable
    ]))
    analysis = {sid: html for sid, html in results if html}

    if req.mode in ('charts_only', 'charts_raw'):
        analysis = {sid: _re.sub(r'<!--INS-->.*?<!--/INS-->', '', html, flags=_re.DOTALL)
                    for sid, html in analysis.items()}
        synthesis_html = ''
    else:
        try:
            synthesis_html = await _with_timeout(
                loop.run_in_executor(_EXECUTOR, _analyze_sync, synthesize, section_texts)
            )
        except HTTPException:
            raise
        except Exception:
            print(f'[synthesis] EXCEPTION:\n{traceback.format_exc()}', flush=True)
            synthesis_html = ''

    output_html = await _with_timeout(
        loop.run_in_executor(_EXECUTOR, build_output, header_html, sections, req.selected_ids, analysis, synthesis_html, req.mode)
    )

    safe_name = Path(req.output_filename).name
    if not safe_name.endswith(".html"):
        safe_name += ".html"

    out_path = OUTPUT_DIR / safe_name
    await loop.run_in_executor(_EXECUTOR, out_path.write_text, output_html, "utf-8")

    return FileResponse(
        path=str(out_path),
        filename=safe_name,
        media_type="text/html",
    )
