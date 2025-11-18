import sys
import requests
import os
from registry_client import registry_client

# URL de fallback si el registry no está disponible
API_URL = os.getenv("API_URL","http://127.0.0.1:8000")

def get_server_url():
    """Obtiene la URL de un servidor desde el registry o usa fallback"""
    try:
        server_url = registry_client.get_server_url(strategy="random")
        if server_url:
            return server_url
    except Exception as e:
        print(f"[INFO] No se pudo obtener servidor del registry: {e}")
    return API_URL

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
        print(tags_str)
        tags = tags_str.split(',')

        if not tags:
            print("[ERROR] Debes indicar al menos una etiqueta válida.")
            return
        for t in tags:
            if " " in t:
                print(f"[ERROR] La etiqueta '{t}' contiene espacios. Usa '_' en su lugar.")
                return

        for file_path in files:
            if not os.path.exists(file_path):
                print(f"[ERROR] El archivo '{file_path}' no existe.")
                continue

            with open(file_path, "rb") as f:
                try:
                    print(tags)
                    server_url = get_server_url()
                    response = requests.post(
                        f"{server_url}/add",
                        files={"file": (os.path.basename(file_path), f)},
                        data={"tags": ",".join(tags)},
                    )
                    response.raise_for_status()
                    print(f"[OK] Archivo '{file_path}' agregado correctamente.")
                except requests.RequestException as e:
                    print(f"[ERROR] No se pudo subir '{file_path}': {e}")
    # --- LIST ---
    elif command == "list":
        tag_query = sys.argv[2:] if len(sys.argv) > 2 else []
        try:
            server_url = get_server_url()
            response = requests.get(f"{server_url}/list", params=[("tags", t) for t in tag_query])
            response.raise_for_status()
            data = response.json().get("files", [])
            if not data:
                print("[INFO] No se encontraron archivos.")
            else:
                for f in data:
                    print(f"Nombre: {f['name']} | Etiquetas: {f['tags']}")
        except requests.RequestException as e:
            print(f"[ERROR] No se pudo listar archivos: {e}")

    # --- DELETE FILES ---
    elif command == "delete":
        if len(sys.argv) < 3:
            print("[ERROR] Uso: python main.py delete <etiqueta1,etiqueta2,...>")
            return

        tag_query = sys.argv[2]
        try:
            server_url = get_server_url()
            response = requests.delete(f"{server_url}/delete", params={"tags": tag_query})
            response.raise_for_status()
            msg = response.json().get("message", "")
            print(f"[INFO] {msg}")
        except requests.RequestException as e:
            print(f"[ERROR] No se pudo eliminar archivos: {e}")

    # --- ADD TAGS ---
    elif command == "add-tags":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py add-tags <tags_consulta> <nuevas_etiquetas>")
            return

        query_tags = sys.argv[2]
        new_tags = sys.argv[3]

        try:
            server_url = get_server_url()
            response = requests.post(f"{server_url}/add-tags", params={"query": query_tags, "new_tags": new_tags})
            response.raise_for_status()
            if response.json().get("success"):
                print("[OK] Etiquetas agregadas correctamente.")
            else:
                print("[INFO] No se encontraron archivos con esas etiquetas.")
        except requests.RequestException as e:
            print(f"[ERROR] No se pudieron agregar etiquetas: {e}")

    # --- DELETE TAGS ---
    elif command == "delete-tags":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py delete-tags <tags_consulta> <etiquetas_a_eliminar>")
            return

        query_tags = sys.argv[2]
        del_tags = sys.argv[3]

        try:
            server_url = get_server_url()
            response = requests.post(f"{server_url}/delete-tags", params={"query": query_tags, "del_tags": del_tags})
            response.raise_for_status()
            if response.json().get("success"):
                print("[OK] Etiquetas eliminadas correctamente.")
            else:
                print("[INFO] No se encontraron coincidencias para eliminar etiquetas.")
        except requests.RequestException as e:
            print(f"[ERROR] No se pudieron eliminar etiquetas: {e}")

    # --- RESET ---
    # elif command == "reset":
    #     confirm = input("⚠️ Esto eliminará toda la base de datos y archivos. ¿Continuar? (y/N): ").lower()
    #     if confirm == "y":
    #         database.reset_db()
    #         print("[OK] Base de datos reiniciada.")
    #     else:
    #         print("[CANCELADO] Operación abortada.")
    
    elif command == "download":
        if len(sys.argv) < 4:
            print("[ERROR] Uso: python main.py download <nombre_archivo> <carpeta_destino>")
            return

        file_name = sys.argv[2]
        dest_folder = sys.argv[3]
        os.makedirs(dest_folder, exist_ok=True)
        try:
            server_url = get_server_url()
            response = requests.get(f"{server_url}/download/{file_name}", stream=True)
            response.raise_for_status()
            path = os.path.join(dest_folder, file_name)
            with open(path, "wb") as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)
            print(f"[OK] Archivo '{file_name}' descargado en '{dest_folder}'")
        except requests.RequestException as e:
            print(f"[ERROR] No se pudo descargar '{file_name}': {e}")

    else:
        print(f"[ERROR] Comando desconocido: {command}")
        print("Comandos válidos: add, delete, list, add-tags, delete-tags, reset")

if __name__ == "__main__":
    main()