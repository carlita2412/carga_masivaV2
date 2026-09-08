# car
from datetime import datetime
from backend.db.db_connection import get_connection
from backend.db.utils import obtener_persona_id_existente
import pandas as pd

# Mapeo columna del Excel MAESTRO -> tipo_pesquisa_id (catalogo psi_tipo_pesquisa),
# tomado del formulario de pesquisa sanguinea v3 (evaluacion_sanguinea.php) en produccion.
COLUMNAS_TIPO_PESQUISA = {
    # Hematologia
    "Hemoglobina": 3,
    "Hematocrito": 165,
    "Hematies": 2176,
    "Leucocitos": 2175,
    "Plaquetas": 167,
    "Neutrofilos_%": 2206,
    "Linfocitos_%": 2204,
    "Monocitos_%": 2205,
    "Eosinofilos_%": 2202,
    "VCM": 2207,
    "HCM": 2203,
    "CHCM": 2201,
    "VSG": 227,
    # Quimica
    "Glicemia": 241,
    "HbA1c": 2179,
    "Urea": 2181,
    "Creatinina": 2180,
    "Acido_Urico": 1513,
    "Proteinas_Totales": 2185,
    "Albumina": 2186,
    "Globulinas": 2187,
    "Relacion_AG": 2189,
    "AST_TGO": 2191,
    "ALT_TGP": 2190,
    "Fosfatasa_Alcalina": 393,
    "Bilirrubina_Total": 1518,
    "Bilirrubina_Directa": 2183,
    "Bilirrubina_Indirecta": 2184,
    "Colesterol_Total": 243,
    "HDL": 244,
    "LDL": 246,
    "VLDL": 245,
    "Trigliceridos": 242,
    "Apo_A1": 2192,
    "Apo_B": 2193,
    "PCR": 395,
    # Serologia
    "VDRL": 394,
    "HIV": 2210,
    # Uroanalisis - caracteres fisicos y quimicos
    "Orina_Color": 449,
    "Orina_Aspecto": 1643,
    "Orina_Densidad": 448,
    "Orina_pH": 447,
    "Orina_Proteinas": 360,
    "Orina_Hemoglobina": 361,
    "Orina_Glucosa": 363,
    "Orina_Cetonas": 362,
    "Orina_Bilirrubina": 366,
    "Orina_Leucocitos_Quim": 2196,
    "Orina_Urobilinogeno": 364,
    "Orina_Nitritos": 365,
    # Uroanalisis - examen microscopico
    "Orina_Cel_Epiteliales": 369,
    "Orina_Leucocitos_Mic": 367,
    "Orina_Hematies_Mic": 368,
    "Orina_Bacterias": 372,
    # Coprologia - caracteres fisicos
    "Heces_Color": 380,
    "Heces_Consistencia": 379,
    "Heces_Aspecto": 375,
    "Heces_Moco": 381,
    "Heces_Sangre": 378,
    # Coprologia - examen microscopico
    "Heces_Leucocitos": 2195,
    "Heces_Hematies": 2194,
    # Coprologia - examen parasitologico (hallazgo de protozoarios y/o helmintos en un solo texto)
    "Parasitos_Intestinales": 383,
    # Especiales
    "PSA_Total": 397,
    "PSA_Libre": 398,
}

COLUMNAS_LAB = list(COLUMNAS_TIPO_PESQUISA.keys())


def procesar_excel_pesquisa_sanguineo_avanzada(df: pd.DataFrame, pais: str, actividad: str, destino_id: int):
    conn = get_connection(pais)
    cursor = conn.cursor()

    resultados = {
        "pesquisas": [],
        "errores": []
    }

    for _, row in df.iterrows():
        id_digisalud = str(row.get("id_digisalud")).strip()
        if not id_digisalud or id_digisalud.lower() == "nan":
            continue

        persona_id = obtener_persona_id_existente(cursor, id_digisalud)
        if not persona_id:
            resultados["errores"].append(f"No existe beneficiario con ID {id_digisalud}")
            continue

        campo_id = f"{actividad}_id"
        tabla_asociacion = f"psi_pacientes_x_{actividad}s"
        cursor.execute(
            f"SELECT 1 FROM {tabla_asociacion} WHERE persona_id = %s AND {campo_id} = %s",
            (persona_id, destino_id)
        )
        if not cursor.fetchone():
            resultados["errores"].append(f"Beneficiario {id_digisalud} no está cargado en {actividad} {destino_id}")
            continue

        fecha_valor = row.get("Fecha_Ingreso")
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

        for campo, tipo_id in COLUMNAS_TIPO_PESQUISA.items():
            if campo not in df.columns:
                continue
            valor = row.get(campo)
            if pd.notna(valor) and str(valor).strip() != "":
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
