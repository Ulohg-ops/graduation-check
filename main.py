import io
from pathlib import Path

import pdfplumber
import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
RULES_FILE = BASE_DIR / "rules.yaml"

app = FastAPI(title="化材系畢業學分檢核系統")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def load_rules() -> dict:
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # 學年度統一視為字串 key，避免 YAML 把純數字的 key 讀成 int
    return {str(year): data for year, data in raw.items()}


def save_rules(rules: dict) -> None:
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, allow_unicode=True, sort_keys=False)


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


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", {})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    rules = load_rules()
    years = sorted(rules.keys(), reverse=True)
    return templates.TemplateResponse(request, "index.html", {"years": years})


def mock_transcript(year_data: dict) -> dict:
    """假成績單資料，先讓上傳流程可以預覽結果頁，之後再串接真正的 PDF 解析比對邏輯。"""
    required_courses = year_data.get("required_courses", [])
    picks = [c for c in required_courses if c.get("category") == "必修"][:3]
    if not picks:
        picks = [
            {"name": "普通化學", "credits": 3},
            {"name": "普通物理", "credits": 3},
            {"name": "微積分", "credits": 4},
        ]
    courses = [{"name": c["name"], "credit": c["credits"], "grade": "85"} for c in picks]
    total_credit = sum(c["credit"] for c in courses)
    return {"courses": courses, "total_credit": total_credit}


@app.post("/check", response_class=HTMLResponse)
async def check(request: Request, year: str = Form(...), file: UploadFile | None = File(None)):
    rules = load_rules()
    year_data = rules.get(year, {})

    if file is not None and file.filename:
        pdf_bytes = await file.read()
        result = parse_transcript(pdf_bytes)
        is_mock = False
    else:
        result = mock_transcript(year_data)
        is_mock = True

    required_total = year_data.get("total_credits", 0)
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
            "is_mock": is_mock,
        },
    )


def _normalize_course(index: int, c: dict) -> dict:
    return {
        "index": index,  # rules.yaml 裡實際的位置，新增/刪除/編輯都靠這個對應，跟畫面排序無關
        "name": c.get("name", ""),
        "code": c.get("code", ""),
        "credits": c.get("credits", 0),
        "category": c.get("category") or "必修",
        "note": c.get("note", ""),
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, year: str | None = None, edit: int | None = None):
    rules = load_rules()
    years = sorted(rules.keys(), reverse=True)
    selected_year = year if year in rules else (years[0] if years else None)
    year_data = rules.get(selected_year, {}) if selected_year else {}

    # 必修排在選修前面，方便對照原始應修科目表；用 index 記住原始位置，畫面排序不影響編輯/刪除
    required_courses = [_normalize_course(i, c) for i, c in enumerate(year_data.get("required_courses", []))]
    required_courses.sort(key=lambda c: c["category"] != "必修")

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "years": years,
            "year": selected_year,
            "total_credits": year_data.get("total_credits", 0),
            "required_courses": required_courses,
            "edit_index": edit,
        },
    )


@app.post("/admin/course/add")
async def admin_course_add(
    year: str = Form(...),
    name: str = Form(...),
    code: str = Form(""),
    credits: float = Form(...),
    category: str = Form("必修"),
    note: str = Form(""),
):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data.setdefault("required_courses", []).append(
        {"name": name, "code": code, "credits": credits, "category": category, "note": note}
    )
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/course/update")
async def admin_course_update(
    year: str = Form(...),
    index: int = Form(...),
    name: str = Form(...),
    code: str = Form(""),
    credits: float = Form(...),
    category: str = Form("必修"),
    note: str = Form(""),
):
    rules = load_rules()
    courses = rules.get(year, {}).get("required_courses", [])
    if 0 <= index < len(courses):
        courses[index] = {"name": name, "code": code, "credits": credits, "category": category, "note": note}
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/course/delete")
async def admin_course_delete(year: str = Form(...), index: int = Form(...)):
    rules = load_rules()
    courses = rules.get(year, {}).get("required_courses", [])
    if 0 <= index < len(courses):
        courses.pop(index)
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/total_credits")
async def admin_total_credits(year: str = Form(...), total_credits: float = Form(...)):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data["total_credits"] = total_credits
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)
