import io
from pathlib import Path

import pdfplumber
import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
RULES_FILE = BASE_DIR / "rules.yaml"

app = FastAPI(title="畢業學分檢核系統")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def load_rules() -> dict:
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_transcript(pdf_bytes: bytes) -> dict:
    """從成績單 PDF 擷取課程表格與已修學分總和（簡易版，依表格欄位標題比對）。"""
    courses = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table or len(table) < 2:
                continue

            header = [(c or "").strip() for c in table[0]]
            credit_col = next((i for i, h in enumerate(header) if "學分" in h), None)
            name_col = next((i for i, h in enumerate(header) if "課程" in h or "科目" in h), None)
            grade_col = next((i for i, h in enumerate(header) if "成績" in h), None)
            if credit_col is None:
                continue

            for row in table[1:]:
                if not row or len(row) <= credit_col:
                    continue
                raw_credit = (row[credit_col] or "").strip()
                try:
                    credit = float(raw_credit)
                except ValueError:
                    continue
                courses.append(
                    {
                        "name": (row[name_col] or "").strip() if name_col is not None else "",
                        "credit": credit,
                        "grade": (row[grade_col] or "").strip() if grade_col is not None else "",
                    }
                )

    total_credit = sum(c["credit"] for c in courses)
    return {"courses": courses, "total_credit": total_credit}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    rules = load_rules()
    years = sorted(rules.keys(), reverse=True)
    return templates.TemplateResponse(request, "index.html", {"years": years})


@app.post("/check", response_class=HTMLResponse)
async def check(request: Request, year: str = Form(...), file: UploadFile = File(...)):
    rules = load_rules()
    pdf_bytes = await file.read()
    result = parse_transcript(pdf_bytes)

    required = rules.get(year, {})
    required_total = required.get("total_credits", 0)
    passed = result["total_credit"] >= required_total

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "year": year,
            "required_total": required_total,
            "total_credit": result["total_credit"],
            "passed": passed,
            "courses": result["courses"],
        },
    )
