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
        """
        Crea todos los botones y la tabla de la interfaz.
        """
        # Marco donde agregaremos los botones
        frame_buttons = tk.Frame(self)
        frame_buttons.pack(pady=5)

        # Creamos los botones y dentro del marco.
        btn_add = tk.Button(frame_buttons, text="➕ Agregar archivo(s)", command=self.add_files)
        btn_add.pack(side=tk.LEFT, padx=5)

        btn_list = tk.Button(frame_buttons, text="📖 Listar archivos", command=self.refresh_list)
        btn_list.pack(side=tk.LEFT, padx=5)

        tree_frame = tk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Estilicemos la lista de archivos
        style = ttk.Style()
        style.configure("Treeview",rowheight=50, font=("Arial",11))

        # Creamos la lista de archivos
        self.tree = ttk.Treeview(tree_frame, columns=("path", "tags"), show="headings")
        self.tree.heading("path", text="Nombre")
        self.tree.heading("tags", text="Etiquetas")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)
        self.refresh_list()

    def add_files(self):
        """
        Agrega el conjunto de archivos a la base de datos.
        """
        dialog = AddFileDialog(self)
        self.wait_window(dialog)  # espera hasta que se cierre el diálogo

        ## Debugging
        # l = dialog.result
        # print(l)

        if dialog.result:
            file_names, tags = dialog.result
            print(dialog.result)
            manager.add_files(file_names,tags)  # guardamos los archivos
            messagebox.showinfo("Éxito", f"Archivo(s): '{', '.join(file_names)}' \n agregado con etiquetas: {', '.join(tags)}.")
        
        self.refresh_list() # mostramos todos los archivos
    
    def refresh_list(self):
        """
        Refresca la tabla que muestra todos los ficheros.
        """
        # Limpia la tabla
        for row in self.tree.get_children():
            self.tree.delete(row)

        # Obtiene archivos del manager
        files = manager.list_files("")
        for _, name, tags in files:
            self.tree.insert("","end",values=([name],",".join([tags])))
