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

import logging
import threading
import time

MAX_LISTADO = 5000
MAX_ELIMINAR = 200
TTL_SEGUNDOS = 600          # pasado este tiempo, se sirve lo guardado y se recalcula en segundo plano

logger = logging.getLogger("carga_masiva.api")


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

    # Orden por poder de descarte REAL (medido en Venezuela, 206 mil personas): la jornada descarta al 60 %, el
    # centro a otro 36 %, y de las ~52 mil que quedan los familiares descartan al 99 % (quedan ~260). Las pesquisas
    # casi no descartan a nadie en ese punto, así que van al final: probarlas antes costaba ~100 mil consultas inútiles.
    sin_filas("a", "psi_pacientes_x_jornada", "persona_id")
    sin_filas("b", "psi_pacientes_x_centros", "persona_id")
    sin_filas("e", "psi_familiares", "persona_id_A")
    sin_filas("f", "psi_familiares", "persona_id_B")
    for alias, tabla in (("g", "psi_campo_evaluaciones"), ("h", "psi_campo_evaluaciones_centro")):
        if _tabla_existe(cursor, tabla):
            sin_filas(alias, tabla, "persona_id")
    sin_filas("c", "psi_pesquisas_x_paciente", "persona_id")
    sin_filas("d", "psi_pesquisas_x_centro", "persona_id")
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


# ---------------------------------------------------------------------------
# Caché en memoria por país
# ---------------------------------------------------------------------------
# La búsqueda recorre a todas las personas (≈5 s en Venezuela) y casi nunca cambia el resultado, así que se guarda:
#  * abrir la pantalla sirve lo guardado al instante;
#  * si lo guardado tiene más de TTL_SEGUNDOS, se sirve igual y se recalcula en segundo plano (la próxima vez ya está);
#  * «Actualizar» fuerza el recálculo y espera el resultado;
#  * tras eliminar se quitan esos ids de lo guardado, sin recalcular;
#  * al iniciar sesión se precalcula lo que falte (`precalentar`), para que la primera apertura tampoco espere.
# La caché solo afecta a lo que se MUESTRA: `eliminar` siempre vuelve a comprobar contra la base de datos.
# Es por proceso: con varios workers cada uno tendría la suya (no es un problema de corrección).

_cache = {}                 # pais -> {"datos": {...}, "calculado": epoch}
_candado = threading.Lock()
_calculando = set()         # países que se están recalculando ahora mismo
_candados_pais = {}         # un candado por país: evita dos cálculos simultáneos del mismo país


def _candado_de(pais: str):
    with _candado:
        return _candados_pais.setdefault(pais, threading.Lock())


def _calcular(pais: str, abrir_conexion) -> dict:
    """Ejecuta la búsqueda y la guarda. Si otro hilo ya la estaba calculando, espera y reutiliza ese resultado."""
    visto = _cache.get(pais, {}).get("calculado")
    with _candado_de(pais):
        reciente = _cache.get(pais)
        if reciente and reciente["calculado"] != visto:       # otro hilo terminó mientras esperábamos
            return reciente["datos"]
        with _candado:
            _calculando.add(pais)
        conn = abrir_conexion(pais)
        try:
            datos = listar(conn.cursor())
        finally:
            conn.close()
            with _candado:
                _calculando.discard(pais)
        with _candado:
            _cache[pais] = {"datos": datos, "calculado": time.time()}
        return datos


def _en_segundo_plano(pais: str, abrir_conexion) -> None:
    def tarea():
        try:
            _calcular(pais, abrir_conexion)
        except Exception:
            logger.exception("No se pudo recalcular la lista de beneficiarios sin actividad (%s)", pais)
    threading.Thread(target=tarea, daemon=True, name=f"huerfanos-{pais}").start()


def listar_con_cache(pais: str, abrir_conexion, refrescar: bool = False) -> dict:
    """Lo mismo que `listar`, más cuándo se calculó (`edad_segundos`) y si se está recalculando (`actualizando`)."""
    entrada = _cache.get(pais)
    if entrada is None or refrescar:
        datos = _calcular(pais, abrir_conexion)
        return {**datos, "edad_segundos": 0, "actualizando": False, "desde_cache": False}
    edad = int(time.time() - entrada["calculado"])
    actualizando = pais in _calculando
    if edad > TTL_SEGUNDOS and not actualizando:
        _en_segundo_plano(pais, abrir_conexion)
        actualizando = True
    return {**entrada["datos"], "edad_segundos": edad, "actualizando": actualizando, "desde_cache": True}


def quitar_de_cache(pais: str, persona_ids: list) -> None:
    """Quita de lo guardado a los ya eliminados, para que la lista no los vuelva a mostrar."""
    quitar = {str(i) for i in persona_ids}
    with _candado:
        entrada = _cache.get(pais)
        if entrada:
            entrada["datos"] = {**entrada["datos"], "beneficiarios": [b for b in entrada["datos"]["beneficiarios"] if b["persona_id"] not in quitar]}


def precalentar(paises, abrir_conexion) -> None:
    """Calcula en segundo plano, uno tras otro, los países que aún no tienen lista guardada o ya está vencida."""
    pendientes = [p for p in paises if p not in _cache or time.time() - _cache[p]["calculado"] > TTL_SEGUNDOS]
    if not pendientes:
        return

    def tarea():
        for pais in pendientes:
            try:
                _calcular(pais, abrir_conexion)
            except Exception:
                logger.exception("No se pudo precalcular la lista de beneficiarios sin actividad (%s)", pais)
    threading.Thread(target=tarea, daemon=True, name="huerfanos-precalentar").start()
