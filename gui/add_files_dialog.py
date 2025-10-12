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

        # Botón para explorar archivos
        tk.Button(self, text="📁 Explorar...", command=self.browse_files).grid(row=0, column=1, padx=5, pady=5)

        # Cuadro de entrada del conjunto de archivos
        tk.Label(self, text="Nombre(s) de archivo(s) (separados por coma)\nEj: archivo1, archivo2, ...").grid(row=1, column=0, sticky="nw", padx=5, pady=5)
        self.files_entry = tk.Entry(self, width=40)
        self.files_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=5)

        # Etiquetas
        tk.Label(self, text="Etiqueta(s) (separadas por coma)\n                     Ej: etiqueta1, etiqueta2, ...").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.tags_entry = tk.Entry(self, width=40)
        self.tags_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=5)

        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=3, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=3, column=2, pady=10)

        self.grab_set()  # modal

    
    def browse_files(self):
        # Cuadro de diálogo para seleccionar varios archivos
        filenames = filedialog.askopenfilenames(
        parent=self,
        title="Seleccionar archivos",
        initialdir=os.getcwd(),  # carpeta inicial del programa
        filetypes=(("Todos los archivos", "*.*"),)
        )
        if filenames:
            # Insertar los nombres en el entry (separados por coma)
            self.files_entry.delete(0, tk.END)
            self.files_entry.insert(0, ", ".join(filenames))

    def on_ok(self):
        if not self.files_entry.get().strip():
            self.lift()
            messagebox.showwarning("Faltan archivos", "Debe insertar al menos un archivo.",parent=self)
            return

        if not self.tags_entry.get().strip():
            self.lift()
            messagebox.showwarning("Faltan datos", "Debe escribir al menos una etiqueta.",parent=self)
            return
        
        file_names = [fn.strip() for fn in self.files_entry.get().split(",")]
        print(f"Nombres de los archivos: {file_names}")
        tags = self.tags_entry.get().strip().split(",")
        print(f"Tags: {tags}")
        
        # # Guardar resultado: lista de tuplas (nombre del archivo completo, tags)
        self.result = [file_names, tags]
        
        # print(self.result) # debug

        self.destroy()
