import copy
import io
import os
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional

import pdfplumber
import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 一般用 `python -m uvicorn main:app`／啟動.bat 執行時，__file__ 就在專案資料夾裡，唯讀資源
# （templates/static）跟會被修改、要持久保存的資料（rules.yaml等）放在同一個地方沒問題。
# 但打包成單一檔案的PyInstaller執行檔（--onefile）後，兩者不能再用同一個目錄：
# - RESOURCE_DIR：sys._MEIPASS，PyInstaller在「這次執行」解壓縮唯讀資源的暫存資料夾，每次
#   執行都是新路徑、程式關掉就消失——只能放bundle進去、不會被改動的東西（樣板、CSS、圖示）。
# - DATA_DIR：使用者的 %APPDATA%，不會因為.exe被搬到別的地方、或每次重新解壓縮就跟丟資料，
#   rules.yaml這種要跨次執行持續存在、又會被/admin頁面修改的檔案要放這裡。
# sys.frozen 是 PyInstaller 打包後才會有的旗標，開發時（直接跑python/uvicorn）兩者是同一個
# 目錄，行為跟以前一樣。
if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(sys._MEIPASS)
    DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "GraduationCheck"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 換成新打包的.exe（commit hash跟上次記錄的不一樣）時，把DATA_DIR整個清空重置——跟下面
    # 「只在檔案不存在時才seed」的做法不同，這裡是故意每次換版本都打回bundle內建的原廠規則，
    # 不保留使用者上一版累積的修改（包含預口試名單）。version.txt是CI打包時寫入的commit
    # hash＋打包日期（見.github/workflows/build-windows.yml、launcher.py的_read_version），
    # 拿不到版本字串（例如version.txt沒跟著打包進來）就不清空，避免誤判成「換版本」。
    _version_file = RESOURCE_DIR / "version.txt"
    _current_version = _version_file.read_text(encoding="utf-8").strip() if _version_file.exists() else ""
    _version_marker = DATA_DIR / ".bundled_version"
    _last_version = _version_marker.read_text(encoding="utf-8").strip() if _version_marker.exists() else None
    if _current_version and _current_version != _last_version:
        for _child in DATA_DIR.iterdir():
            if _child.is_dir():
                shutil.rmtree(_child, ignore_errors=True)
            else:
                _child.unlink(missing_ok=True)
        _version_marker.write_text(_current_version, encoding="utf-8")
else:
    RESOURCE_DIR = Path(__file__).resolve().parent
    DATA_DIR = RESOURCE_DIR

RULES_FILE = DATA_DIR / "rules.yaml"
GRADUATE_RULES_FILE = DATA_DIR / "graduate_rules.yaml"
# 「匯入規則」覆蓋前的備份，只保留最近一次匯入前的版本（不是每次匯入都留一份新檔案），
# 匯錯檔案的話可以手動把這個複製回 rules.yaml / graduate_rules.yaml 救回來。
RULES_BACKUP_FILE = DATA_DIR / "rules.yaml.bak"
GRADUATE_RULES_BACKUP_FILE = DATA_DIR / "graduate_rules.yaml.bak"
# 手動確認項目裡「預口試通過」「英文能力」這兩類有名單制自動判斷：項目文字開頭符合下面哪個
# 前綴，就比對哪一份名單——學號「不在」名單裡預設當作已通過（勾選框自動打勾），名單上的學號
# 才是還沒通過的例外。多數人最後都會通過，用「記錄還沒過的少數人」取代「記錄已經過的大多數
# 人」，辦公人員平常只要處理例外、不用每個人都手動勾過一次，見_build_graduate_entry的說明。
MANUAL_REVIEW_ROSTER_PREFIXES = {
    "predefense": "預口試",
    "english": "英文能力",
}
# 沒有對應的「bundle預設版本」可以seed——這份名單本來就是系上自己維護、隨時間變動的資料，
# 第一次執行時是空的（=沒有任何例外、全部人都當作已通過）很正常，不像rules.yaml需要一份
# 起始規則才能用。
MANUAL_REVIEW_EXCEPTIONS_FILE = DATA_DIR / "manual_review_exceptions.yaml"

# DATA_DIR裡還沒有這兩個檔案時，把bundle裡打包的預設版本複製過去當起始資料——不然使用者
# 第一次雙擊執行檔，/admin會找不到任何規則可以編輯。「還沒有檔案」除了真正第一次執行之外，
# 也包含剛被上面的換版本清空邏輯清掉之後，等於每次換版本都會重新補回bundle內建的原廠規則。
for _seed_name in ("rules.yaml", "graduate_rules.yaml"):
    _dest = DATA_DIR / _seed_name
    _src = RESOURCE_DIR / _seed_name
    if not _dest.exists() and _src.exists():
        shutil.copy(_src, _dest)

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB，一般文字型成績單PDF遠小於這個數字，超過大概是傳錯檔案
MAX_FILES = 100  # 一次最多同時處理幾份，避免有人整個資料夾誤傳上來拖垮伺服器

app = FastAPI(title="化材系畢業學分檢核系統")
templates = Jinja2Templates(directory=str(RESOURCE_DIR / "templates"))
# 樣式改用本機打包好的 static/tailwind.css（不再用 CDN 版），同仁電腦沒有網路也能正常顯示畫面；
# 樣板裡新增的 Tailwind class 沒被這份編譯好的CSS涵蓋到的話，要重新用 tailwindcss CLI 打包一次
# （指令見 static/tailwind_input.css 旁邊，掃描 templates/ 底下用到的 class 重新編譯 tailwind.css）。
app.mount("/static", StaticFiles(directory=str(RESOURCE_DIR / "static")), name="static")
# 樣板裡引用 tailwind.css 時要帶上這個版本號查詢字串（?v=...），不然瀏覽器可能會沿用舊版CSS的
# 快取，改完樣式、重新編譯tailwind.css之後畫面卻沒更新——用檔案的修改時間當版本號，
# 每次重新編譯內容一定會變、時間跟著變，剛好順便當成快取破壞用的版本號。
templates.env.globals["css_version"] = int((RESOURCE_DIR / "static" / "tailwind.css").stat().st_mtime)


def load_rules() -> dict:
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # 學年度統一視為字串 key，避免 YAML 把純數字的 key 讀成 int
    return {str(year): data for year, data in raw.items()}


def save_rules(rules: dict) -> None:
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, allow_unicode=True, sort_keys=False)


def load_manual_review_exceptions() -> dict:
    """讀取「還沒通過」名單（見MANUAL_REVIEW_EXCEPTIONS_FILE說明），回傳{roster_key: set(學號)}。
    還沒建立過檔案、或某個roster_key底下還沒有任何資料時，該roster是空集合——代表沒有例外，
    全部人都當作已通過，呼叫端不用另外判斷檔案在不在。
    """
    raw = {}
    if MANUAL_REVIEW_EXCEPTIONS_FILE.exists():
        with open(MANUAL_REVIEW_EXCEPTIONS_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    return {
        key: {str(x).strip() for x in (raw.get(key) or []) if str(x).strip()}
        for key in MANUAL_REVIEW_ROSTER_PREFIXES
    }


def save_manual_review_exceptions(data: dict) -> None:
    with open(MANUAL_REVIEW_EXCEPTIONS_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {key: sorted(data.get(key) or []) for key in MANUAL_REVIEW_ROSTER_PREFIXES},
            f,
            allow_unicode=True,
            sort_keys=False,
        )


def _manual_review_roster_key(item_text: str) -> Optional[str]:
    """判斷一筆manual_review_items的文字屬於哪一份名單制roster（見MANUAL_REVIEW_ROSTER_
    PREFIXES），都不符合就回傳None——這種項目（資格考、論文點數等）沒有名單可以自動判斷，
    維持舊行為、預設不勾，要辦公人員自己逐項手動確認。
    """
    for key, prefix in MANUAL_REVIEW_ROSTER_PREFIXES.items():
        if item_text.startswith(prefix):
            return key
    return None


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
                        "term": next((i for i, c in enumerate(cells) if c == "學年學期"), None),
                        "note": next((i for i, c in enumerate(cells) if c == "備註"), None),
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
                        # 學年學期（例如「1131」＝113學年第1學期），先修/順序修習規定要比對
                        # 兩門課的修課先後順序時會用到；沒有這欄的成績單（例如舊格式）就是 None，
                        # 對應規則會自動退回「只看有沒有通過、不看順序」的舊邏輯。
                        "term": cells[col["term"]] if col["term"] is not None else "",
                        # 轉系生常見「※抵修[EG1007]」「※抵免[CH1022]」這種備註——學生在原系所
                        # 修過的課（課號是原系所的，例如CM1002），拿去抵免本系應修科目表裡的
                        # 另一個課號，教務處自己的判定已經把這個算完成了，見_parse_substitutions()
                        # 的說明。沒有「備註」欄的成績單（例如舊格式）就是空字串，不影響原本邏輯。
                        "note": cells[col["note"]] if col["note"] is not None else "",
                    }
                )
    return courses


def _parse_term(term: str) -> Optional[int]:
    """把「學年學期」字串（例如「1131」＝113學年第1學期）轉成可以比大小的整數，
    數字越大代表學期越晚。格式不是純數字（缺欄、掃描辨識錯誤...）就回傳 None，
    呼叫端要能容忍 None、退回不比較順序的舊邏輯，不能讓解析失敗變成整個檢核壞掉。
    """
    term = (term or "").strip()
    return int(term) if term.isdigit() else None


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


_SUBSTITUTION_NOTE_RE = re.compile(r"抵[修免]\s*\[([^\]]*)\]")
_COURSE_CODE_SHAPE_RE = re.compile(r"[A-Z]{2,3}\d{3,5}")


def _parse_substitutions(courses: list) -> dict:
    """轉系生的成績單常見「※抵修[EG1007]」「※抵免[CH1022]」這種備註：學生在原系所修過的課
    （課號是原系所的，例如CM1002普通化學），拿去抵免本系應修科目表裡的另一門課（EG1007），
    教務處的「判定」欄本身就已經把這個算完成了，只是那門課在成績單上的課號還是原系所的
    （CM1002），不是應修科目表登記的課號（EG1007）——我們自己比對應修科目表時只看課號，
    不知道CM1002其實就是在滿足EG1007，會誤判EG1007還沒通過，這份對照表就是要接住這個情況。

    回傳 {原課號: [被頂替滿足的課號, ...]}，只收通過的課、且備註裡的課號要符合課號格式
    （避免抓到備註裡其他不相干的文字）。一個備註理論上可能同時列多個課號（不常見，但格式
    上允許），所以值是list不是單一字串。
    """
    substitutions: dict = {}
    for c in courses:
        if not c.get("passed") or not c.get("code"):
            continue
        note = c.get("note") or ""
        m = _SUBSTITUTION_NOTE_RE.search(note)
        if not m:
            continue
        targets = _COURSE_CODE_SHAPE_RE.findall(m.group(1))
        if targets:
            substitutions.setdefault(c["code"], []).extend(targets)
    return substitutions


def parse_transcript(pdf_bytes: bytes) -> dict:
    """從成績單／畢業審核紀錄表 PDF 擷取課程明細，並加總「已通過」課程的學分數。

    呼叫端要用 try/except 包住：pdfplumber 打開損毀檔案或非PDF檔案時會丟例外，
    這裡不吞掉，讓呼叫端決定怎麼呈現錯誤（通常是顯示「這份檔案無法解析」而不是整個request壞掉）。
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        courses = _extract_course_rows(pdf)
        unmet_categories = _extract_unmet_categories(pdf)
        # 用來分辨「掃描版、根本沒有文字」跟「有文字但抓不到課程表格」這兩種不同的失敗原因，
        # 結果頁可以給更精確的提示，不要籠統地都說「可能是掃描版」
        has_text = any((page.extract_text() or "").strip() for page in pdf.pages)
        # 大學部/輔系/雙主修跟碩博共用同一種「畢業審核紀錄表」報表格式，第一頁一樣有「學號：...」
        # 這一行，見_extract_student_id的說明——抓不到時回傳None，不影響後面的學分/科目判定。
        student_id = _extract_student_id(pdf)

    # 同一課號重補修會出現多筆（例如二一二不及格、二二二補修通過），
    # 只要有一次通過就算一次學分，避免加總時重複計入。
    deduped = {}
    for c in courses:
        key = c["code"] or c["name"]
        existing = deduped.get(key)
        if existing is None or (c["passed"] and not existing["passed"]):
            deduped[key] = c

    total_credit = sum(c["credit"] for c in deduped.values() if c["passed"])
    passed_codes = {c["code"] for c in deduped.values() if c["passed"] and c["code"]}
    passed_courses = [
        {"code": c["code"], "name": c["name"], "credit": c["credit"], "term": _parse_term(c.get("term"))}
        for c in deduped.values()
        if c["passed"]
    ]
    substitutions = _parse_substitutions(deduped.values())
    return {
        "courses": courses,
        "total_credit": total_credit,
        "unmet_categories": unmet_categories,
        "passed_codes": passed_codes,
        "passed_courses": passed_courses,
        "substitutions": substitutions,
        "has_text": has_text,
        "student_id": student_id,
    }


def load_graduate_rules() -> dict:
    """碩博規則跟大學部一樣依入學學年度分（見_graduate_admin_context），頂層key是學年度字串——
    跟load_rules()一樣要把純數字key統一轉成字串，避免YAML讀成int跟表單送來的字串比對不到。
    """
    with open(GRADUATE_RULES_FILE, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {str(year): data for year, data in raw.items()}


def save_graduate_rules(data: dict) -> None:
    with open(GRADUATE_RULES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


_GRAD_COURSE_CODE_RE = re.compile(r"^[A-Z]{2,3}\d{3,5}$")
_GRAD_SEMESTER_RE = re.compile(r"第(\d+)學年度第(\d+)學期")

# 碩博成績單（教務系統匯出的『學生個人成績一覽表』格式，沒有「判定」欄、只有原始成績）的及格
# 分數，固定70分、四個學制共用，不開放後台調整——校方修業規章就是統一標準，沒有逐年調整的需求。
GRAD_PASSING_SCORE = 70.0


def _parse_grad_score(raw: str) -> tuple:
    """判斷一筆數字成績通不通過：「抵免」「一般課程通過」這種文字成績（0學分的通過/不通過制
    課程、學分抵免）一律視為通過；數字成績跟GRAD_PASSING_SCORE比較。回傳(數字成績或None, 是否通過)。
    """
    text = (raw or "").strip()
    if "抵免" in text or "通過" in text:
        return None, True
    try:
        value = float(text)
    except ValueError:
        return None, False
    return value, value >= GRAD_PASSING_SCORE


def _grad_table_column_bounds(table_obj) -> list:
    """從table的某一列（挑非None儲存格最多的那列，通常是表頭或欄位齊全的課程列）推算出每一欄
    的x座標分界，供_recover_trailing_grad_rows()用座標比對重組漏抓的課程列。抓不到足夠欄位
    （少於7條分界＝6欄，正常應該有8欄）就回傳空list，呼叫端要能處理「沒有欄位資訊」的狀況。
    """
    best_cells = []
    for row in table_obj.rows:
        non_none = [c for c in row.cells if c]
        if len(non_none) > len(best_cells):
            best_cells = non_none
    if len(best_cells) < 6:
        return []
    return [c[0] for c in best_cells] + [best_cells[-1][2]]


def _recover_trailing_grad_rows(
    page, table_obj, crop_bottom: float, current_term: Optional[tuple]
) -> list:
    """pdfplumber 的表格框線偵測在頁面底部邊緣不穩定：當表格最後一列的底部框線剛好落在頁尾、
    沒被偵測成表格的一部分時，extract_tables() 會直接把這一整列資料漏掉（不是學期標題列那種
    「標題列本身消失」，是連課程列整列都消失），而且漏掉的這一列有時候内容本身還會接著延伸到
    下一頁開頭（常見於英文課名很長要換行的科目），所以復原到的中文課名/課號/學分/成績通常是
    完整的，只有英文課名尾段可能被截斷——不影響學分加總或及格判定，只是顯示的課名不完整。

    做法：table偵測到的範圍跟（下一個table開始的地方，或這頁可印內容的下緣）之間那一小段，
    重新用文字座標掃一次，用「課號欄裡符合課號格式」當作一列的錨點——課號本身在視覺上不一定
    剛好在那一列的最上緣（常常因為課名欄位換行比較多行，課號反而落在那一列的中間偏下），
    所以不能直接拿錨點的top當「這一列從這裡開始」，而是用「跟上一個/下一個錨點的中點」切分，
    每個文字片段歸給top最接近的那個錨點，再把同一欄位、屬於同一個錨點的文字片段接起來組回
    一筆完整的課程列。
    """
    col_bounds = _grad_table_column_bounds(table_obj)
    if len(col_bounds) < 7:
        return []

    x0, _, x1, bottom = table_obj.bbox
    if crop_bottom <= bottom:
        return []
    words = page.crop((x0, bottom, x1, crop_bottom)).extract_words()
    if not words:
        return []

    def col_index(word) -> int:
        center = (word["x0"] + word["x1"]) / 2
        for i in range(len(col_bounds) - 1):
            if col_bounds[i] <= center < col_bounds[i + 1]:
                return i
        return len(col_bounds) - 2

    anchors = sorted(
        w["top"] for w in words if col_index(w) == 0 and _GRAD_COURSE_CODE_RE.match(w["text"])
    )
    if not anchors:
        return []

    boundaries = (
        [float("-inf")]
        + [(anchors[i] + anchors[i + 1]) / 2 for i in range(len(anchors) - 1)]
        + [float("inf")]
    )

    recovered = []
    for i in range(len(anchors)):
        lo, hi = boundaries[i], boundaries[i + 1]
        by_col: dict = {}
        for w in words:
            if lo <= w["top"] < hi:
                by_col.setdefault(col_index(w), []).append(w)
        texts = [
            "".join(w["text"] for w in sorted(by_col.get(i, []), key=lambda w: (w["top"], w["x0"])))
            for i in range(len(col_bounds) - 1)
        ]
        if len(texts) < 7:
            continue
        code = texts[0].strip()
        if not _GRAD_COURSE_CODE_RE.match(code):
            continue
        try:
            credit = float(texts[5].strip())
        except ValueError:
            continue
        score_text = texts[6].strip()
        value, passed = _parse_grad_score(score_text)
        recovered.append(
            {
                "code": code,
                "name": texts[2].strip(),
                "category": texts[3].strip(),
                "credit": credit,
                "score": value,
                "score_text": score_text,
                "passed": passed,
                "year": current_term[0] if current_term else None,
                "term": current_term[1] if current_term else None,
            }
        )
    return recovered


def _extract_grad_course_rows(pdf: pdfplumber.PDF) -> list:
    """逐學期解析碩博成績單，格式跟大學部『畢業審核紀錄表』完全不同：沒有「判定」欄，而是按學期
    分成一張張表格，中間穿插「第X學年度第X學期」的標題列。pdfplumber在每頁最前面常會把整頁課程
    誤判成一大列雜訊（一整頁的文字擠在同一個儲存格），但雜訊列的第一欄不會是乾淨的課號格式，
    天生就會被下面的課號格式檢查濾掉，不用特別處理。

    書報討論／專題研究／產業專題研究這幾門課，每學期實際開課用的課號會逐學期輪替（例如專題研究
    在某個學年是CH8018/CH8019交替），不是固定課號——規定要求的是「修滿幾個學期」，不是「有沒有
    通過某個課號」，所以這裡刻意保留每一筆課程所屬的（學年,學期），不對同課號的多筆記錄去重，
    交给呼叫端依名稱＋學期組合去算修了幾個不同學期。
    """
    rows = []
    current_term = None
    for page in pdf.pages:
        table_objs = page.find_tables()
        for t_index, table_obj in enumerate(table_objs):
            codes_in_table = set()
            for row in table_obj.extract():
                cells = [(c or "") for c in row]
                if not cells:
                    continue
                first = cells[0].replace("\n", "").strip()

                m = _GRAD_SEMESTER_RE.search(first)
                if m:
                    current_term = (int(m.group(1)), int(m.group(2)))
                    continue

                if first == "課號" or not _GRAD_COURSE_CODE_RE.match(first):
                    continue
                if len(cells) < 7:
                    continue

                try:
                    credit = float(cells[5].replace("\n", "").strip())
                except ValueError:
                    continue

                score_text = cells[6].replace("\n", "").strip()
                value, passed = _parse_grad_score(score_text)
                rows.append(
                    {
                        "code": first,
                        "name": cells[2].replace("\n", "").strip(),
                        "category": cells[3].replace("\n", "").strip(),
                        "credit": credit,
                        "score": value,
                        "score_text": score_text,
                        "passed": passed,
                        "year": current_term[0] if current_term else None,
                        "term": current_term[1] if current_term else None,
                    }
                )
                codes_in_table.add(first)

            # 這個table的正常列都掃完、current_term也已經更新到這個table所屬的學期，可以安全地
            # 拿去補救table底部被切掉漏抓的那幾列——範圍是這個table偵測到的下緣，到下一個table
            # 開始的地方（同一頁還有下一段學期的話）或這頁的下緣（整頁可印範圍的底部）為止。
            next_top = table_objs[t_index + 1].bbox[1] if t_index + 1 < len(table_objs) else page.height
            for recovered_row in _recover_trailing_grad_rows(page, table_obj, next_top, current_term):
                if recovered_row["code"] not in codes_in_table:
                    rows.append(recovered_row)
    return rows


def _extract_grad_rows_from_audit_record(pdf: pdfplumber.PDF) -> list:
    """碩博成績單的第二種格式：教務處匯出的「畢業審核紀錄表」（跟大學部主系用的是同一種報表，
    只是系所欄位多了「碩士班」字樣），表格欄位（課號/課程名稱/學分數/成績/判定/學年學期）
    的形狀跟大學部完全一樣，直接重用 _extract_course_rows() 掃表格，不用再寫一套一樣的邏輯。

    這份報表每一列的「學年學期」本身就是4碼（例如「1141」＝114學年第1學期），跟另一種格式
    （學生個人成績一覽表的「第X學年度第X學期」標題列）不一樣的是：學期資訊直接跟著每一筆課程
    列走，不需要像標題列那樣跨表格、跨分頁延續同一個current_term狀態——也因此不會有標題列
    剛好卡在分頁交界被pdfplumber漏掉、導致整學期課程被誤判成上一學期的問題（見_extract_
    grad_course_rows的經驗）。「判定」欄本來就有通過/不通過，也不需要再比對及格分數。
    """
    courses = _extract_course_rows(pdf)
    rows = []
    for c in courses:
        term_str = (c.get("term") or "").strip()
        year = int(term_str[:3]) if len(term_str) == 4 and term_str.isdigit() else None
        term = int(term_str[3]) if len(term_str) == 4 and term_str.isdigit() else None
        rows.append(
            {
                "code": c["code"],
                "name": c["name"],
                "credit": c["credit"],
                "score": None,
                "score_text": c["grade"],
                "passed": c["passed"],
                "year": year,
                "term": term,
            }
        )
    return rows


_AUDIT_STUDENT_ID_RE = re.compile(r"學號\s*[：:]\s*(\d+)")


def _extract_student_id(pdf: pdfplumber.PDF) -> Optional[str]:
    """抓這份成績單是哪個學生的學號，給「學生總覽」顯示、手動確認項目名單制自動比對用
    （見MANUAL_REVIEW_ROSTER_PREFIXES的說明）。兩種格式的抓法不一樣：

    1. 「畢業審核紀錄表」：第一頁最上面就有一行「學號：114324076、姓名：OOO、系所：OOO」，
       直接用正規表達式從文字抓就好。
    2. 「學生個人成績一覽表」：沒有這種單行格式，是「基本資料」區塊「姓名/學號/性別/身分別/
       系所/年級」標籤一行、數值緊接著下一行，沒有被pdfplumber偵測成表格，只能用座標比對：
       找到「學號」兩個字的位置，再找同一欄（x座標相近）、正下方最近的一行文字當作學號值。

    兩種都抓不到時（例如掃描版PDF、或報表格式又改版）回傳None，呼叫端要能接受抓不到學號的
    狀況，不強制要求一定要有學號才能繼續檢核。
    """
    if not pdf.pages:
        return None
    page = pdf.pages[0]

    m = _AUDIT_STUDENT_ID_RE.search(page.extract_text() or "")
    if m:
        return m.group(1)

    words = page.extract_words()
    label = next((w for w in words if w["text"] == "學號"), None)
    if not label:
        return None
    candidates = [
        w for w in words
        if abs(w["x0"] - label["x0"]) < 5 and label["top"] < w["top"] <= label["top"] + 40
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda w: w["top"])["text"].strip() or None


def parse_grad_transcript(pdf_bytes: bytes) -> dict:
    """從碩博成績單PDF擷取逐學期課程明細，呼叫端要用try/except包住，理由跟parse_transcript
    一樣：pdfplumber打開損毀檔案或非PDF檔案時會丟例外。

    碩博現在收兩種格式，靠報表自己的標題文字分辨該用哪一套解析邏輯，使用者上傳時不用自己選：
    - 「畢業審核紀錄表」：跟大學部共用的報表格式，見_extract_grad_rows_from_audit_record。
    - 其餘（教務系統「學生個人成績一覽表」）：見_extract_grad_course_rows，用固定的
      GRAD_PASSING_SCORE（70分）判斷及格與否。
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join((page.extract_text() or "") for page in pdf.pages)
        if "畢業審核紀錄表" in full_text:
            rows = _extract_grad_rows_from_audit_record(pdf)
        else:
            rows = _extract_grad_course_rows(pdf)
        has_text = bool(full_text.strip())
        student_id = _extract_student_id(pdf)
    return {"rows": rows, "has_text": has_text, "student_id": student_id}


def _build_graduate_check(rows: list, track: dict) -> dict:
    """碩博資格門檻判斷：學分池、六門課程池通過門數、化工/材料領域各一門、書報討論／專題研究／
    產業專題研究的修習學期數，四種判斷邏輯分開算，缺一項就整體判定不通過。每個學制的課程池／
    領域對照表是各自獨立的一份資料（見_graduate_admin_context的說明），不是全學制共用同一份。
    """
    track_courses = track.get("courses", [])
    courses_by_code = {c["code"]: c for c in track_courses}
    pool_codes = {c["code"] for c in track_courses if c.get("in_pool")}

    seen = set()
    total_credit = 0.0
    passed_pool_by_code: dict = {}
    domains_covered: set = set()
    # 一門課可能同時掛化工跟材料兩個領域，所以每個領域各自存一份「課號->課名」，
    # 不能只存課號集合，不然沒辦法回答「化工領域是靠哪幾門課通過的」這種回溯問題。
    domain_courses: dict = {}
    seminar_terms: set = set()
    topics_terms: set = set()
    industry_terms: set = set()

    for r in rows:
        # 同一（課號,學年,學期）只算一次，避免萬一同一學期表格被掃到兩次時學分被重複加總
        key = (r["code"], r["year"], r["term"])
        if key in seen:
            continue
        seen.add(key)
        if not r["passed"]:
            continue

        total_credit += r["credit"] or 0

        course_info = courses_by_code.get(r["code"])
        if course_info:
            domains = course_info.get("domains", [])
            domains_covered.update(domains)
            for d in domains:
                domain_courses.setdefault(d, {})[r["code"]] = course_info["name"]
            if r["code"] in pool_codes:
                passed_pool_by_code[r["code"]] = course_info["name"]

        name = r["name"] or ""
        term_key = (r["year"], r["term"])
        if "產業專題研究" in name:
            industry_terms.add(term_key)
        elif "專題研究" in name:
            topics_terms.add(term_key)
        elif "書報討論" in name:
            seminar_terms.add(term_key)

    industry_min = track.get("industry_topics_min_semesters", 0)
    credit_ok = total_credit >= track["min_total_credits"]
    pool_ok = len(passed_pool_by_code) >= track["pool_min_pass"]
    domain_ok = (not track.get("require_domain_each")) or ({"化工", "材料"} <= domains_covered)
    seminar_ok = len(seminar_terms) >= track["seminar_min_semesters"]
    topics_ok = len(topics_terms) >= track["topics_min_semesters"]
    industry_ok = len(industry_terms) >= industry_min if industry_min else True

    return {
        "total_credit": total_credit,
        "min_total_credits": track["min_total_credits"],
        "credit_ok": credit_ok,
        "pool_passed": sorted(
            ({"code": code, "name": name} for code, name in passed_pool_by_code.items()),
            key=lambda c: c["code"],
        ),
        "pool_min_pass": track["pool_min_pass"],
        "pool_ok": pool_ok,
        "require_domain_each": track.get("require_domain_each", False),
        "domains_covered": sorted(domains_covered),
        "domain_courses": {
            domain: sorted(
                ({"code": code, "name": name} for code, name in courses.items()), key=lambda c: c["code"]
            )
            for domain, courses in domain_courses.items()
        },
        "domain_ok": domain_ok,
        "seminar_semesters": len(seminar_terms),
        "seminar_min_semesters": track["seminar_min_semesters"],
        "seminar_ok": seminar_ok,
        "topics_semesters": len(topics_terms),
        "topics_min_semesters": track["topics_min_semesters"],
        "topics_ok": topics_ok,
        "industry_topics_semesters": len(industry_terms),
        "industry_topics_min_semesters": industry_min,
        "industry_topics_ok": industry_ok,
        "passed": credit_ok and pool_ok and domain_ok and seminar_ok and topics_ok and industry_ok,
    }


def _mark_counts_as_fail(courses: list) -> list:
    """幫courses清單裡每筆課程就地加上counts_as_fail欄位，標出結果頁「未通過課程」要顯示哪幾筆：
    同一課號重複修習會出現多筆記錄（例如先不通過、後來重補修通過），只要其中一次真的通過，
    這門課對學生來說就不是「還沒通過」，不該出現在「未通過課程」清單裡誤導使用者——但課程
    明細（courses本身）仍然照實列出每一筆，不隱藏教務處原始紀錄，只是多一個欄位註記。
    """
    passed_once_codes = {c["code"] for c in courses if c["passed"]}
    for c in courses:
        c["counts_as_fail"] = not c["passed"] and c["code"] not in passed_once_codes
    return courses


def _build_graduate_entry(filename: str, parsed: dict, track_key: str, year_data: dict) -> dict:
    """/graduate 上傳頁的結果項，沿用跟_build_result_entry一樣的欄位骨架（見_build_minor_only_entry
    的說明），主系相關欄位全部給「不檢查」的中性值，只有graduate欄位是真的算出來的判定結果。
    year_data是graduate_rules.yaml裡「某一個入學學年度」底下的那份資料（tracks），不是整份
    多學年度的規則檔。
    """
    track = year_data["tracks"][track_key]
    rows = parsed["rows"]
    check = _build_graduate_check(rows, track)

    # 依學年期排序（而不是課號）：碩博的判定邏輯（書報討論/專題研究要修幾學期、課程池通過
    # 門數）本來就是以學期為單位累計的，依時間順序列出課程明細，使用者比較看得出來系統是
    # 怎麼一學期一學期算出書報討論/專題研究的學期數、哪些課落進課程池。
    courses_display = sorted(
        (
            {
                "code": r["code"],
                "name": r["name"],
                "credit": r["credit"],
                "grade": r["score_text"],
                "passed": r["passed"],
                "year": r["year"],
                "term": r["term"],
                "term_label": f"{r['year']}-{r['term']}" if r["year"] and r["term"] else "（未標示學期）",
            }
            for r in rows
        ),
        key=lambda c: (c["year"] or 0, c["term"] or 0, c["code"]),
    )
    courses_display = _mark_counts_as_fail(courses_display)

    manual_review_items = track.get("manual_review_items", [])
    # 預口試通過／英文能力這兩項有名單制自動判斷（見MANUAL_REVIEW_ROSTER_PREFIXES說明）：
    # 預設當作已通過（自動打勾），只有學號出現在「還沒通過」名單裡才不勾，省得辦公人員每份
    # 都要手動點一次（見_extract_student_id兩種格式各自怎麼抓）。抓不到學號、或項目沒有對應
    # 的名單制（例如資格考、論文點數）就維持舊行為不自動勾選，不會因為抓不到學號就誤判。
    student_id = parsed.get("student_id")
    exceptions = load_manual_review_exceptions()
    manual_review_checked = []
    for item in manual_review_items:
        roster_key = _manual_review_roster_key(item)
        if roster_key is None or not student_id:
            manual_review_checked.append(False)
        else:
            manual_review_checked.append(student_id not in exceptions[roster_key])

    return {
        "filename": filename,
        "error": None,
        "student_id": student_id,
        "total_credit": check["total_credit"],
        "credit_ok": True,
        "required_credits": 0,
        "required_credit_total": 0,
        "required_credit_by_tier": {},
        "required_credit_ok": True,
        "elective_credits": 0,
        "elective_credit_total": 0,
        "elective_credit_ok": True,
        "elective_courses": [],
        "passed": check["passed"],
        "courses": courses_display,
        "has_text": parsed["has_text"],
        "unmet_categories": [],
        "missing_required": [],
        "note_results": [],
        "note_sections": [],
        "minor": None,
        "graduate": {
            "track_label": track["label"],
            **check,
            "manual_review_items": manual_review_items,
            "manualReviewChecked": manual_review_checked,
            "student_id": student_id,
        },
    }


def _split_codes(code_field: str) -> list:
    """把應修科目表課號欄位（多門課用「/」合併記錄，例如「CH1023/CH1024」）拆成單一課號的list。"""
    return [c.strip() for c in (code_field or "").split("/") if c.strip()]


def _bucket_required_courses(required_courses: list) -> tuple:
    """把應修科目表依 `group` 欄位分堆成「一般必修」跟「M選N群組必修」兩種，兩種都要用到的地方
    （missing_required_courses、_consumed_required_codes）共用同一份分堆結果，不用各自重新掃一次。

    回傳 (plain, groups)：
    - plain：沒有分組的科目，每筆多帶一個 `codes`（課號用「/」拆開後的list）。
    - groups：{分組名稱: [科目, ...]}，同一組的科目按 required_courses 原本的順序排列，
      每筆一樣多帶 `codes`。
    """
    plain = []
    groups: dict = {}
    for course in required_courses:
        codes = _split_codes(course.get("code"))
        if not codes:
            continue
        group = (course.get("group") or "").strip()
        entry = {**course, "codes": codes}
        if group:
            groups.setdefault(group, []).append(entry)
        else:
            plain.append(entry)
    return plain, groups


def missing_required_courses(
    required_courses: list, passed_codes: set, group_requirements: dict = None, substitutions: dict = None
) -> list:
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

    substitutions（轉系生抵修/抵免，見_parse_substitutions）：原課號對應的目標課號，視同
    目標課號也已經通過，不然轉系生在原系所修過、拿來抵免的課會因為課號對不上應修科目表而
    被誤判成還沒通過。
    """
    group_requirements = group_requirements or {}
    substituted_codes = {t for targets in (substitutions or {}).values() for t in targets}
    effective_passed_codes = passed_codes | substituted_codes
    plain, groups = _bucket_required_courses(required_courses)

    missing = []
    for course in plain:
        missing_codes = [c for c in course["codes"] if c not in effective_passed_codes]
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
        passed_count = sum(1 for c in all_codes if c in effective_passed_codes)
        if passed_count >= required_count:
            continue  # 這組已經選夠門數，畢業條件已經滿足，不算缺
        missing_codes = [c for c in all_codes if c not in effective_passed_codes]
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


def _consumed_required_codes(
    required_courses: list,
    group_requirements: dict,
    passed_codes: set,
    passed_courses: list = None,
    substitutions: dict = None,
) -> tuple:
    """算出「被拿去滿足必修/必選修門檻」的課號集合，選修學分來源規則要拿這個集合去排除必修課。

    一般必修（沒有 group 的科目）沒有「多修」的概念，攤平後全部算必修消耗掉。核心必選修/專題
    必選修這種M選N分組就不一樣：分組底下的選項全部列在應修科目表裡，但學生真的通過門檻只需要
    N門，超過N門的部分依應修科目表備註「核心必選修與專題必選修超修所得之學分，得計入本系之
    畢業學分總數」，應該要能算進選修學分，不能整組通通當必修排除掉，不然超修的學分會憑空消失、
    永遠沒辦法被任何門檻採計到。跟 missing_required_courses() 一樣用「攤平後的課號數」而不是
    「完整選項數」去算門檻（這裡用同一個順序取前N個已通過的課號當作「消耗掉」的），跟官方畢業
    審核系統的認定方式一致。

    另外「國文」「外文」「通識課程」「體育課程」「服務學習課程」這種必修項目，選課方式多元、
    沒有登記固定課號（`code` 是空字串），但有登記 `code_prefixes`（例如外文都是LN開頭）的話，
    成績單裡課號符合前綴的課，也要算必修消耗掉，不然學生實際選的那門課（例如大一英文LN1001）
    課號沒登記在應修科目表裡，會被誤判成選修，選修學分因此虛胖。

    這種課號前綴比對也要有「只消耗到門檻為止」的上限：這幾項都各自有自己的學分門檻（該筆
    required_courses 項目的 `credits`，例如外文6學分、通識14學分），真實成績單裡符合前綴的課
    學分加總常常超過這個數字（例如外文修了大一英文6學分又修了日文3學分，通識超修2學分）。跟
    M選N分組不一樣的是，這種超修原則上「不能」流向選修學分——選修學分只能是本系專業課程或應修
    科目表列出的必修/必選修課程超修的部分，國文/外文這種共同必修categories多修的課不算數（既不是
    必修、也不是選修，單純不列入這兩個子門檻，但還是算在總學分裡），超過門檻的部分從consumed
    排除、另外歸進 excluded 集合，_credit_breakdown 要把這個集合也從選修學分池扣掉。**通識課程、
    語言中心開設的第二外語課程是例外**：教務處確認通識多修的部分可以計入選修學分（跟M選N分組
    超修一樣）；外文應修科目表的備註本來就寫明「大一英文／英文系課程／語言中心第二外語課程
    擇一修讀」，三選一已經滿足門檻後，學生額外修的語言中心第二外語課（課號是LN00開頭，例如
    LN0025日文、LN0043德文，跟大一英文/英文系課程的LN1開頭不同範圍）不該被當成「選了另一個
    選項的超修」排除掉，應該跟通識超修一樣流向選修學分。這兩種超修都不進consumed（不算必修）
    也不進excluded（不會被排除在選修外），直接留給_credit_breakdown的elective_courses算選修。
    門檻是0（例如體育、服務學習課程本身沒有學分門檻）的項目維持全部算必修消耗掉，因為沒有
    「多少算超修」的基準可以拿來切。

    substitutions（轉系生抵修/抵免，見_parse_substitutions）：原課號對應的目標課號，視同目標
    課號也已經通過——這裡額外把「目標課號有被consumed」的原課號也併入consumed，不然passed_
    courses裡這門課用的是原課號，光是consumed裡有目標課號比對不到，學分會被錯放進選修。

    回傳 (consumed, excluded, consumed_tier)：consumed 是必修學分池的課號，excluded 是「不算
    必修、但也不能算選修」的超修課號（目前只有課號前綴超修這一種情況），consumed_tier 是
    「課號 -> 應修科目表上這門課的層級（tier，例如共同必修/院訂必修/系訂必修）」的對照，給
    結果頁把必修學分拆成三塊顯示用——拆分依據直接沿用應修科目表已經有的tier欄位（純畫面分類
    用，見_is_header_row附近tier的說明），不是另外發明一套分類規則。
    """
    group_requirements = group_requirements or {}
    substitutions = substitutions or {}
    substituted_codes = {t for targets in substitutions.values() for t in targets}
    effective_passed_codes = passed_codes | substituted_codes
    plain, groups = _bucket_required_courses(required_courses)
    consumed = {c for course in plain for c in course["codes"]}
    consumed_tier = {c: course.get("tier") or "" for course in plain for c in course["codes"]}

    for group, group_courses in groups.items():
        required_count = group_requirements.get(group, 1)
        all_codes = [c for gc in group_courses for c in gc["codes"]]
        code_tier = {c: gc.get("tier") or "" for gc in group_courses for c in gc["codes"]}
        passed_in_group = [c for c in all_codes if c in effective_passed_codes]
        newly_consumed = passed_in_group[:required_count]
        consumed.update(newly_consumed)
        for c in newly_consumed:
            consumed_tier[c] = code_tier.get(c, "")

    for original_code, targets in substitutions.items():
        satisfied = next((t for t in targets if t in consumed), None)
        if satisfied is not None:
            consumed.add(original_code)
            consumed_tier[original_code] = consumed_tier.get(satisfied, "")

    excluded = set()
    if passed_courses:
        for course in required_courses:
            prefixes = course.get("code_prefixes") or []
            if not prefixes:
                continue
            threshold = course.get("credits") or 0
            is_general_education = "通識" in (course.get("name") or "")
            matched = [
                c for c in passed_courses
                if c["code"] and c["code"] not in consumed and any(c["code"].startswith(p) for p in prefixes)
            ]
            if threshold <= 0:
                consumed.update(c["code"] for c in matched)
                for c in matched:
                    consumed_tier[c["code"]] = course.get("tier") or ""
                continue
            accumulated = 0.0
            for c in matched:
                # 語言中心第二外語課程課號固定是LN00開頭（例如LN0025日文、LN0043德文），跟
                # 大一英文/英文系課程的LN1xxx不同範圍——外文超修如果是這個範圍的課，比照通識
                # 超修放行，見上面docstring的說明。
                is_language_center_course = c["code"].startswith("LN0")
                if accumulated < threshold:
                    consumed.add(c["code"])
                    consumed_tier[c["code"]] = course.get("tier") or ""
                    accumulated += c["credit"]
                elif not is_general_education and not is_language_center_course:
                    excluded.add(c["code"])
                # 通識／語言中心第二外語超修：不加進consumed（不算必修）也不加進excluded（不會
                # 被排除在選修外），直接留在consumed/excluded之外，_credit_breakdown的
                # elective_courses自然就會把它算進選修學分。

    return consumed, excluded, consumed_tier


def _credit_breakdown(
    required_courses: list,
    group_requirements: dict,
    passed_codes: set,
    passed_courses: list,
    substitutions: dict = None,
) -> dict:
    """把成績單切成「必修學分」跟「選修學分」兩塊：必修學分＝被拿去滿足必修/必選修門檻的課學分
    加總（含用課號前綴比對到的國文/外文/通識這種沒登記固定課號的必修項目）；選修學分則是其餘
    已通過課程扣掉「共同必修超修」（excluded，見_consumed_required_codes說明）後的學分加總——
    這種超修只能算在總學分裡，不能算選修，選修必須是本系專業課程或必修/必選修超修的部分（通識
    是例外：通識超修不會進excluded，所以會自然留在這裡被算進選修學分，見_consumed_required_codes
    的說明）。`/check` 的必修/選修學分門檻，跟 credit_condition 備註規則（scope="elective"時）
    要算的「選修來源」，都是同一份切分結果，這裡算一次共用，不用兩邊各自重算。

    必修學分另外依應修科目表的tier欄位（共同必修/院訂必修/系訂必修）拆成required_credit_by_tier，
    給結果頁／Excel分開顯示用；沒有設定tier的項目（例如輔系應修科目表沒有tier欄位）會歸在
    空字串那個key底下。
    """
    consumed, excluded, consumed_tier = _consumed_required_codes(
        required_courses, group_requirements, passed_codes, passed_courses, substitutions
    )
    elective_courses = [
        c for c in passed_courses if c["code"] and c["code"] not in consumed and c["code"] not in excluded
    ]
    required_credit_total = sum(c["credit"] for c in passed_courses if c["code"] and c["code"] in consumed)
    elective_credit_total = sum(c["credit"] for c in elective_courses)
    required_credit_by_tier: dict = {}
    for c in passed_courses:
        if c["code"] and c["code"] in consumed:
            tier = consumed_tier.get(c["code"], "")
            required_credit_by_tier[tier] = required_credit_by_tier.get(tier, 0) + c["credit"]
    return {
        "elective_courses": elective_courses,
        "required_credit_total": required_credit_total,
        "required_credit_by_tier": required_credit_by_tier,
        "elective_credit_total": elective_credit_total,
    }


def evaluate_note_rules(
    note_rules: list,
    passed_codes: set,
    passed_courses: list,
    required_courses: list,
    group_requirements: dict = None,
    substitutions: dict = None,
) -> list:
    """把應修科目表下方的「備註」規則（rules.yaml 的 note_rules）拿去對照成績單，算出每條的完成狀態。

    兩種 kind 對應畢業門檻PDF備註裡實際會出現的規則形狀：
    - credit_condition（學分條件）：由兩個獨立設定組成，先選範圍再選方向，四種組合對應四種
      實際會遇到的備註形狀：
      範圍 scope：
        - "elective"：限定在選修學分池（成績單裡「已通過但不在必修清單裡」的課）範圍內算，
          例如「選修16學分中至少6學分要CH課號」。
        - "all"：不限選修，直接對整份成績單（passed_courses）算，不管課是不是已經被算進
          必修學分池，例如「通識核心必修三大領域至少須修習一個領域」。
      方向 direction：
        - "include"：範圍內的課，課號前綴/名單「符合」min_credits/code_prefixes/extra_codes
          指定條件的部分要達標（例如至少6學分要CH開頭或名單內的課）。
        - "exclude"：範圍內的課，「不符合」條件的部分要達標（例如至少3學分要「不是」CH開頭，
          也就是外系課程）。
      同一段備註如果同時有「須屬於」跟「不可屬於」兩種條件（例如「至少6學分CH課程」+「至少3學分
      外系課程」），就開兩條規則、category填一樣的名稱，結果頁會自動歸在同一段落顯示。
    - info：像「同一學期不可同時修讀X和Y」「依本校雙主修辦法」這種沒有學期資料/純政策引用、
      根本沒辦法從成績單自動判斷的備註，就只顯示文字提醒，不判斷完成與否。
    """
    elective_courses = _credit_breakdown(
        required_courses, group_requirements, passed_codes, passed_courses, substitutions
    )["elective_courses"]

    results = []
    for rule in note_rules:
        kind = rule.get("kind", "info")
        text = rule.get("text", "")
        category = rule.get("category", "")

        if kind == "credit_condition":
            scope = rule.get("scope") or "elective"
            direction = rule.get("direction") or "include"
            min_credits = rule.get("min_credits") or 0
            prefixes = rule.get("code_prefixes") or []
            # 額外名單同時比對課號跟課名：學院公告的課群名單有時只給課名、沒有課號（例如還沒實際開課
            # 排課號），兩種都收才不會因為拿到的名單格式不一樣就沒辦法用。
            extra_matches = set(rule.get("extra_codes") or [])

            pool = elective_courses if scope == "elective" else [c for c in passed_courses if c["code"]]

            def _matches(c, prefixes=prefixes, extra_matches=extra_matches):
                return (
                    c["code"] in extra_matches
                    or c["name"] in extra_matches
                    or any(c["code"].startswith(p) for p in prefixes)
                )

            matched = [c for c in pool if _matches(c) == (direction == "include")]
            total = sum(c["credit"] for c in matched)
            ok = total >= min_credits
            label = "符合條件" if direction == "include" else "排除條件"
            results.append(
                {
                    "text": text,
                    "kind": kind,
                    "category": category,
                    "status": "ok" if ok else "fail",
                    "detail": f"{label} {total}/{min_credits}",
                    # 學分為0的列（例如「操行」CR0001這種評量/行政紀錄，不是真的課）不列進顯示清單，
                    # 理由跟elective_courses那邊一樣：反正對學分加總本來就貢獻0，列出來只會讓人
                    # 誤以為那是一門真的被算進條件的課；total是用完整matched算的，不受這個顯示過濾影響。
                    "matched_courses": [c for c in matched if c["credit"] > 0],
                    "match_label": f"{label}的課程",
                }
            )

        else:
            results.append({"text": text, "kind": "info", "category": category, "status": "info", "detail": ""})

    return results


def _group_note_results_by_category(note_results: list) -> list:
    """把備註規則的判斷結果依 category（分類，例如「二、院、系訂必修」）分組，維持第一次出現的分類順序，
    好讓結果頁能照著應修科目表原本的「一/二/三」段落分開顯示，而不是全部備註擠成一長串列表。
    """
    sections: list = []
    index_by_category: dict = {}
    for n in note_results:
        cat = n.get("category") or "其他備註"
        if cat not in index_by_category:
            index_by_category[cat] = len(sections)
            # 這裡故意不叫 "items"：dict 內建就有 .items() 方法，Jinja 樣板裡用 section.items
            # 這種點記法時會先撞到內建方法（拿到 bound method）而不是這個 key，要避開命名衝突。
            sections.append({"category": cat, "entries": []})
        sections[index_by_category[cat]]["entries"].append(n)
    return sections


def _build_minor_result(minor_data: dict, result: dict, fallback_name: str = "輔系") -> dict:
    """輔系／碩士班／博士班這類「非主系」資格，都是跟主系完全獨立的一套「應修科目表」（自己的
    required_courses/group_requirements/note_rules），檢核邏輯直接重用missing_required_courses()
    跟evaluate_note_rules()——規則形狀（固定必修幾門＋M選N分組、限修總學分門檻）跟主系的完全一樣，
    差別只在這套規則不能混進主系的required_courses，不然主系學生也會被要求要修這些限定的課。
    fallback_name是該學年度還沒替這個項目自訂名稱時要顯示的預設名稱（例如「輔系」或「碩士班／博士班」）。
    """
    required_courses = minor_data.get("required_courses", [])
    group_requirements = minor_data.get("group_requirements", {})
    note_rules = minor_data.get("note_rules", [])
    missing_required = missing_required_courses(
        required_courses, result["passed_codes"], group_requirements, result.get("substitutions")
    )
    note_results = evaluate_note_rules(
        note_rules,
        result["passed_codes"],
        result["passed_courses"],
        required_courses,
        group_requirements,
        result.get("substitutions"),
    )
    note_failed = any(n["status"] == "fail" for n in note_results)
    return {
        "name": minor_data.get("name") or fallback_name,
        "passed": not missing_required and not note_failed,
        "missing_required": missing_required,
        "note_results": note_results,
        "note_sections": _group_note_results_by_category(note_results),
    }


# 導覽列上每個身份別的上傳頁（index.html）共用同一份樣板，用program區分要顯示哪個身份、
# 送出表單後要跑哪一套規則。之後要加新身份，多半只要在這裡加一個key、在rules.yaml每個學年度
# 底下加對應的規則區塊，不用再另外複製一份樣板或/check的邏輯。有後台管理介面（/admin可以編輯
# 應修科目）的身份另外要加進_SECONDARY_TARGETS，兩邊是各自獨立的清單——graduate目前只開了
# 上傳頁，還沒有後台管理介面，所以不在_SECONDARY_TARGETS裡。
_PROGRAM_LABELS = {"main": "畢業", "minor": "輔系", "double_major": "雙主修"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    rules = load_rules()
    years = sorted(rules.keys(), reverse=True)
    return templates.TemplateResponse(request, "index.html", {"years": years, "program": "main"})


_SECONDARY_STATUS_CHOICES = [("minor", "輔系"), ("double_major", "雙主修")]


async def _secondary_status_index(request: Request, default_program: str) -> HTMLResponse:
    """輔系／雙主修資格檢核的上傳頁，合併成一個頁面用「身份」下拉選單切換（跟/graduate的
    「學制」選單同樣做法）——兩者跟主系一樣依入學學年度分規則、規則形狀也完全相同（見
    _build_minor_result），只差在rules.yaml年度資料底下用的是哪個key（minor/double_major），
    沒必要拆成兩個長得一樣的頁面。/minor、/double_major兩個網址都導到這個頁面，差別只在
    下拉選單預設選哪一個身份。
    """
    rules = load_rules()
    years = sorted(rules.keys(), reverse=True)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "years": years,
            "program": default_program,
            "program_choices": _SECONDARY_STATUS_CHOICES,
        },
    )


@app.get("/minor", response_class=HTMLResponse)
async def minor_index(request: Request):
    return await _secondary_status_index(request, "minor")


@app.get("/graduate", response_class=HTMLResponse)
async def graduate_index(request: Request, year: Optional[str] = None):
    """碩士班／博士班資格檢核的上傳頁。碩博的規則存在獨立的graduate_rules.yaml，跟主系／輔系／
    雙主修用的rules.yaml是兩份不相干的檔案，所以用專屬的graduate.html樣板，但一樣依入學學年度
    分規則、也一樣要選學制（碩士班/博士班/工學博士班）。
    """
    grad_rules = load_graduate_rules()
    years, selected_year, year_data = _resolve_year(grad_rules, year)
    return templates.TemplateResponse(
        request,
        "graduate.html",
        {"years": years, "year": selected_year, "tracks": year_data.get("tracks", {})},
    )


@app.get("/double_major", response_class=HTMLResponse)
async def double_major_index(request: Request):
    return await _secondary_status_index(request, "double_major")


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
    passed_courses = [{"code": c["code"], "name": c["name"], "credit": c["credit"], "term": None} for c in courses]
    return {
        "courses": courses,
        "total_credit": total_credit,
        "unmet_categories": [],
        "passed_codes": passed_codes,
        "passed_courses": passed_courses,
        "has_text": True,
    }


# 12xxx（一般系訂必修12100/12101、核心必選修/專題必選修分組12200~12203）算出來的內容，
# 跟我們自己 missing_required_courses() 比對出來的「還沒通過的科目」是同一件事、來源不同，
# 兩邊都顯示會重複，所以「教務處判定尚未完成的項目」這區塊只保留12xxx以外的類別
# （共同必修11xxx、操行體育軍訓服務學習18xxx、特殊檢核19xxx這些我們自己沒有在追蹤的項目）。
_DUPLICATED_CATEGORY_PREFIX = "12"

# 19120（特殊檢核條件-修習系所選修總學分）雖然前綴是19xxx（特殊檢核），但內容其實就是
# 選修學分夠不夠，跟我們自己算的elective_credit_total/elective_credits是同一件事，
# 也要排除掉避免重複顯示。跟12xxx用前綴排除不同，這個用精確代碼比對，不要整個19xxx都濾掉
# （英文能力門檻等其他19xxx項目我們自己沒有在追蹤，還是要保留顯示）。
#
# 18201（大一體育）、18202（其它年級體育）是18200（體育）底下的兩個子類別，應修科目數
# 剛好是18200的子集加總（例如18200應修5科目＝18201應修2科目＋18202應修3科目），18200
# 的備註欄本身就會寫「未通過類別:18202」點出哪個子類別沒過，所以子類別沒必要跟18200的
# 「體育」一起重複列出——兩筆資訊其實是同一件事，列兩次只會讓人誤以為是兩個不相干的門檻。
#
# 18101（操行）是18100（操行）底下唯一的子類別，兩筆名稱、應修/實修數字完全一樣（18100
# 就只有18101這一個子項，沒有像體育那樣再細分），單純是報表格式重複列了一次，一樣排除掉。
_DUPLICATED_CATEGORY_CODES = {"19120", "18201", "18202", "18101"}


def _build_result_entry(
    filename: str,
    result: dict,
    required_total: float,
    required_credits: float,
    elective_credits: float,
    required_courses: list,
    group_requirements: dict,
    note_rules: list,
) -> dict:
    """把單一份成績單的解析結果，組成結果頁要顯示的一筆資料。

    是否「通過」不是只看總學分數字，還要求：應修科目表裡的必修科目（含核心必選修/專題必選修
    這種M選N分組）全部滿足、必修/選修學分子項門檻（required_credits/elective_credits，選填，
    對應PDF「必修112學分、選修16學分」這種寫法）達標、應修科目表備註規則（note_rules，例如
    選修學分來源限制）也沒有不合格的項目，四個條件都成立才算真的達到畢業資格。
    """
    substitutions = result.get("substitutions") or {}
    courses_display = _mark_counts_as_fail(result["courses"])
    missing_required = missing_required_courses(
        required_courses, result["passed_codes"], group_requirements, substitutions
    )
    credit_ok = result["total_credit"] >= required_total
    breakdown = _credit_breakdown(
        required_courses, group_requirements, result["passed_codes"], result["passed_courses"], substitutions
    )
    required_credit_ok = breakdown["required_credit_total"] >= required_credits if required_credits else True
    elective_credit_ok = breakdown["elective_credit_total"] >= elective_credits if elective_credits else True
    unmet_categories = [
        u
        for u in result["unmet_categories"]
        if not u["code"].startswith(_DUPLICATED_CATEGORY_PREFIX) and u["code"] not in _DUPLICATED_CATEGORY_CODES
    ]
    note_results = evaluate_note_rules(
        note_rules,
        result["passed_codes"],
        result["passed_courses"],
        required_courses,
        group_requirements,
        substitutions,
    )
    note_rules_failed = any(n["status"] == "fail" for n in note_results)
    return {
        "filename": filename,
        "error": None,
        "student_id": result.get("student_id"),
        "total_credit": result["total_credit"],
        "credit_ok": credit_ok,
        "required_credits": required_credits,
        "required_credit_total": breakdown["required_credit_total"],
        "required_credit_by_tier": breakdown["required_credit_by_tier"],
        "required_credit_ok": required_credit_ok,
        "elective_credits": elective_credits,
        "elective_credit_total": breakdown["elective_credit_total"],
        "elective_credit_ok": elective_credit_ok,
        # 學分為0的列（例如「操行」CR0001這種評量/行政紀錄，不是真的選修課）不列進顯示清單，
        # 反正對選修學分加總本來就貢獻0學分，列出來只會讓人誤以為那是一門「被算進選修」的課
        "elective_courses": sorted(
            (c for c in breakdown["elective_courses"] if c["credit"] > 0), key=lambda c: c["code"]
        ),
        "passed": (
            credit_ok
            and required_credit_ok
            and elective_credit_ok
            and not missing_required
            and not note_rules_failed
            # unmet_categories 是教務處自己在畢業審核紀錄表裡標記「未完成」的項目，已經濾掉
            # 跟其他檢查重複的部分（見上面的_DUPLICATED_CATEGORY_PREFIX/CODES）——剩下的
            # 通常是我們自己的規則完全沒建模的特殊檢核條件（例如英文能力鑑定），教務處都說
            # 沒過了，不能因為我們自己的學分/科目檢查都過就顯示「已符合畢業資格」蓋過這件事。
            and not unmet_categories
        ),
        "courses": courses_display,
        "has_text": result["has_text"],
        "unmet_categories": unmet_categories,
        "missing_required": missing_required,
        "note_results": note_results,
        "note_sections": _group_note_results_by_category(note_results),
        # 輔系資格檢核是/minor專用頁面的獨立流程（見_build_minor_only_entry），不會混進主系
        # 這裡的判定結果，所以這裡固定是None——結果頁看到None就不會顯示輔系那個區塊。
        "minor": None,
        "graduate": None,
    }


def _build_error_entry(filename: str, error: str) -> dict:
    """檔案太大、不是有效PDF、或解析途中出例外時用這個，讓結果頁能顯示明確的錯誤原因，
    而不是讓整個request壞掉、變成使用者看不懂的500錯誤頁。"""
    return {
        "filename": filename,
        "error": error,
        "student_id": None,
        "total_credit": 0,
        "credit_ok": False,
        "required_credits": 0,
        "required_credit_total": 0,
        "required_credit_by_tier": {},
        "required_credit_ok": False,
        "elective_credits": 0,
        "elective_credit_total": 0,
        "elective_credit_ok": False,
        "elective_courses": [],
        "passed": False,
        "courses": [],
        "has_text": False,
        "unmet_categories": [],
        "missing_required": [],
        "note_results": [],
        "note_sections": [],
        "minor": None,
        "graduate": None,
    }


def _build_minor_only_entry(filename: str, result: dict, minor_data: dict, fallback_name: str = "輔系") -> dict:
    """/minor、/graduate 這類專用上傳頁（跟主系上傳頁分開）的結果項——沿用跟_build_result_entry
    一樣的欄位骨架，讓result.html既有的樣板／CSV／JS邏輯不用額外判斷欄位缺不缺，主系相關欄位
    （必修/選修學分門檻等）全部給「不檢查」的中性值，只有minor欄位是真的算出來的判定結果。passed
    故意設成minor_result的passed，這樣結果頁最上面「學生總覽」清單的狀態欄、CSV的判定結果欄，
    不用另外改邏輯就能正確顯示這個項目的判定（不是主系判定）。
    """
    minor_result = _build_minor_result(minor_data, result, fallback_name)
    return {
        "filename": filename,
        "error": None,
        "student_id": result.get("student_id"),
        "total_credit": result["total_credit"],
        "credit_ok": True,
        "required_credits": 0,
        "required_credit_total": 0,
        "required_credit_by_tier": {},
        "required_credit_ok": True,
        "elective_credits": 0,
        "elective_credit_total": 0,
        "elective_credit_ok": True,
        "elective_courses": [],
        "passed": minor_result["passed"],
        "courses": result["courses"],
        "has_text": result["has_text"],
        "unmet_categories": [],
        "missing_required": [],
        "note_results": [],
        "note_sections": [],
        "minor": minor_result,
        "graduate": None,
    }


@app.post("/check", response_class=HTMLResponse)
async def check(
    request: Request,
    year: str = Form(...),
    files: List[UploadFile] = File([]),
    program: str = Form("main"),
):
    rules = load_rules()
    year_data = rules.get(year, {})

    # /minor、/double_major 上傳頁送出的表單都走這個分支：只比對各自身份的規則（存在year_data
    # 底下同名的key），完全不管主系的必修/選修學分門檻，結果頁也只顯示該身份的資格判定，不會出現
    # 「畢業資格」字樣（那是主系專屬的判定）。program不是這幾種就一律當main處理。
    # 碩博（graduate）的規則存在獨立的graduate_rules.yaml，走專屬的/graduate/check，不會送表單來這裡。
    if program in ("minor", "double_major"):
        fallback_name = _PROGRAM_LABELS[program]
        back_url = f"/{program}"
        secondary_data = year_data.get(program)
        if not secondary_data:
            return templates.TemplateResponse(
                request,
                "result.html",
                {
                    "year": year,
                    "required_total": 0,
                    "results": [],
                    "is_mock": False,
                    "rules_not_configured": True,
                    "program": program,
                    "fallback_name": fallback_name,
                    "back_url": back_url,
                },
            )

        uploaded = [f for f in files if f.filename][:MAX_FILES]
        results = []
        is_mock = not uploaded
        if is_mock:
            result = mock_transcript({"required_courses": secondary_data.get("required_courses", [])})
            results.append(
                _build_minor_only_entry("（預覽假資料，尚未上傳成績單）", result, secondary_data, fallback_name)
            )
        else:
            for f in uploaded:
                pdf_bytes = await f.read()
                if len(pdf_bytes) > MAX_FILE_SIZE:
                    results.append(
                        _build_error_entry(f.filename, f"檔案大小超過{MAX_FILE_SIZE // (1024 * 1024)}MB，請確認是不是正確的成績單PDF")
                    )
                    continue
                try:
                    result = parse_transcript(pdf_bytes)
                except Exception:
                    results.append(_build_error_entry(f.filename, "這份檔案無法解析，可能不是PDF格式、或檔案已經損毀"))
                    continue
                results.append(_build_minor_only_entry(f.filename, result, secondary_data, fallback_name))

        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "year": year,
                "required_total": 0,
                "results": results,
                "is_mock": is_mock,
                "program": program,
                "fallback_name": fallback_name,
                "back_url": back_url,
            },
        )

    required_total = year_data.get("total_credits", 0)
    required_credits = year_data.get("required_credits", 0)
    elective_credits = year_data.get("elective_credits", 0)
    required_courses = year_data.get("required_courses", [])
    group_requirements = year_data.get("group_requirements", {})
    note_rules = year_data.get("note_rules", [])

    # 這個學年度還沒有在 /admin 建過畢業門檻規則（找不到、或必修清單是空的、或總學分是0）：
    # 沒有規則可以比對，不能假裝檢核過然後判定通過——空規則不是「沒有門檻」，是「還沒設定」，
    # 直接擋下來、明確告訴使用者，不要讓 credit_ok（0學分門檻永遠True）跟空的必修清單
    # （永遠沒有缺項）湊出一個看起來正常、其實完全沒檢查過的「✅ 已符合畢業資格」。
    if not required_courses or required_total <= 0:
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "year": year,
                "required_total": required_total,
                "results": [],
                "is_mock": False,
                "rules_not_configured": True,
            },
        )

    uploaded = [f for f in files if f.filename][:MAX_FILES]

    results = []
    is_mock = not uploaded
    if is_mock:
        result = mock_transcript(year_data)
        results.append(
            _build_result_entry(
                "（預覽假資料，尚未上傳成績單）",
                result,
                required_total,
                required_credits,
                elective_credits,
                required_courses,
                group_requirements,
                note_rules,
            )
        )
    else:
        for f in uploaded:
            pdf_bytes = await f.read()
            if len(pdf_bytes) > MAX_FILE_SIZE:
                results.append(
                    _build_error_entry(f.filename, f"檔案大小超過{MAX_FILE_SIZE // (1024 * 1024)}MB，請確認是不是正確的成績單PDF")
                )
                continue
            try:
                result = parse_transcript(pdf_bytes)
            except Exception:
                # pdfplumber 打不開非PDF檔案、損毀檔案時會丟例外，這裡接住讓單一檔案錯誤
                # 只影響那一份的顯示結果，不要讓整個request壞掉、其他份檔案也一起看不到結果
                results.append(
                    _build_error_entry(f.filename, "這份檔案無法解析，可能不是PDF格式、或檔案已經損毀")
                )
                continue
            results.append(
                _build_result_entry(
                    f.filename,
                    result,
                    required_total,
                    required_credits,
                    elective_credits,
                    required_courses,
                    group_requirements,
                    note_rules,
                )
            )

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
        # 課號前綴：給「國文/外文/通識/體育/服務學習」這種沒有登記固定課號（因為選課方式多元）
        # 的必修項目用，只用來判斷選修學分池要排除哪些課，不影響必修有沒有通過的判定。
        "code_prefixes": ", ".join(c.get("code_prefixes") or []),
    }


_NOTE_RULE_KIND_LABELS = {
    "credit_condition": "學分條件",
}

# credit_condition 底下「範圍」「方向」兩個獨立子選項的下拉選單文字
_CREDIT_CONDITION_SCOPE_LABELS = {"elective": "選修學分", "all": "不限選修"}
_CREDIT_CONDITION_DIRECTION_LABELS = {"include": "須屬於", "exclude": "不可屬於"}

# 應修科目表原始PDF的備註段落順序，給「類別」欄位自動完成建議用，管理者也可以自己輸入別的分類
_NOTE_RULE_CATEGORY_SUGGESTIONS = ["一、共同必修", "二、院、系訂必修", "三、雙主修規定"]


def _parse_code_list(s: str) -> list:
    """把表單裡逗號分隔的課號/課名字串拆成list，順便去重（保留第一次出現的順序）。"""
    seen = set()
    result = []
    for c in (s or "").split(","):
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _normalize_note_rule(index: int, r: dict) -> dict:
    """把 rules.yaml 存的 note_rule（list欄位是真的list）轉成表單好用的格式（list join成逗號字串）。"""
    kind = r.get("kind") or "info"
    scope = r.get("scope") or "elective"
    direction = r.get("direction") or "include"
    return {
        "index": index,
        "kind": kind,
        "kind_label": _NOTE_RULE_KIND_LABELS.get(kind, kind),
        "category": r.get("category", ""),
        "text": r.get("text", ""),
        "scope": scope,
        "scope_label": _CREDIT_CONDITION_SCOPE_LABELS.get(scope, scope),
        "direction": direction,
        "direction_label": _CREDIT_CONDITION_DIRECTION_LABELS.get(direction, direction),
        "min_credits": r.get("min_credits", 0),
        "code_prefixes": ", ".join(r.get("code_prefixes") or []),
        "extra_codes": ", ".join(r.get("extra_codes") or []),
    }


def _course_catalog(required_courses: list) -> list:
    """把應修科目表課號（含「/」合併的）攤平成 {code, name} 清單，去重、依課號排序。

    這是系統目前唯一有的課程資料來源，給備註規則表單「搜尋課號/課名加入」那個輔助輸入用，
    不是真的去查什麼外部課程資料庫。
    """
    seen = set()
    catalog = []
    for course in required_courses:
        name = course.get("name", "")
        for code in _split_codes(course.get("code")):
            if code not in seen:
                seen.add(code)
                catalog.append({"code": code, "name": name})
    catalog.sort(key=lambda c: c["code"])
    return catalog


def _build_note_rule(
    kind: str,
    category: str,
    text: str,
    scope: str = "elective",
    direction: str = "include",
    min_credits: float = 0,
    code_prefixes: str = "",
    extra_codes: str = "",
) -> dict:
    rule = {"kind": kind, "category": category.strip(), "text": text}
    if kind == "credit_condition":
        rule["scope"] = scope if scope in _CREDIT_CONDITION_SCOPE_LABELS else "elective"
        rule["direction"] = direction if direction in _CREDIT_CONDITION_DIRECTION_LABELS else "include"
        rule["min_credits"] = min_credits or 0
        rule["code_prefixes"] = _parse_code_list(code_prefixes)
        rule["extra_codes"] = _parse_code_list(extra_codes)
    return rule


def _resolve_year(rules: dict, year: Optional[str]) -> tuple:
    """三個 /admin 分頁（科目管理/分組與學年度設定/備註規則設定）共用的「目前選的是哪個學年度」邏輯：
    網址帶的 year 存在就用它，不然預設選最新的學年度（沒有任何學年度就是 None）。
    """
    years = sorted(rules.keys(), reverse=True)
    selected_year = year if year in rules else (years[0] if years else None)
    year_data = rules.get(selected_year, {}) if selected_year else {}
    return years, selected_year, year_data


def _group_options_and_requirements(required_courses: list, year_data: dict) -> tuple:
    """分組設定（分組管理頁）跟課程表單的分組自動完成建議（科目管理頁）都要用到同一份分組清單，
    抽出來共用，不用兩個路由各自重算一次。
    """
    # 分組名稱來自兩個地方：課程已經在用的分組、還有已經設定過「選幾門」但還沒有任何科目掛上去的分組
    # （例如剛用「新增分組」建立、還沒開始加科目的新分組），兩邊聯集才不會漏掉還沒綁課的空分組
    stored_group_requirements = year_data.get("group_requirements", {})
    group_options = sorted({c["group"] for c in required_courses if c["group"]} | set(stored_group_requirements))
    # 每個分組要選幾門：rules.yaml 裡沒特別設定的分組，預設是選1門
    group_requirements = [
        {"group": g, "required_count": stored_group_requirements.get(g, 1)} for g in group_options
    ]
    return group_options, group_requirements


def _build_course_sections(required_courses: list, group_requirements: dict) -> list:
    """科目管理頁（主系、輔系共用）畫面分區的依據：優先用「分組」（N選M功能性分組），沒有分組才退而
    用「層級」（純顯示分類），兩者都沒有的科目不分區、直接顯示。

    用一般字典依key收集（不用itertools.groupby），因為groupby只會合併「清單中緊鄰」的相同key
    項目——新增科目是直接append到清單最後面，如果用groupby，同分組的科目只要不是緊接在一起，
    就會被拆成兩個同名區塊，畫面上看起來像新科目沒被放進分組裡。
    """
    sections_by_key: dict = {}
    for c in required_courses:
        key = c["group"] or c["tier"]
        sections_by_key.setdefault(key, []).append(c)

    course_sections = []
    for key, items_list in sections_by_key.items():
        is_group = bool(items_list[0]["group"])
        course_sections.append(
            {
                "key": key,
                "is_group": is_group,
                "required_count": group_requirements.get(key, 1) if is_group else None,
                "courses": items_list,
            }
        )
    return course_sections


def _admin_context(
    year: Optional[str],
    tab: str = "courses",
    edit: Optional[int] = None,
    edit_note: Optional[int] = None,
    edit_minor: Optional[int] = None,
    edit_minor_note: Optional[int] = None,
    edit_double_major: Optional[int] = None,
    edit_double_major_note: Optional[int] = None,
    edit_grad_track: Optional[str] = None,
    edit_grad_course: Optional[int] = None,
    grad_year: Optional[str] = None,
) -> dict:
    """/admin、/admin/programs 兩個頁面共用的畫面資料，抽出來一起算省得各自重算一次年度/課程
    解析邏輯。"""
    rules = load_rules()
    years, selected_year, year_data = _resolve_year(rules, year)

    # 必修排在選修前面；同一類別內維持 rules.yaml 原本的順序（stable sort不會打亂同類別內的相對順序）。
    # 這樣同一層級/分組的科目只要在資料裡本來就排在一起，畫面就會照著文件原本的順序（共同必修→院訂
    # 必修→系訂必修→...）分區顯示，不會因為改用字母排序而把順序弄亂（「院訂必修」「系訂必修」這幾個
    # 詞的字母順序剛好跟文件邏輯順序不一樣）。
    required_courses = [_normalize_course(i, c) for i, c in enumerate(year_data.get("required_courses", []))]
    required_courses.sort(key=lambda c: c["category"] != "必修")

    # 表格欄位數固定是5（課程名稱/課號/學分/備註/操作），分組跟區塊標題列用這個算colspan
    table_colspan = 5

    group_options, group_requirements = _group_options_and_requirements(required_courses, year_data)
    # 層級純粹是顯示分類用（共同必修/院訂必修/系訂必修...），不影響判定邏輯，給表單自動完成選項用
    tier_options = sorted({c["tier"] for c in required_courses if c["tier"]})
    course_sections = _build_course_sections(required_courses, year_data.get("group_requirements", {}))

    note_rules = [_normalize_note_rule(i, r) for i, r in enumerate(year_data.get("note_rules", []))]
    # 分類自動完成建議：PDF原本的一/二/三段落，加上這個學年度已經用過的分類（可能是自訂的）
    note_category_options = sorted(
        set(_NOTE_RULE_CATEGORY_SUGGESTIONS) | {r["category"] for r in note_rules if r["category"]},
        key=lambda c: (
            _NOTE_RULE_CATEGORY_SUGGESTIONS.index(c) if c in _NOTE_RULE_CATEGORY_SUGGESTIONS else 99,
            c,
        ),
    )
    course_catalog = _course_catalog(year_data.get("required_courses", []))

    # 輔系是跟主系完全獨立的一套小型「應修科目表」（見_build_minor_result的說明），這裡照主系
    # 那一套算法（正規化課程、分組選項、備註規則正規化）算一份輔系專用的版本，給/admin的
    # 「輔系」分頁籤用；輔系目前不需要層級（tier）分區，科目數量少、直接列表就好。
    minor_data = year_data.get("minor") or {}
    minor_required_courses = [
        _normalize_course(i, c) for i, c in enumerate(minor_data.get("required_courses", []))
    ]
    minor_group_options, minor_group_requirements = _group_options_and_requirements(
        minor_required_courses, minor_data
    )
    minor_course_sections = _build_course_sections(minor_required_courses, minor_data.get("group_requirements", {}))
    minor_note_rules = [_normalize_note_rule(i, r) for i, r in enumerate(minor_data.get("note_rules", []))]

    # 雙主修跟輔系是同一種「次要身份應修科目表」，算法完全一樣，只是各自存在rules.yaml年度資料
    # 底下不同的key（見_SECONDARY_TARGETS），給/admin的「雙主修」分頁籤用。
    double_major_data = year_data.get("double_major") or {}
    double_major_required_courses = [
        _normalize_course(i, c) for i, c in enumerate(double_major_data.get("required_courses", []))
    ]
    double_major_group_options, double_major_group_requirements = _group_options_and_requirements(
        double_major_required_courses, double_major_data
    )
    double_major_course_sections = _build_course_sections(
        double_major_required_courses, double_major_data.get("group_requirements", {})
    )
    double_major_note_rules = [
        _normalize_note_rule(i, r) for i, r in enumerate(double_major_data.get("note_rules", []))
    ]

    return {
        "active_tab": tab,
        "years": years,
        "year": selected_year,
        "total_credits": year_data.get("total_credits", 0),
        "required_credits": year_data.get("required_credits", 0),
        "elective_credits": year_data.get("elective_credits", 0),
        "required_courses": required_courses,
        "course_sections": course_sections,
        "table_colspan": table_colspan,
        "group_options": group_options,
        "group_requirements": group_requirements,
        "tier_options": tier_options,
        "edit_index": edit,
        "note_rules": note_rules,
        "credit_condition_scopes": _CREDIT_CONDITION_SCOPE_LABELS,
        "credit_condition_directions": _CREDIT_CONDITION_DIRECTION_LABELS,
        "course_catalog": course_catalog,
        "note_category_options": note_category_options,
        "edit_note_index": edit_note,
        "minor_name": minor_data.get("name") or "輔系",
        "minor_required_courses": minor_required_courses,
        "minor_course_sections": minor_course_sections,
        "minor_group_options": minor_group_options,
        "minor_group_requirements": minor_group_requirements,
        "minor_note_rules": minor_note_rules,
        "edit_minor_index": edit_minor,
        "edit_minor_note_index": edit_minor_note,
        "double_major_name": double_major_data.get("name") or "雙主修",
        "double_major_required_courses": double_major_required_courses,
        "double_major_course_sections": double_major_course_sections,
        "double_major_group_options": double_major_group_options,
        "double_major_group_requirements": double_major_group_requirements,
        "double_major_note_rules": double_major_note_rules,
        "edit_double_major_index": edit_double_major,
        "edit_double_major_note_index": edit_double_major_note,
        **_graduate_admin_context(grad_year, edit_grad_track, edit_grad_course),
    }


def _graduate_admin_context(
    grad_year: Optional[str] = None,
    edit_grad_track: Optional[str] = None,
    edit_grad_course: Optional[int] = None,
) -> dict:
    """/admin/programs 底下「碩士班／博士班／工學博士班」分頁籤要用的資料——碩博規則
    存在獨立的graduate_rules.yaml，頂層一樣是入學學年度（見load_graduate_rules的說明），跟
    大學部/輔系/雙主修用的rules.yaml是兩份不相干的檔案、兩份不相干的學年度清單，所以獨立一個
    函式算，不跟著_admin_context其餘部分共用同一個year/rules。

    每個學制的六門課程池／化工材料領域對照表、以及需人工確認項目，都是各自獨立的一份資料
    （不是四個學制共用同一份）——碩士班沒有資格考、SCI論文點數、英文能力這些博士班才有的畢業
    條件，工學博士班的口試委員產業專家規定也不適用其他學制，所以拆開維護；改一個學制不會意外
    影響到其他學制。edit_grad_track/edit_grad_course是目前正在編輯哪個學制的第幾筆課程（index只在
    該學制自己的清單裡有意義，不是全域唯一）。
    """
    grad_rules = load_graduate_rules()
    grad_years, selected_grad_year, year_data = _resolve_year(grad_rules, grad_year)
    tracks = {}
    for key, track in year_data.get("tracks", {}).items():
        courses = [
            {"index": i, "code": c.get("code", ""), "name": c.get("name", ""),
             "domains": c.get("domains", []), "in_pool": bool(c.get("in_pool"))}
            for i, c in enumerate(track.get("courses", []))
        ]
        manual_review_items = list(enumerate(track.get("manual_review_items", [])))
        tracks[key] = {**track, "courses": courses, "manual_review_items": manual_review_items}
    return {
        "grad_years": grad_years,
        "grad_year": selected_grad_year,
        "grad_tracks": tracks,
        "edit_grad_track": edit_grad_track,
        "edit_grad_course_index": edit_grad_course,
    }


@app.post("/graduate/check", response_class=HTMLResponse)
async def graduate_check(
    request: Request,
    year: str = Form(...),
    track: str = Form(...),
    files: List[UploadFile] = File([]),
):
    """碩博資格檢核，跟/check分開路由：碩博規則存在獨立的graduate_rules.yaml（見/graduate的
    說明），表單多送一個track（學制），硬塞進共用的/check會讓那個函式的program分支邏輯更難懂，
    不如獨立一個。
    """
    grad_rules = load_graduate_rules()
    years = sorted(grad_rules.keys(), reverse=True)
    if year not in grad_rules:
        year = years[0] if years else year
    year_data = grad_rules.get(year, {})
    tracks = year_data.get("tracks", {})
    if track not in tracks:
        track = next(iter(tracks), "")

    uploaded = [f for f in files if f.filename][:MAX_FILES]
    results = []
    is_mock = not uploaded
    if is_mock:
        results.append(_build_graduate_entry("（預覽假資料，尚未上傳成績單）", {"rows": [], "has_text": True}, track, year_data))
    else:
        for f in uploaded:
            pdf_bytes = await f.read()
            if len(pdf_bytes) > MAX_FILE_SIZE:
                results.append(
                    _build_error_entry(f.filename, f"檔案大小超過{MAX_FILE_SIZE // (1024 * 1024)}MB，請確認是不是正確的成績單PDF")
                )
                continue
            try:
                parsed = parse_grad_transcript(pdf_bytes)
            except Exception:
                results.append(_build_error_entry(f.filename, "這份檔案無法解析，可能不是PDF格式、或檔案已經損毀"))
                continue
            results.append(_build_graduate_entry(f.filename, parsed, track, year_data))

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "year": year,
            "required_total": 0,
            "results": results,
            "is_mock": is_mock,
            "program": "graduate",
            "fallback_name": tracks.get(track, {}).get("label", "碩／博士班"),
            "back_url": "/graduate",
        },
    )


def _grad_domains_from_form(domain_chemical: bool, domain_material: bool) -> list:
    domains = []
    if domain_chemical:
        domains.append("化工")
    if domain_material:
        domains.append("材料")
    return domains


@app.post("/admin/graduate/course/add")
async def admin_graduate_course_add(
    year: str = Form(...),
    track: str = Form(...),
    code: str = Form(...),
    name: str = Form(...),
    domain_chemical: bool = Form(False),
    domain_material: bool = Form(False),
    in_pool: bool = Form(False),
):
    grad_rules = load_graduate_rules()
    track_data = grad_rules.get(year, {}).get("tracks", {}).get(track)
    if track_data is not None:
        track_data.setdefault("courses", []).append(
            {
                "code": code.strip(),
                "name": name.strip(),
                "domains": _grad_domains_from_form(domain_chemical, domain_material),
                "in_pool": in_pool,
            }
        )
        save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={track}&grad_year={year}", status_code=303)


@app.post("/admin/graduate/course/update")
async def admin_graduate_course_update(
    year: str = Form(...),
    track: str = Form(...),
    index: int = Form(...),
    code: str = Form(...),
    name: str = Form(...),
    domain_chemical: bool = Form(False),
    domain_material: bool = Form(False),
    in_pool: bool = Form(False),
):
    grad_rules = load_graduate_rules()
    courses = grad_rules.get(year, {}).get("tracks", {}).get(track, {}).get("courses", [])
    if 0 <= index < len(courses):
        courses[index] = {
            "code": code.strip(),
            "name": name.strip(),
            "domains": _grad_domains_from_form(domain_chemical, domain_material),
            "in_pool": in_pool,
        }
        save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={track}&grad_year={year}", status_code=303)


@app.post("/admin/graduate/course/delete")
async def admin_graduate_course_delete(year: str = Form(...), track: str = Form(...), index: int = Form(...)):
    grad_rules = load_graduate_rules()
    courses = grad_rules.get(year, {}).get("tracks", {}).get(track, {}).get("courses", [])
    if 0 <= index < len(courses):
        courses.pop(index)
        save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={track}&grad_year={year}", status_code=303)


@app.post("/admin/graduate/track/update")
async def admin_graduate_track_update(
    year: str = Form(...),
    track: str = Form(...),
    min_total_credits: float = Form(...),
    pool_min_pass: int = Form(...),
    require_domain_each: bool = Form(False),
    seminar_min_semesters: int = Form(...),
    topics_min_semesters: int = Form(...),
    industry_topics_min_semesters: int = Form(0),
):
    grad_rules = load_graduate_rules()
    tracks = grad_rules.setdefault(year, {}).setdefault("tracks", {})
    if track in tracks:
        tracks[track].update(
            {
                "min_total_credits": min_total_credits,
                "pool_min_pass": pool_min_pass,
                "require_domain_each": require_domain_each,
                "seminar_min_semesters": seminar_min_semesters,
                "topics_min_semesters": topics_min_semesters,
                "industry_topics_min_semesters": industry_topics_min_semesters,
            }
        )
    save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={track}&grad_year={year}", status_code=303)


@app.post("/admin/graduate/manual_review/add")
async def admin_graduate_manual_review_add(year: str = Form(...), track: str = Form(...), text: str = Form(...)):
    """需人工確認項目（資格考排名、論文口試...）跟課程池一樣，四個學制各自獨立一份清單——碩士班
    沒有資格考、SCI論文點數、英文能力這些博士班才有的條件，共用一份會讓碩士班的結果頁出現不適用
    的提醒文字。
    """
    text = text.strip()
    if text:
        grad_rules = load_graduate_rules()
        track_data = grad_rules.get(year, {}).get("tracks", {}).get(track)
        if track_data is not None:
            track_data.setdefault("manual_review_items", []).append(text)
            save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={track}&grad_year={year}", status_code=303)


@app.post("/admin/graduate/manual_review/delete")
async def admin_graduate_manual_review_delete(year: str = Form(...), track: str = Form(...), index: int = Form(...)):
    grad_rules = load_graduate_rules()
    items = grad_rules.get(year, {}).get("tracks", {}).get(track, {}).get("manual_review_items", [])
    if 0 <= index < len(items):
        items.pop(index)
    save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={track}&grad_year={year}", status_code=303)


@app.get("/admin/graduate/manual_review_exceptions", response_class=HTMLResponse)
async def admin_graduate_manual_review_exceptions_page(request: Request):
    """「預口試通過」「英文能力」這兩類手動確認項目的名單制管理頁——每一類各自一份「還沒
    通過」名單，看目前名單有哪些學號、單筆新增/刪除、清空全部。這份名單是所有學制、所有
    學年度共用（這兩項都是學生個人的里程碑，跟報考哪個學制、哪年入學無關），不像課程池/
    manual_review_items需要分學制維護；跟規則同步拆成/admin/sync一樣，獨立一頁，不用擠在
    碩博設定那個分頁籤裡。
    """
    exceptions = load_manual_review_exceptions()
    rosters = [
        {"key": key, "label": label, "ids": sorted(exceptions[key])}
        for key, label in MANUAL_REVIEW_ROSTER_PREFIXES.items()
    ]
    return templates.TemplateResponse(
        request, "admin_manual_review_exceptions.html", {"rosters": rosters}
    )


@app.post("/admin/graduate/manual_review_exceptions/add")
async def admin_graduate_manual_review_exceptions_add(
    roster_key: str = Form(...), student_id: str = Form(...)
):
    student_id = student_id.strip()
    if student_id and roster_key in MANUAL_REVIEW_ROSTER_PREFIXES:
        exceptions = load_manual_review_exceptions()
        exceptions[roster_key].add(student_id)
        save_manual_review_exceptions(exceptions)
    return RedirectResponse("/admin/graduate/manual_review_exceptions", status_code=303)


@app.post("/admin/graduate/manual_review_exceptions/delete")
async def admin_graduate_manual_review_exceptions_delete(
    roster_key: str = Form(...), student_id: str = Form(...)
):
    if roster_key in MANUAL_REVIEW_ROSTER_PREFIXES:
        exceptions = load_manual_review_exceptions()
        exceptions[roster_key].discard(student_id)
        save_manual_review_exceptions(exceptions)
    return RedirectResponse("/admin/graduate/manual_review_exceptions", status_code=303)


@app.post("/admin/graduate/manual_review_exceptions/clear")
async def admin_graduate_manual_review_exceptions_clear(roster_key: str = Form(...)):
    if roster_key in MANUAL_REVIEW_ROSTER_PREFIXES:
        exceptions = load_manual_review_exceptions()
        exceptions[roster_key] = set()
        save_manual_review_exceptions(exceptions)
    return RedirectResponse("/admin/graduate/manual_review_exceptions", status_code=303)



@app.post("/admin/graduate/year/add")
async def admin_graduate_year_add(
    year: str = Form(...), copy_from: str = Form(""), redirect_tab: str = Form("master")
):
    """新增一個碩博規則的入學學年度，邏輯跟/admin/year/add（大學部）一樣：可以從既有學年度
    深拷貝一份（總學分、課程池、需人工確認項目全部一起複製），或留空白從頭設定。
    """
    year = year.strip()
    copy_from = copy_from.strip()
    grad_rules = load_graduate_rules()

    if year not in grad_rules:
        if copy_from and copy_from in grad_rules:
            grad_rules[year] = copy.deepcopy(grad_rules[copy_from])
        else:
            grad_rules[year] = {"tracks": {}}
        save_graduate_rules(grad_rules)

    return RedirectResponse(f"/admin/programs?tab={redirect_tab}&grad_year={year}", status_code=303)


@app.post("/admin/graduate/year/rename")
async def admin_graduate_year_rename(
    old_year: str = Form(...), new_year: str = Form(...), redirect_tab: str = Form("master")
):
    old_year = old_year.strip()
    new_year = new_year.strip()
    grad_rules = load_graduate_rules()

    if new_year and new_year != old_year and new_year not in grad_rules and old_year in grad_rules:
        grad_rules[new_year] = grad_rules.pop(old_year)
        save_graduate_rules(grad_rules)
        return RedirectResponse(f"/admin/programs?tab={redirect_tab}&grad_year={new_year}", status_code=303)

    return RedirectResponse(f"/admin/programs?tab={redirect_tab}&grad_year={old_year}", status_code=303)


@app.post("/admin/graduate/year/delete")
async def admin_graduate_year_delete(year: str = Form(...), redirect_tab: str = Form("master")):
    grad_rules = load_graduate_rules()
    grad_rules.pop(year, None)
    save_graduate_rules(grad_rules)
    return RedirectResponse(f"/admin/programs?tab={redirect_tab}", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
async def admin(
    request: Request,
    year: Optional[str] = None,
    tab: str = "courses",
    edit: Optional[int] = None,
    edit_note: Optional[int] = None,
):
    """大學部的應修科目表管理頁，分「科目管理／分組與學年度設定／備註規則設定」三個分頁籤，
    用前端JS切換顯示、不用重新整頁——`tab` 這個查詢參數只是給「切哪個分頁後刷新頁面」（例如
    表單送出後跳轉回來）時，能一開始就顯示對的分頁籤，避免每次存檔後又跳回第一個分頁籤。
    輔系／雙主修／研究所課程分類已經拆到獨立的/admin/programs頁面（見_admin_nav.html的分頁
    切換列），這裡的_admin_context()雖然還是會順便算出那幾個身份的資料，但這個頁面的樣板不會
    用到，兩個路由共用同一個context函式單純是省得複製一份年度/課程解析邏輯。
    """
    return templates.TemplateResponse(
        request,
        "admin.html",
        _admin_context(year, tab, edit, edit_note),
    )


@app.get("/admin/sync", response_class=HTMLResponse)
async def admin_sync(request: Request, import_error: bool = False):
    """規則同步（匯出/匯入 rules.yaml、graduate_rules.yaml 合併成的一份檔案）是跟特定學年度
    無關的整機操作，獨立成一頁，不用跟著科目管理／分組設定／備註規則設定擠在同一個
    「分組與學年度設定」分頁籤裡。"""
    return templates.TemplateResponse(
        request,
        "admin_sync.html",
        {"import_error": import_error},
    )


@app.get("/admin/programs", response_class=HTMLResponse)
async def admin_programs(
    request: Request,
    year: Optional[str] = None,
    tab: str = "minor",
    edit_minor: Optional[int] = None,
    edit_minor_note: Optional[int] = None,
    edit_double_major: Optional[int] = None,
    edit_double_major_note: Optional[int] = None,
    edit_grad_track: Optional[str] = None,
    edit_grad_course: Optional[int] = None,
    grad_year: Optional[str] = None,
):
    """輔系／雙主修／碩士班／博士班／工學博士班的後台管理頁，跟/admin（大學部）分開
    頁面——這幾個身份各自的表單/資料形狀跟大學部不一樣，擠在同一個頁面分頁籤太多，拆開後
    兩邊分頁籤數量都比較好抓。碩博的規則存在獨立的graduate_rules.yaml，學年度跟輔系/雙主修
    （存在rules.yaml）是兩份不相干的清單，所以grad_year跟year是分開的查詢參數。
    """
    return templates.TemplateResponse(
        request,
        "admin_programs.html",
        _admin_context(
            year, tab, None, None, edit_minor, edit_minor_note,
            edit_double_major, edit_double_major_note, edit_grad_track, edit_grad_course, grad_year,
        ),
    )


@app.post("/admin/year/add")
async def admin_year_add(year: str = Form(...), copy_from: str = Form(""), redirect_tab: str = Form("")):
    """輔系／雙主修的規則是巢狀掛在跟大學部同一個學年度底下（同一份rules.yaml），沒有自己獨立
    的學年度清單，所以輔系／雙主修頁面要新增學年度時也是呼叫這條路由——redirect_tab記得是從
    哪個分頁籤按的「新增」，建立完成後要跳回那裡，不是固定跳回大學部頁面。
    """
    year = year.strip()
    copy_from = copy_from.strip()
    rules = load_rules()

    # 不能覆蓋已經存在的學年度規則（不管是空白建立還是從別的學年度複製），避免手滑蓋掉既有資料
    if year not in rules:
        if copy_from and copy_from in rules:
            # 深拷貝來源學年度的完整規則（總學分、必修科目、分組設定），新學年度要能獨立編輯、
            # 改動不能互相影響到來源學年度，所以不能直接共用同一份 list/dict 物件
            rules[year] = copy.deepcopy(rules[copy_from])
        else:
            rules[year] = {
                "total_credits": 0,
                "required_courses": [],
                "group_requirements": {},
                "note_rules": [],
            }
        save_rules(rules)

    if redirect_tab in _SECONDARY_TARGETS:
        return RedirectResponse(f"/admin/programs?tab={redirect_tab}&year={year}", status_code=303)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/year/rename")
async def admin_year_rename(old_year: str = Form(...), new_year: str = Form(...)):
    old_year = old_year.strip()
    new_year = new_year.strip()
    rules = load_rules()

    # 新名稱不能是空的、不能跟舊的一樣、也不能撞到已經存在的另一個學年度（避免資料被覆蓋掉）
    if new_year and new_year != old_year and new_year not in rules and old_year in rules:
        rules[new_year] = rules.pop(old_year)
        save_rules(rules)
        return RedirectResponse(f"/admin?year={new_year}&tab=settings", status_code=303)

    return RedirectResponse(f"/admin?year={old_year}&tab=settings", status_code=303)


@app.post("/admin/year/delete")
async def admin_year_delete(year: str = Form(...)):
    rules = load_rules()
    rules.pop(year, None)
    save_rules(rules)
    return RedirectResponse("/admin", status_code=303)


# 主系以外，跟主系共用同一套「應修科目表」管理表單（科目/分組/備註規則）的身份別，
# key是rules.yaml裡年度資料底下的區塊名稱，value是這個區塊第一次被編輯、還沒建立過時的預設名稱。
# 之後要加新身份（例如某個雙學位），這裡加一行就好，不用再複製一份表單或路由。
_SECONDARY_TARGETS = {"minor": "輔系", "double_major": "雙主修"}


def _admin_redirect_base(tab: str) -> str:
    """大學部的分頁籤（科目管理/分組與學年度設定/備註規則設定）在/admin頁面，輔系/雙主修這些
    「其他身份」分頁籤已經拆到獨立的/admin/programs頁面——表單存檔後要跳轉回原本編輯的分頁籤，
    但兩邊分頁籤在不同網址，要先判斷tab屬於哪一頁才知道該轉址回哪裡。
    """
    return "/admin/programs" if tab in _SECONDARY_TARGETS else "/admin"


def _resolve_target_data(rules: dict, year: str, target: str) -> dict:
    """後台的科目/分組/層級/備註規則管理，主系跟輔系、雙主修這些「次要身份」共用同一套表單跟
    路由，只差在改的是 rules[year] 本身還是 rules[year][target]——target 就是用來分辨要改哪一份。
    次要身份的規則第一次被編輯時如果還沒建立過，就順便建一個空骨架出來，不用另外一個「新增」步驟。
    """
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    if target in _SECONDARY_TARGETS:
        return year_data.setdefault(
            target,
            {"name": _SECONDARY_TARGETS[target], "required_courses": [], "group_requirements": {}, "note_rules": []},
        )
    return year_data


@app.post("/admin/group/set_requirement")
async def admin_group_set_requirement(
    year: str = Form(...), group: str = Form(...), required_count: int = Form(...), target: str = Form("main")
):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    target_data.setdefault("group_requirements", {})[group] = required_count
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "settings"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


@app.post("/admin/group/rename")
async def admin_group_rename(
    year: str = Form(...), old_group: str = Form(...), new_group: str = Form(""), target: str = Form("main")
):
    old_group = old_group.strip()
    new_group = new_group.strip()
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)

    if new_group and new_group != old_group:
        # 把用到舊名稱的科目全部改成新名稱，這個分組底下的科目才不會因為改名字就散掉
        for c in target_data.get("required_courses", []):
            if c.get("group") == old_group:
                c["group"] = new_group
        group_requirements = target_data.setdefault("group_requirements", {})
        if old_group in group_requirements:
            old_count = group_requirements.pop(old_group)
            # 如果改名後的名稱本來就是另一個既有分組，保留那個分組原本設定的「選幾門」，不要被覆蓋掉
            group_requirements.setdefault(new_group, old_count)
        save_rules(rules)

    tab = target if target in _SECONDARY_TARGETS else "settings"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


@app.post("/admin/group/delete")
async def admin_group_delete(year: str = Form(...), group: str = Form(...), target: str = Form("main")):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)

    # 刪除分組不會連科目一起刪掉，科目會變回沒有分組的一般必修/選修科目，只是不再綁在一起判定
    for c in target_data.get("required_courses", []):
        if c.get("group") == group:
            c["group"] = ""
    target_data.get("group_requirements", {}).pop(group, None)
    save_rules(rules)

    tab = target if target in _SECONDARY_TARGETS else "settings"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


@app.post("/admin/tier/rename")
async def admin_tier_rename(year: str = Form(...), old_tier: str = Form(...), new_tier: str = Form("")):
    old_tier = old_tier.strip()
    new_tier = new_tier.strip()
    rules = load_rules()
    year_data = rules.get(year, {})

    if new_tier and new_tier != old_tier:
        # 層級沒有像分組那樣另外存設定，單純把用到舊層級名稱的科目全部改成新名稱
        for c in year_data.get("required_courses", []):
            if c.get("tier") == old_tier:
                c["tier"] = new_tier
        save_rules(rules)

    return RedirectResponse(f"/admin?year={year}&tab=settings", status_code=303)


@app.post("/admin/tier/delete")
async def admin_tier_delete(year: str = Form(...), tier: str = Form(...)):
    rules = load_rules()
    year_data = rules.get(year, {})

    # 刪除層級不會連科目一起刪掉，科目會變回沒有層級標記的一般必修/選修科目，只是不再分區顯示
    for c in year_data.get("required_courses", []):
        if c.get("tier") == tier:
            c["tier"] = ""
    save_rules(rules)

    return RedirectResponse(f"/admin?year={year}&tab=settings", status_code=303)


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
    code_prefixes: str = Form(""),
    target: str = Form("main"),
):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    target_data.setdefault("required_courses", []).append(
        {
            "name": name, "code": code, "credits": credits, "category": category,
            "group": group, "tier": tier, "note": note, "code_prefixes": _parse_code_list(code_prefixes),
        }
    )
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "courses"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


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
    code_prefixes: str = Form(""),
    target: str = Form("main"),
):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    courses = target_data.get("required_courses", [])
    if 0 <= index < len(courses):
        courses[index] = {
            "name": name, "code": code, "credits": credits, "category": category,
            "group": group, "tier": tier, "note": note, "code_prefixes": _parse_code_list(code_prefixes),
        }
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "courses"
    row_prefix = f"{target}-row" if target in _SECONDARY_TARGETS else "row"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}#{row_prefix}-{index}", status_code=303)


@app.post("/admin/course/delete")
async def admin_course_delete(year: str = Form(...), index: int = Form(...), target: str = Form("main")):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    courses = target_data.get("required_courses", [])
    if 0 <= index < len(courses):
        courses.pop(index)
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "courses"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


@app.post("/admin/total_credits")
async def admin_total_credits(year: str = Form(...), total_credits: float = Form(...)):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data["total_credits"] = total_credits
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/required_credits")
async def admin_required_credits(year: str = Form(...), required_credits: float = Form(0)):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data["required_credits"] = required_credits
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/elective_credits")
async def admin_elective_credits(year: str = Form(...), elective_credits: float = Form(0)):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data["elective_credits"] = elective_credits
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}", status_code=303)


@app.post("/admin/note_rule/add")
async def admin_note_rule_add(
    year: str = Form(...),
    kind: str = Form("credit_condition"),
    category: str = Form(""),
    text: str = Form(...),
    scope: str = Form("elective"),
    direction: str = Form("include"),
    min_credits: float = Form(0),
    code_prefixes: str = Form(""),
    extra_codes: str = Form(""),
    target: str = Form("main"),
):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    target_data.setdefault("note_rules", []).append(
        _build_note_rule(kind, category, text, scope, direction, min_credits, code_prefixes, extra_codes)
    )
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "notes"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


@app.post("/admin/note_rule/update")
async def admin_note_rule_update(
    year: str = Form(...),
    index: int = Form(...),
    kind: str = Form("credit_condition"),
    category: str = Form(""),
    text: str = Form(...),
    scope: str = Form("elective"),
    direction: str = Form("include"),
    min_credits: float = Form(0),
    code_prefixes: str = Form(""),
    extra_codes: str = Form(""),
    target: str = Form("main"),
):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    note_rules = target_data.get("note_rules", [])
    if 0 <= index < len(note_rules):
        note_rules[index] = _build_note_rule(kind, category, text, scope, direction, min_credits, code_prefixes, extra_codes)
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "notes"
    row_prefix = f"{target}-note-row" if target in _SECONDARY_TARGETS else "note-row"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}#{row_prefix}-{index}", status_code=303)


@app.post("/admin/note_rule/delete")
async def admin_note_rule_delete(year: str = Form(...), index: int = Form(...), target: str = Form("main")):
    rules = load_rules()
    target_data = _resolve_target_data(rules, year, target)
    note_rules = target_data.get("note_rules", [])
    if 0 <= index < len(note_rules):
        note_rules.pop(index)
    save_rules(rules)
    tab = target if target in _SECONDARY_TARGETS else "notes"
    return RedirectResponse(f"{_admin_redirect_base(tab)}?year={year}&tab={tab}", status_code=303)


@app.get("/admin/rules/export")
async def admin_rules_export():
    """把 rules.yaml（大學部／輔系／雙主修）跟 graduate_rules.yaml（碩／博士班）兩份規則檔
    包成同一份下載檔，方便系上人員一次同步到其他各自獨立安裝的電腦（這個系統每台電腦是各自
    獨立一份規則資料，沒有連網同步）——原本是兩份檔案要分開匯出/匯入，合併成一份之後，系上
    人員只要記得傳一個檔案、按一次匯入就好，不用擔心漏傳其中一份。
    """
    combined = yaml.safe_dump(
        {"rules": load_rules(), "graduate_rules": load_graduate_rules()},
        allow_unicode=True,
        sort_keys=False,
    )
    return Response(
        content=combined,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=all_rules.yaml"},
    )


@app.post("/admin/rules/import")
async def admin_rules_import(request: Request, file: UploadFile = File(...)):
    """上傳一份「匯出規則」下載的檔案，整份覆蓋掉這台電腦目前的 rules.yaml 跟
    graduate_rules.yaml——用來對照 /admin/rules/export，讓系上人員可以不用自己去檔案總管找
    檔案覆蓋，直接在網頁上同步另一台電腦匯出的規則。

    整份覆蓋是刻意的設計（不是只合併有變動的學年度）：規則之間常常互相關聯（例如某個學年度的
    note_rules、group_requirements要跟該學年度的required_courses對得起來），部分合併容易讓
    資料兜不起來、產生看起來合理但實際上前後矛盾的規則。覆蓋前一定要先備份現有的兩份規則檔
    （成 .bak），才不會匯錯檔案就再也找不回原本這台電腦的資料。兩份規則檔要嘛一起換掉、要嘛
    都不換（檔案格式不對時兩份都不寫），不會變成「一份換了、一份沒換」的中間狀態。
    """
    content = await file.read()
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        parsed = None
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("rules"), dict)
        or not isinstance(parsed.get("graduate_rules"), dict)
    ):
        return templates.TemplateResponse(request, "admin_sync.html", {"import_error": True})

    RULES_BACKUP_FILE.write_bytes(RULES_FILE.read_bytes())
    GRADUATE_RULES_BACKUP_FILE.write_bytes(GRADUATE_RULES_FILE.read_bytes())
    save_rules({str(y): data for y, data in parsed["rules"].items()})
    save_graduate_rules({str(y): data for y, data in parsed["graduate_rules"].items()})
    return RedirectResponse("/admin/sync", status_code=303)
