"""給 PyInstaller 打包成 Windows 執行檔用的進入點（見 .github/workflows/build-windows.yml）。

一般開發、或電腦本來就有裝 Python 的情況，還是用 `python -m uvicorn main:app` 或
啟動.bat（本質上也是呼叫同一行指令），不會用到這個檔案——這個檔案只在「不想讓使用者碰
命令列」的情況下，包成一個雙擊就能跑的執行檔時才會用到。

跟直接跑 uvicorn 的差別：
1. 背景執行緒跑伺服器，主執行緒開一個簡單的 tkinter 小視窗取代黑色命令列視窗，不熟悉電腦的
   使用者不會被一堆看不懂的英文字嚇到，也有清楚的地方可以按「結束系統」（不然打包成
   --noconsole 之後，使用者根本沒有視窗可以關閉伺服器，只能開工作管理員硬砍）。
2. 自動開瀏覽器，使用者不用自己打網址。
3. 偵測 port 已經被佔用（使用者手滑點兩次、或系統其實已經在背景跑）時，不重複啟動，直接開瀏覽器。
4. 補上sys.stdout/stderr：PyInstaller打包成--noconsole後沒有主控台視窗，Windows上
   sys.stdout/sys.stderr會直接是None（不是被重導向、是真的None）——uvicorn預設的logging
   設定檔會呼叫sys.stdout.isatty()判斷要不要上色，None沒有這個方法就直接炸掉整個程式
   啟動失敗。要在uvicorn的任何程式碼執行「之前」補上，不然設定logging那一步就先掛了。
"""
import socket
import sys
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

if sys.stdout is None:
    sys.stdout = open("nul" if sys.platform == "win32" else "/dev/null", "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open("nul" if sys.platform == "win32" else "/dev/null", "w", encoding="utf-8")

# 跟main.py同一套判斷（見那邊的說明）：打包後要用.exe實際的位置當基準，圖示/版本檔才找得到。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
ICON_PATH = BASE_DIR / "static" / "icon.ico"
VERSION_FILE = BASE_DIR / "version.txt"

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"

# 這個小視窗要看起來跟瀏覽器裡的畫面是同一套系統，顏色直接照抄templates/index.html等頁面
# 實際用的Tailwind class對應的色碼（bg-gray-50頁面、bg-white卡片、bg-blue-600按鈕、
# text-green-800/bg-green-50「已通過」狀態列……），不是另外發明一套配色。
PAGE_BG = "#F9FAFB"     # tailwind gray-50，網頁版body背景
CARD_BG = "#FFFFFF"     # 卡片背景
CARD_BORDER = "#E5E7EB"  # tailwind gray-200，卡片邊框（沒有陰影可以用時的替代）
TEXT_DARK = "#111827"   # tailwind gray-900，標題文字
TEXT_GRAY = "#6B7280"   # tailwind gray-500，說明文字
TEXT_MUTED = "#9CA3AF"  # tailwind gray-400，版本號這種最不重要的文字
BLUE = "#2563EB"        # tailwind blue-600，按鈕/連結
BLUE_HOVER = "#1D4ED8"  # tailwind blue-700，滑鼠移上去
GREEN_BG = "#F0FDF4"    # tailwind green-50，「系統執行中」狀態列底色
GREEN_BORDER = "#BBF7D0"  # tailwind green-200
GREEN_TEXT = "#166534"  # tailwind green-800

FONT_FAMILY = "Microsoft JhengHei"


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        # 開發時直接跑`python launcher.py`不會有這個檔案（只有CI打包時才會產生，見
        # build-windows.yml），這種情況就老實顯示「開發版」，不要假裝有版本號。
        return "開發版"


def _set_icon(root: tk.Tk) -> None:
    """.ico只有Windows的iconbitmap吃得下，開發時在macOS/Linux跑會直接丟例外——
    圖示顯示失敗不該讓整個視窗開不起來，失敗就算了，不影響其他功能。
    """
    try:
        if ICON_PATH.exists():
            root.iconbitmap(default=str(ICON_PATH))
    except Exception:
        pass


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def _center(root: tk.Tk, width: int, height: int) -> None:
    root.update_idletasks()
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 3
    root.geometry(f"{width}x{height}+{x}+{y}")


def _rounded_card(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int) -> None:
    """畫一張跟網頁版卡片（bg-white shadow-md rounded-lg）same風格的圓角卡片。
    tkinter沒有原生圓角矩形，用四個圓角的圓弧+中間補滿的矩形拼出來是常見做法；
    先畫一層極淺灰色、往右下偏移幾個像素當「陰影」，弱化網頁版shadow-md的效果。
    """
    shadow_offset = 3
    for dx, dy, fill in ((shadow_offset, shadow_offset, "#EEF0F3"), (0, 0, CARD_BG)):
        ax1, ay1, ax2, ay2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        # 這幾片矩形/圓弧只是把圓角卡片的「內部填色」拼起來，outline留空——邊框只在最後
        # 用create_line沿著卡片真正的外緣描一圈，不然這些拼接矩形自己的outline會在
        # 卡片內部畫出一堆多餘的接縫線。
        canvas.create_rectangle(ax1 + radius, ay1, ax2 - radius, ay2, fill=fill, outline="")
        canvas.create_rectangle(ax1, ay1 + radius, ax2, ay2 - radius, fill=fill, outline="")
        for cx, cy, start in (
            (ax1 + radius, ay1 + radius, 90), (ax2 - radius, ay1 + radius, 0),
            (ax1 + radius, ay2 - radius, 180), (ax2 - radius, ay2 - radius, 270),
        ):
            canvas.create_arc(
                cx - radius, cy - radius, cx + radius, cy + radius,
                start=start, extent=90, fill=fill, outline="", style="pieslice",
            )
        if fill == CARD_BG:
            canvas.create_line(ax1 + radius, ay1, ax2 - radius, ay1, fill=CARD_BORDER)
            canvas.create_line(ax1 + radius, ay2, ax2 - radius, ay2, fill=CARD_BORDER)
            canvas.create_line(ax1, ay1 + radius, ax1, ay2 - radius, fill=CARD_BORDER)
            canvas.create_line(ax2, ay1 + radius, ax2, ay2 - radius, fill=CARD_BORDER)


def _hoverable_button(parent, text, command):
    """跟網頁版按鈕（bg-blue-600 hover:bg-blue-700）同樣的滑鼠移上去變色效果——
    tkinter的activebackground只有「按著不放」才會生效，滑鼠單純移過去不會變色，
    要另外綁Enter/Leave事件才能做出網頁版那種hover效果。
    """
    btn = tk.Label(
        parent, text=text, font=(FONT_FAMILY, 10, "bold"), bg=BLUE, fg="white",
        padx=16, pady=8, cursor="hand2",
    )
    btn.bind("<Enter>", lambda _e: btn.configure(bg=BLUE_HOVER))
    btn.bind("<Leave>", lambda _e: btn.configure(bg=BLUE))
    btn.bind("<Button-1>", lambda _e: command())
    return btn


def _fatal_error(exc: Exception) -> None:
    # 打包成 --noconsole 之後沒有黑色視窗可以看錯誤訊息，啟動失敗（通常是資料檔案沒打包對、
    # 或路徑算錯）預設會整個「安靜地」關掉，使用者只會覺得「點了沒反應」，完全沒有線索能回報
    # 問題。這裡開一個訊息框把完整錯誤內容印出來，至少能截圖回報。
    root = tk.Tk()
    _set_icon(root)
    root.withdraw()
    messagebox.showerror(
        "化材系畢業學分檢核系統 - 啟動失敗",
        f"系統啟動時發生錯誤（版本 {_read_version()}），請把下面的錯誤訊息截圖回報：\n\n"
        + "".join(traceback.format_exception(exc)),
    )


def main() -> None:
    if _port_in_use():
        # 已經有一份在跑了（使用者手滑點兩次、或忘記之前開過）：不要再啟動第二個伺服器搶同一個
        # port，直接開瀏覽器連過去就好，跳個提示讓使用者知道發生了什麼事，不要讓程式悄悄關掉。
        webbrowser.open(URL)
        root = tk.Tk()
        _set_icon(root)
        root.withdraw()
        messagebox.showinfo(
            "化材系畢業學分檢核系統",
            f"系統似乎已經在執行中了，已經幫你開啟瀏覽器。\n若沒有自動開啟，請手動在瀏覽器輸入：\n{URL}",
        )
        return

    try:
        import uvicorn

        from main import app

        config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        # 等伺服器真的準備好再開瀏覽器，不然使用者會先看到「無法連線」的錯誤頁，以為系統壞了
        for _ in range(50):
            if _port_in_use():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("伺服器啟動超過5秒還沒能連線，可能是port被防火牆擋住，或啟動時發生例外。")
    except Exception as exc:  # noqa: BLE001 - 這裡就是要接住「所有」啟動失敗的情況給使用者看
        _fatal_error(exc)
        return

    webbrowser.open(URL)

    WIDTH, HEIGHT = 480, 380
    root = tk.Tk()
    root.title("化材系畢業學分檢核系統")
    root.configure(bg=PAGE_BG)
    root.resizable(False, False)
    _set_icon(root)
    _center(root, WIDTH, HEIGHT)

    # 整個視窗其實是一張Canvas：網頁版「灰色頁面上放一張白色卡片」（bg-gray-50搭配
    # bg-white shadow-md rounded-lg）這種圓角+陰影的效果，tkinter原生widget疊層做不出來，
    # 只能整張畫在canvas上，卡片裡的文字/按鈕再疊在上面。
    canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, bg=PAGE_BG, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    margin = 24
    _rounded_card(canvas, margin, margin, WIDTH - margin, HEIGHT - margin, radius=14)

    card = tk.Frame(canvas, bg=CARD_BG)
    inner_pad = 32
    canvas.create_window(
        WIDTH / 2, HEIGHT / 2, window=card,
        width=WIDTH - margin * 2 - inner_pad, height=HEIGHT - margin * 2 - inner_pad,
    )

    tk.Label(
        card, text="🎓 化材系畢業學分檢核系統", font=(FONT_FAMILY, 14, "bold"), bg=CARD_BG, fg=TEXT_DARK,
    ).pack(pady=(2, 16))

    # 「系統執行中」狀態列，直接照抄result.html裡「已符合畢業資格」那個綠色狀態列的視覺
    # 語言（✅ + 綠字 + 淺綠底 + 綠框），讓使用者一眼就認出這跟網頁版是同一套視覺系統。
    status = tk.Frame(card, bg=GREEN_BG, highlightbackground=GREEN_BORDER, highlightthickness=1)
    status.pack(fill="x")
    tk.Label(
        status, text="✅  系統執行中", font=(FONT_FAMILY, 11, "bold"), bg=GREEN_BG, fg=GREEN_TEXT,
    ).pack(padx=14, pady=10, anchor="w")

    link = tk.Label(
        card, text=URL, font=(FONT_FAMILY, 10, "underline"), bg=CARD_BG, fg=BLUE, cursor="hand2",
    )
    link.pack(anchor="w", pady=(14, 0))
    link.bind("<Button-1>", lambda _e: webbrowser.open(URL))

    tk.Frame(card, bg=CARD_BORDER, height=1).pack(fill="x", pady=14)

    tk.Label(
        card,
        text="這個小視窗代表系統正在背景執行，關閉視窗會一併停止系統。\n瀏覽器分頁可以直接關掉沒關係，要再打開就回來點上面的網址。",
        font=(FONT_FAMILY, 9), bg=CARD_BG, fg=TEXT_GRAY, wraplength=WIDTH - margin * 2 - 60, justify="left",
    ).pack(anchor="w")

    def on_close() -> None:
        server.should_exit = True
        root.destroy()

    bottom_row = tk.Frame(card, bg=CARD_BG)
    bottom_row.pack(fill="x", side="bottom", pady=(14, 0))
    tk.Label(
        bottom_row, text=f"版本 {_read_version()}", font=(FONT_FAMILY, 8), bg=CARD_BG, fg=TEXT_MUTED,
    ).pack(side="left")
    _hoverable_button(bottom_row, "結束系統", on_close).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
