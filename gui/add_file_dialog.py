import tkinter as tk
from tkinter import filedialog, messagebox

class AddFileDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Agregar archivo")
        self.result = None

        # Nombre del archivo
        tk.Label(self, text="Nombre:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.name_entry = tk.Entry(self, width=40)
        self.name_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=5)

        # Etiquetas
        tk.Label(self, text="Etiquetas (separadas por coma)\nEj: etiqueta1, etiqueta2, ...").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.tags_entry = tk.Entry(self, width=40)
        self.tags_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=5)

        # Botones
        tk.Button(self, text="Aceptar", command=self.on_ok).grid(row=3, column=1, pady=10)
        tk.Button(self, text="Cancelar", command=self.destroy).grid(row=3, column=2, pady=10)

        self.grab_set()  # Bloquea ventana principal hasta cerrar este dialog

    def choose_file(self):
        filepath = filedialog.askopenfilename()
        if filepath:
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, filepath)

    def on_ok(self):
        name = self.name_entry.get().strip()
        tags = self.tags_entry.get().strip()

        if not name:
            messagebox.showwarning("Faltan datos", "Debe escribir un nombre.")
            return
        if not tags:
            messagebox.showwarning("Faltan datos", "Debe escribir al menos una etiqueta.")
            return

        self.result = (name, [t.strip() for t in tags.split(",") if t.strip()])
        self.destroy()
