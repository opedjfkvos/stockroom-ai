"""Start the local Stockroom AI dashboard without a console window."""

from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import locale
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import shutil
import subprocess
import sys
from threading import Thread
from time import sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import webbrowser

import app
from app import Handler
from assistant import DEFAULT_MODEL
from launcher_text import LAUNCHER_TEXT
from ollama_setup import installed_models, pull_model
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
        with urlopen(url + "/i18n.js", timeout=5) as response:
            if "Помічник зі складу" not in response.read().decode("utf-8"):
                raise RuntimeError("Packaged dashboard translations are missing.")
        for language, question, expected in (
            ("ru", "Сколько товара по артикулу 0002?", "в наличии 3 шт."),
            ("uk", "Скільки товару за артикулом 0002?", "у наявності 3 шт."),
        ):
            localized = post("/api/ask", json.dumps({
                "question": question, "source": "imported", "language": language,
            }).encode(), {"Content-Type": "application/json"})
            if expected not in localized["answer"] or localized["rows"][0]["sku"] != "0002":
                raise RuntimeError(f"Could not query imported stock in {language}.")
        output.write_text("PASS: dashboard, Excel import, exact SKU and Russian/Ukrainian queries.", encoding="utf-8")
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
    app.LANGUAGE_FILE = data / "language.txt"
    language = (locale.getlocale()[0] or "en").split("_")[0].lower()
    try:
        language = app.LANGUAGE_FILE.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        pass
    language = {"russian": "ru", "ukrainian": "uk", "english": "en"}.get(language, language)
    if language not in LAUNCHER_TEXT:
        language = "en"

    language_names = {"en": "English", "ru": "Русский", "uk": "Українська"}

    def tr(key, **fields):
        return LAUNCHER_TEXT[language][key].format(**fields)

    root = tk.Tk()
    root.title("Stockroom AI")
    root.resizable(False, False)
    try:
        server, url = start_server()
    except OSError as exc:
        messagebox.showerror("Stockroom AI", tr("boot_error") + "\n" + str(exc), parent=root)
        root.destroy()
        return

    model = os.environ.get("STOCKROOM_MODEL", DEFAULT_MODEL)
    ollama = None
    local_ready = False
    events = SimpleQueue()
    state = ("ready", {})
    panel = ttk.Frame(root, padding=24)
    panel.pack(fill="both", expand=True)
    status = tk.StringVar()
    progress = ttk.Progressbar(panel, maximum=100, mode="determinate")

    def set_status(key, **fields):
        nonlocal state
        state = (key, fields)
        status.set(tr(key, **fields))

    def open_dashboard():
        sync_language()
        options = {"lang": language}
        if local_ready:
            options["mode"] = "ollama"
        with inventory.use_inventory("imported"):
            try:
                if inventory.inventory_path().is_file():
                    options["source"] = "imported"
            except ValueError:
                pass
        target = url + "?" + urlencode(options)
        if not webbrowser.open(target):
            set_status("open_url", url=target)

    def check_model():
        nonlocal ollama
        try:
            try:
                models = installed_models()
            except OSError:
                standard = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "Ollama" / "ollama.exe"
                executable = str(standard) if standard.is_file() else shutil.which("ollama")
                if not executable:
                    events.put(("installer", None))
                    return
                if ollama is None or ollama.poll() is not None:
                    ollama = subprocess.Popen([executable, "serve"], stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for _ in range(20):
                    try:
                        models = installed_models()
                        break
                    except OSError:
                        sleep(0.5)
                else:
                    raise OSError("Ollama did not start.")
            events.put(("ready" if model in models else "offer_model", None))
        except (OSError, ValueError, KeyError, TypeError):
            events.put(("failed", None))

    def download_model():
        try:
            pull_model(model, lambda percent: events.put(("progress", percent)))
        except (OSError, ValueError, KeyError, TypeError):
            events.put(("failed", None))
        else:
            events.put(("ready", None))

    def start_ollama():
        sync_language()
        ai_button.state(["disabled"])
        progress.pack_forget()
        set_status("checking")
        Thread(target=check_model, daemon=True).start()

    def handle_event(kind, value):
        nonlocal local_ready
        if kind == "progress":
            progress.configure(value=value)
            set_status("progress", percent=value)
            return
        if kind == "offer_model" and messagebox.askyesno(
            "Stockroom AI", tr("offer_model", model=model), parent=root
        ):
            progress.configure(value=0)
            progress.pack(fill="x", pady=(10, 0))
            set_status("pull")
            Thread(target=download_model, daemon=True).start()
            return
        ai_button.state(["!disabled"])
        progress.pack_forget()
        if kind == "installer":
            if messagebox.askyesno("Stockroom AI", tr("offer_installer"), parent=root):
                webbrowser.open("https://ollama.com/download/windows")
                set_status("install")
            else:
                set_status("ready")
        elif kind == "ready":
            local_ready = True
            set_status("ai_ready")
            open_dashboard()
        else:
            local_ready = False
            set_status("failed" if kind == "failed" else "ready")

    def poll():
        try:
            while True:
                handle_event(*events.get_nowait())
        except Empty:
            pass
        root.after(150, poll)

    def close():
        root.destroy()
        server.shutdown()
        server.server_close()
        if ollama is not None and ollama.poll() is None:
            ollama.terminate()

    header = ttk.Frame(panel)
    header.pack(fill="x")
    ttk.Label(header, text="Stockroom AI", font=("Segoe UI", 18, "bold")).pack(side="left")
    picker = ttk.Combobox(header, values=list(language_names.values()), width=13, state="readonly")
    picker.set(language_names[language])
    picker.pack(side="right", padx=(18, 0))
    language_label = ttk.Label(header)
    language_label.pack(side="right")
    ttk.Label(panel, textvariable=status, wraplength=420).pack(anchor="w", pady=(12, 16))
    buttons = ttk.Frame(panel)
    buttons.pack(fill="x")
    dashboard_button = ttk.Button(buttons, command=open_dashboard)
    dashboard_button.pack(side="left")
    ai_button = ttk.Button(buttons, command=start_ollama)
    ai_button.pack(side="left", padx=8)
    quit_button = ttk.Button(buttons, command=close)
    quit_button.pack(side="left")

    def translate_launcher():
        language_label.configure(text="🌐 " + tr("language"))
        dashboard_button.configure(text=tr("dashboard"))
        ai_button.configure(text=tr("ai"))
        quit_button.configure(text=tr("quit"))
        set_status(state[0], **state[1])

    def change_language(_event):
        nonlocal language
        language = list(language_names)[picker.current()]
        try:
            app.save_language(language)
        except OSError:
            pass
        translate_launcher()

    def sync_language():
        nonlocal language
        try:
            current = app.LANGUAGE_FILE.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return
        if current in LAUNCHER_TEXT and current != language:
            language = current
            picker.set(language_names[language])
            translate_launcher()

    picker.bind("<<ComboboxSelected>>", change_language)
    translate_launcher()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, open_dashboard)
    root.after(150, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
