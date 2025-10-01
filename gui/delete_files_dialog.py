import tkinter as tk
from tkinter import messagebox

class DeleteFilesDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Eliminar archivos por etiquetas")
        self.result = None

        # Etiquetas
        tk.Label(self, text="Etiquetas de los archivos a eliminar (separadas por coma)").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.tag_query = tk.Entry(self, width=40)
        self.tag_query.grid(row=0, column=1, columnspan=2, padx=5, pady=5)

        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=1, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=1, column=2, pady=10)

        self.grab_set()  # modal

    def on_ok(self):
        tags = self.tag_query.get().strip()
        if not len(tags):
            print("no  hubo tags")
            self.lift()
            messagebox.showwarning("Error", "Debe introducir al menos una etiqueta para realizar la búsqueda.",parent=self)
        else:
            print("hubo tags")
            self.result = tags.split(",")   

        self.destroy()
