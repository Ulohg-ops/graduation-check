1. 要做一個可以上傳同學成績單(pdf) 的網頁系統
2. 然後根據畢業要求 顯現出有沒有達到畢業的要求
3. 要能夠根據學年度更新規則
4. 有推薦的 Software Stack 嗎 


由於需要處理 PDF 檔案解析與規則比對，建議使用 Python (Flask 或 FastAPI) 作為後端（Python 擁有非常成熟且強大的 PDF 文字提取套件）：

| 元件 | 免費工具 / 平台選擇 | 說明與優勢 |
| --- | --- | --- |
| 網頁前端 (Frontend) | HTML5 + Tailwind CSS / Bootstrap | 簡潔的檔案上傳介面與審查結果面板，可以直接寫在 Python 後端模板中 |
| 後端邏輯 (Backend) | Python (FastAPI / Flask) | 處理檔案接收、PDF 文字解析、學分邏輯比對 |
| PDF 解析套件 | pdfplumber（優先）+ pypdf / pdfminer.six 作備援 | 精確提取文字與表格數據（如課程名稱、學分數、成績）。**注意**：若成績單是掃描版（圖片型 PDF）而非電子產生的文字型 PDF，需額外搭配 OCR（如 `pytesseract` 或 `PaddleOCR`）才能解析，建議先確認實際成績單格式再定案 |
| 畢業規則設定 | YAML / JSON 規則檔（依學年度分版本），或存入資料庫的規則表 | 將必修學分、選修學分、通識學分等門檻結構化、版本化，新學年度規則變動時只需新增一份設定檔／一筆資料，不用改程式碼 |
| 免費主機部署 | Render.com 或 Hugging Face Spaces | 免費支援 Python / Docker Web 服務，且支援綁定自訂網域 |
| 資料庫 | Supabase 或 Render PostgreSQL | 建議仍保留：即使不儲存學生個資，也用來存放「各學年度畢業規則」的版本化資料，方便查詢與維護；若規則單純想放在程式碼旁的 YAML/JSON 檔管理，也可以不用資料庫 |