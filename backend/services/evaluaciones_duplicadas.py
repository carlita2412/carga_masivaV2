"""Evaluaciones duplicadas en CENTROS.

En un centro una persona puede tener varias evaluaciones (en jornadas no), pero a veces el mismo
Excel se carga más de una vez y quedan copias idénticas de la misma medición. Este módulo las
detecta y permite eliminar las sobrantes.

Definiciones:
  * Una fila de `psi_pesquisas_x_centro` es UNA medición (un tipo de pesquisa con su valor).
  * Grupo repetido: mismas persona + centro + tipo de pesquisa + fecha de evaluación, con más de una fila.
    Solo se consideran las filas vigentes (`pesq_x_pac_status = 0`); las que tienen un timestamp en
    ese campo son versiones históricas y nunca se tocan.
  * Una «carga» es el conjunto de filas que se guardaron en el mismo instante (`control_fecha_creacion`).
  * Grupo idéntico: todas sus filas tienen el mismo valor. Grupo con valores distintos: hay que decidir
    cuál carga se conserva.

Igual que el resto de los servicios, recibe un cursor y NO hace commit/rollback.
"""

TABLA = "psi_pesquisas_x_centro"
FECHA = "pesq_x_pac_fecha_evauacion"
VIGENTE = "pesq_x_pac_status = 0"
LOTE_BORRADO = 500


class EvaluacionesError(Exception):
    """Error de negocio mostrable al usuario (el endpoint responde 400/404/409)."""


def _iso(valor) -> str:
    if not valor:
        return ""
    return valor.strftime("%Y-%m-%d") if hasattr(valor, "strftime") else str(valor)[:10]


def _hora(valor) -> str:
    if not valor:
        return ""
    return valor.strftime("%d/%m/%Y %H:%M:%S") if hasattr(valor, "strftime") else str(valor)


def _en_lotes(lista, tamano):
    for i in range(0, len(lista), tamano):
        yield lista[i:i + tamano]


# ---------------------------------------------------------------------------
# Filtros: cliente (organización) → centro
# ---------------------------------------------------------------------------

def listar_clientes(cursor) -> list:
    cursor.execute(
        "SELECT o.organizacion_id, o.organizacion_nombre, COUNT(c.centro_id) "
        "FROM psi_organizacion o INNER JOIN psi_centros c ON c.organizacion_id = o.organizacion_id "
        "GROUP BY o.organizacion_id, o.organizacion_nombre ORDER BY o.organizacion_nombre"
    )
    return [{"organizacion_id": r[0], "nombre": (r[1] or "").strip(), "centros": r[2]} for r in cursor.fetchall()]


def listar_centros(cursor, organizacion_id: int) -> list:
    """Centros del cliente con cuántas mediciones sobrantes (repetidas) tiene cada uno."""
    cursor.execute(
        "SELECT centro_id, centro_nombre FROM psi_centros WHERE organizacion_id = %s ORDER BY centro_nombre",
        (organizacion_id,),
    )
    centros = [{"centro_id": r[0], "nombre": " ".join((r[1] or "").split()), "repetidas": 0} for r in cursor.fetchall()]
    if not centros:
        return centros
    ids = [c["centro_id"] for c in centros]
    ph = ", ".join(["%s"] * len(ids))
    cursor.execute(
        f"SELECT centro_id, SUM(n - 1) FROM (SELECT centro_id, COUNT(*) n FROM {TABLA} "
        f"WHERE {VIGENTE} AND centro_id IN ({ph}) "
        f"GROUP BY persona_id, centro_id, tipo_pesquisa_id, DATE({FECHA}) HAVING COUNT(*) > 1) t GROUP BY centro_id",
        ids,
    )
    sobrantes = {r[0]: int(r[1]) for r in cursor.fetchall()}
    for c in centros:
        c["repetidas"] = sobrantes.get(c["centro_id"], 0)
    return centros


def _centro(cursor, centro_id: int) -> dict:
    cursor.execute("SELECT centro_id, centro_nombre, organizacion_id FROM psi_centros WHERE centro_id = %s", (centro_id,))
    fila = cursor.fetchone()
    if not fila:
        raise EvaluacionesError("El centro no existe.")
    return {"centro_id": fila[0], "nombre": " ".join((fila[1] or "").split()), "organizacion_id": fila[2]}


# ---------------------------------------------------------------------------
# Lista de beneficiarios de un centro
# ---------------------------------------------------------------------------

def beneficiarios_del_centro(cursor, centro_id: int, incluir_sin_repetidas: bool = False) -> dict:
    centro = _centro(cursor, centro_id)
    cursor.execute(
        f"SELECT persona_id, tipo_pesquisa_id, DATE({FECHA}), COUNT(*), COUNT(DISTINCT IFNULL(pesq_x_pac_valor, '<nulo>')) "
        f"FROM {TABLA} WHERE centro_id = %s AND {VIGENTE} GROUP BY persona_id, tipo_pesquisa_id, DATE({FECHA})",
        (centro_id,),
    )
    por_persona = {}
    resumen = {"beneficiarios_con_repetidas": 0, "grupos_repetidos": 0, "filas_sobrantes": 0,
               "filas_sobrantes_identicas": 0, "filas_sobrantes_distintas": 0, "grupos_distintos": 0}
    for persona_id, _tipo, fecha, n, valores in cursor.fetchall():
        d = por_persona.setdefault(persona_id, {"fechas": set(), "fechas_repetidas": set(), "sobrantes": 0, "distintos": 0})
        d["fechas"].add(fecha)
        if n > 1:
            d["fechas_repetidas"].add(fecha)
            d["sobrantes"] += n - 1
            resumen["grupos_repetidos"] += 1
            resumen["filas_sobrantes"] += n - 1
            if valores > 1:
                d["distintos"] += 1
                resumen["grupos_distintos"] += 1
                resumen["filas_sobrantes_distintas"] += n - 1
            else:
                resumen["filas_sobrantes_identicas"] += n - 1
    resumen["beneficiarios_con_repetidas"] = sum(1 for d in por_persona.values() if d["sobrantes"])

    ids = [pid for pid, d in por_persona.items() if incluir_sin_repetidas or d["sobrantes"]]
    nombres = {}
    for lote in _en_lotes(ids, 1000):
        ph = ", ".join(["%s"] * len(lote))
        cursor.execute(
            f"SELECT persona_id, id_digisalud, persona_nombre, persona_apellido FROM psi_personas WHERE persona_id IN ({ph})", lote
        )
        for r in cursor.fetchall():
            nombres[r[0]] = r

    beneficiarios = []
    for pid in ids:
        d, r = por_persona[pid], nombres.get(pid)
        beneficiarios.append({
            "persona_id": str(pid),
            "id_digisalud": ((r[1] if r else "") or "").strip(),
            "nombres": ((r[2] if r else "") or "").strip(),
            "apellidos": ((r[3] if r else "") or "").strip(),
            "fechas": len(d["fechas"]),
            "fechas_repetidas": len(d["fechas_repetidas"]),
            "sobrantes": d["sobrantes"],
            "con_valores_distintos": d["distintos"] > 0,
        })
    beneficiarios.sort(key=lambda b: (-b["sobrantes"], b["apellidos"], b["nombres"]))
    return {"centro": centro, "resumen": resumen, "beneficiarios": beneficiarios}


# ---------------------------------------------------------------------------
# Detalle de un beneficiario
# ---------------------------------------------------------------------------

def _filas_persona(cursor, centro_id: int, persona_id: int, bloquear: bool = False) -> list:
    cursor.execute(
        f"SELECT x.idunico, x.tipo_pesquisa_id, t.tipo_pesquisa_nombre, x.pesq_x_pac_valor, x.{FECHA}, x.control_fecha_creacion "
        f"FROM {TABLA} x LEFT JOIN psi_tipo_pesquisas t ON t.tipo_pesquisa_id = x.tipo_pesquisa_id "
        f"WHERE x.centro_id = %s AND x.persona_id = %s AND x.{VIGENTE} "
        f"ORDER BY x.{FECHA}, x.control_fecha_creacion, x.idunico" + (" FOR UPDATE" if bloquear else ""),
        (centro_id, persona_id),
    )
    return cursor.fetchall()


def _agrupar(filas) -> list:
    """Agrupa por fecha: copias (cargas) numeradas desde 1 y filas por tipo de pesquisa."""
    por_fecha = {}
    for idunico, tipo_id, tipo_nombre, valor, fecha, creada in filas:
        f = por_fecha.setdefault(_iso(fecha), {"copias": {}, "tipos": {}})
        clave_copia = _hora(creada)
        copia = f["copias"].setdefault(clave_copia, len(f["copias"]) + 1)
        t = f["tipos"].setdefault(tipo_id, {"tipo_id": tipo_id, "nombre": tipo_nombre or f"Tipo {tipo_id}", "filas": []})
        t["filas"].append({"idunico": idunico, "copia": copia, "valor": "" if valor is None else str(valor)})
    resultado = []
    for fecha in sorted(por_fecha):
        f = por_fecha[fecha]
        tipos = list(f["tipos"].values())
        repetida = any(len(t["filas"]) > 1 for t in tipos)
        identica = repetida and all(
            len({fila["valor"].strip() for fila in t["filas"]}) == 1 for t in tipos if len(t["filas"]) > 1
        )
        resultado.append({
            "fecha": fecha,
            "copias": [{"n": n, "creada": c} for c, n in sorted(f["copias"].items(), key=lambda kv: kv[1])],
            "tipos": tipos,
            "repetida": repetida,
            "identica": identica,
        })
    return resultado


def detalle_beneficiario(cursor, centro_id: int, persona_id: int) -> dict:
    centro = _centro(cursor, centro_id)
    cursor.execute(
        "SELECT persona_id, id_digisalud, persona_nombre, persona_apellido, persona_fecha_nacimiento "
        "FROM psi_personas WHERE persona_id = %s", (persona_id,),
    )
    p = cursor.fetchone()
    if not p:
        raise EvaluacionesError("El beneficiario no existe.")
    fechas = _agrupar(_filas_persona(cursor, centro_id, persona_id))
    if not fechas:
        raise EvaluacionesError("Ese beneficiario no tiene evaluaciones en este centro.")
    return {
        "centro": centro,
        "persona": {"persona_id": str(p[0]), "id_digisalud": (p[1] or "").strip(), "nombres": (p[2] or "").strip(),
                    "apellidos": (p[3] or "").strip(), "fecha_nacimiento": _iso(p[4])},
        "fechas": fechas,
    }


# ---------------------------------------------------------------------------
# Plan y ejecución
# ---------------------------------------------------------------------------

def _plan_seleccion(fechas: list, conservar: dict) -> list:
    """idunico a eliminar. En cada fecha repetida se conserva la carga elegida (por defecto la primera);
    un tipo que NO existe en esa carga conserva su fila más antigua: nunca se pierde un tipo de pesquisa."""
    eliminar = []
    for f in fechas:
        if not f["repetida"]:
            continue
        elegida = int(conservar.get(f["fecha"], 1))
        if elegida not in {c["n"] for c in f["copias"]}:
            raise EvaluacionesError(f"La carga elegida para el {f['fecha']} no existe.")
        for t in f["tipos"]:
            filas = t["filas"]
            if len(filas) < 2:
                continue
            candidatas = [x for x in filas if x["copia"] == elegida] or filas
            conservada = min(x["idunico"] for x in candidatas)
            eliminar += [x["idunico"] for x in filas if x["idunico"] != conservada]
    return eliminar


def _plan_exactas(cursor, centro_id: int, persona_id=None, bloquear: bool = False):
    """Grupos repetidos con valores idénticos: se conserva la fila más antigua. Devuelve (ids, personas)."""
    sql = (f"SELECT idunico, persona_id, tipo_pesquisa_id, DATE({FECHA}), pesq_x_pac_valor FROM {TABLA} "
           f"WHERE centro_id = %s AND {VIGENTE}")
    params = [centro_id]
    if persona_id is not None:
        sql += " AND persona_id = %s"
        params.append(persona_id)
    sql += f" ORDER BY persona_id, tipo_pesquisa_id, DATE({FECHA}), idunico" + (" FOR UPDATE" if bloquear else "")
    cursor.execute(sql, params)
    grupos = {}
    for idunico, pid, tipo, fecha, valor in cursor.fetchall():
        grupos.setdefault((pid, tipo, fecha), []).append((idunico, None if valor is None else str(valor).strip()))
    eliminar, personas = [], set()
    for (pid, _t, _f), filas in grupos.items():
        if len(filas) > 1 and len({v for _i, v in filas}) == 1:
            eliminar += [i for i, _v in filas[1:]]
            personas.add(pid)
    return eliminar, personas


def construir_plan(cursor, centro_id: int, modo: str, persona_id=None, conservar=None, bloquear: bool = False) -> dict:
    """Calcula qué se eliminaría, sin escribir. `modo`: 'seleccion' (un beneficiario, eligiendo la carga
    de cada fecha) o 'exactas' (solo copias idénticas, de un beneficiario o de todo el centro)."""
    _centro(cursor, centro_id)
    conservar = conservar or {}
    if modo == "seleccion":
        if persona_id is None:
            raise EvaluacionesError("Falta el beneficiario.")
        fechas = _agrupar(_filas_persona(cursor, centro_id, persona_id, bloquear))
        eliminar = _plan_seleccion(fechas, conservar)
        personas = {persona_id} if eliminar else set()
    elif modo == "exactas":
        eliminar, personas = _plan_exactas(cursor, centro_id, persona_id, bloquear)
    else:
        raise EvaluacionesError("Modo no válido.")
    return {"modo": modo, "centro_id": centro_id, "persona_id": persona_id, "eliminar": eliminar,
            "total": len(eliminar), "beneficiarios": len(personas)}


def eliminar_repetidas(cursor, centro_id: int, modo: str, persona_id=None, conservar=None, esperado=None) -> dict:
    """Elimina las filas sobrantes del plan. `esperado` (opcional) es el total que vio el usuario en la
    vista previa: si los datos cambiaron desde entonces, no se borra nada."""
    plan = construir_plan(cursor, centro_id, modo, persona_id, conservar, bloquear=True)
    if esperado is not None and plan["total"] != esperado:
        raise EvaluacionesError(
            f"Los datos cambiaron desde la vista previa (ahora serían {plan['total']} filas en vez de {esperado}). Vuelve a revisar.")
    borradas = 0
    for lote in _en_lotes(plan["eliminar"], LOTE_BORRADO):
        ph = ", ".join(["%s"] * len(lote))
        cursor.execute(f"DELETE FROM {TABLA} WHERE centro_id = %s AND {VIGENTE} AND idunico IN ({ph})", [centro_id] + lote)
        borradas += cursor.rowcount
    if borradas != plan["total"]:
        raise EvaluacionesError("No se pudieron eliminar todas las filas previstas. No se guardó ningún cambio.")
    return {"eliminadas": borradas, "beneficiarios": plan["beneficiarios"]}
