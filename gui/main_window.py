import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from core import manager  # nuestro backend centralizado
from gui.add_file_dialog import AddFileDialog

class MainWindow(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.pack(fill="both", expand=True)
        self.create_widgets()

    def create_widgets(self):
        # Marco donde agregaremos los botones
        frame_buttons = tk.Frame(self)
        frame_buttons.pack(pady=5)

        # Creamos los botones y dentro del marco.
        btn_add = tk.Button(frame_buttons, text="➕ Agregar archivo", command=self.add_file)
        btn_add.pack(side=tk.LEFT, padx=5)

        btn_list = tk.Button(frame_buttons, text="📖 Listar archivos", command=self.add_file)
        btn_list.pack(side=tk.LEFT, padx=5)

        # Lista de archivos
        self.tree = ttk.Treeview(self, columns=("path", "tags"), show="headings")
        self.tree.heading("path", text="Ruta")
        self.tree.heading("tags", text="Etiquetas")
        self.tree.pack(fill="both", expand=True, padx=5, pady=5)

        self.refresh_list()

    def add_file(self):
        dialog = AddFileDialog(self)
        self.wait_window(dialog)  # espera hasta que se cierre el diálogo

        l = dialog.result
        print(l)
        if dialog.result:
            name, tags = dialog.result
            manager.add_file(tags)  # aquí puedes guardar también el nombre si lo necesitas
            messagebox.showinfo("Éxito", f"Archivo '{name}' agregado con etiquetas: {', '.join(tags)}.")
            self.refresh_list()


    def refresh_list(self):
    #     # Limpia la tabla
    #     for row in self.tree.get_children():
    #         self.tree.delete(row)
    #     # Obtiene archivos del manager
    #     files = manager.list_files()
    #     for f in files:
    #         self.tree.insert("", "end", values=(f["path"], ",".join(f["tags"])))
        pass
