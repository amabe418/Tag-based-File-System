# fs_tags.py (simplificado)
import sys
from core import manager
from core import database 

def main():
    database.init_db()
    command = sys.argv[1]
    
    if command == "add":
        files = sys.argv[2].split(",")
        tags = sys.argv[3].split(",")
        manager.add_files(files, tags)
    
    elif command == "list":
        tag_query = sys.argv[2:]
        manager.list_files(tag_query)
    
if __name__ == "__main__":
    main()
