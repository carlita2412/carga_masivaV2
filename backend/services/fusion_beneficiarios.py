"""Unificación de beneficiarios duplicados.

Reemplaza el flujo manual con los procedimientos almacenados a001..a010
(a009_BUSCAR, a001/a004 INUPDE, a002/a005 borra, a006/a008 UPDA, a010 borraPersona).

Reglas de negocio (las mismas que se aplicaban a mano):
  * El usuario elige qué persona_id "queda" y cuál "se va" (la duplicada).
  * JORNADA: un beneficiario no puede ser evaluado dos veces en la misma jornada.
      - Si ambos tienen pesquisas en la jornada, se conservan las del que queda y
        se descartan las del duplicado.
      - Si solo el duplicado tiene pesquisas, pasan al que queda (si este no estaba
        inscrito en la jornada, primero se le inscribe).
  * CENTRO: un beneficiario sí puede tener varias evaluaciones, así que las del
    duplicado siempre pasan al que queda.
  * Las relaciones familiares se reasignan al que queda.

Este módulo recibe un cursor ya abierto y NO hace commit/rollback: eso lo decide el
endpoint, igual que en el resto de la app (los servicios no cierran la transacción).
"""
import re
import unicodedata

# Tablas dinámicas: los nombres vienen de esta configuración, nunca del cliente.
_CONFIG = {
    "jornada": {
        "etiqueta": "jornada",
        "esta": "esta jornada",
        "inscripcion": "psi_pacientes_x_jornada",
        "clave": "jornada_id",
        "entidad": "psi_jornadas",
        "entidad_nombre": "jornada_nombre",
        "pesquisas": "psi_pesquisas_x_paciente",
        "campo": "psi_campo_evaluaciones",
        "hijas": [
            "psi_escolaridad",
            "psi_aut_pac_x_jornada",
            "psi_antecedentes_x_paciente",
            "psi_fases_pac_x_jornada",
        ],
        "motivo_col": "pac_x_jor_motivo_no_autoriza",
        "status_col": "pac_x_jornada_status",
        "una_evaluacion": True,
    },
    "centro": {
        "etiqueta": "centro",
        "esta": "este centro",
        "inscripcion": "psi_pacientes_x_centros",
        "clave": "centro_id",
        "entidad": "psi_centros",
        "entidad_nombre": "centro_nombre",
        "pesquisas": "psi_pesquisas_x_centro",
        "campo": "psi_campo_evaluaciones_centro",
        "hijas": [
            "psi_escolaridad_centro",
            "psi_aut_pac_x_centro",
            "psi_antecedentes_x_paciente_centro",
        ],
        "motivo_col": "pac_x_centro_motivo_no_autoriza",
        "status_col": "pac_x_centro_status",
        "una_evaluacion": False,
    },
}

LIMITE_BUSQUEDA = 100


class FusionError(Exception):
    """Error de negocio mostrable al usuario (no es un fallo inesperado)."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _tabla_existe(cursor, tabla: str, cache: dict) -> bool:
    if tabla not in cache:
        cursor.execute(
            "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (tabla,),
        )
        cache[tabla] = cursor.fetchone() is not None
    return cache[tabla]


def _columna_existe(cursor, tabla: str, columna: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (tabla, columna),
    )
    return cursor.fetchone() is not None


def _normalizar(texto) -> str:
    """Mayúsculas, sin tildes y con espacios simples, para comparar nombres."""
    texto = unicodedata.normalize("NFD", str(texto or ""))
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return " ".join(texto.upper().split())


def _fecha(valor) -> str:
    if not valor:
        return ""
    return valor.strftime("%Y-%m-%d") if hasattr(valor, "strftime") else str(valor)[:10]


def _escapar_like(palabra: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", palabra)


def _persona_dict(fila) -> dict:
    return {
        "persona_id": str(fila[0]),
        "id_digisalud": (fila[1] or "").strip(),
        "nombres": (fila[2] or "").strip(),
        "apellidos": (fila[3] or "").strip(),
        "sexo": (fila[4] or "").strip().upper(),
        "fecha_nacimiento": _fecha(fila[5]),
        "cedula": (fila[6] or "").strip(),
        "cedula_escolar": (fila[7] or "").strip(),
    }


_COLS_PERSONA = (
    "persona_id, id_digisalud, persona_nombre, persona_apellido, persona_sexo, "
    "persona_fecha_nacimiento, persona_cedula, persona_cedula_escolar"
)


# ---------------------------------------------------------------------------
# Paso 1: búsqueda (equivale al SELECT * FROM psi_personas ... LIKE '%X%')
# ---------------------------------------------------------------------------

def buscar_personas(cursor, nombre: str = "", apellido: str = "", documento: str = "") -> dict:
    """Busca personas por palabras del nombre/apellido (todas deben aparecer) y/o
    por ID Digisalud / cédula. Devuelve también cuántas inscripciones y
    evaluaciones tiene cada una, para decidir con contexto."""
    palabras_nombre = nombre.split()
    palabras_apellido = apellido.split()
    documento = documento.strip()

    if sum(len(p) for p in palabras_nombre + palabras_apellido) + len(documento) < 2:
        raise FusionError("Escribe al menos 2 letras en nombre, apellido o ID/cédula.")

    condiciones, params = [], []
    for palabra in palabras_nombre:
        condiciones.append("p.persona_nombre LIKE %s")
        params.append(f"%{_escapar_like(palabra)}%")
    for palabra in palabras_apellido:
        condiciones.append("p.persona_apellido LIKE %s")
        params.append(f"%{_escapar_like(palabra)}%")
    if documento:
        condiciones.append(
            "(p.id_digisalud LIKE %s OR p.persona_cedula LIKE %s OR p.persona_cedula_escolar LIKE %s)"
        )
        like = f"%{_escapar_like(documento)}%"
        params.extend([like, like, like])

    cursor.execute(
        "SELECT p.persona_id, p.id_digisalud, p.persona_nombre, p.persona_apellido, p.persona_sexo, "
        "p.persona_fecha_nacimiento, p.persona_cedula, p.persona_cedula_escolar, "
        "(SELECT COUNT(*) FROM psi_pacientes_x_jornada x WHERE x.persona_id = p.persona_id), "
        "(SELECT COUNT(*) FROM psi_pacientes_x_centros x WHERE x.persona_id = p.persona_id), "
        "(SELECT COUNT(*) FROM psi_pesquisas_x_paciente x WHERE x.persona_id = p.persona_id), "
        "(SELECT COUNT(*) FROM psi_pesquisas_x_centro x WHERE x.persona_id = p.persona_id) "
        "FROM psi_personas p WHERE " + " AND ".join(condiciones) +
        " ORDER BY p.persona_apellido, p.persona_nombre, p.persona_fecha_nacimiento LIMIT %s",
        params + [LIMITE_BUSQUEDA + 1],
    )
    filas = cursor.fetchall()
    resultados = []
    for f in filas[:LIMITE_BUSQUEDA]:
        d = _persona_dict(f)
        d.update({"jornadas": f[8], "centros": f[9], "pesquisas": f[10] + f[11]})
        resultados.append(d)
    return {"resultados": resultados, "hay_mas": len(filas) > LIMITE_BUSQUEDA}


# ---------------------------------------------------------------------------
# Detección automática de posibles duplicados
# ---------------------------------------------------------------------------

def listar_posibles_duplicados(cursor) -> dict:
    """Pares de personas con mismo apellido, sexo y fecha de nacimiento cuyos
    nombres son iguales o donde uno contiene al otro (ej. "ABY" y "ABY GUADALUPE").
    Mismo apellido+fecha con nombres distintos (gemelos, hermanos) NO se listan, y
    tampoco los pares registrados como familiares entre sí."""
    cursor.execute(
        "SELECT p.persona_id, p.id_digisalud, p.persona_nombre, p.persona_apellido, p.persona_sexo, "
        "p.persona_fecha_nacimiento, p.persona_cedula, p.persona_cedula_escolar, "
        "(SELECT COUNT(*) FROM psi_pacientes_x_jornada x WHERE x.persona_id = p.persona_id), "
        "(SELECT COUNT(*) FROM psi_pacientes_x_centros x WHERE x.persona_id = p.persona_id), "
        "(SELECT COUNT(*) FROM psi_pesquisas_x_paciente x WHERE x.persona_id = p.persona_id), "
        "(SELECT COUNT(*) FROM psi_pesquisas_x_centro x WHERE x.persona_id = p.persona_id) "
        "FROM psi_personas p INNER JOIN ("
        "  SELECT persona_apellido, persona_sexo, persona_fecha_nacimiento FROM psi_personas "
        "  WHERE persona_fecha_nacimiento IS NOT NULL AND persona_apellido IS NOT NULL AND persona_apellido <> '' "
        "  GROUP BY persona_apellido, persona_sexo, persona_fecha_nacimiento HAVING COUNT(*) > 1"
        ") g ON g.persona_apellido = p.persona_apellido AND g.persona_sexo <=> p.persona_sexo "
        "AND g.persona_fecha_nacimiento = p.persona_fecha_nacimiento"
    )
    grupos = {}
    for f in cursor.fetchall():
        d = _persona_dict(f)
        d.update({"jornadas": f[8], "centros": f[9], "pesquisas": f[10] + f[11]})
        clave = (_normalizar(d["apellidos"]), d["sexo"], d["fecha_nacimiento"])
        grupos.setdefault(clave, []).append(d)

    pares = []
    for miembros in grupos.values():
        for i in range(len(miembros)):
            for j in range(i + 1, len(miembros)):
                a, b = miembros[i], miembros[j]
                na, nb = _normalizar(a["nombres"]), _normalizar(b["nombres"])
                ta, tb = set(na.split()), set(nb.split())
                if not ta or not tb:
                    continue
                if ta == tb:  # mismas palabras, aunque cambie el orden
                    confianza = "alta"
                elif ta <= tb or tb <= ta:
                    confianza = "media"
                else:
                    continue
                pares.append({"a": a, "b": b, "confianza": confianza})

    if pares:
        cursor.execute("SELECT persona_id_A, persona_id_B FROM psi_familiares")
        emparentados = {frozenset((str(x), str(y))) for x, y in cursor.fetchall()}
        pares = [p for p in pares if frozenset((p["a"]["persona_id"], p["b"]["persona_id"])) not in emparentados]

    orden = {"alta": 0, "media": 1}
    pares.sort(key=lambda p: (orden[p["confianza"]], _normalizar(p["a"]["apellidos"]), _normalizar(p["a"]["nombres"])))
    return {"pares": pares, "total": len(pares)}


# ---------------------------------------------------------------------------
# Paso 2: comparar (equivale a a009_BUSCAR, pero con nombres y conteos legibles)
# ---------------------------------------------------------------------------

def _obtener_persona(cursor, persona_id: int, bloquear: bool = False):
    cursor.execute(
        f"SELECT {_COLS_PERSONA} FROM psi_personas WHERE persona_id = %s" + (" FOR UPDATE" if bloquear else ""),
        (persona_id,),
    )
    fila = cursor.fetchone()
    if not fila:
        raise FusionError(f"El beneficiario {persona_id} no existe (¿ya fue unificado?). Vuelve a buscar.")
    return _persona_dict(fila)


def _contar(cursor, tabla: str, clave: str, personas: list) -> dict:
    """{(persona_id, clave_valor): filas} de una tabla hija."""
    cursor.execute(
        f"SELECT persona_id, {clave}, COUNT(*) FROM {tabla} WHERE persona_id IN (%s, %s) GROUP BY persona_id, {clave}",
        personas,
    )
    return {(int(r[0]), r[1]): r[2] for r in cursor.fetchall()}


def _actividades_de_persona(cursor, tipo: str, persona_id: int, cache: dict) -> list:
    """Jornadas o centros donde está inscrita la persona, con su nº de evaluaciones."""
    cfg = _CONFIG[tipo]
    clave = cfg["clave"]
    cursor.execute(
        f"SELECT i.{clave}, e.{cfg['entidad_nombre']} FROM {cfg['inscripcion']} i "
        f"LEFT JOIN {cfg['entidad']} e ON e.{clave} = i.{clave} WHERE i.persona_id = %s ORDER BY i.{clave}",
        (persona_id,),
    )
    inscritas = cursor.fetchall()
    pesquisas = _contar(cursor, cfg["pesquisas"], clave, [persona_id, persona_id])
    campo = (
        _contar(cursor, cfg["campo"], clave, [persona_id, persona_id])
        if _tabla_existe(cursor, cfg["campo"], cache) else {}
    )
    return [
        {
            "id": r[0],
            "nombre": r[1] or f"{cfg['etiqueta'].capitalize()} {r[0]}",
            "pesquisas": pesquisas.get((persona_id, r[0]), 0),
            "evaluaciones_campo": campo.get((persona_id, r[0]), 0),
        }
        for r in inscritas
    ]


def _detalle_persona(cursor, persona: dict, cache: dict) -> dict:
    pid = int(persona["persona_id"])
    detalle = dict(persona)
    detalle["jornadas"] = _actividades_de_persona(cursor, "jornada", pid, cache)
    detalle["centros"] = _actividades_de_persona(cursor, "centro", pid, cache)
    cursor.execute(
        "SELECT COUNT(*) FROM psi_familiares WHERE persona_id_A = %s OR persona_id_B = %s", (pid, pid)
    )
    detalle["familiares"] = cursor.fetchone()[0]
    return detalle


def _advertencias(cursor, a: dict, b: dict) -> list:
    """Señales de que quizá NO son la misma persona. nivel 'alto' exige confirmación explícita."""
    avisos = []
    if a["sexo"] != b["sexo"]:
        avisos.append({"nivel": "alto", "mensaje": f"El sexo es distinto ({a['sexo'] or '—'} vs {b['sexo'] or '—'})."})
    if a["fecha_nacimiento"] != b["fecha_nacimiento"]:
        avisos.append({
            "nivel": "alto",
            "mensaje": f"La fecha de nacimiento es distinta ({a['fecha_nacimiento'] or '—'} vs {b['fecha_nacimiento'] or '—'}).",
        })
    if _normalizar(a["apellidos"]) != _normalizar(b["apellidos"]):
        avisos.append({"nivel": "alto", "mensaje": "Los apellidos no coinciden."})

    na, nb = set(_normalizar(a["nombres"]).split()), set(_normalizar(b["nombres"]).split())
    if na and nb and not (na <= nb or nb <= na):
        avisos.append({"nivel": "alto", "mensaje": "Los nombres no se parecen (ninguno contiene al otro)."})

    cursor.execute(
        "SELECT COUNT(*) FROM psi_familiares WHERE (persona_id_A = %s AND persona_id_B = %s) "
        "OR (persona_id_A = %s AND persona_id_B = %s)",
        (a["persona_id"], b["persona_id"], b["persona_id"], a["persona_id"]),
    )
    if cursor.fetchone()[0]:
        avisos.append({
            "nivel": "alto",
            "mensaje": "Están registrados como familiares entre sí: casi seguro son personas distintas.",
        })
    return avisos


def comparar(cursor, id_a: int, id_b: int) -> dict:
    if id_a == id_b:
        raise FusionError("Elige dos beneficiarios distintos.")
    cache = {}
    a = _obtener_persona(cursor, id_a)
    b = _obtener_persona(cursor, id_b)
    return {
        "personas": [_detalle_persona(cursor, a, cache), _detalle_persona(cursor, b, cache)],
        "advertencias": _advertencias(cursor, a, b),
    }


# ---------------------------------------------------------------------------
# Paso 3: plan (vista previa) y ejecución
# ---------------------------------------------------------------------------

def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _plan_actividad(cursor, tipo: str, queda: int, fuera: int, cache: dict) -> list:
    cfg = _CONFIG[tipo]
    clave = cfg["clave"]
    campo_existe = _tabla_existe(cursor, cfg["campo"], cache)

    cursor.execute(
        f"SELECT i.{clave}, e.{cfg['entidad_nombre']} FROM {cfg['inscripcion']} i "
        f"LEFT JOIN {cfg['entidad']} e ON e.{clave} = i.{clave} WHERE i.persona_id = %s ORDER BY i.{clave}",
        (fuera,),
    )
    de_fuera = cursor.fetchall()
    cursor.execute(f"SELECT {clave} FROM {cfg['inscripcion']} WHERE persona_id = %s", (queda,))
    inscritas_queda = {r[0] for r in cursor.fetchall()}

    pesquisas = _contar(cursor, cfg["pesquisas"], clave, [queda, fuera])
    campo = _contar(cursor, cfg["campo"], clave, [queda, fuera]) if campo_existe else {}

    items = []
    for actividad_id, nombre in de_fuera:
        p_queda = pesquisas.get((queda, actividad_id), 0)
        p_fuera = pesquisas.get((fuera, actividad_id), 0)
        c_fuera = campo.get((fuera, actividad_id), 0)
        ya_inscrita = actividad_id in inscritas_queda
        etiqueta = cfg["etiqueta"]
        esta = cfg["esta"]

        if cfg["una_evaluacion"] and p_queda and p_fuera:
            accion = "conservar_queda"
            texto = (
                f"Ambos fueron evaluados en {esta} y no se permite doble evaluación: "
                f"se conservan las {_plural(p_queda, 'pesquisa', 'pesquisas')} del que se queda y se "
                f"descartan las {p_fuera} del duplicado."
            )
        elif p_fuera or c_fuera:
            accion = "trasladar" if ya_inscrita else "inscribir_y_trasladar"
            total = p_fuera + c_fuera
            base = "" if ya_inscrita else f"El que se queda no estaba inscrito en {esta}: se inscribe y "
            texto = (
                f"{base}{'se trasladan' if not ya_inscrita else 'Se trasladan'} "
                f"{_plural(total, 'registro', 'registros')} de evaluación al que se queda."
            )
        elif ya_inscrita:
            accion = "quitar_inscripcion"
            texto = f"Ya estaba inscrito en {esta} y el duplicado no tiene evaluaciones: solo se retira la inscripción repetida."
        else:
            accion = "inscribir"
            texto = f"El que se queda no estaba inscrito en {esta}: se inscribe (el duplicado no tiene evaluaciones)."

        items.append({
            "id": actividad_id,
            "nombre": nombre or f"{etiqueta.capitalize()} {actividad_id}",
            "accion": accion,
            "descripcion": texto,
            "ya_inscrita": ya_inscrita,
            "pesquisas_queda": p_queda,
            "pesquisas_fuera": p_fuera,
            "campo_fuera": c_fuera,
        })
    return items


def construir_plan(cursor, queda: int, fuera: int, bloquear: bool = False) -> dict:
    """Calcula qué pasará al unificar, sin modificar nada. Es la única fuente de
    verdad: `ejecutar_fusion` recorre este mismo plan."""
    if queda == fuera:
        raise FusionError("El beneficiario que se queda y el que se elimina no pueden ser el mismo.")
    cache = {}
    # Bloquear ambas filas evita que dos usuarios unifiquen los mismos registros a la vez.
    p_queda = _obtener_persona(cursor, queda, bloquear)
    p_fuera = _obtener_persona(cursor, fuera, bloquear)

    jornadas = _plan_actividad(cursor, "jornada", queda, fuera, cache)
    centros = _plan_actividad(cursor, "centro", queda, fuera, cache)

    cursor.execute(
        "SELECT COUNT(*) FROM psi_familiares WHERE (persona_id_A = %s AND persona_id_B = %s) "
        "OR (persona_id_A = %s AND persona_id_B = %s)", (queda, fuera, fuera, queda),
    )
    entre_ambos = cursor.fetchone()[0]
    cursor.execute(
        "SELECT COUNT(*) FROM psi_familiares WHERE persona_id_A = %s OR persona_id_B = %s", (fuera, fuera)
    )
    familiares = cursor.fetchone()[0] - entre_ambos

    todas = jornadas + centros
    return {
        "queda": p_queda,
        "fuera": p_fuera,
        "advertencias": _advertencias(cursor, p_queda, p_fuera),
        "jornadas": jornadas,
        "centros": centros,
        "familiares": {"reasignar": familiares, "eliminar_entre_ambos": entre_ambos},
        "resumen": {
            "inscripciones_nuevas": sum(1 for i in todas if not i["ya_inscrita"]),
            "inscripciones_retiradas": len(todas),
            # Las evaluaciones de campo siempre se trasladan; las pesquisas, salvo donde ya hay una del que se queda.
            "evaluaciones_trasladadas": sum(i["campo_fuera"] for i in todas)
            + sum(i["pesquisas_fuera"] for i in todas if i["accion"] != "conservar_queda"),
            "evaluaciones_descartadas": sum(i["pesquisas_fuera"] for i in todas if i["accion"] == "conservar_queda"),
        },
    }


def _copiar_inscripcion(cursor, tipo: str, queda: int, fuera: int, actividad_id: int, usuario_id) -> None:
    """Inscribe a `queda` copiando la inscripción de `fuera` (misma institución, estado,
    responsable y autorización), como hacía a001/a004 pero sin valores fijos."""
    cfg = _CONFIG[tipo]
    clave, tabla = cfg["clave"], cfg["inscripcion"]
    columnas = [clave, "institucion_id", cfg["motivo_col"], cfg["status_col"], "control_fecha_creacion",
                "control_usuario_creacion"]
    if _columna_existe(cursor, tabla, "persona_id_responsable"):
        columnas.append("persona_id_responsable")
    lista = ", ".join(columnas)
    cursor.execute(
        f"INSERT INTO {tabla} (persona_id, {lista}, control_fecha_modificacion, control_usuario_modificacion) "
        f"SELECT %s, {lista}, NOW(), %s FROM {tabla} WHERE persona_id = %s AND {clave} = %s",
        (queda, usuario_id, fuera, actividad_id),
    )


def ejecutar_fusion(cursor, queda: int, fuera: int, usuario_id=None) -> dict:
    """Unifica `fuera` dentro de `queda` y elimina a `fuera`. El llamador hace commit
    si todo salió bien o rollback si lanza cualquier excepción."""
    plan = construir_plan(cursor, queda, fuera, bloquear=True)
    cache = {}
    cont = {
        "inscripciones_nuevas": 0, "evaluaciones_trasladadas": 0, "evaluaciones_descartadas": 0,
        "datos_asociados_trasladados": 0, "datos_asociados_repetidos_descartados": 0,
        "inscripciones_retiradas": 0, "familiares_reasignados": 0, "relaciones_entre_ambos_eliminadas": 0,
    }

    for tipo, items in (("jornada", plan["jornadas"]), ("centro", plan["centros"])):
        cfg = _CONFIG[tipo]
        clave, insc = cfg["clave"], cfg["inscripcion"]
        for item in items:
            aid = item["id"]

            # 1) Que `queda` esté inscrito: sin esto las FK compuestas (persona, actividad) impiden mover nada.
            if not item["ya_inscrita"]:
                _copiar_inscripcion(cursor, tipo, queda, fuera, aid, usuario_id)
                cont["inscripciones_nuevas"] += 1

            # 2) Pesquisas
            if item["accion"] == "conservar_queda":
                cursor.execute(
                    f"DELETE FROM {cfg['pesquisas']} WHERE persona_id = %s AND {clave} = %s", (fuera, aid)
                )
                cont["evaluaciones_descartadas"] += cursor.rowcount
            else:
                cursor.execute(
                    f"UPDATE {cfg['pesquisas']} SET persona_id = %s WHERE persona_id = %s AND {clave} = %s",
                    (queda, fuera, aid),
                )
                cont["evaluaciones_trasladadas"] += cursor.rowcount

            # 3) Evaluaciones de campo (tienen mediciones colgando: siempre se trasladan, nunca se borran)
            if _tabla_existe(cursor, cfg["campo"], cache):
                cursor.execute(
                    f"UPDATE {cfg['campo']} SET persona_id = %s WHERE persona_id = %s AND {clave} = %s",
                    (queda, fuera, aid),
                )
                cont["evaluaciones_trasladadas"] += cursor.rowcount

            # 4) Escolaridad, autorizaciones, antecedentes, fases: pasan solo si `queda` no los tenía ya.
            for hija in cfg["hijas"]:
                if not _tabla_existe(cursor, hija, cache):
                    continue
                cursor.execute(
                    f"UPDATE IGNORE {hija} SET persona_id = %s WHERE persona_id = %s AND {clave} = %s",
                    (queda, fuera, aid),
                )
                cont["datos_asociados_trasladados"] += cursor.rowcount
                cursor.execute(f"DELETE FROM {hija} WHERE persona_id = %s AND {clave} = %s", (fuera, aid))
                cont["datos_asociados_repetidos_descartados"] += cursor.rowcount

            # 5) Ya sin hijos, se retira la inscripción duplicada.
            cursor.execute(f"DELETE FROM {insc} WHERE persona_id = %s AND {clave} = %s", (fuera, aid))
            cont["inscripciones_retiradas"] += cursor.rowcount

        # Quien figuraba como responsable del duplicado pasa a serlo `queda` (sin FK, no hay riesgo).
        if _columna_existe(cursor, insc, "persona_id_responsable"):
            cursor.execute(
                f"UPDATE {insc} SET persona_id_responsable = %s WHERE persona_id_responsable = %s", (queda, fuera)
            )

    # Familiares: una relación entre ambos se volvería "consigo mismo", así que se elimina.
    cursor.execute(
        "DELETE FROM psi_familiares WHERE (persona_id_A = %s AND persona_id_B = %s) "
        "OR (persona_id_A = %s AND persona_id_B = %s)", (queda, fuera, fuera, queda),
    )
    cont["relaciones_entre_ambos_eliminadas"] = cursor.rowcount
    for col in ("persona_id_A", "persona_id_B"):
        cursor.execute(f"UPDATE IGNORE psi_familiares SET {col} = %s WHERE {col} = %s", (queda, fuera))
        cont["familiares_reasignados"] += cursor.rowcount
    # Lo que no pudo reasignarse ya existía para `queda` (relación repetida).
    cursor.execute("DELETE FROM psi_familiares WHERE persona_id_A = %s OR persona_id_B = %s", (fuera, fuera))

    # Encuestas legadas (persona_id guardado como texto, sin FK).
    if _tabla_existe(cursor, "encuesta", cache):
        cursor.execute("UPDATE encuesta SET persona_id = %s WHERE persona_id = %s", (str(queda), str(fuera)))

    # Si quedó algo apuntando a `fuera` que no conocemos, la FK hará fallar este DELETE y se hará rollback completo.
    cursor.execute("DELETE FROM psi_personas WHERE persona_id = %s", (fuera,))
    if cursor.rowcount != 1:
        raise FusionError("No se pudo eliminar el beneficiario duplicado. No se guardó ningún cambio.")

    return {"plan": plan, "contadores": cont}
