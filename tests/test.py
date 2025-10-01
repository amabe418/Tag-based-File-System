import os
import unittest
import sqlite3
from core.manager import add_files, query_files, delete_files, delete_tags, add_tags
from core.database import init_db, get_connection, close_connection

TEST_DB_PATH = "database/test_db.db"

class TestFileTagSystem(unittest.TestCase):

    def setUp(self):
        """
        Se ejecuta antes de cada prueba.
        Crea una nueva base de datos limpia.
        """
        # Si ya existe una base de datos de prueba → borrar
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

        # Inicializar nueva BD vacía
        init_db(TEST_DB_PATH)

    def tearDown(self):
        """
        Se ejecuta después de cada prueba.
        Elimina la base de datos de prueba.
        """
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def test_add_files(self):
        """
        Verifica que agregar archivos funciona correctamente.
        """
        add_files(["file1", "file2"], ["tag1", "tag2"], db_path=TEST_DB_PATH)

        results = query_files([], db_path=TEST_DB_PATH)
        file_names = [r[1] for r in results]

        self.assertIn("file1", file_names)
        self.assertIn("file2", file_names)

    def test_list_files_by_tag(self):
        """
        Verifica que la consulta por etiquetas funciona.
        """
        add_files(["file1"], ["tag1", "tag2"], db_path=TEST_DB_PATH)
        add_files(["file2"], ["tag2"], db_path=TEST_DB_PATH)

        # Buscar archivos que tengan ambas etiquetas (tag1 y tag2)
        results = query_files(["tag1", "tag2"], db_path=TEST_DB_PATH)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "file1")

        # Buscar archivos que tengan solo tag2
        results = query_files(["tag2"], db_path=TEST_DB_PATH)
        file_names = [r[1] for r in results]
        self.assertIn("file1", file_names)
        self.assertIn("file2", file_names)

    def test_delete_files(self):
        """
        Verifica que eliminar archivos funciona.
        """
        add_files(["file1", "file2"], ["tag1"], db_path=TEST_DB_PATH)

        # Verificar que hay 2 archivos
        results = query_files([], db_path=TEST_DB_PATH)
        self.assertEqual(len(results), 2)

        # Borrar archivo con tag1
        delete_files(["tag1"], db_path=TEST_DB_PATH)

        # Verificar que la tabla de archivos está vacía
        results = query_files([], db_path=TEST_DB_PATH)
        self.assertEqual(len(results), 0)

    def test_add_tags(self):
        """Verifica que se agreguen etiquetas correctamente."""
        # 1. Agregar archivo con etiquetas iniciales
        add_files(["file1"], ["tag1"], db_path=TEST_DB_PATH)

        # 2. Agregar nuevas etiquetas
        add_tags(["tag1"], ["tag2", "tag3"], db_path=TEST_DB_PATH)

        results = query_files(["tag1"], db_path=TEST_DB_PATH)
        self.assertEqual(len(results), 1)

        file_id, name, tags = results[0]
        tags_set = set(tags.split(","))
        self.assertTrue({"tag1", "tag2", "tag3"}.issubset(tags_set))

        # 3. Corner case: intentar agregar etiqueta duplicada
        add_tags(["tag1"], ["tag2"], db_path=TEST_DB_PATH)
        results = query_files(["tag1"], db_path=TEST_DB_PATH)
        file_id, name, tags = results[0]
        tags_set = set(tags.split(","))
        self.assertEqual(tags_set, {"tag1", "tag2", "tag3"})  # no se duplican

    def test_delete_tags(self):
        """Verifica que se eliminen etiquetas correctamente."""
        add_files(["file1"], ["tag1", "tag2", "tag3"], db_path=TEST_DB_PATH)

        # 1. Eliminar una etiqueta
        delete_tags(["tag1"], ["tag2"], db_path=TEST_DB_PATH)
        results = query_files(["tag1"], db_path=TEST_DB_PATH)
        file_id, name, tags = results[0]
        tags_set = set(tags.split(","))
        self.assertEqual(tags_set, {"tag1", "tag3"})

        # 2. Eliminar varias etiquetas
        delete_tags(["tag1"], ["tag1", "tag3"], db_path=TEST_DB_PATH)
        results = query_files([], db_path=TEST_DB_PATH)  # consulta vacía = todos
        file_id, name, tags = results[0]
        self.assertIsNone(tags)  # ya no tiene etiquetas

        # 3. Corner case: eliminar etiqueta inexistente (no debería fallar)
        delete_tags([], ["no_existe"], db_path=TEST_DB_PATH)
        results = query_files([], db_path=TEST_DB_PATH)
        self.assertEqual(len(results), 1)

        # 4. Corner case: eliminar etiquetas de archivos que no cumplen query
        delete_tags(["no_existe"], ["tagX"], db_path=TEST_DB_PATH)  # no debería afectar nada
        results = query_files([], db_path=TEST_DB_PATH)
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
