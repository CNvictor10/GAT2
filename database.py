"""Compatibilidad con el comando histórico `python database.py`."""

from database import *


if __name__ == '__main__':
    inicializar_db()
    print("\n✅ Base de datos Donglai.db lista para usar.")
    print("Para crear o restablecer tu administrador, ejecuta: python crear_admin.py")
    conn = crear_conexion()
    try:
        total = conn.execute("SELECT COUNT(*) AS total FROM inventario").fetchone()['total']
        print(f"📦 Total de productos en inventario: {total}")
    finally:
        conn.close()
