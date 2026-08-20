import io
import re
from itertools import groupby
from pathlib import Path
from typing import List, Optional

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


_CREDIT_LABELS = {"學分數", "學分"}
_NAME_LABELS = {"課程名稱", "課程", "科目"}
_HEADER_LABELS = _CREDIT_LABELS | _NAME_LABELS | {"學年學期", "課號", "成績", "判定", "備註"}


def _is_header_row(cells: list) -> bool:
    """判斷一列是不是課程明細表的表頭列（例如「學年學期/課號/課程名稱/學分數/成績/判定/備註」）。

    畢業審核紀錄表整頁其實只會被 pdfplumber 認成一張大表格，裡面穿插了很多「分類彙總列」和課程列，
    必須逐欄「完全比對」已知表頭文字（而不是用 in 判斷子字串），否則像「自主學習微學分(一)」
    （含「學分」二字）、「一般課程通過(Pass)」（含「課程」二字）這種課程資料列會被誤判成表頭。
    """
    exact_hits = sum(1 for c in cells if c in _HEADER_LABELS)
    has_credit = any(c in _CREDIT_LABELS for c in cells)
    has_name = any(c in _NAME_LABELS for c in cells)
    return exact_hits >= 2 and has_credit and has_name


def _extract_course_rows(pdf: pdfplumber.PDF) -> list:
    """逐列掃描每頁的表格，動態抓表頭欄位並讀出後面的課程列。

    教務處匯出的「畢業審核紀錄表」整頁常被 pdfplumber 判讀成同一張大表格，裡面依類別
    (共同必修/系訂必修/選修...) 重複出現「表頭列 + 課程列」，表頭不是固定在 table[0]，
    所以要逐列掃描、遇到表頭就重新對應欄位，直到遇到不像課程列的資料（例如下一段的分類
    彙總列）才結束這個區塊，再繼續往下找下一個表頭。
    """
    courses = []
    col = None  # 課程列的欄位對應；同一張課程表常常跨頁延續，欄位對應要沿用到下一頁，不能整頁重置
    for page in pdf.pages:
        for table in page.extract_tables():
            for row in table:
                cells = [(c or "").replace("\n", "").strip() for c in row]

                if _is_header_row(cells):
                    col = {
                        "credit": next(i for i, c in enumerate(cells) if c in _CREDIT_LABELS),
                        "name": next(i for i, c in enumerate(cells) if c in _NAME_LABELS),
                        "code": next((i for i, c in enumerate(cells) if c == "課號"), None),
                        "grade": next((i for i, c in enumerate(cells) if c == "成績"), None),
                        "verdict": next((i for i, c in enumerate(cells) if c == "判定"), None),
                    }
                    continue

                if col is None:
                    continue
                if len(cells) <= col["credit"]:
                    col = None
                    continue
                try:
                    credit = float(cells[col["credit"]])
                except ValueError:
                    col = None
                    continue

                verdict = cells[col["verdict"]] if col["verdict"] is not None else ""
                courses.append(
                    {
                        "code": cells[col["code"]] if col["code"] is not None else "",
                        "name": cells[col["name"]],
                        "credit": credit,
                        "grade": cells[col["grade"]] if col["grade"] is not None else "",
                        # 畢業審核紀錄表才有「判定」欄；沒有這欄的成績單就先當作都已通過
                        "passed": verdict != "不通過" if col["verdict"] is not None else True,
                    }
                )
    return courses


_UNMET_CATEGORY_RE = re.compile(
    r"^(?P<code>\d{5})\s*-\s*(?P<name>.+?)，"
    r"應修學分：(?P<req_credit>\d+(?:\.\d+)?)、應修科目：(?P<req_count>\d+)；"
    r"實修學分：(?P<act_credit>\d+(?:\.\d+)?)、實修科目：(?P<act_count>\d+)"
)


def _extract_unmet_categories(pdf: pdfplumber.PDF) -> list:
    """從「畢業審核紀錄表」抓出教務處自己判定「未完成」的細項類別（例如系訂必修學分不足幾學分）。

    這份報表本身就會逐類別（共同必修/系訂必修/選修...）標記完成或未完成，比我們自己「只看總學分」
    的判斷更精確，可以直接告訴使用者具體差在哪個類別、差多少學分或科目數。
    """
    unmet = []
    for page in pdf.pages:
        for table in page.extract_tables():
            for row in table:
                if not row or row[0] != "未完成":
                    continue
                text = (row[1] or "").replace("\n", "")
                m = _UNMET_CATEGORY_RE.match(text)
                if not m:
                    continue
                unmet.append(
                    {
                        "code": m.group("code"),
                        "name": m.group("name"),
                        "required_credit": float(m.group("req_credit")),
                        "required_count": int(m.group("req_count")),
                        "actual_credit": float(m.group("act_credit")),
                        "actual_count": int(m.group("act_count")),
                    }
                )
    return unmet


def parse_transcript(pdf_bytes: bytes) -> dict:
    """從成績單／畢業審核紀錄表 PDF 擷取課程明細，並加總「已通過」課程的學分數。"""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        courses = _extract_course_rows(pdf)
        unmet_categories = _extract_unmet_categories(pdf)

    # 同一課號重補修會出現多筆（例如二一二不及格、二二二補修通過），
    # 只要有一次通過就採計一次學分，避免加總時重複計入。
    deduped = {}
    for c in courses:
        key = c["code"] or c["name"]
        existing = deduped.get(key)
        if existing is None or (c["passed"] and not existing["passed"]):
            deduped[key] = c

    total_credit = sum(c["credit"] for c in deduped.values() if c["passed"])
    passed_codes = {c["code"] for c in deduped.values() if c["passed"] and c["code"]}
    return {
        "courses": courses,
        "total_credit": total_credit,
        "unmet_categories": unmet_categories,
        "passed_codes": passed_codes,
    }


def missing_required_courses(required_courses: list, passed_codes: set, group_requirements: dict = None) -> list:
    """把應修科目表（rules.yaml）跟成績單已通過課號比對，抓出還沒通過的必修/選修科目。

    有兩種情況要分開處理：
    1. 一般必修課（`group` 欄位空白，例如「材料工程概論 I / II」課號 CH1023/CH1024）：
       每個課號都要修過，課號用「/」合併記錄的要逐一拆開比對，才能標出「只差哪一門」
       而不是整組算沒修。
    2. 「M選N」的群組必修（`group` 欄位填了同一個分組名稱，例如核心必選修A/B組、專題必選修）：
       組內所有科目的課號攤平後，通過的課號數只要達到 `group_requirements` 指定的門數就算滿足
       （沒特別指定的分組預設選1門）。這裡刻意用「攤平後的課號數」而不是「完整選項數」去算，
       是因為拿真實畢業審核紀錄表驗證過：像「理論與實務整合專題實作(一)/(二)/(三)」這種本身
       合併三個課號的選項，官方系統只要通過其中一個課號（例如(一)）就已經算滿足最低門檻，
       不要求三部曲全部修完才算選了這一門——用「完整選項」去算會比官方系統更嚴格，判斷錯誤。
       分組名稱、要選幾門都是 /admin 頁面上可以調整的資料，不是寫死的固定清單。
    """
    group_requirements = group_requirements or {}
    groups: dict = {}
    plain = []
    for course in required_courses:
        codes = [c.strip() for c in (course.get("code") or "").split("/") if c.strip()]
        if not codes:
            continue
        group = (course.get("group") or "").strip()
        if group:
            groups.setdefault(group, []).append({**course, "codes": codes})
        else:
            plain.append({**course, "codes": codes})

    missing = []
    for course in plain:
        missing_codes = [c for c in course["codes"] if c not in passed_codes]
        if missing_codes:
            missing.append(
                {
                    "name": course["name"],
                    "codes": course["codes"],
                    "missing_codes": missing_codes,
                    "note": course.get("note", ""),
                }
            )

    for group, group_courses in groups.items():
        required_count = group_requirements.get(group, 1)
        all_codes = [c for gc in group_courses for c in gc["codes"]]
        passed_count = sum(1 for c in all_codes if c in passed_codes)
        if passed_count >= required_count:
            continue  # 這組已經選夠門數，畢業條件已經滿足，不算缺
        missing_codes = [c for c in all_codes if c not in passed_codes]
        options = "、".join(gc["name"] for gc in group_courses)
        if required_count == 1:
            label = f"{group}（{len(group_courses)}選1，需擇一修習且及格）"
        else:
            still_missing = required_count - passed_count
            label = f"{group}（{len(group_courses)}選{required_count}，已通過{passed_count}門課，還差{still_missing}門）"
        missing.append(
            {
                "name": label,
                "codes": all_codes,
                "missing_codes": missing_codes,
                "note": f"可選：{options}",
            }
        )
    return missing


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
    courses = [
        {"code": c.get("code", ""), "name": c["name"], "credit": c["credits"], "grade": "85", "passed": True}
        for c in picks
    ]
    total_credit = sum(c["credit"] for c in courses)
    passed_codes = {c["code"] for c in courses if c["code"]}
    return {"courses": courses, "total_credit": total_credit, "unmet_categories": [], "passed_codes": passed_codes}


# 12xxx（一般系訂必修12100/12101、核心必選修/專題必選修分組12200~12203）算出來的內容，
# 跟我們自己 missing_required_courses() 比對出來的「還沒通過的科目」是同一件事、來源不同，
# 兩邊都顯示會重複，所以「教務處判定尚未完成的項目」這區塊只保留12xxx以外的類別
# （共同必修11xxx、操行體育軍訓服務學習18xxx、特殊檢核19xxx這些我們自己沒有在追蹤的項目）。
_DUPLICATED_CATEGORY_PREFIX = "12"


def _build_result_entry(
    filename: str, result: dict, required_total: float, required_courses: list, group_requirements: dict
) -> dict:
    """把單一份成績單的解析結果，組成結果頁要顯示的一筆資料。

    是否「通過」不是只看總學分數字，還要求應修科目表裡的必修科目（含核心必選修/專題必選修
    這種M選N分組）全部滿足，兩個條件都成立才算真的達到畢業資格，避免學生隨便湊學分就被判定通過。
    """
    missing_required = missing_required_courses(required_courses, result["passed_codes"], group_requirements)
    credit_ok = result["total_credit"] >= required_total
    unmet_categories = [
        u for u in result["unmet_categories"] if not u["code"].startswith(_DUPLICATED_CATEGORY_PREFIX)
    ]
    return {
        "filename": filename,
        "total_credit": result["total_credit"],
        "credit_ok": credit_ok,
        "passed": credit_ok and not missing_required,
        "courses": result["courses"],
        "unmet_categories": unmet_categories,
        "missing_required": missing_required,
    }


@app.post("/check", response_class=HTMLResponse)
async def check(request: Request, year: str = Form(...), files: List[UploadFile] = File([])):
    rules = load_rules()
    year_data = rules.get(year, {})
    required_total = year_data.get("total_credits", 0)
    required_courses = year_data.get("required_courses", [])
    group_requirements = year_data.get("group_requirements", {})

    uploaded = [f for f in files if f.filename]

    results = []
    is_mock = not uploaded
    if is_mock:
        result = mock_transcript(year_data)
        results.append(
            _build_result_entry(
                "（預覽假資料，尚未上傳成績單）", result, required_total, required_courses, group_requirements
            )
        )
    else:
        for f in uploaded:
            pdf_bytes = await f.read()
            result = parse_transcript(pdf_bytes)
            results.append(_build_result_entry(f.filename, result, required_total, required_courses, group_requirements))

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "year": year,
            "required_total": required_total,
            "results": results,
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
        "group": c.get("group", ""),
        # 層級（共同必修/院訂必修/系訂必修...）純粹是畫面分類用，判定邏輯完全不看這個欄位，
        # 跟「分組」不一樣——分組會影響N選M的判定，層級只是讓應修科目表在畫面上照原始文件的
        # 結構分區顯示，方便對照。
        "tier": c.get("tier", ""),
        "note": c.get("note", ""),
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, year: Optional[str] = None, edit: Optional[int] = None):
    rules = load_rules()
    years = sorted(rules.keys(), reverse=True)
    selected_year = year if year in rules else (years[0] if years else None)
    year_data = rules.get(selected_year, {}) if selected_year else {}

    # 必修排在選修前面；同一類別內維持 rules.yaml 原本的順序（stable sort不會打亂同類別內的相對順序）。
    # 這樣同一層級/分組的科目只要在資料裡本來就排在一起，畫面就會照著文件原本的順序（共同必修→院訂
    # 必修→系訂必修→...）分區顯示，不會因為改用字母排序而把順序弄亂（「院訂必修」「系訂必修」這幾個
    # 詞的字母順序剛好跟文件邏輯順序不一樣）。
    required_courses = [_normalize_course(i, c) for i, c in enumerate(year_data.get("required_courses", []))]
    required_courses.sort(key=lambda c: c["category"] != "必修")

    # 類別不是寫死的「必修/選修」兩選項，是自由輸入、給表單自動完成選項用；「必修」是預設值，
    # 空白會視為必修（_normalize_course 已經處理），所以這裡固定加進選項，即使目前沒有任何科目用到
    category_options = sorted({"必修"} | {c["category"] for c in required_courses if c["category"]})
    # 科目列表裡的「類別」欄位只在真的有一種以上類別時才顯示，全部都是必修的話這欄只是重複噪音；
    # 一旦哪天真的加了選修科目，這欄會自動出現，不用手動改
    show_category = len({c["category"] for c in required_courses}) > 1
    table_colspan = 6 if show_category else 5

    # 分組名稱來自兩個地方：課程已經在用的分組、還有已經設定過「選幾門」但還沒有任何科目掛上去的分組
    # （例如剛用「新增分組」建立、還沒開始加科目的新分組），兩邊聯集才不會漏掉還沒綁課的空分組
    stored_group_requirements = year_data.get("group_requirements", {})
    group_options = sorted({c["group"] for c in required_courses if c["group"]} | set(stored_group_requirements))

    # 每個分組要選幾門：rules.yaml 裡沒特別設定的分組，預設是選1門
    group_requirements = [
        {"group": g, "required_count": stored_group_requirements.get(g, 1)} for g in group_options
    ]

    # 層級純粹是顯示分類用（共同必修/院訂必修/系訂必修...），不影響判定邏輯，給表單自動完成選項用
    tier_options = sorted({c["tier"] for c in required_courses if c["tier"]})

    # 畫面分區的依據：優先用「分組」（N選M功能性分組），沒有分組才退而用「層級」（純顯示分類），
    # 兩者都沒有的科目不分區、直接顯示
    course_sections = []
    for key, items in groupby(required_courses, key=lambda c: c["group"] or c["tier"]):
        items_list = list(items)
        is_group = bool(items_list[0]["group"])
        course_sections.append(
            {
                "key": key,
                "is_group": is_group,
                "required_count": stored_group_requirements.get(key, 1) if is_group else None,
                "courses": items_list,
            }
        )

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "years": years,
            "year": selected_year,
            "total_credits": year_data.get("total_credits", 0),
            "required_courses": required_courses,
            "course_sections": course_sections,
            "category_options": category_options,
            "show_category": show_category,
            "table_colspan": table_colspan,
            "group_options": group_options,
            "group_requirements": group_requirements,
            "tier_options": tier_options,
            "edit_index": edit,
        },
    )


@app.post("/admin/year/add")
async def admin_year_add(year: str = Form(...)):
    year = year.strip()
    rules = load_rules()
    rules.setdefault(year, {"total_credits": 0, "required_courses": [], "group_requirements": {}})
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/group/set_requirement")
async def admin_group_set_requirement(year: str = Form(...), group: str = Form(...), required_count: int = Form(...)):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": [], "group_requirements": {}})
    year_data.setdefault("group_requirements", {})[group] = required_count
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/group/rename")
async def admin_group_rename(year: str = Form(...), old_group: str = Form(...), new_group: str = Form("")):
    old_group = old_group.strip()
    new_group = new_group.strip()
    rules = load_rules()
    year_data = rules.get(year, {})

    if new_group and new_group != old_group:
        # 把用到舊名稱的科目全部改成新名稱，這個分組底下的科目才不會因為改名字就散掉
        for c in year_data.get("required_courses", []):
            if c.get("group") == old_group:
                c["group"] = new_group
        group_requirements = year_data.setdefault("group_requirements", {})
        if old_group in group_requirements:
            old_count = group_requirements.pop(old_group)
            # 如果改名後的名稱本來就是另一個既有分組，保留那個分組原本設定的「選幾門」，不要被覆蓋掉
            group_requirements.setdefault(new_group, old_count)
        save_rules(rules)

    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/group/delete")
async def admin_group_delete(year: str = Form(...), group: str = Form(...)):
    rules = load_rules()
    year_data = rules.get(year, {})

    # 刪除分組不會連科目一起刪掉，科目會變回沒有分組的一般必修/選修科目，只是不再綁在一起判定
    for c in year_data.get("required_courses", []):
        if c.get("group") == group:
            c["group"] = ""
    year_data.get("group_requirements", {}).pop(group, None)
    save_rules(rules)

    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/category/rename")
async def admin_category_rename(year: str = Form(...), old_category: str = Form(...), new_category: str = Form("")):
    old_category = old_category.strip()
    new_category = new_category.strip()
    rules = load_rules()
    year_data = rules.get(year, {})

    if new_category and new_category != old_category:
        # 類別沒有像分組那樣另外存「選幾門」的設定，單純把用到舊類別名稱的科目全部改成新名稱
        for c in year_data.get("required_courses", []):
            if (c.get("category") or "必修") == old_category:
                c["category"] = new_category
        save_rules(rules)

    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/course/add")
async def admin_course_add(
    year: str = Form(...),
    name: str = Form(...),
    code: str = Form(""),
    credits: float = Form(...),
    category: str = Form("必修"),
    group: str = Form(""),
    tier: str = Form(""),
    note: str = Form(""),
):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data.setdefault("required_courses", []).append(
        {
            "name": name, "code": code, "credits": credits, "category": category,
            "group": group, "tier": tier, "note": note,
        }
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
    group: str = Form(""),
    tier: str = Form(""),
    note: str = Form(""),
):
    rules = load_rules()
    courses = rules.get(year, {}).get("required_courses", [])
    if 0 <= index < len(courses):
        courses[index] = {
            "name": name, "code": code, "credits": credits, "category": category,
            "group": group, "tier": tier, "note": note,
        }
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}#row-{index}", status_code=303)


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
