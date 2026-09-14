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

# 跟main.py同一套判斷（見那邊的說明）：打包後要用.exe實際的位置當基準，圖示檔才找得到。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
ICON_PATH = BASE_DIR / "static" / "icon.ico"

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"

# 跟網頁介面（templates裡的Tailwind設定）用同一套配色，讓執行檔的小視窗跟瀏覽器裡的畫面
# 看起來是同一套系統，不是兩個風格對不上的介面。
BLUE = "#2563EB"       # tailwind blue-600
BLUE_DARK = "#1D4ED8"  # tailwind blue-700，按鈕按下時的深色
GREEN = "#16A34A"      # tailwind green-600
GRAY_TEXT = "#6B7280"  # tailwind gray-500
BG = "#F9FAFB"         # tailwind gray-50


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


def _fatal_error(exc: Exception) -> None:
    # 打包成 --noconsole 之後沒有黑色視窗可以看錯誤訊息，啟動失敗（通常是資料檔案沒打包對、
    # 或路徑算錯）預設會整個「安靜地」關掉，使用者只會覺得「點了沒反應」，完全沒有線索能回報
    # 問題。這裡開一個訊息框把完整錯誤內容印出來，至少能截圖回報。
    root = tk.Tk()
    _set_icon(root)
    root.withdraw()
    messagebox.showerror(
        "化材系畢業學分檢核系統 - 啟動失敗",
        "系統啟動時發生錯誤，請把下面的錯誤訊息截圖回報：\n\n" + "".join(traceback.format_exception(exc)),
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

    root = tk.Tk()
    root.title("化材系畢業學分檢核系統")
    root.configure(bg=BG)
    root.resizable(False, False)
    _set_icon(root)
    _center(root, 440, 280)

    # 頂部品牌色橫幅，呼應網頁版導覽列的藍色（bg-blue-600），讓執行檔的小視窗跟瀏覽器裡的
    # 畫面一眼看出是同一套系統，不是隨便一個Tk預設灰色視窗。
    header = tk.Frame(root, bg=BLUE, height=64)
    header.pack(fill="x")
    header.pack_propagate(False)
    tk.Label(
        header, text="🎓 化材系畢業學分檢核系統", font=("Microsoft JhengHei", 13, "bold"),
        bg=BLUE, fg="white",
    ).pack(expand=True)

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=28, pady=(20, 16))

    status_row = tk.Frame(body, bg=BG)
    status_row.pack(anchor="w")
    dot = tk.Canvas(status_row, width=10, height=10, bg=BG, highlightthickness=0)
    dot.create_oval(1, 1, 9, 9, fill=GREEN, outline="")
    dot.pack(side="left", padx=(0, 6))
    tk.Label(status_row, text="系統執行中", font=("Microsoft JhengHei", 12, "bold"), bg=BG, fg="#111827").pack(side="left")

    # 網址做成看起來像連結的樣子、點下去直接開瀏覽器——使用者不用自己選取文字複製貼上，
    # 瀏覽器分頁不小心關掉時，這裡就是唯一能再打開畫面的地方。
    link = tk.Label(
        body, text=URL, font=("Microsoft JhengHei", 10, "underline"), bg=BG, fg=BLUE, cursor="hand2",
    )
    link.pack(anchor="w", pady=(6, 0))
    link.bind("<Button-1>", lambda _e: webbrowser.open(URL))

    tk.Frame(body, bg="#E5E7EB", height=1).pack(fill="x", pady=14)

    tk.Label(
        body,
        text="這個小視窗代表系統正在背景執行，關閉視窗會一併停止系統。\n瀏覽器分頁可以直接關掉沒關係，要再打開就回來點上面的網址。",
        font=("Microsoft JhengHei", 9), bg=BG, fg=GRAY_TEXT, wraplength=380, justify="left",
    ).pack(anchor="w")

    def on_close() -> None:
        server.should_exit = True
        root.destroy()

    footer = tk.Frame(root, bg=BG)
    footer.pack(fill="x", padx=28, pady=(0, 20))
    stop_btn = tk.Button(
        footer, text="結束系統", command=on_close, font=("Microsoft JhengHei", 10, "bold"),
        bg=BLUE, fg="white", activebackground=BLUE_DARK, activeforeground="white",
        relief="flat", padx=16, pady=6, cursor="hand2",
    )
    stop_btn.pack(anchor="e")

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
