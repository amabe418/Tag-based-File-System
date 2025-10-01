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
    elif command == "delete":
        tag_query = sys.argv[2:]
        manager.delete_files(tag_query)
    elif command == "add-tags":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py add-tags <tags_consulta> <nuevas_etiquetas>")
            exit()

        query_tags = sys.argv[2].split(",")
        new_tags = sys.argv[3].split(",")

        manager.add_tags(query_tags, new_tags)
    elif command == "delete-tags":
        if len(sys.argv) < 4:
            print("[ERROR] Debes indicar etiquetas para eliminar.")
        else:
            query_tags = sys.argv[2].split(",")
            del_tags_str = " ".join(sys.argv[3:]).strip()
            del_tags = [t.strip() for t in del_tags_str.split(",")]

            # Validación de etiquetas
            for t in del_tags:
                if " " in t:
                    print(f"[ERROR] La etiqueta '{t}' contiene espacios. Usa guion bajo '_' en lugar de espacios.")
                    exit()

            manager.delete_tags(query_tags, del_tags)

    elif command == "reset":
        database.reset_db()
    else:
        print(f"[ERROR] las funciones permitidas son add, delete, list, add-tags, delete-tags")
        exit()



    
if __name__ == "__main__":
    database.init_db() # Inicializamos la bd antes de ejercer cualquier accion
    if not sys.argv[1:]:
        root = tk.Tk()
        root.title("Tag-Based File System")
        app = MainWindow(root)
        root.mainloop()
    else:
        main()
