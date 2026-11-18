"""Start the local Stockroom AI dashboard without a console window."""

from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
from urllib.request import Request, urlopen
import webbrowser

from app import Handler
import inventory


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def self_test(output):
    """Exercise the bundled server and spreadsheet dependencies on Windows."""
    from openpyxl import Workbook

    server, url = start_server()
    try:
        with urlopen(url, timeout=5) as response:
            if b"Stockroom AI" not in response.read():
                raise RuntimeError("Bundled dashboard could not be opened.")
        workbook = Workbook()
        workbook.active.append(["SKU", "Product", "Category", "Stock", "Minimum", "Unit cost"])
        workbook.active.append(["0002", "Test Coffee", "Drinks", 3, 5, 2.50])
        stream = BytesIO()
        workbook.save(stream)
        data = stream.getvalue()

        def post(path, body, headers):
            with urlopen(Request(url + path, data=body, headers=headers), timeout=5) as response:
                return json.load(response)

        params = "?filename=stock.xlsx&currency=PLN"
        headers = {"Content-Type": "application/octet-stream", "X-Stockroom-Import": "1"}
        preview = post("/api/import/preview" + params, data, headers)
        headers["X-Stockroom-Confirmation"] = preview["confirmation"]
        post("/api/import/confirm" + params, data, headers)
        result = post("/api/ask", b'{"question":"What about SKU 0002?","source":"imported"}',
                      {"Content-Type": "application/json"})
        if len(result["rows"]) != 1 or result["rows"][0]["stock"] != 3:
            raise RuntimeError("Imported stock did not match the spreadsheet.")
        output.write_text("PASS: bundled dashboard, Excel preview, import and exact SKU lookup.", encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        output = Path(sys.argv[2]).resolve()
        inventory.DEFAULT_DB = output.parent / "self-test.sqlite3"
        try:
            self_test(output)
        except Exception as exc:
            output.write_text(f"FAIL: {exc}", encoding="utf-8")
            raise SystemExit(1) from exc
        return

    import tkinter as tk
    from tkinter import messagebox, ttk

    # The unpacked resources of a one-file executable are temporary.
    data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StockroomAI"
    inventory.DEFAULT_DB = data / "stockroom.sqlite3"
    root = tk.Tk()
    root.title("Stockroom AI")
    root.resizable(False, False)
    try:
        server, url = start_server()
    except OSError as exc:
        messagebox.showerror("Stockroom AI", f"The app could not start.\n{exc}", parent=root)
        root.destroy()
        return

    ollama = None
    panel = ttk.Frame(root, padding=24)
    panel.pack()
    status = tk.StringVar(value="Dashboard ready. Import an Excel or CSV file in your browser.")

    def open_dashboard():
        if not webbrowser.open(url):
            status.set(f"Open {url} in your browser.")

    def start_ollama():
        nonlocal ollama
        try:
            with urlopen("http://127.0.0.1:11434/api/tags", timeout=1):
                status.set("Ollama is running. Choose Local LLM in the dashboard.")
                return
        except OSError:
            pass
        if ollama is not None and ollama.poll() is None:
            status.set("Ollama is starting. Try Local LLM in a moment.")
            return
        standard = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "Ollama" / "ollama.exe"
        executable = str(standard) if standard.is_file() else shutil.which("ollama")
        if not executable:
            status.set("Install Ollama and a model to use Local LLM. Stock search is ready now.")
            return
        try:
            ollama = subprocess.Popen([executable, "serve"], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            status.set(f"Could not start Ollama: {exc}")
            return
        status.set("Ollama is starting. Once ready, choose Local LLM in the dashboard.")

    def close():
        root.destroy()
        server.shutdown()
        server.server_close()
        if ollama is not None and ollama.poll() is None:
            ollama.terminate()

    ttk.Label(panel, text="Stockroom AI", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(panel, textvariable=status, wraplength=400).pack(anchor="w", pady=(8, 16))
    buttons = ttk.Frame(panel)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Open dashboard", command=open_dashboard).pack(side="left")
    ttk.Button(buttons, text="Start local AI", command=start_ollama).pack(side="left", padx=8)
    ttk.Button(buttons, text="Quit", command=close).pack(side="left")
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, open_dashboard)
    root.mainloop()


if __name__ == "__main__":
    main()
