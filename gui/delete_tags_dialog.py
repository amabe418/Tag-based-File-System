import tkinter as tk
from tkinter import filedialog, messagebox
import os

class DeleteTagsDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Eliminar etiqueta(s)")
        self.result = None

        # Etiquetas de archivos que desea añadir las nuevas etiquetas
        tk.Label(self, text="               Etiquetas que se desean eliminar (separadas por coma)\nEj: etiqueta1, etiqueta2, ...").grid(row=0, column=0, sticky="nw", padx=5, pady=5)
        self.tag_list = tk.Entry(self, width=40)
        self.tag_list.grid(row=0, column=1, columnspan=2, padx=5, pady=5)

        # Etiquetas a añadir
        tk.Label(self, text="               Etiqueta(s) de archivos sobre los que se realizará la operación (separadas por coma)\nEj: etiqueta1, etiqueta2, ...").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.tag_query = tk.Entry(self, width=40)
        self.tag_query.grid(row=1, column=1, columnspan=2, padx=5, pady=5)

        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=3, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=3, column=2, pady=10)

        self.grab_set()  # modal

    def on_ok(self):
        if not self.tag_list.get().strip():
            self.lift()
            messagebox.showwarning("Error","Debe escribir al menos una etiqueta a eliminar.",parent=self)
            return
        
        if not self.tag_query.get().strip():
            self.lift()
            messagebox.showwarning("Error","Debe introducir al menos una etiqueta para realizar la búsqueda.",parent=self)
            return
        
        
        # print(f"Las etiquetas a buscar son: {self.tag_query.get()}")
        # print(f"Las etiquetas a eliminar son: {self.tag_list.get()}")
        # exit()
        tag_query = [tag.strip() for tag in self.tag_query.get().split(",")]
        # print(f"Las etiquetas a buscar son: {tag_query}")


        tag_list = [tag.strip() for tag in self.tag_list.get().split(",")]
        # print(f"Las etiquetas a eliminar son: {tag_list}")
        
        self.result = [tag_query, tag_list]
        # print(f"Result: {self.result}")
        self.destroy()
