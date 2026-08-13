import uuid
import asyncio
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


@asynccontextmanager
async def lifespan(_app):
    yield


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

    header_html, sections = parse_sections(html)

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


@app.post("/export")
async def export_file(req: ExportRequest):
    if req.session_id not in sessions:
        raise HTTPException(404, "Session not found. Please re-upload the file.")

    header_html, sections = sessions[req.session_id]

    import re as _re
    import copy

    def _apply_time_filter(section):
        fn = TITLE_TIME_FILTERS.get(section.title)
        if fn is None or (not req.time_from and not req.time_to):
            return section
        text = '\n'.join(_re.findall(r'<pre>(.*?)</pre>', section.content_html, _re.DOTALL | _re.IGNORECASE))
        filtered_text = fn(text, req.time_from, req.time_to)
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

    sections = [_apply_time_filter(s) for s in sections]

    # Run all applicable analyzers in parallel for selected sections
    selected_sections = [s for s in sections if s.id in req.selected_ids]
    analyzable = [(s, SECTION_ANALYZERS[s.id]) for s in selected_sections if s.id in SECTION_ANALYZERS]

    async def run_analyzer(section, fn):
        import traceback
        try:
            text = '\n'.join(_re.findall(r'<pre>(.*?)</pre>', section.content_html, _re.DOTALL | _re.IGNORECASE))
            return section.id, await fn(text)
        except Exception:
            print(f'[analyzer] {section.id} EXCEPTION:\n{traceback.format_exc()}', flush=True)
            return section.id, ''

    results = await asyncio.gather(*[run_analyzer(s, fn) for s, fn in analyzable])
    analysis = {sid: html for sid, html in results if html}

    if req.mode in ('charts_only', 'charts_raw'):
        analysis = {sid: _re.sub(r'<!--INS-->.*?<!--/INS-->', '', html, flags=_re.DOTALL)
                    for sid, html in analysis.items()}
        synthesis_html = ''
    else:
        # Cross-section synthesis — runs after all individual analyzers
        section_texts = {
            s.id: '\n'.join(_re.findall(r'<pre>(.*?)</pre>', s.content_html, _re.DOTALL | _re.IGNORECASE))
            for s in selected_sections
        }
        try:
            synthesis_html = await synthesize(section_texts)
        except Exception:
            import traceback
            print(f'[synthesis] EXCEPTION:\n{traceback.format_exc()}', flush=True)
            synthesis_html = ''

    output_html = build_output(header_html, sections, req.selected_ids, analysis=analysis,
                               synthesis=synthesis_html, mode=req.mode)

    safe_name = Path(req.output_filename).name
    if not safe_name.endswith(".html"):
        safe_name += ".html"

    out_path = OUTPUT_DIR / safe_name
    out_path.write_text(output_html, encoding="utf-8")

    return FileResponse(
        path=str(out_path),
        filename=safe_name,
        media_type="text/html",
    )
