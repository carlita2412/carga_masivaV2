# backend/services/carga_beneficiario.py

import pandas as pd
import time
import unicodedata
from datetime import datetime
from threading import Lock

from backend.db.utils import obtener_persona_id_existente


# ============================================================================
# GENERADOR DE ID ÚNICO ROBUSTO
# Evita colisiones cuando varias personas se crean en el mismo milisegundo.
# ============================================================================
_id_lock = Lock()
_last_ms = 0
_seq = 0


def generar_persona_id():
    global _last_ms, _seq

    with _id_lock:
        ms = int(time.time() * 1000)

        if ms == _last_ms:
            _seq += 1
        else:
            _last_ms = ms
            _seq = 0

        return ms * 1000 + _seq


def obtener_cabecera(pais: str) -> str:
    return {
        "vzla": "VE",
        "colombia": "CO",
        "elsalvador": "SV"
    }.get(pais.lower(), "XX")


def safe_val(val):
    """
    Convierte NaN/vacío a None.
    """
    if pd.isna(val):
        return None
    val = str(val).strip()
    return val if val != "" else None


def normalizar_texto(texto):
    """
    Quita acentos y caracteres combinados para generar IDs consistentes.
    """
    texto = "" if texto is None else str(texto)
    texto = unicodedata.normalize('NFKD', texto)
    return ''.join([c for c in texto if not unicodedata.combining(c)]).replace('´', '').strip()


def generar_id_digisalud(nombre: str, apellido: str, genero: str, cabecera: str, fecha_nac: str) -> str:
    """
    Genera el ID DIGISALUD con la lógica actual del sistema.
    """
    nombre2 = normalizar_texto(nombre).upper()
    apellido2 = normalizar_texto(apellido).upper()

    partes_nombre = nombre2.split()
    name1 = partes_nombre[0][:3] if partes_nombre else ''
    name2 = partes_nombre[1][:1] if len(partes_nombre) > 1 else ''

    partes_apellido = apellido2.split()
    last_name1 = partes_apellido[0][:3] if partes_apellido else ''
    last_name2 = partes_apellido[1][:1] if len(partes_apellido) > 1 else ''

    fecha_format = fecha_nac.replace("-", "")
    return f"{cabecera}{str(genero).upper()}{name1}{name2}{last_name1}{last_name2}{fecha_format}"


def _valor_columna(row, *nombres):
    """
    Devuelve el primer valor no vacío encontrado entre varios nombres de columna.
    Sirve para soportar diferentes plantillas o encabezados con pequeñas variaciones.
    """
    for nombre in nombres:
        if nombre in row.index:
            valor = safe_val(row.get(nombre))
            if valor is not None:
                return valor
    return None


def procesar_excel(df, pais, actividad, destino_id, institucion_id, cursor):
    """
    Procesa el Excel y prepara estas colecciones:
    - personas nuevas a insertar
    - personas ya existentes
    - pacientes a asociar a la actividad
    - escolaridades
    - autorizaciones
    - familiares

    Importante:
    - Si la persona ya existe, NO se vuelve a crear.
    - Si no existe, se agrega a la lista de nuevas personas.
    - Si en el mismo Excel una persona aparece varias veces, se reutiliza el mismo persona_id.
    """
    actividad = actividad.lower().strip()
    cabecera = obtener_cabecera(pais)
    fecha_creacion = datetime.today().strftime("%Y-%m-%d")

    personas = []
    pacientes = []
    escolaridades = []
    autorizaciones = []
    familiares = []
    personas_existentes = set()
    errores = []

    # ------------------------------------------------------------------------
    # Cache local para no recrear ni duplicar personas dentro del mismo Excel
    # clave = combinación de identificadores
    # valor = persona_id ya resuelto
    # ------------------------------------------------------------------------
    mapa_personas_excel = {}

    # ------------------------------------------------------------------------
    # Sets para no duplicar registros dentro del mismo archivo
    # ------------------------------------------------------------------------
    pacientes_keys = set()
    escolaridad_keys = set()
    autorizaciones_keys = set()
    familiares_keys = set()

    # Filtrar filas vacías reales
    df = df[df["Nombres"].notna()].reset_index(drop=True)
    df = df.where(pd.notnull(df), None)

    def agregar_paciente_si_no_existe(persona_id):
        key = (persona_id, destino_id, institucion_id, actividad)
        if key not in pacientes_keys:
            pacientes_keys.add(key)
            pacientes.append({
                "persona_id": persona_id,
                f"{actividad}_id": destino_id,
                "institucion_id": institucion_id,
                f"pac_x_{actividad}_status": 0,
                "control_usuario_creacion": 1522702145282,
                "control_fecha_creacion": fecha_creacion
            })

    def agregar_escolaridad_si_no_existe(persona_id, grado, seccion, turno, escuela):
        key = (
            persona_id, destino_id,
            str(grado).strip() if grado is not None else None,
            str(seccion).strip() if seccion is not None else None,
            str(turno).strip() if turno is not None else None,
            str(escuela).strip() if escuela is not None else None
        )
        if key not in escolaridad_keys:
            escolaridad_keys.add(key)
            escolaridades.append({
                "persona_id": persona_id,
                f"{actividad}_id": destino_id,
                "escolaridad_grado": grado,
                "escolaridad_seccion": seccion,
                "escolaridad_turno": turno,
                "escolaridad_escuela": escuela
            })

    def agregar_autorizacion_si_no_existe(persona_id, autorizacion_id):
        key = (persona_id, destino_id, autorizacion_id, actividad)
        if key not in autorizaciones_keys:
            autorizaciones_keys.add(key)
            autorizaciones.append({
                "persona_id": persona_id,
                f"{actividad}_id": destino_id,
                "autorizacion_id": autorizacion_id
            })

    def agregar_familiar_si_no_existe(persona_id_a, persona_id_b, parentesco_id=1):
        key = (persona_id_a, persona_id_b, parentesco_id)
        if key not in familiares_keys:
            familiares_keys.add(key)
            familiares.append({
                "persona_id_A": persona_id_a,
                "persona_id_B": persona_id_b,
                "parentesco_id": parentesco_id,
                "familiar_status": 0
            })

    for i, row in df.iterrows():
        fila_excel = i + 3  # header=1 => datos empiezan aprox. en la fila 3 del Excel

        try:
            # ==================================================================
            # BENEFICIARIO
            # ==================================================================
            nombre = str(row["Nombres"]).strip().upper()
            apellido = safe_val(row["Apellidos"])
            apellido = apellido.upper() if apellido else None
            genero = str(row["Genero Beneficiario"]).strip().upper()

            fecha_nac = pd.to_datetime(
                row["Fecha Nac. DD/MM/AAAA"],
                dayfirst=True
            ).date().strftime("%Y-%m-%d")

            id_digisalud = _valor_columna(row, "ID DIGISALUD")
            if not id_digisalud:
                id_digisalud = generar_id_digisalud(nombre, apellido, genero, cabecera, fecha_nac)

            # Si no existen columnas separadas, se usa el mismo valor como fallback.
            cedula = _valor_columna(row, "CEDULA", "CÉDULA")
            if not cedula:
                cedula = id_digisalud

            cedula_escolar = _valor_columna(row, "CEDULA ESCOLAR", "CÉDULA ESCOLAR")
            if not cedula_escolar:
                cedula_escolar = id_digisalud

            clave_beneficiario = f"{id_digisalud}|{cedula}|{cedula_escolar}"

            if clave_beneficiario in mapa_personas_excel:
                persona_id = mapa_personas_excel[clave_beneficiario]
            else:
                persona_id = obtener_persona_id_existente(
                    cursor,
                    id_digisalud=id_digisalud,
                    cedula=cedula,
                    cedula_escolar=cedula_escolar
                )

                if persona_id:
                    personas_existentes.add(persona_id)
                else:
                    persona_id = generar_persona_id()

                    personas.append({
                        "persona_id": persona_id,
                        "persona_nombre": nombre,
                        "persona_apellido": apellido,
                        "persona_sexo": genero,
                        "persona_fecha_nacimiento": fecha_nac,
                        "persona_cedula": cedula,
                        "persona_cedula_escolar": cedula_escolar,
                        "id_digisalud": id_digisalud,
                        "persona_direccion_ciudad": _valor_columna(row, "MUNICIPIO beneficiario", "persona_direccion_ciudad"),
                        "persona_direccion_parroquia_id": _valor_columna(row, "PARROQUIA beneficiario", "persona_direccion_parroquia_id"),
                        "control_usuario_creacion": 1522702145282,
                        "control_fecha_creacion": fecha_creacion,
                        "datos_dispositivo": "carga-masivav2",
                        "persona_direccion_punto_referencia": _valor_columna(row, "DIRECCION")
                    })

                mapa_personas_excel[clave_beneficiario] = persona_id

            # Asociar beneficiario a la actividad
            agregar_paciente_si_no_existe(persona_id)

            # Escolaridad
            escuela = _valor_columna(row, "INSTITUCION EDUCATIVA")
            if escuela:
                agregar_escolaridad_si_no_existe(
                    persona_id=persona_id,
                    grado=_valor_columna(row, "GRADO"),
                    seccion=_valor_columna(row, "SECCION"),
                    turno=_valor_columna(row, "TURNO"),
                    escuela=escuela
                )

            # Autorizaciones del beneficiario
            for autorizacion_id in range(1, 6):
                agregar_autorizacion_si_no_existe(persona_id, autorizacion_id)

            # ==================================================================
            # REPRESENTANTE
            # Puede existir ya en psi_personas, o puede venir nuevo.
            # Además, puede ser evaluado también en la misma actividad.
            # ==================================================================
            tiene_representante = any([
                _valor_columna(row, "nombre_representante"),
                _valor_columna(row, "ID DIGISALUD representante"),
                _valor_columna(row, "APELLIDO(S) representante"),
            ])

            if tiene_representante:
                representante_nombre = _valor_columna(row, "nombre_representante")
                representante_apellido = _valor_columna(row, "APELLIDO(S) representante")
                representante_genero = _valor_columna(row, "GENERO representante")
                representante_fecha_raw = _valor_columna(row, "fecha nacimiento representante")

                # Si falta algún dato esencial, no se procesa el representante
                if representante_nombre and representante_apellido and representante_genero and representante_fecha_raw:
                    representante_nombre = str(representante_nombre).strip().upper()
                    representante_apellido = str(representante_apellido).strip().upper()
                    representante_genero = str(representante_genero).strip().upper()

                    representante_nac = pd.to_datetime(
                        representante_fecha_raw,
                        dayfirst=True
                    ).date().strftime("%Y-%m-%d")

                    id_digisalud_repr = _valor_columna(row, "ID DIGISALUD representante")
                    if not id_digisalud_repr:
                        id_digisalud_repr = generar_id_digisalud(
                            representante_nombre,
                            representante_apellido,
                            representante_genero,
                            cabecera,
                            representante_nac
                        )

                    cedula_repr = _valor_columna(row, "CEDULA representante", "CÉDULA representante")
                    if not cedula_repr:
                        cedula_repr = id_digisalud_repr

                    cedula_escolar_repr = _valor_columna(
                        row,
                        "CEDULA ESCOLAR representante",
                        "CÉDULA ESCOLAR representante"
                    )
                    if not cedula_escolar_repr:
                        cedula_escolar_repr = id_digisalud_repr

                    clave_representante = f"{id_digisalud_repr}|{cedula_repr}|{cedula_escolar_repr}"

                    if clave_representante in mapa_personas_excel:
                        representante_id = mapa_personas_excel[clave_representante]
                    else:
                        representante_id = obtener_persona_id_existente(
                            cursor,
                            id_digisalud=id_digisalud_repr,
                            cedula=cedula_repr,
                            cedula_escolar=cedula_escolar_repr
                        )

                        if representante_id:
                            personas_existentes.add(representante_id)
                        else:
                            representante_id = generar_persona_id()
                            personas.append({
                                "persona_id": representante_id,
                                "persona_nombre": representante_nombre,
                                "persona_apellido": representante_apellido,
                                "persona_sexo": representante_genero,
                                "persona_fecha_nacimiento": representante_nac,
                                "persona_cedula": cedula_repr,
                                "persona_cedula_escolar": cedula_escolar_repr,
                                "id_digisalud": id_digisalud_repr,
                                "control_usuario_creacion": 1522702145282,
                                "control_fecha_creacion": fecha_creacion,
                                "datos_dispositivo": "carga-masivav2",
                                "persona_direccion_ciudad": None,
                                "persona_direccion_parroquia_id": None,
                                "persona_direccion_punto_referencia": None
                            })

                        mapa_personas_excel[clave_representante] = representante_id

                    # Relación familiar beneficiario -> representante
                    agregar_familiar_si_no_existe(
                        persona_id_a=persona_id,
                        persona_id_b=representante_id,
                        parentesco_id=1
                    )

                    # Si el representante también debe evaluarse en esta actividad
                    if str(_valor_columna(row, "EVALUACIÓN MÉDICA INTEGRAL") or "").strip().lower() == "si":
                        agregar_paciente_si_no_existe(representante_id)
                        agregar_autorizacion_si_no_existe(representante_id, 1)

        except Exception as e:
            errores.append(f"Fila {fila_excel}: {str(e)}")

    return {
        "personas": personas,                        # solo las nuevas
        "personas_existentes": list(personas_existentes),  # ya estaban en BD
        "pacientes": pacientes,
        "escolaridades": escolaridades,
        "autorizaciones": autorizaciones,
        "familiares": familiares,
        "errores": errores
    }