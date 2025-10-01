import tkinter as tk
from tkinter import messagebox

class ListFilesDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Listar archivos por etiquetas")
        self.result = None

        # Etiquetas
        tk.Label(self, text="Etiquetas (separadas por coma)\nDejar vacío para mostrar todos").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.tags_entry = tk.Entry(self, width=40)
        self.tags_entry.grid(row=0, column=1, columnspan=2, padx=5, pady=5)

        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=1, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=1, column=2, pady=10)

        self.grab_set()  # modal

    def on_ok(self):
        tags = self.tags_entry.get().strip()
        if len(tags):
            self.result = tags.split(",")
        else:
            self.result = []  # vacío → mostrar todos los archivos

        self.destroy()
