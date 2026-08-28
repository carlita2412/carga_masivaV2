import pandas as pd
from datetime import datetime
from backend.db.db_connection import get_connection
from backend.db.utils import obtener_persona_id_existente
from backend.services.carga_pesquisas import normalizar_id_digisalud_manual, limpiar_valor_excel


def procesar_excel_vitales(df: pd.DataFrame, pais: str, actividad: str, destino_id: int):
    """
    Procesa el Excel de signos vitales y devuelve la lista de pesquisas a insertar.
    """
    conn = get_connection(pais)
    cursor = conn.cursor()

    resultados = {
        "pesquisas": [],
        "errores": []
    }

    # Mapeo: nombre columna Excel -> tipo_pesquisa_id
    VITALES_MAP = {
        "TEMPERATURA": 204,
        "TA_SISTOLICA": 205,
        "TA_DIASTOLICA": 206,
        "FRECUENCIA_CARDIACA": 207,
        "FRECUENCIA_RESPIRATORIA": 208,
        "SATURACION_OXIGENO": 2200,
    }

    try:
        df = df.dropna(how='all').reset_index(drop=True)

        for i, row in df.iterrows():
            fila_excel = i + 2

            # Obtener id_digisalud
            id_digisalud_raw = str(row.get("id_digisalud", "")).strip()
            id_digisalud = normalizar_id_digisalud_manual(id_digisalud_raw)

            if not id_digisalud:
                resultados["errores"].append(
                    f"Fila {fila_excel}: Falta id_digisalud."
                )
                continue

            # Buscar persona
            cursor.execute("""
                SELECT persona_id, persona_nombre, persona_apellido
                FROM psi_personas
                WHERE id_digisalud = %s
            """, (id_digisalud,))
            persona = cursor.fetchone()

            if not persona:
                resultados["errores"].append(
                    f"Fila {fila_excel}: No existe persona con id_digisalud '{id_digisalud}'."
                )
                continue

            persona_id, persona_nombre, persona_apellido = persona

            # Verificar asociación a actividad
            if actividad == "jornada":
                tabla = "psi_pacientes_x_jornada"
                id_campo = "jornada_id"
            else:
                tabla = "psi_pacientes_x_centros"
                id_campo = "centro_id"

            cursor.execute(
                f"SELECT {id_campo} FROM {tabla} WHERE persona_id = %s",
                (persona_id,)
            )
            asociaciones = cursor.fetchall()

            ids_asociados = []
            for a in asociaciones:
                try:
                    ids_asociados.append(int(a[0]))
                except Exception:
                    pass

            if int(destino_id) not in ids_asociados:
                if ids_asociados:
                    resultados["errores"].append(
                        f"Fila {fila_excel}: El beneficiario '{persona_nombre} {persona_apellido}' "
                        f"existe pero no está asociado a la {actividad} seleccionada (ID {destino_id})."
                    )
                else:
                    resultados["errores"].append(
                        f"Fila {fila_excel}: El beneficiario '{persona_nombre} {persona_apellido}' "
                        f"no está registrado como paciente en esta {actividad}."
                    )
                continue

            # Fecha de evaluación
            fecha_raw = row.get("Fecha de Evaluacion")
            try:
                if pd.notna(fecha_raw):
                    fecha_evaluacion = pd.to_datetime(fecha_raw, dayfirst=True).date()
                else:
                    resultados["errores"].append(
                        f"Fila {fila_excel}: Falta fecha de evaluación para '{persona_nombre} {persona_apellido}'."
                    )
                    continue
            except Exception:
                resultados["errores"].append(
                    f"Fila {fila_excel}: Fecha inválida '{fecha_raw}' para '{persona_nombre} {persona_apellido}'."
                )
                continue

            # Pro cada campo vital
            campos_vitales = [
                ("Temperatura (°C)", "TEMPERATURA"),
                ("TA_SISTOLICA", "TA_SISTOLICA"),
                ("TA_DIASTOLICA", "TA_DIASTOLICA"),
                ("Frecuencia Cardíaca (lpm)", "FRECUENCIA_CARDIACA"),
                ("Frecuencia Respiratoria (rpm)", "FRECUENCIA_RESPIRATORIA"),
                ("Saturacion de Oxigeno", "SATURACION_OXIGENO"),
            ]

            for col_excel, nombre_vital in campos_vitales:
                valor = row.get(col_excel)
                if pd.notna(valor):
                    tipo_pesquisa_id = VITALES_MAP[nombre_vital]
                    valor_limpio = limpiar_valor_excel(valor)
                    resultados["pesquisas"].append({
                        "persona_id": persona_id,
                        f"{actividad}_id": destino_id,
                        "tipo_pesquisa_id": tipo_pesquisa_id,
                        "pesquisa_valor": valor_limpio,
                        "fecha": fecha_evaluacion,
                    })

        return resultados

    finally:
        conn.close()
