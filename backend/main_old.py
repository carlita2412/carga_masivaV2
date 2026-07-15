from fastapi import FastAPI, Request, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
# para endpoint dinamico
from fastapi.middleware.cors import CORSMiddleware
from backend.db.db_connection import get_connection
from fastapi.responses import JSONResponse
#para procesar excel
from backend.services.carga_beneficiario import procesar_excel
from backend.db.utils import obtener_persona_id_existente
from pymysql.err import IntegrityError
app = FastAPI()
from backend.services.auth import auth_router
app.include_router(auth_router)
import hashlib
from fastapi.responses import RedirectResponse

from fastapi import status
# Monta los archivos estáticos como CSS o JS si los agregas en /static
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend/templates"))

# Configuración de plantillas
templates = Jinja2Templates(directory="frontend/templates")
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Simulación simple de sesión (solo memoria temporal)
SESSIONS = {}
 
 
#para endpoint dinamico
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Recomendado restringir en producción
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.get("/api/opciones")
async def get_opciones(pais: str = Query(...), actividad: str = Query(...)):
    conn = get_connection(pais)
    cursor = conn.cursor()

    if actividad == "centro":
        sql = """
        SELECT a.centro_id, a.centro_nombre, b.institucion_id
        FROM psi_centros a
        INNER JOIN psi_instituciones_x_centro b ON a.centro_id = b.centro_id
         ORDER BY a.centro_id DESC
        """
    elif actividad == "jornada":
        sql = """
        SELECT a.jornada_id, a.jornada_nombre, b.institucion_id
        FROM psi_jornadas a
        INNER JOIN psi_instituciones_x_jornada b ON a.jornada_id = b.jornada_id
        ORDER BY a.jornada_id DESC
        """
    else:
        return JSONResponse(status_code=400, content={"error": "Actividad no válida"})

    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()

    return [{"id": r[0], "nombre": r[1], "institucion_id": r[2]} for r in rows]

#para procesar excel carga befeiciarios
import pandas as pd
from io import BytesIO
from backend.db.queries import (
    insertar_persona, insertar_paciente, insertar_escolaridad,
    insertar_autorizacion, insertar_familiar
)
@app.post("/api/cargar_excel")
async def cargar_excel(
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...),
    institucion_id: int = Form(...)
):
    try:
        actividad = actividad.lower().strip() 
        content = await file.read()
        df = pd.read_excel(BytesIO(content), header=1)
        print(df.columns.tolist())
        df = df.dropna(how='all').reset_index(drop=True)
        resultado = procesar_excel(df, pais, actividad, destino_id, institucion_id)
        conn = get_connection(pais)
        cursor = conn.cursor()
        ids_persona_confirmados = set()
        duplicados = []

       # Insertar personas o reutilizar existentes
        for persona in resultado["personas"]:
            # Usamos id_digisalud para buscar la existencia
            existente_id = obtener_persona_id_existente(cursor, persona["id_digisalud"])

            if existente_id:
                persona["persona_id"] = existente_id
                ids_persona_confirmados.add(existente_id)
                duplicados.append(f"Ya existe: {persona['persona_nombre']} {persona['persona_apellido']}")
            else:
                try:
                    insertar_persona(cursor, persona)
                    ids_persona_confirmados.add(persona["persona_id"])
                    print(f"Insertada persona nueva: {persona['persona_nombre']} {persona['persona_apellido']} - ID: {persona['persona_id']}")
                except Exception as e:
                    duplicados.append(f"Error insertando {persona['id_digisalud']}: {str(e)}")
                    continue



        # Insertar pacientes
        # Insertar pacientes
        for pac in resultado["pacientes"]:
            persona_id = pac["persona_id"]
            campo_id = f"{actividad}_id"
            tabla = f"psi_pacientes_x_{actividad}"

            # Verificar si ya existe en esta actividad específica (jornada o centro)
            check_sql = f"SELECT 1 FROM {tabla} WHERE persona_id = %s AND {campo_id} = %s"
            cursor.execute(check_sql, (persona_id, pac[campo_id]))
            existe = cursor.fetchone()

            if existe:
                duplicados.append(f"Ya estaba asociado a {actividad}: persona_id={persona_id}")
                continue

            try:
                insertar_paciente(cursor, pac, actividad)
            except IntegrityError as e:
                if e.args[0] == 1062:
                    duplicados.append(f"Paciente duplicado (clave primaria): persona_id={persona_id}")
                else:
                    raise e
            except Exception as e:
                duplicados.append(f"Error insertando paciente persona_id={persona_id}: {str(e)}")


        # Insertar autorizaciones
        for au in resultado["autorizaciones"]:
            if au["persona_id"] not in ids_persona_confirmados:
                duplicados.append(f"Omitida autorización: persona_id={au['persona_id']} no existe")
                continue
            try:
                insertar_autorizacion(cursor, au, actividad)
            except IntegrityError as e:
                if e.args[0] == 1062:
                    duplicados.append(f"Autorización duplicada: persona_id={au['persona_id']}")
                else:
                    raise e

        # Insertar escolaridades
        for es in resultado["escolaridades"]:
            if es["persona_id"] not in ids_persona_confirmados:
                duplicados.append(f"Omitida escolaridad: persona_id={es['persona_id']} no existe")
                continue
            try:
                insertar_escolaridad(cursor, es, actividad)
            except IntegrityError as e:
                if e.args[0] == 1062:
                    duplicados.append(f"Escolaridad duplicada: persona_id={es['persona_id']}")
                else:
                    raise e

        # Insertar familiares (representante)
        for fam in resultado["familiares"]:
            a, b = fam["persona_id_A"], fam["persona_id_B"]
            if a not in ids_persona_confirmados or b not in ids_persona_confirmados:
                duplicados.append(f"Omitido vínculo familiar: A={a} / B={b} no existen")
                continue
            try:
                insertar_familiar(cursor, fam)
            except IntegrityError as e:
                if e.args[0] == 1062:
                    duplicados.append(f"Familiar duplicado: A={a}, B={b}")
                else:
                    raise e

        conn.commit()
        print("Completado commit para actividad:", actividad)

        conn.close()

        return {
            "status": "ok",
            "insertados": {
                "personas": len(ids_persona_confirmados),
                "pacientes": len(resultado["pacientes"]),
                "escolaridades": len(resultado["escolaridades"]),
                "autorizaciones": len(resultado["autorizaciones"]),
                "familiares": len(resultado["familiares"]),
            },
            "duplicados": sorted(set(duplicados))
        }

    except Exception as e:
        return {"status": "error1", "mensaje": str(e)}

from backend.services.carga_pesquisas import procesar_excel_pesquisa_antropometrica
@app.post("/api/cargar_excel_pesquisa_antropometrica")
async def cargar_excel_pesquisa_antropometrica(
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    try:
        actividad = actividad.lower().strip()  # Normaliza la actividad

        content = await file.read()
        df = pd.read_excel(BytesIO(content), header=1)

        # Validación de columnas mínimas
        columnas_obligatorias = ["Id Digisalud Beneficiario", "PESO", "TALLA"]
        for col in columnas_obligatorias:
            if col not in df.columns:
                return {"status": "error", "mensaje": f"Falta columna obligatoria: '{col}'"}

        resultado = procesar_excel_pesquisa_antropometrica(df, pais, actividad, destino_id)

        conn = get_connection(pais)
        cursor = conn.cursor()

        if actividad == "jornada":
            tabla = "psi_pesquisas_x_paciente"
            id_campo = "jornada_id"
        else:
            tabla = "psi_pesquisas_x_centro"
            id_campo = "centro_id"

        for r in resultado["pesquisas"]:
            print("Insertando pesquisa:", r)  # Debug

            sql = f"""
                INSERT INTO {tabla} (
                    persona_id, {id_campo}, tipo_pesquisa_id, pesq_x_pac_valor,
                    pesq_x_pac_fecha_evaluacion, control_usuario_creacion, control_fecha_creacion
                )
                VALUES (%s, %s, %s, %s, CURDATE(), %s, CURDATE())
            """

            data = (
                r["persona_id"],
                r[id_campo],
                r["tipo_pesquisa_id"],
                r["pesquisa_valor"],
                1522702145282
            )

            cursor.execute(sql, data)

        conn.commit()
        conn.close()

        return {
            "status": "ok",
            "insertados": len(resultado["pesquisas"]),
            "errores": resultado["errores"]
        }

    except Exception as e:
        print("Error al procesar pesquisa:", str(e))  # Consola para depuración
        return {"status": "error2", "mensaje": str(e)}
    

 

# Mostrar formulario de login (GET)
@app.get("/login", response_class=HTMLResponse)
async def show_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})




@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    hashed_password = hashlib.sha1(password.encode()).hexdigest()

    conn = get_connection("vzla")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT usuario_id FROM psi_usuarios 
        WHERE usuario_username = %s 
          AND usuario_password = %s 
          AND usuario_status = 0 
          AND usuario_organizacion_id = 5
    """, (username, hashed_password))
    result = cursor.fetchone()
    conn.close()

    if result:
        user_id = result[0]
        session_token = f"token-{user_id}"
        SESSIONS[session_token] = username

        # Redirección segura con cookie correcta
        response = RedirectResponse(url="/carga", status_code=302)
        response.set_cookie(
            key="session",
            value=session_token,
            httponly=True,
            samesite="lax",
            path="/"
        )
        return response

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "Credenciales incorrectas"
    })



# Logout
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    session_token = request.cookies.get("session")
    if session_token and session_token in SESSIONS:
        return RedirectResponse(url="/carga")
    return RedirectResponse(url="/login")


@app.get("/carga", response_class=HTMLResponse)
async def carga_masiva(request: Request):
    session_token = request.cookies.get("session")
    usuario = SESSIONS.get(session_token)

    if not session_token or not usuario:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "usuario": usuario
    })

 