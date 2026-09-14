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
"""
import socket
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from tkinter import messagebox

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def _fatal_error(exc: Exception) -> None:
    # 打包成 --noconsole 之後沒有黑色視窗可以看錯誤訊息，啟動失敗（通常是資料檔案沒打包對、
    # 或路徑算錯）預設會整個「安靜地」關掉，使用者只會覺得「點了沒反應」，完全沒有線索能回報
    # 問題。這裡開一個訊息框把完整錯誤內容印出來，至少能截圖回報。
    root = tk.Tk()
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
    root.geometry("380x180")
    root.resizable(False, False)

    tk.Label(root, text="系統執行中", font=("Microsoft JhengHei", 14, "bold")).pack(pady=(24, 6))
    tk.Label(root, text=f"瀏覽器網址：{URL}", font=("Microsoft JhengHei", 10)).pack()
    tk.Label(
        root,
        text="這個小視窗代表系統正在背景執行，關閉視窗會一併停止系統。\n瀏覽器分頁可以直接關掉沒關係，要再打開就回來點這個視窗旁邊的網址。",
        font=("Microsoft JhengHei", 9),
        fg="gray",
        wraplength=340,
        justify="left",
    ).pack(pady=(8, 16))

    def on_close() -> None:
        server.should_exit = True
        root.destroy()

    tk.Button(root, text="結束系統", command=on_close, width=16).pack()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
