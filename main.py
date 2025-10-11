# main.py 
import sys
from core import manager
from core import database 
import tkinter as tk
from gui.main_window import MainWindow

def main():
    if len(sys.argv) < 2:
        print("[ERROR] Debes indicar un comando: add, delete, list, add-tags, delete-tags, reset")
        return

    command = sys.argv[1].strip().lower()
    print(command)

    # --- ADD ---
    if command == "add":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py add <archivo1,archivo2,...> <etiqueta1,etiqueta2,...>")
            return

        files = sys.argv[2].split(",")
        tags_str = " ".join(sys.argv[3:]).strip()
        etiquetas = [t.strip() for t in tags_str.split(",") if t.strip()]

        # Validaciones
        if not etiquetas:
            print("[ERROR] Debes indicar al menos una etiqueta válida.")
            return
        for t in etiquetas:
            if " " in t:
                print(f"[ERROR] La etiqueta '{t}' contiene espacios. Usa '_' en su lugar.")
                return

        print(f"[DEBUG] Archivos: {files}")
        print(f"[DEBUG] Etiquetas: {etiquetas}")

        success = manager.add_files(files, etiquetas)
        if success:
            print("[OK] Archivos agregados correctamente.")
        else:
            print("[INFO] No se agregaron archivos (ver mensajes anteriores).")

    # --- LIST ---
    elif command == "list":
        tag_query = sys.argv[2:] if len(sys.argv) > 2 else []
        manager.list_files(tag_query)

    # --- DELETE FILES ---
    elif command == "delete":
        if len(sys.argv) < 3:
            print("[ERROR] Uso: python main.py delete <etiqueta1,etiqueta2,...>")
            return

        tag_query = sys.argv[2].split(",")
        success = manager.delete_files(tag_query)
        if success:
            print("[OK] Archivos eliminados correctamente.")
        else:
            print("[INFO] No se encontraron archivos con esas etiquetas.")

    # --- ADD TAGS ---
    elif command == "add-tags":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py add-tags <tags_consulta> <nuevas_etiquetas>")
            return

        query_tags = sys.argv[2].split(",")
        new_tags = sys.argv[3].split(",")

        success = manager.add_tags(query_tags, new_tags)
        if success:
            print("[OK] Etiquetas agregadas correctamente.")
        else:
            print("[INFO] No se encontraron archivos con esas etiquetas.")

    # --- DELETE TAGS ---
    elif command == "delete-tags":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py delete-tags <tags_consulta> <etiquetas_a_eliminar>")
            return

        query_tags = sys.argv[2].split(",")
        del_tags_str = " ".join(sys.argv[3:]).strip()
        del_tags = [t.strip() for t in del_tags_str.split(",") if t.strip()]

        for t in del_tags:
            if " " in t:
                print(f"[ERROR] La etiqueta '{t}' contiene espacios. Usa '_' en su lugar.")
                return

        success = manager.delete_tags(query_tags, del_tags)
        if success:
            print("[OK] Etiquetas eliminadas correctamente.")
        else:
            print("[INFO] No se encontraron coincidencias para eliminar etiquetas.")

    # --- RESET ---
    elif command == "reset":
        confirm = input("⚠️ Esto eliminará toda la base de datos y archivos. ¿Continuar? (y/N): ").lower()
        if confirm == "y":
            database.reset_db()
            print("[OK] Base de datos reiniciada.")
        else:
            print("[CANCELADO] Operación abortada.")
    
    elif command == "download":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py download <nombre_archivo> <carpeta_destino>")
            return
        file_name = sys.argv[2]
        destination_folder = sys.argv[3]
        success = manager.download_file(file_name, destination_folder)
        if not success:
            print("[INFO] No se pudo completar la descarga.")


    else:
        print(f"[ERROR] Comando desconocido: {command}")
        print("Comandos válidos: add, delete, list, add-tags, delete-tags, reset")



    
if __name__ == "__main__":
    database.init_db() # Inicializamos la bd antes de ejercer cualquier accion
    if not sys.argv[1:]:
        root = tk.Tk()
        root.title("Tag-Based File System")
        app = MainWindow(root)
        root.mainloop()
    else:
        main()
