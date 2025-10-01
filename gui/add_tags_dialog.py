import tkinter as tk
from tkinter import filedialog, messagebox
import os

class AddTagsDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Agregar etiqueta(s)")
        self.result = None

        # Etiquetas a añadir
        tk.Label(self, text="               Etiqueta(s) a añadir (separadas por coma)\nEj: etiqueta1, etiqueta2, ...").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.new_tags = tk.Entry(self, width=40)
        self.new_tags.grid(row=0, column=1, columnspan=2, padx=5, pady=5)

        # Etiquetas de archivos que desea añadir las nuevas etiquetas
        tk.Label(self, text="Etiquetas de los archivos sobre los que se realizará la operación").grid(row=1, column=0, sticky="nw", padx=5, pady=5)
        self.query_tags = tk.Entry(self, width=40)
        self.query_tags.grid(row=1, column=1, columnspan=2, padx=5, pady=5)
        
        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=3, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=3, column=2, pady=10)

        self.grab_set()  # modal

    def on_ok(self):
        if not self.new_tags.get().strip():
            self.lift()
            messagebox.showwarning("Error","Debe introducir al menos una etiqueta a añadir.",parent=self)
            return
        
        if not self.query_tags.get().strip():
            self.lift()
            messagebox.showwarning("Error","Debe escribir al menos una etiqueta para realizar la búsqueda.",parent=self)
            return
        
        new_tags = [tag.strip() for tag in self.new_tags.get().split(",")]
        print(f"Las etiquetas a agregar son: {new_tags}")


        query_tags = [tag.strip() for tag in self.query_tags.get().split(",")]
        
        self.result = [query_tags, new_tags]
        self.destroy()
