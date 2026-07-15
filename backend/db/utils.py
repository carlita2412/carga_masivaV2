# backend/db/utils.py

def _limpiar_identificador(valor):
    """
    Limpia un identificador:
    - None -> None
    - espacios al inicio/final
    - string vacío -> None
    """
    if valor is None:
        return None

    valor = str(valor).strip()
    return valor if valor else None


def obtener_persona_id_existente(cursor, id_digisalud=None, cedula=None, cedula_escolar=None):
    """
    Busca una persona existente en psi_personas usando cualquiera de estos campos:
    - id_digisalud
    - persona_cedula
    - persona_cedula_escolar

    Regla:
    - Si solo uno coincide, devuelve ese persona_id
    - Si más de uno coincide pero apuntan al MISMO persona_id, devuelve ese id
    - Si apuntan a personas distintas, lanza excepción para evitar asociar mal una persona
    """
    id_digisalud = _limpiar_identificador(id_digisalud)
    cedula = _limpiar_identificador(cedula)
    cedula_escolar = _limpiar_identificador(cedula_escolar)

    ids_encontrados = set()

    if id_digisalud:
        cursor.execute(
            "SELECT persona_id FROM psi_personas WHERE id_digisalud = %s LIMIT 1",
            (id_digisalud,)
        )
        row = cursor.fetchone()
        if row:
            ids_encontrados.add(row[0])

    if cedula:
        cursor.execute(
            "SELECT persona_id FROM psi_personas WHERE persona_cedula = %s LIMIT 1",
            (cedula,)
        )
        row = cursor.fetchone()
        if row:
            ids_encontrados.add(row[0])

    if cedula_escolar:
        cursor.execute(
            "SELECT persona_id FROM psi_personas WHERE persona_cedula_escolar = %s LIMIT 1",
            (cedula_escolar,)
        )
        row = cursor.fetchone()
        if row:
            ids_encontrados.add(row[0])

    if len(ids_encontrados) > 1:
        raise ValueError(
            f"Conflicto de identidad: id_digisalud={id_digisalud}, "
            f"cedula={cedula}, cedula_escolar={cedula_escolar} "
            f"apuntan a personas distintas: {sorted(ids_encontrados)}"
        )

    if len(ids_encontrados) == 1:
        return list(ids_encontrados)[0]

    return None


def existe_persona_por_id(cursor, persona_id):
    """
    Verifica si existe una persona por persona_id.
    """
    cursor.execute(
        "SELECT 1 FROM psi_personas WHERE persona_id = %s LIMIT 1",
        (persona_id,)
    )
    return cursor.fetchone() is not None