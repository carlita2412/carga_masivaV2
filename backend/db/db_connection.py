import os
import pymysql
from dotenv import load_dotenv

load_dotenv()

def get_connection(pais: str):
    db_map = {
        "vzla": os.getenv("MYSQL_DB_VZLA"),
        "colombia": os.getenv("MYSQL_DB_COL"),
        "elsalvador": os.getenv("MYSQL_DB_ES"),
    }

    db_name = db_map.get(pais)
    if not db_name:
        raise ValueError("País no válido")

    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=db_name,
        port=int(os.getenv("MYSQL_PORT", 33066)),  # <- Asegúrate que este sea 33066
        cursorclass=pymysql.cursors.Cursor
    )
