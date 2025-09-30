import tkinter as tk
from tkinter import filedialog, messagebox
import os

class AddFileDialog(tk.Toplevel):
    """
    Cuadro de dialogo para agregar un conjunto de archivos.
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Agregar archivo(s)")
        self.result = None

        # Cuadro de entrada del conjunto de archivos
        tk.Label(self, text="Nombre(s) de archivo(s) (separados por coma)\nEj: archivo1, archivo2, ...").grid(row=1, column=0, sticky="nw", padx=5, pady=5)
        self.files_entry = tk.Entry(self, width=40)
        self.files_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=5)

        # Etiquetas
        tk.Label(self, text="Etiqueta(s) (separadas por coma)\nEj: etiqueta1, etiqueta2, ...").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.tags_entry = tk.Entry(self, width=40)
        self.tags_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=5)

        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=3, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=3, column=2, pady=10)

        self.grab_set()  # modal

    def on_ok(self):
        file_names = [fn.strip() for fn in self.files_entry.get().split(",")]
        print(file_names)


        if not file_names:
            messagebox.showwarning("Faltan archivos", "Debe insertar al menos un archivo.")
            return

        tags = self.tags_entry.get().strip().split(",")
        if not tags:
            messagebox.showwarning("Faltan datos", "Debe escribir al menos una etiqueta.")
            return
        
        # # Guardar resultado: lista de tuplas (nombre del archivo completo, tags)
        self.result = [file_names, tags]
        
        # print(self.result) # debug

        self.destroy()
