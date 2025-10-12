import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from core import manager  # nuestro backend centralizado
from gui.add_files_dialog import AddFileDialog
from gui.list_files_dialog import ListFilesDialog
from gui.add_tags_dialog import AddTagsDialog
from gui.delete_tags_dialog import DeleteTagsDialog 
from gui.delete_files_dialog import DeleteFilesDialog 
import requests
import os


API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")

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

        btn_list = tk.Button(frame_buttons, text="📖 Listar archivos", command=self.list_files)
        btn_list.pack(side=tk.LEFT, padx=5)

        btn_list = tk.Button(frame_buttons, text="🔖➕ Agregar etiqueta(s)",command=self.add_tags)
        btn_list.pack(side=tk.LEFT, padx=5)

        btn_add = tk.Button(frame_buttons, text="🔖❌ Eliminar etiqueta(s)", command=self.delete_tags)
        btn_add.pack(side=tk.LEFT, padx=5)

        btn_add = tk.Button(frame_buttons, text="🗑️ Eliminar archivo(s)", command=self.delete_files)
        btn_add.pack(side=tk.LEFT, padx=5)

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

        # Menú contextual (click derecho)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="⬇️ Descargar archivo", command=self.download_selected_file)

        # Asociamos click derecho al treeview
        self.tree.bind("<Button-3>", self.show_context_menu)

        # Asociamos click izquierdo global para cerrar el menú si está abierto
        self.bind_all("<Button-1>", self.hide_context_menu)

        # Mostramos todos los archivos
        self.refresh_list()
    
    # region Utility

    # Mostrar la opcion para descargar el archivo al hacer click derecho
    def show_context_menu(self, event):
        # Selecciona la fila donde se hizo click derecho
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)  # selecciona la fila
            self.menu.post(event.x_root, event.y_root)
    
    # Esconder la opcion para descargar un archivo al hacer click izquierdo
    def hide_context_menu(self, event):
        """Cierra el menú contextual solo si el click no fue dentro del menú."""
        if event.widget != self.menu:
            try:
                self.menu.unpost()
            except:
                pass

    def download_selected_file(self):
        """
        Lógica para descargar el archivo seleccionado en el treeview.
        """
        selected = self.tree.selection()
        if not selected:
            return

        # Obtén los valores de la fila seleccionada
        values = self.tree.item(selected[0], "values")
        file_name = values[0]   # columna "path"
        tags = values[1]        # columna "tags"

        # Abrir cuadro para seleccionar directorio de destino
        dest_dir = filedialog.askdirectory(title="Seleccionar carpeta de destino", parent=self)
        if not dest_dir:
            return  # usuario canceló

        # Lógica de descarga (ejemplo: copiar archivo desde el manager)
        res = manager.download_file(file_name, dest_dir)  # este método deberías implementarlo en tu backend
        if res:
            messagebox.showinfo("Éxito", f"Archivo '{file_name}' descargado en:\n{dest_dir}")
        else:
            messagebox.showerror("Error", f"No se pudo descargar el archivo '{file_name}'")

    def refresh_list(self, files=None):
        """
        Refresca la tabla para mostrar todos los ficheros desde la API.
        """
        # Limpia la tabla
        for row in self.tree.get_children():
            self.tree.delete(row)

        try:
            if not files:
                # Consultar todos los archivos al backend
                response = requests.get(f"{API_URL}/list")
                response.raise_for_status()
                data = response.json()
                files = data.get("files", [])
            
            # Insertar cada archivo en la tabla
            for f in files:
                name = f.get("name", "Desconocido")
                tags = f.get("tags")
                self.tree.insert("", "end", values=(name, tags))

        except requests.RequestException as e:
            messagebox.showerror("Error", f"No se pudo obtener la lista de archivos:\n{e}")

    # region Main Functions
    def add_files(self):
        """
        Envía los archivos seleccionados y sus etiquetas al backend (API FastAPI).
        """
        dialog = AddFileDialog(self)
        self.wait_window(dialog)

        if dialog.result:
            file_names, tags = dialog.result

            for file_path in file_names:
                try:
                    with open(file_path, "rb") as f:
                        files = {"file": (file_path.split("/")[-1], f, "application/octet-stream")}
                        # print(files)
                        data = {"tags": ",".join(tags)}
                        # print(data)

                        response = requests.post(f"{API_URL}/add", files=files, data=data)
                        response.raise_for_status()

                        messagebox.showinfo("Éxito", f"Archivo '{file_path}' agregado con etiquetas: {', '.join(tags)}.")
                except requests.RequestException as e:
                    messagebox.showerror("Error", f"No se pudo subir '{file_path}': {e}")
                except Exception as e:
                    messagebox.showerror("Error", f"Ocurrió un error: {e}")

        self.refresh_list()

    
    def add_tags(self):
        dialog = AddTagsDialog(self)
        self.wait_window(dialog)
        
        if dialog.result:
            query_tags, new_tags = dialog.result
            params = {"query":",".join(query_tags), "new_tags":",".join(new_tags)}
            print(params)
            print(f"Las etiquetas a buscar son {query_tags}")
            print(f"Las etiquetas a annadir son {new_tags}")
            # res = manager.add_tags(query_tags, new_tags)
            try:
                response = requests.post(f"{API_URL}/add-tags",params=params)
                response.raise_for_status()
                data = response.json()
                if data.get("success"):
                    messagebox.showinfo("Éxito", "Las nuevas etiquetas se agregaron correctamente a los archivos cuyas etiquetas se corresponden con su criterio de búsqueda.")
                else:
                    messagebox.showinfo("Fracaso", "No se encontraron archivos que coincidan con su criterio de búsqueda.")
            except requests.RequestException as e:
                messagebox.showerror("Error", f"Ocurrió un error al comunicarse con el servidor:\n{e}.")

        self.refresh_list()
    
    def delete_tags(self):
        dialog = DeleteTagsDialog(self)
        self.wait_window(dialog)

        if dialog.result:
            tag_query, tag_list = dialog.result
            params = {"query": ','.join(tag_query), "del_tags": ','.join(tag_list)}
            print(f"las etiquetas a buscar son {tag_query}")
            print(f"las etiquetas a eliminar son {tag_list}")
            # res = manager.delete_tags(tag_query,tag_list)
            try:
                response = requests.post(f"{API_URL}/delete-tags",params=params)
                response.raise_for_status()
                data = response.json()
                if data.get("success"):
                    messagebox.showinfo("Éxito","Las etiquetas se eliminaron correctamente sobre los archivos cuyas etiquetas se corresponden con su criterio de búsqueda.")
                else:
                    messagebox.showinfo("Fracaso","No existe ningún archivo que coincida con su criterio de búsqueda.")
            except requests.RequestException as e:
                messagebox.showerror("Error", f"Ocurrió un error al comunicarse con el servidor:\n{e}.")

        self.refresh_list()

    def delete_files(self):
        dialog = DeleteFilesDialog(self)
        self.wait_window(dialog)

        if dialog.result:
            print(f"Resultado de borrar {dialog.result}")
            tag_query = dialog.result
            params = {"tags": ','.join(tag_query)}
            try:
                # res = manager.delete_files(tag_query)
                response = requests.delete(f"{API_URL}/delete",params=params)
                response.raise_for_status()
                data = response.json()
                if data.get("success"):
                    messagebox.showinfo("Éxito","Los archivos cuyas etiquetas coinciden su criterio de búsqueda fueron eliminados.")
                else:
                    messagebox.showinfo("Fracaso","No existe ningún archivo que coincida con su criterio de búsqueda.")
            except requests.RequestException as e:
                messagebox.showerror("Error", f"Ocurrió un error al comunicarse con el servidor:\n{e}.")
                print(e)


        self.refresh_list()
        
    def list_files(self):
        dialog = ListFilesDialog(self)
        self.wait_window(dialog)
        params = {}
        
        if dialog.result is not None:  # el usuario no canceló
            tags = dialog.result
            params["tags"] = tags

            try:
                response = requests.get(f"{API_URL}/list", params)
                response.raise_for_status()
                data = response.json()
                print(data)
                print(len(data['files']))
                
            except requests.exceptions.RequestException as e:
                print(f"[ERROR] No se pudo obtener los archivos {e}")

            if tags: # Se hizo una busqueda por etiquetas
                head_message = "Archivos filtrados:"
                body = f"se encontrarion {len(data['files'])}"
                tail_message = " con esas etiquetas."
            else: # No se especificaron etiquetas para hacer la busqueda
                head_message = "Mostrando todos los archivos:"
                body = f" hay un total de {len(data['files'])}"
                tail_message = "."

            # Mostramos el mensaje de exito o fracaso y refrescamos lista en la ventana principal para mostrar resultados 
            if len(data['files']):
                messagebox.showinfo("Mostrando Archivos",message=head_message + body + tail_message)
                self.refresh_list(data['files'])
            elif not len(data['files']) and not tags:
                messagebox.showinfo(":(","No hay archivos en el sistema.")
            elif not len(data['files']):
                messagebox.showinfo(":(", "No se encontraron archivos con las etiquetas especificadas.")
            