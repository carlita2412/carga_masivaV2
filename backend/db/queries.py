# backend/db/queries.py

def insertar_persona(cursor, persona: dict):
    def clean(val):
        import pandas as pd
        return None if pd.isna(val) else val

    sql = """
        INSERT INTO psi_personas (
            persona_id, persona_nombre, persona_apellido, persona_sexo, persona_fecha_nacimiento,
            persona_cedula, persona_cedula_escolar, id_digisalud,
            persona_direccion_ciudad, persona_direccion_parroquia_id,
            control_usuario_creacion, control_fecha_creacion, datos_dispositivo, persona_direccion_punto_referencia
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    datos = (
        persona["persona_id"],
        persona["persona_nombre"],
        persona["persona_apellido"],
        persona["persona_sexo"],
        persona["persona_fecha_nacimiento"],
        persona["persona_cedula"],
        persona["persona_cedula_escolar"],
        persona["id_digisalud"],
        clean(persona.get("persona_direccion_ciudad")),
        clean(persona.get("persona_direccion_parroquia_id")),
        persona["control_usuario_creacion"],
        persona["control_fecha_creacion"],
        persona["datos_dispositivo"],
        clean(persona.get("persona_direccion_punto_referencia"))
    )

    print("Insertando persona:", datos)
    cursor.execute(sql, datos)
    print("rowcount persona:", cursor.rowcount)


def insertar_paciente(cursor, paciente: dict, actividad: str):
    if actividad == "centro":
        tabla = "psi_pacientes_x_centros"
        id_campo = "centro_id"
    else:
        tabla = "psi_pacientes_x_jornada"
        id_campo = "jornada_id"

    sql = f"""
        INSERT INTO {tabla} (
            persona_id, {id_campo}, institucion_id,
            pac_x_{actividad}_status,
            control_fecha_creacion, control_usuario_creacion
        )
        VALUES (%s, %s, %s, %s, %s, %s)
    """

    datos = (
        paciente["persona_id"],
        paciente[f"{actividad}_id"],
        paciente["institucion_id"],
        paciente[f"pac_x_{actividad}_status"],
        paciente["control_fecha_creacion"],
        paciente["control_usuario_creacion"]
    )

    print("Insertando en", tabla, ":", datos)
    cursor.execute(sql, datos)
    print("Fila afectada:", cursor.rowcount)


def insertar_escolaridad(cursor, esc: dict, actividad: str):
    if actividad == "centro":
        tabla = "psi_escolaridad_centro"
        id_campo = "centro_id"
    else:
        tabla = "psi_escolaridad"
        id_campo = "jornada_id"

    sql = f"""
        INSERT INTO {tabla} (
            persona_id, {id_campo},
            escolaridad_grado, escolaridad_seccion,
            escolaridad_turno, escolaridad_escuela
        )
        VALUES (%s, %s, %s, %s, %s, %s)
    """

    datos = (
        esc["persona_id"],
        esc[f"{actividad}_id"],
        esc.get("escolaridad_grado"),
        esc.get("escolaridad_seccion"),
        esc.get("escolaridad_turno"),
        esc.get("escolaridad_escuela"),
    )

    print("Insertando en psi_escolaridad:", datos)
    cursor.execute(sql, datos)
    print("Filas afectadas escolaridad:", cursor.rowcount)


def insertar_autorizacion(cursor, auto: dict, actividad: str):
    tabla = "psi_aut_pac_x_jornada" if actividad == "jornada" else "psi_aut_pac_x_centro"

    sql = f"""
        INSERT INTO {tabla} (
            persona_id, {actividad}_id, autorizacion_id
        )
        VALUES (%s, %s, %s)
    """

    datos = (
        auto["persona_id"],
        auto[f"{actividad}_id"],
        auto["autorizacion_id"]
    )

    print("Insertando en psi_aut_pac:", datos)
    cursor.execute(sql, datos)


def insertar_familiar(cursor, fam: dict):
    sql = """
        INSERT INTO psi_familiares (
            persona_id_A, persona_id_B, parentesco_id, familiar_status
        )
        VALUES (%s, %s, %s, %s)
    """

    datos = (
        fam["persona_id_A"],
        fam["persona_id_B"],
        fam["parentesco_id"],
        fam["familiar_status"]
    )

    print("Insertando en psi_familiares:", datos)
    cursor.execute(sql, datos)