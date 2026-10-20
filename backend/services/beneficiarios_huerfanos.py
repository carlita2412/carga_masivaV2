"""Beneficiarios huérfanos: registros de `psi_personas` que no tienen ningún vínculo con el resto del sistema.

Una persona es "huérfana" cuando NO tiene:
  * inscripción en ninguna jornada ni en ningún centro,
  * ninguna evaluación (pesquisas de jornada o de centro, ni evaluaciones de campo),
  * ninguna relación familiar con otro beneficiario (en cualquiera de los dos sentidos),
  * ningún otro beneficiario que la tenga como responsable (`persona_id_responsable`),
  * encuestas legadas asociadas.

Se excluyen también las inscritas en un CENTRO (no solo en una jornada): borrar a alguien que está
atendido en un centro le quitaría ese vínculo.

Rendimiento: en Venezuela hay ~206 mil personas. Las comprobaciones son anti-joins (`LEFT JOIN ... IS NULL`)
sobre columnas indexadas, en orden de mayor a menor poder de descarte, y el listado trae a TODOS los
huérfanos en una sola consulta (son cientos) para que el navegador filtre y pagine sin volver al servidor.

Igual que el resto de los servicios, recibe un cursor y NO hace commit/rollback.
"""

MAX_LISTADO = 5000
MAX_ELIMINAR = 200


class HuerfanosError(Exception):
    """Error de negocio mostrable al usuario."""


def _tabla_existe(cursor, tabla: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (tabla,)
    )
    return cursor.fetchone() is not None


def _columna_existe(cursor, tabla: str, columna: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (tabla, columna),
    )
    return cursor.fetchone() is not None


def _fragmentos(cursor):
    """(joins, condiciones) SQL sobre el alias `p` de psi_personas que dejan solo a los huérfanos.
    Las tablas y columnas opcionales se incluyen solo si existen en esa base."""
    joins, donde = [], []

    def sin_filas(alias, tabla, columna):
        joins.append(f"LEFT JOIN {tabla} {alias} ON {alias}.{columna} = p.persona_id")
        donde.append(f"{alias}.{columna} IS NULL")

    # De mayor a menor poder de descarte: casi todos tienen jornada, y de los que no, la mayoría tiene evaluaciones.
    sin_filas("a", "psi_pacientes_x_jornada", "persona_id")
    sin_filas("b", "psi_pacientes_x_centros", "persona_id")
    sin_filas("c", "psi_pesquisas_x_paciente", "persona_id")
    sin_filas("d", "psi_pesquisas_x_centro", "persona_id")
    sin_filas("e", "psi_familiares", "persona_id_A")
    sin_filas("f", "psi_familiares", "persona_id_B")
    for alias, tabla in (("g", "psi_campo_evaluaciones"), ("h", "psi_campo_evaluaciones_centro")):
        if _tabla_existe(cursor, tabla):
            sin_filas(alias, tabla, "persona_id")
    # persona_id_responsable no tiene índice: se materializa una vez en vez de recorrerla por cada persona.
    for tabla in ("psi_pacientes_x_jornada", "psi_pacientes_x_centros"):
        if _columna_existe(cursor, tabla, "persona_id_responsable"):
            donde.append(f"p.persona_id NOT IN (SELECT x.persona_id_responsable FROM {tabla} x WHERE x.persona_id_responsable IS NOT NULL)")
    if _tabla_existe(cursor, "encuesta"):
        # persona_id es texto y su colación cambia entre bases: se compara en binario para evitar el error 1267.
        donde.append("NOT EXISTS (SELECT 1 FROM encuesta x WHERE BINARY x.persona_id = BINARY CAST(p.persona_id AS CHAR))")
    return " ".join(joins), " AND ".join(donde)


def _iso(valor) -> str:
    if not valor:
        return ""
    return valor.strftime("%Y-%m-%d") if hasattr(valor, "strftime") else str(valor)[:10]


def _fecha_hora(valor) -> str:
    if not valor:
        return ""
    return valor.strftime("%Y-%m-%d %H:%M") if hasattr(valor, "strftime") else str(valor)[:16]


def listar(cursor) -> dict:
    """Todos los huérfanos (hasta MAX_LISTADO), del registro más reciente al más antiguo."""
    joins, donde = _fragmentos(cursor)
    tiene_usuarios = _tabla_existe(cursor, "psi_usuarios")
    creador = "u.usuario_username" if tiene_usuarios else "NULL"
    union_usuarios = "LEFT JOIN psi_usuarios u ON u.usuario_id = p.control_usuario_creacion" if tiene_usuarios else ""
    cursor.execute(
        f"SELECT p.persona_id, p.id_digisalud, p.persona_nombre, p.persona_apellido, p.persona_sexo, "
        f"p.persona_fecha_nacimiento, p.control_fecha_creacion, p.registro_source, {creador} "
        f"FROM psi_personas p {joins} {union_usuarios} WHERE {donde} "
        f"ORDER BY p.control_fecha_creacion DESC, p.persona_id DESC LIMIT %s",
        (MAX_LISTADO + 1,),
    )
    filas = cursor.fetchall()
    beneficiarios = [{
        "persona_id": str(r[0]),
        "id_digisalud": (r[1] or "").strip(),
        "nombres": (r[2] or "").strip(),
        "apellidos": (r[3] or "").strip(),
        "sexo": (r[4] or "").strip().upper(),
        "fecha_nacimiento": _iso(r[5]),
        "fecha_registro": _fecha_hora(r[6]),
        "origen": (r[7] or "").strip(),
        "registrado_por": (r[8] or "").strip(),
    } for r in filas[:MAX_LISTADO]]
    return {"beneficiarios": beneficiarios, "truncado": len(filas) > MAX_LISTADO}


def eliminar(cursor, persona_ids: list) -> dict:
    """Elimina a los huérfanos indicados. Se vuelve a comprobar, con las filas bloqueadas, que sigan siendo
    huérfanos: quien haya recibido un vínculo desde que se mostró la lista se omite y no se toca."""
    ids = sorted({int(i) for i in persona_ids})
    if not ids:
        raise HuerfanosError("Selecciona al menos un beneficiario.")
    if len(ids) > MAX_ELIMINAR:
        raise HuerfanosError(f"No se pueden eliminar más de {MAX_ELIMINAR} beneficiarios a la vez.")
    ph = ", ".join(["%s"] * len(ids))

    # Bloqueo para que nadie los inscriba o relacione mientras se decide
    cursor.execute(f"SELECT persona_id FROM psi_personas WHERE persona_id IN ({ph}) FOR UPDATE", ids)
    existentes = {r[0] for r in cursor.fetchall()}
    joins, donde = _fragmentos(cursor)
    cursor.execute(f"SELECT p.persona_id FROM psi_personas p {joins} WHERE p.persona_id IN ({ph}) AND {donde}", ids)
    validos = sorted(r[0] for r in cursor.fetchall())

    if validos:
        ph_v = ", ".join(["%s"] * len(validos))
        cursor.execute(f"DELETE FROM psi_personas WHERE persona_id IN ({ph_v})", validos)
        if cursor.rowcount != len(validos):
            raise HuerfanosError("No se pudieron eliminar todos los beneficiarios. No se guardó ningún cambio.")
    return {
        "eliminados": len(validos),
        "omitidos": len(existentes) - len(validos),      # existen pero ya tienen algún vínculo
        "inexistentes": len(ids) - len(existentes),      # alguien los eliminó antes
        "ids_eliminados": validos,
    }
