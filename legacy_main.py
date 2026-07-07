"""Legacy Tkinter launcher retained during the PySide6 transition."""

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from tkinterdnd2 import TkinterDnD
from runtime_paths import initialize_runtime_data
from ui_components import Sidebar, StatusHeader, HomeFrame, OrganizerFrame, TransferFrame
from utils import HashCache

class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("Duplicate & Transfer Manager")
        self.geometry("1400x950")
        self.minsize(1100, 760)
        self.selected_adb_serial = ""

        project_root = Path(__file__).resolve().parent
        self.runtime_paths, self.migration_result = initialize_runtime_data(project_root)
        self.hash_cache = HashCache(str(self.runtime_paths.hash_cache))
        self.hash_cache.load()

        self.header = StatusHeader(self)
        self.header.pack(side="top", fill="x")

        self.sidebar = Sidebar(self, self)
        self.sidebar.pack(side="left", fill="y")

        self.content = ttk.Frame(self, padding=20)
        self.content.pack(side="right", fill="both", expand=True)

        self.frames = {}
        for F in (HomeFrame, OrganizerFrame, TransferFrame):
            frame = F(self.content, self)
            self.frames[F.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.show_frame("HomeFrame")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def show_frame(self, name):
        self.frames[name].tkraise()

    def get_selected_device_serial(self):
        return self.selected_adb_serial

    def set_selected_device_serial(self, serial):
        self.selected_adb_serial = serial or ""

    def _on_close(self):
        self.hash_cache.save()
        self.destroy()

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
