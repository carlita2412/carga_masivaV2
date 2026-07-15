from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import hashlib
from backend.db.db_connection import get_connection

auth_router = APIRouter()
templates = Jinja2Templates(directory="frontend/templates")

@auth_router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@auth_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    pais: str = Form("vzla")
):
    hashed_password = hashlib.sha1(password.encode()).hexdigest()

    try:
        conn = get_connection(pais)
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
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(key="usuario", value=username, httponly=True)
            return response
        else:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Credenciales inválidas o sin autorización"
            })
    except Exception as e:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": f"Error: {str(e)}"
        })
