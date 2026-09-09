import copy
import io
import re
from pathlib import Path
from typing import List, Optional

import pdfplumber
import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
RULES_FILE = BASE_DIR / "rules.yaml"
# 「匯入規則」覆蓋前的備份，只保留最近一次匯入前的版本（不是每次匯入都留一份新檔案），
# 匯錯檔案的話可以手動把這個複製回 rules.yaml 救回來。
RULES_BACKUP_FILE = BASE_DIR / "rules.yaml.bak"

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB，一般文字型成績單PDF遠小於這個數字，超過大概是傳錯檔案
MAX_FILES = 30  # 一次最多同時處理幾份，避免有人整個資料夾誤傳上來拖垮伺服器

app = FastAPI(title="化材系畢業學分檢核系統")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# 樣式改用本機打包好的 static/tailwind.css（不再用 CDN 版），同仁電腦沒有網路也能正常顯示畫面；
# 樣板裡新增的 Tailwind class 沒被這份編譯好的CSS涵蓋到的話，要重新用 tailwindcss CLI 打包一次
# （指令見 static/tailwind_input.css 旁邊，掃描 templates/ 底下用到的 class 重新編譯 tailwind.css）。
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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
                        "term": next((i for i, c in enumerate(cells) if c == "學年學期"), None),
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
    return {
        "courses": courses,
        "total_credit": total_credit,
        "unmet_categories": unmet_categories,
        "passed_codes": passed_codes,
        "passed_courses": passed_courses,
        "has_text": has_text,
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
    plain, groups = _bucket_required_courses(required_courses)

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


def _consumed_required_codes(
    required_courses: list, group_requirements: dict, passed_codes: set, passed_courses: list = None
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
    學分加總常常超過這個數字（例如外文修了大一英文6學分又修了日文3學分，通識超修2學分）。但跟
    M選N分組不一樣的是，這種超修「不能」流向選修學分——選修學分只能是本系專業課程或應修科目表
    列出的必修/必選修課程超修的部分，通識/外文/國文這種共同必修categories多修的課不算數（既不是
    必修、也不是選修，單純不列入這兩個子門檻，但還是算在總學分裡）。所以超過門檻的部分只從
    consumed排除、另外歸進 excluded 集合，_credit_breakdown 要把這個集合也從選修學分池扣掉。
    門檻是0（例如體育、服務學習課程本身沒有學分門檻）的項目維持全部算必修消耗掉，因為沒有
    「多少算超修」的基準可以拿來切。

    回傳 (consumed, excluded)：consumed 是必修學分池的課號，excluded 是「不算必修、但也不能算
    選修」的超修課號（目前只有課號前綴超修這一種情況）。
    """
    group_requirements = group_requirements or {}
    plain, groups = _bucket_required_courses(required_courses)
    consumed = {c for course in plain for c in course["codes"]}

    for group, group_courses in groups.items():
        required_count = group_requirements.get(group, 1)
        all_codes = [c for gc in group_courses for c in gc["codes"]]
        passed_in_group = [c for c in all_codes if c in passed_codes]
        consumed.update(passed_in_group[:required_count])

    excluded = set()
    if passed_courses:
        for course in required_courses:
            prefixes = course.get("code_prefixes") or []
            if not prefixes:
                continue
            threshold = course.get("credits") or 0
            matched = [
                c for c in passed_courses
                if c["code"] and c["code"] not in consumed and any(c["code"].startswith(p) for p in prefixes)
            ]
            if threshold <= 0:
                consumed.update(c["code"] for c in matched)
                continue
            accumulated = 0.0
            for c in matched:
                if accumulated < threshold:
                    consumed.add(c["code"])
                    accumulated += c["credit"]
                else:
                    excluded.add(c["code"])

    return consumed, excluded


def _credit_breakdown(
    required_courses: list, group_requirements: dict, passed_codes: set, passed_courses: list
) -> dict:
    """把成績單切成「必修學分」跟「選修學分」兩塊：必修學分＝被拿去滿足必修/必選修門檻的課學分
    加總（含用課號前綴比對到的國文/外文/通識這種沒登記固定課號的必修項目）；選修學分則是其餘
    已通過課程扣掉「共同必修超修」（excluded，見_consumed_required_codes說明）後的學分加總——
    這種超修只能算在總學分裡，不能算選修，選修必須是本系專業課程或必修/必選修超修的部分。
    `/check` 的必修/選修學分門檻，跟 elective_source 備註規則要算的「選修來源」，都是同一份
    切分結果，這裡算一次共用，不用兩邊各自重算。
    """
    consumed, excluded = _consumed_required_codes(required_courses, group_requirements, passed_codes, passed_courses)
    elective_courses = [
        c for c in passed_courses if c["code"] and c["code"] not in consumed and c["code"] not in excluded
    ]
    required_credit_total = sum(c["credit"] for c in passed_courses if c["code"] and c["code"] in consumed)
    elective_credit_total = sum(c["credit"] for c in elective_courses)
    return {
        "elective_courses": elective_courses,
        "required_credit_total": required_credit_total,
        "elective_credit_total": elective_credit_total,
    }


def _check_prerequisite(
    passed_codes: set,
    trigger_codes: list,
    require_codes: list,
    require_count: int,
    term_by_code: dict = None,
) -> tuple:
    """先修規定的核心判斷：`trigger_codes` 有任一門通過，才要求 `require_codes` 裡至少通過
    `require_count` 門。回傳 (status, detail)，status 是 "na"（trigger都沒通過，規則不適用）/
    "ok"/"fail"。

    這也是「順序修習規定」(sequence) 的底層邏輯——A→B→C依序修習，拆開來看就是「B通過了就要求A
    也通過（1取1）」「C通過了就要求B也通過（1取1）」兩條先修規定接在一起，兩種 kind 共用同一個
    判斷函式，不用各自重複寫一次「trigger通過了才檢查require門數夠不夠」這段邏輯。

    `term_by_code`（課號→學年學期整數，數字越大代表學期越晚，來自成績單「學年學期」欄位）是選填的：
    沒帶的話只看「有沒有通過」，跟以前行為一樣。有帶的話，門數夠了之後還會多檢查一次「真的是先修完
    才修trigger」——比對通過的require_codes是不是真的在trigger最早通過的那個學期「之前」完成，
    抓出「順序真的顛倒過，只是後來兩門都補到及格」這種只看有沒有通過會漏掉的違規。某門課的學期
    資料缺失（None，例如舊格式PDF沒有這欄）時當作「無法反證」，不算違規，避免因為資料不全就誤判。
    """
    term_by_code = term_by_code or {}
    if not any(t in passed_codes for t in trigger_codes):
        return "na", ""
    # 算「通過幾門」一定要用不重複的課號集合去算：require_codes 萬一不小心重複打了同一個課號
    # 兩次（例如手動輸入、或直接改 rules.yaml），不能讓同一門通過的課被算兩次、虛報通過門數。
    distinct_required = set(require_codes)
    passed_required = distinct_required & passed_codes
    if len(passed_required) < require_count:
        missing = dict.fromkeys(r for r in require_codes if r not in passed_codes)  # 去重但保留原本順序
        return "fail", f"已通過{len(passed_required)}/{require_count}門，尚缺：{'、'.join(missing)}"

    trigger_terms = [
        term_by_code[t] for t in trigger_codes if t in passed_codes and term_by_code.get(t) is not None
    ]
    if not trigger_terms:
        return "ok", ""
    earliest_trigger_term = min(trigger_terms)

    on_time = {
        r for r in passed_required if term_by_code.get(r) is None or term_by_code[r] < earliest_trigger_term
    }
    if len(on_time) >= require_count:
        return "ok", ""
    too_late = sorted(passed_required - on_time)
    return "fail", f"已通過「{'、'.join(too_late)}」，但是跟先修課程同一學期或之後才通過，不符合先修順序"


def evaluate_note_rules(
    note_rules: list,
    passed_codes: set,
    passed_courses: list,
    required_courses: list,
    group_requirements: dict = None,
) -> list:
    """把應修科目表下方的「備註」規則（rules.yaml 的 note_rules）拿去對照成績單，算出每條的完成狀態。

    三種 kind 對應畢業門檻PDF備註裡實際會出現的規則形狀，設計成可重複套用的通用類型，
    之後系上備註調整時大多只要用既有 kind 開新規則，不用改程式碼：
    - prerequisite（先修規定）：底下合併了兩種形狀，用 `codes` 有沒有填來分辨用哪一種，
      同一條規則只會用到其中一種（另一種欄位留空）：
      (1) 沒填 codes：「觸發課號通過了 → 要求課號要通過夠多門」，例如「微積分任一門通過才能修
      工程數學」「四門課任兩門以上才能修程序設計」「學士論文Ⅰ通過才能修學士論文Ⅱ」。trigger_codes
      一門都沒通過就代表這條規則根本沒被觸發（例如沒修工程數學），status 給 "na"（不適用），不算
      沒過，也不列入及格判定，避免跟學生根本沒選的課無關的規則害他被判不及格。
      (2) 有填 codes：一串課號規定依序修習，例如「理論與實務整合專題實作需依照EG3001、EG3002、
      EG3003順序修習」；成績單有學年學期欄位的話會比對真正的修課順序，沒有的話至少能抓出「後面的
      通過了、前面的卻沒通過」這種違反順序的矛盾情況。一門都沒通過（還沒碰這個系列課）一樣給 "na"。
      這兩種形狀底層都是同一個 _check_prerequisite() 判斷邏輯（依序修習就是把課號兩兩相鄰拆成
      一條條「後面通過了就要求前面也通過」接起來），合併成同一個 kind 只是介面上少一個選項，
      不用讓使用者猜「這條備註算先修規定還是順序修習規定」。
    - elective_source（選修學分來源）：例如「選修16學分中至少6學分要CH課號或特定課群」——必修/選修
      學分「夠不夠」是 /check 的頂層門檻（跟總學分門檻同一層級，在應修科目表管理頁設定），這條
      規則只管更細節的子條件：選修學分「從哪裡來」符不符合規定的來源。
      不是對照固定課號清單，而是要從成績單裡挑出「已通過但不在必修清單裡」的課當選修學分來源。
      同一筆規則底下有兩組獨立的子條件，都設定才都要通過、只設定一組就只檢查那一組：
      正向的「符合來源條件」（min_source_credits/source_code_prefixes/source_extra_codes，
      例如至少6學分要CH開頭或名單內的課）跟反向的「排除來源條件」（min_exclude_credits/
      exclude_code_prefixes，例如至少3學分要「不是」CH開頭，也就是外系課程）。
    - info：像「同一學期不可同時修讀X和Y」「依本校雙主修辦法」這種沒有學期資料/純政策引用、
      根本沒辦法從成績單自動判斷的備註，就只顯示文字提醒，不判斷完成與否。
    """
    elective_courses = _credit_breakdown(required_courses, group_requirements, passed_codes, passed_courses)[
        "elective_courses"
    ]
    term_by_code = {c["code"]: c.get("term") for c in passed_courses if c["code"]}

    results = []
    for rule in note_rules:
        kind = rule.get("kind", "info")
        text = rule.get("text", "")
        category = rule.get("category", "")

        if kind == "prerequisite":
            codes = rule.get("codes") or []
            if codes:
                # 依序課號形狀：每相鄰兩門課都是一條「後面通過了就要求前面也通過（而且要先通過）」
                # 的先修規定（1取1），抓到第一個違反順序的地方就回報，不用再往後檢查。
                if not any(c in passed_codes for c in codes):
                    results.append({"text": text, "kind": kind, "category": category, "status": "na", "detail": ""})
                    continue
                violation_detail = None
                for i in range(1, len(codes)):
                    pair_status, pair_detail = _check_prerequisite(
                        passed_codes, [codes[i]], [codes[i - 1]], 1, term_by_code
                    )
                    if pair_status == "fail":
                        violation_detail = pair_detail or f"已通過{codes[i]}，但尚未通過{codes[i - 1]}，不符合修習順序"
                        break
                status = "fail" if violation_detail else "ok"
                results.append(
                    {"text": text, "kind": kind, "category": category, "status": status, "detail": violation_detail or ""}
                )
            else:
                # 觸發→條件形狀
                trigger_codes = rule.get("trigger_codes") or []
                require_codes = rule.get("require_codes") or []
                require_count = rule.get("require_count") or len(require_codes) or 1
                status, detail = _check_prerequisite(
                    passed_codes, trigger_codes, require_codes, require_count, term_by_code
                )
                results.append({"text": text, "kind": kind, "category": category, "status": status, "detail": detail})

        elif kind == "elective_source":
            min_source = rule.get("min_source_credits") or 0
            prefixes = rule.get("source_code_prefixes") or []
            # 額外名單同時比對課號跟課名：學院公告的課群名單有時只給課名、沒有課號（例如還沒實際開課
            # 排課號），兩種都收才不會因為拿到的名單格式不一樣就沒辦法用。
            extra_matches = set(rule.get("source_extra_codes") or [])
            source_total = sum(
                c["credit"]
                for c in elective_courses
                if c["code"] in extra_matches
                or c["name"] in extra_matches
                or any(c["code"].startswith(p) for p in prefixes)
            )
            source_ok = source_total >= min_source
            detail_parts = [f"符合來源條件 {source_total}/{min_source}"]

            # 排除門檻是「反過來」的來源條件：選修學分裡「不是」某些課號前綴的部分要有多少學分
            # （例如「至少3學分要外系課程」＝選修裡「不是CH開頭」的部分要≥3學分）。跟上面的
            # 「符合來源條件」是同一個 elective_source kind 底下兩組獨立的子條件，都設定才都要通過；
            # 只設定其中一組（另一組留空/0）就只檢查那一組，這樣同一個 kind 可以同時處理「至少X學分
            # 要來自某類課」跟「至少Y學分要不是來自某類課」兩種形狀的備註，之後系上再出現類似的
            # 選修來源子條件，大多能直接在 /admin 新增規則設定，不用再改程式碼。
            min_exclude = rule.get("min_exclude_credits") or 0
            exclude_prefixes = rule.get("exclude_code_prefixes") or []
            exclude_ok = True
            if min_exclude:
                exclude_total = sum(
                    c["credit"]
                    for c in elective_courses
                    if not any(c["code"].startswith(p) for p in exclude_prefixes)
                )
                exclude_ok = exclude_total >= min_exclude
                detail_parts.append(f"排除來源條件 {exclude_total}/{min_exclude}")

            ok = source_ok and exclude_ok
            results.append(
                {
                    "text": text,
                    "kind": kind,
                    "category": category,
                    "status": "ok" if ok else "fail",
                    "detail": "；".join(detail_parts),
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
    missing_required = missing_required_courses(required_courses, result["passed_codes"], group_requirements)
    credit_ok = result["total_credit"] >= required_total
    breakdown = _credit_breakdown(
        required_courses, group_requirements, result["passed_codes"], result["passed_courses"]
    )
    required_credit_ok = breakdown["required_credit_total"] >= required_credits if required_credits else True
    elective_credit_ok = breakdown["elective_credit_total"] >= elective_credits if elective_credits else True
    unmet_categories = [
        u for u in result["unmet_categories"] if not u["code"].startswith(_DUPLICATED_CATEGORY_PREFIX)
    ]
    note_results = evaluate_note_rules(
        note_rules, result["passed_codes"], result["passed_courses"], required_courses, group_requirements
    )
    note_rules_failed = any(n["status"] == "fail" for n in note_results)
    return {
        "filename": filename,
        "error": None,
        "total_credit": result["total_credit"],
        "credit_ok": credit_ok,
        "required_credits": required_credits,
        "required_credit_total": breakdown["required_credit_total"],
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
        ),
        "courses": result["courses"],
        "has_text": result["has_text"],
        "unmet_categories": unmet_categories,
        "missing_required": missing_required,
        "note_results": note_results,
        "note_sections": _group_note_results_by_category(note_results),
    }


def _build_error_entry(filename: str, error: str) -> dict:
    """檔案太大、不是有效PDF、或解析途中出例外時用這個，讓結果頁能顯示明確的錯誤原因，
    而不是讓整個request壞掉、變成使用者看不懂的500錯誤頁。"""
    return {
        "filename": filename,
        "error": error,
        "total_credit": 0,
        "credit_ok": False,
        "required_credits": 0,
        "required_credit_total": 0,
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
    }


@app.post("/check", response_class=HTMLResponse)
async def check(request: Request, year: str = Form(...), files: List[UploadFile] = File([])):
    rules = load_rules()
    year_data = rules.get(year, {})
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
    "info": "純提醒",
    "prerequisite": "先修規定",
    "elective_source": "選修學分來源",
}

# 應修科目表原始PDF的備註段落順序，給「類別」欄位自動完成建議用，管理者也可以自己輸入別的分類
_NOTE_RULE_CATEGORY_SUGGESTIONS = ["一、共同必修", "二、院、系訂必修", "三、雙主修規定"]


def _parse_code_list(s: str) -> list:
    """把表單裡逗號分隔的課號/課名字串拆成list，順便去重（保留第一次出現的順序）。
    去重是必要的：像 require_codes 這種欄位如果不小心重複打了同一個課號兩次，
    _check_prerequisite() 算「通過幾門」時會把同一門通過的課算兩次，可能讓明明沒達到
    門檻的情況被誤判成已達標。
    """
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
    return {
        "index": index,
        "kind": kind,
        "kind_label": _NOTE_RULE_KIND_LABELS.get(kind, kind),
        "category": r.get("category", ""),
        "text": r.get("text", ""),
        "trigger_codes": ", ".join(r.get("trigger_codes") or []),
        "require_codes": ", ".join(r.get("require_codes") or []),
        "require_count": r.get("require_count", 1),
        "codes": ", ".join(r.get("codes") or []),
        "min_source_credits": r.get("min_source_credits", 0),
        "source_code_prefixes": ", ".join(r.get("source_code_prefixes") or []),
        "source_extra_codes": ", ".join(r.get("source_extra_codes") or []),
        "min_exclude_credits": r.get("min_exclude_credits", 0),
        "exclude_code_prefixes": ", ".join(r.get("exclude_code_prefixes") or []),
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
    trigger_codes: str,
    require_codes: str,
    require_count: int,
    codes: str,
    min_source_credits: float,
    source_code_prefixes: str,
    source_extra_codes: str,
    min_exclude_credits: float,
    exclude_code_prefixes: str,
) -> dict:
    rule = {"kind": kind, "category": category.strip(), "text": text}
    if kind == "prerequisite":
        # 「依序課號」欄位有填就是順序修習形狀，沒填才是觸發→條件形狀——同一條規則只會用到一種，
        # 不用把另一種形狀的空欄位也存進 rules.yaml。
        codes_list = _parse_code_list(codes)
        if codes_list:
            rule["codes"] = codes_list
        else:
            rule["trigger_codes"] = _parse_code_list(trigger_codes)
            rule["require_codes"] = _parse_code_list(require_codes)
            rule["require_count"] = require_count or len(rule["require_codes"]) or 1
    elif kind == "elective_source":
        rule["min_source_credits"] = min_source_credits or 0
        rule["source_code_prefixes"] = _parse_code_list(source_code_prefixes)
        rule["source_extra_codes"] = _parse_code_list(source_extra_codes)
        rule["min_exclude_credits"] = min_exclude_credits or 0
        rule["exclude_code_prefixes"] = _parse_code_list(exclude_code_prefixes)
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


def _admin_context(
    year: Optional[str],
    tab: str = "courses",
    edit: Optional[int] = None,
    edit_note: Optional[int] = None,
    import_error: bool = False,
) -> dict:
    """/admin 頁面（GET路由、跟「匯入規則失敗要重新顯示這個頁面」共用）的畫面資料，抽出來共用，
    這樣匯入規則失敗時能重新顯示完整頁面內容（帶錯誤訊息），不用整個複製一份GET路由的邏輯。
    """
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
    stored_group_requirements = year_data.get("group_requirements", {})
    # 層級純粹是顯示分類用（共同必修/院訂必修/系訂必修...），不影響判定邏輯，給表單自動完成選項用
    tier_options = sorted({c["tier"] for c in required_courses if c["tier"]})

    # 畫面分區的依據：優先用「分組」（N選M功能性分組），沒有分組才退而用「層級」（純顯示分類），
    # 兩者都沒有的科目不分區、直接顯示
    # 用一般字典依key收集（不用itertools.groupby），因為groupby只會合併「清單中緊鄰」的
    # 相同key項目——新增科目是直接append到清單最後面，如果用groupby，同分組的科目只要不是
    # 緊接在一起，就會被拆成兩個同名區塊，畫面上看起來像新科目沒被放進分組裡。
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
                "required_count": stored_group_requirements.get(key, 1) if is_group else None,
                "courses": items_list,
            }
        )

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
        "note_rule_kinds": _NOTE_RULE_KIND_LABELS,
        "course_catalog": course_catalog,
        "note_category_options": note_category_options,
        "edit_note_index": edit_note,
        "import_error": import_error,
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin(
    request: Request,
    year: Optional[str] = None,
    tab: str = "courses",
    edit: Optional[int] = None,
    edit_note: Optional[int] = None,
):
    """單一頁面、單一網址（/admin），畫面上分「科目管理／分組與學年度設定／備註規則設定」三個分頁籤，
    用前端JS切換顯示、不用重新整頁——`tab` 這個查詢參數只是給「切哪個分頁後刷新頁面」（例如表單送出
    後跳轉回來）時，能一開始就顯示對的分頁籤，避免每次存檔後又跳回第一個分頁籤。
    """
    return templates.TemplateResponse(request, "admin.html", _admin_context(year, tab, edit, edit_note))


@app.post("/admin/year/add")
async def admin_year_add(year: str = Form(...), copy_from: str = Form("")):
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


@app.post("/admin/group/set_requirement")
async def admin_group_set_requirement(year: str = Form(...), group: str = Form(...), required_count: int = Form(...)):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": [], "group_requirements": {}})
    year_data.setdefault("group_requirements", {})[group] = required_count
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}&tab=settings", status_code=303)


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

    return RedirectResponse(f"/admin?year={year}&tab=settings", status_code=303)


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

    return RedirectResponse(f"/admin?year={year}&tab=settings", status_code=303)


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
):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data.setdefault("required_courses", []).append(
        {
            "name": name, "code": code, "credits": credits, "category": category,
            "group": group, "tier": tier, "note": note, "code_prefixes": _parse_code_list(code_prefixes),
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
    code_prefixes: str = Form(""),
):
    rules = load_rules()
    courses = rules.get(year, {}).get("required_courses", [])
    if 0 <= index < len(courses):
        courses[index] = {
            "name": name, "code": code, "credits": credits, "category": category,
            "group": group, "tier": tier, "note": note, "code_prefixes": _parse_code_list(code_prefixes),
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
    kind: str = Form("info"),
    category: str = Form(""),
    text: str = Form(...),
    trigger_codes: str = Form(""),
    require_codes: str = Form(""),
    require_count: int = Form(0),
    codes: str = Form(""),
    min_source_credits: float = Form(0),
    source_code_prefixes: str = Form(""),
    source_extra_codes: str = Form(""),
    min_exclude_credits: float = Form(0),
    exclude_code_prefixes: str = Form(""),
):
    rules = load_rules()
    year_data = rules.setdefault(year, {"total_credits": 0, "required_courses": []})
    year_data.setdefault("note_rules", []).append(
        _build_note_rule(
            kind, category, text, trigger_codes, require_codes, require_count, codes,
            min_source_credits, source_code_prefixes, source_extra_codes,
            min_exclude_credits, exclude_code_prefixes,
        )
    )
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}&tab=notes", status_code=303)


@app.post("/admin/note_rule/update")
async def admin_note_rule_update(
    year: str = Form(...),
    index: int = Form(...),
    kind: str = Form("info"),
    category: str = Form(""),
    text: str = Form(...),
    trigger_codes: str = Form(""),
    require_codes: str = Form(""),
    require_count: int = Form(0),
    codes: str = Form(""),
    min_source_credits: float = Form(0),
    source_code_prefixes: str = Form(""),
    source_extra_codes: str = Form(""),
    min_exclude_credits: float = Form(0),
    exclude_code_prefixes: str = Form(""),
):
    rules = load_rules()
    note_rules = rules.get(year, {}).get("note_rules", [])
    if 0 <= index < len(note_rules):
        note_rules[index] = _build_note_rule(
            kind, category, text, trigger_codes, require_codes, require_count, codes,
            min_source_credits, source_code_prefixes, source_extra_codes,
            min_exclude_credits, exclude_code_prefixes,
        )
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}&tab=notes#note-row-{index}", status_code=303)


@app.post("/admin/note_rule/delete")
async def admin_note_rule_delete(year: str = Form(...), index: int = Form(...)):
    rules = load_rules()
    note_rules = rules.get(year, {}).get("note_rules", [])
    if 0 <= index < len(note_rules):
        note_rules.pop(index)
    save_rules(rules)
    return RedirectResponse(f"/admin?year={year}&tab=notes", status_code=303)


@app.get("/admin/rules/export")
async def admin_rules_export():
    """把整份 rules.yaml 包成下載檔，方便系上人員手動同步到其他各自獨立安裝的電腦
    （這個系統每台電腦是各自獨立一份規則資料，沒有連網同步）。"""
    return Response(
        content=RULES_FILE.read_bytes(),
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=rules.yaml"},
    )


@app.post("/admin/rules/import")
async def admin_rules_import(request: Request, year: str = Form(""), file: UploadFile = File(...)):
    """上傳一份 rules.yaml 檔案，整份覆蓋掉這台電腦目前的規則設定——用來對照「匯出規則」，
    讓系上人員可以不用自己去檔案總管找檔案覆蓋，直接在網頁上同步另一台電腦匯出的規則。

    整份覆蓋是刻意的設計（不是只合併有變動的學年度）：規則之間常常互相關聯（例如某個學年度的
    note_rules、group_requirements要跟該學年度的required_courses對得起來），部分合併容易讓
    資料兜不起來、產生看起來合理但實際上前後矛盾的規則。覆蓋前一定要先備份現有的
    rules.yaml（成 rules.yaml.bak），才不會匯錯檔案就再也找不回原本這台電腦的資料。
    """
    content = await file.read()
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, dict):
        return templates.TemplateResponse(
            request, "admin.html", _admin_context(year, tab="settings", import_error=True)
        )

    RULES_BACKUP_FILE.write_bytes(RULES_FILE.read_bytes())
    save_rules({str(y): data for y, data in parsed.items()})
    return RedirectResponse(f"/admin?year={year}&tab=settings", status_code=303)
