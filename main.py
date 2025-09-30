# main.py 
import sys
from core import manager
from core import database 
import tkinter as tk
from gui.main_window import MainWindow

def main():
    command = sys.argv[1]    
    if command == "add":
        files = sys.argv[2].split(",")

        if len(sys.argv) < 4:
            print("[ERROR] Debes indicar al menos una etiqueta separada por coma.")
        else:
            # Tomamos todo lo que venga después de los archivos como un solo string
            tags_str = " ".join(sys.argv[3:]).strip()
            print (tags_str)
            # Verificar que cada etiqueta, excepto la última, termine en coma
            etiquetas = [t.strip() for t in tags_str.split(",")]
            for t in etiquetas[:-1]:
                if not t:
                    print("[ERROR] Etiqueta vacía detectada antes de la última etiqueta.")
                    exit()

            # Verificación: si hay espacios dentro de cada etiqueta, lanzar error
            for t in etiquetas:
                if " " in t:
                    print(f"[ERROR] La etiqueta '{t}' contiene espacios. Usa guion bajo '_' en lugar de espacios.")
                    exit()

            manager.add_files(files, etiquetas)

    elif command == "list":
        tag_query = sys.argv[2:]
        manager.list_files(tag_query)

    elif command == "reset":
        print("Base de datos reseteada.")
        database.reset_db()

def launch_gui():
    root = tk.Tk()
    root.title("Tag-Based File System")
    app = MainWindow(root)
    root.geometry("1300x600")
    root.mainloop()

if __name__ == "__main__":
    database.init_db() # Inicializamos la bd antes de ejercer cualquier accion
    if not sys.argv[1:]:
        launch_gui()
    else:
        main()
