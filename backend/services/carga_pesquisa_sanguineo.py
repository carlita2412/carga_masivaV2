#car
from datetime import datetime
from backend.db.db_connection import get_connection
from backend.db.utils import obtener_persona_id_existente
import pandas as pd

def procesar_excel_pesquisa_sanguineo(df: pd.DataFrame, pais: str, actividad: str, destino_id: int):
    conn = get_connection(pais)
    cursor = conn.cursor()

    resultados = {
        "pesquisas": [],
        "errores": []
    }

    for _, row in df.iterrows():
        id_digisalud = str(row.get("Id_digisalud")).strip()
        if not id_digisalud:
            # Si no hay ID de beneficiario, omitir esta fila
            continue

        # Buscar persona_id existente por ID Digisalud
        persona_id = obtener_persona_id_existente(cursor, id_digisalud)
        if not persona_id:
            resultados["errores"].append(f"No existe beneficiario con ID {id_digisalud}")
            continue

        # Verificar si el beneficiario está cargado en el centro/jornada destino
        campo_id = f"{actividad}_id"
        tabla_asociacion = f"psi_pacientes_x_{actividad}s"
        cursor.execute(
            f"SELECT 1 FROM {tabla_asociacion} WHERE persona_id = %s AND {campo_id} = %s",
            (persona_id, destino_id)
        )
        if not cursor.fetchone():
            resultados["errores"].append(f"Beneficiario {id_digisalud} no está cargado en {actividad} {destino_id}")
            continue

        # Obtener y validar la fecha de evaluación
        fecha_valor = row.get("Fecha Eval. DD/MM/AAAA")
        if pd.isna(fecha_valor) or fecha_valor is None or (isinstance(fecha_valor, str) and fecha_valor.strip() == ""):
            resultados["errores"].append(f"Fecha de evaluación faltante o inválida para {id_digisalud}")
            continue
        if isinstance(fecha_valor, str):
            try:
                fecha = datetime.strptime(fecha_valor.strip(), "%d/%m/%Y").date()
            except Exception:
                resultados["errores"].append(f"Fecha inválida para {id_digisalud}: {fecha_valor}")
                continue
        elif isinstance(fecha_valor, datetime):
            fecha = fecha_valor.date()
        elif hasattr(fecha_valor, "to_pydatetime"):
            try:
                fecha = fecha_valor.to_pydatetime().date()
            except Exception:
                resultados["errores"].append(f"Fecha inválida para {id_digisalud}: {fecha_valor}")
                continue
        else:
            resultados["errores"].append(f"Fecha inválida para {id_digisalud}: {fecha_valor}")
            continue

        # Preparar los valores de pesquisas sanguíneas (hemoglobina y glucosa)  PASO 2 AGREGAR NUEVA COLUMNA
        for tipo, campo, tipo_id in [
            ("hemoglobina", "HEMOGLOBINA", 3),
            ("glucosa", "GLUCOSA", 241),
            ("hematocrito", "HEMATOCRITO", 165),
            ("globulos blancos", "GLOBULOS BLANCOS", 166),
            ("plaquetas", "PLAQUETAS", 167),
        ]:
            valor = row.get(campo)
            if pd.notna(valor):
                resultados["pesquisas"].append({
                    "persona_id": persona_id,
                    campo_id: destino_id,
                    "tipo_pesquisa_id": tipo_id,
                    "pesquisa_valor": valor,
                    "fecha": fecha
                })

    cursor.close()
    conn.close()
    return resultados
