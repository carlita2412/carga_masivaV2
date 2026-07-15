import pandas as pd
from datetime import datetime
from backend.db.db_connection import get_connection


def calcular_edad(fecha_nac):
    hoy = datetime.today()
    return hoy.year - fecha_nac.year - ((hoy.month, hoy.day) < (fecha_nac.month, fecha_nac.day))

import unicodedata

def normalizar_id_digisalud_manual(id_digi: str) -> str:
    if not id_digi:
        return ""
    id_digi = id_digi.replace("Ñ", "N").replace("ñ", "n")
    id_digi = unicodedata.normalize("NFKD", id_digi)
    id_digi = ''.join(c for c in id_digi if not unicodedata.combining(c))
    return id_digi

def limpiar_valor_excel(valor):
    """
    Convierte valores del Excel a un string limpio compatible con MySQL.
    - Reemplaza comas por puntos
    - Convierte NaN a None
    - Elimina espacios
    """
    if pd.isna(valor):
        return None
    return str(valor).replace(",", ".").strip()



import pandas as pd
from backend.db.db_connection import get_connection

# Se asume que ya existen estas funciones:
# - normalizar_id_digisalud_manual
# - calcular_edad
# - limpiar_valor_excel


def procesar_excel_pesquisa_antropometrica(df, pais, actividad, destino_id):
    errores = []
    beneficiarios = []

    conn = get_connection(pais)
    cursor = conn.cursor()

    try:
        df = df.dropna(how='all').reset_index(drop=True)
        actividad = actividad.lower().strip()

        for i, row in df.iterrows():
            fila_excel = i + 2

            id_digisalud_raw = str(row["Id Digisalud Beneficiario"]).strip()
            id_digisalud = normalizar_id_digisalud_manual(id_digisalud_raw)

            # Buscar persona
            cursor.execute("""
                SELECT
                    persona_id,
                    persona_fecha_nacimiento,
                    persona_nombre,
                    persona_apellido
                FROM psi_personas
                WHERE id_digisalud = %s
            """, (id_digisalud,))
            persona = cursor.fetchone()

            if not persona:
                errores.append(
                    f"Fila {fila_excel}: No existe persona con id_digisalud '{id_digisalud}'."
                )
                continue

            persona_id, fecha_nac, persona_nombre, persona_apellido = persona
            edad = calcular_edad(fecha_nac)

            # Validar si está asociado a la jornada o centro
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
                    errores.append(
                        f"Fila {fila_excel}: El beneficiario '{persona_nombre} {persona_apellido}' "
                        f"existe como paciente, pero no está asociado a la {actividad} seleccionada "
                        f"(ID {destino_id}).Agregar a {actividad} "
                    )
                else:
                    errores.append(
                        f"Fila {fila_excel}: El beneficiario '{persona_nombre} {persona_apellido}' "
                        f"no está registrado como paciente en esta {actividad}."
                    )
                continue

            # Fecha evaluación
            fecha_excel = row.get("Fecha Eval. DD/MM/AAAA")
            try:
                fecha_evaluacion = pd.to_datetime(
                    fecha_excel, dayfirst=True
                ).date() if pd.notna(fecha_excel) else None
            except Exception:
                errores.append(
                    f"Fila {fila_excel}: Fecha inválida '{fecha_excel}' para '{persona_nombre} {persona_apellido}'."
                )
                continue

            pesquisas_beneficiario = []

            # PESO
            if pd.notna(row.get("PESO")):
                valor_peso = limpiar_valor_excel(row["PESO"])
                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 1,
                    "pesquisa_valor": valor_peso,
                    "pesquisa_fecha": fecha_evaluacion
                })
            else:
                errores.append(
                    f"Fila {fila_excel}: Falta PESO para '{persona_nombre} {persona_apellido}'."
                )

            # TALLA
            if pd.notna(row.get("TALLA")):
                metodo = "m2" if edad < 2 else "m1"
                valor_talla = limpiar_valor_excel(row["TALLA"])

                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 2,
                    "pesquisa_valor": valor_talla,
                    "pesquisa_fecha": fecha_evaluacion
                })

                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 260,
                    "pesquisa_valor": metodo,
                    "pesquisa_fecha": fecha_evaluacion
                })
            else:
                errores.append(
                    f"Fila {fila_excel}: Falta TALLA para '{persona_nombre} {persona_apellido}'."
                )

            # Circunferencia cintura
            if edad >= 19 and pd.notna(row.get("CIRCUNFERENCIA_CINTURA")):
                valor_cintura = limpiar_valor_excel(row["CIRCUNFERENCIA_CINTURA"])
                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 197,
                    "pesquisa_valor": valor_cintura,
                    "pesquisa_fecha": fecha_evaluacion
                })

            # Circunferencia cefálica
            if edad >= 1 and pd.notna(row.get("CIRCUNFERENCIA_CEFALICA")):
                valor_cefalica = limpiar_valor_excel(row["CIRCUNFERENCIA_CEFALICA"])
                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 193,
                    "pesquisa_valor": valor_cefalica,
                    "pesquisa_fecha": fecha_evaluacion
                })

            # Circunferencia brazo
            if edad >= 1 and pd.notna(row.get("CIRCUNFERENCIA_BRAZO")):
                valor_brazo = limpiar_valor_excel(row["CIRCUNFERENCIA_BRAZO"])
                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 195,
                    "pesquisa_valor": valor_brazo,
                    "pesquisa_fecha": fecha_evaluacion
                })

            # Observación
            if pd.notna(row.get("OBSERVACION")):
                observacion_pesq = limpiar_valor_excel(row["OBSERVACION"])
                pesquisas_beneficiario.append({
                    "persona_id": persona_id,
                    f"{actividad}_id": destino_id,
                    "tipo_pesquisa_id": 262,
                    "pesquisa_valor": observacion_pesq,
                    "pesquisa_fecha": fecha_evaluacion
                })

            if pesquisas_beneficiario:
                beneficiarios.append({
                    "fila_excel": fila_excel,
                    "persona_id": persona_id,
                    "persona_nombre": str(persona_nombre).strip(),
                    "persona_apellido": str(persona_apellido).strip(),
                    "id_digisalud": id_digisalud,
                    "pesquisas": pesquisas_beneficiario
                })

        return {
            "beneficiarios": beneficiarios,
            "errores": errores
        }

    finally:
        conn.close()