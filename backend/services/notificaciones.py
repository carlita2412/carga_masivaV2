"""Notificación por correo del resultado de cada carga masiva.

Uso: decorar el endpoint de carga con `@notificar_carga("Nombre del tipo")`
(debajo de `@app.post(...)`). El decorador no altera la respuesta al usuario:
envía el correo en un hilo aparte y cualquier fallo de SMTP solo se registra
en el log, para que un problema de correo nunca rompa ni bloquee una carga.

Configuración por variables de entorno (.env):
MAIL_HOST, MAIL_PORT, MAIL_SECURE (ssl|starttls), MAIL_USER, MAIL_PASS,
MAIL_FROM, MAIL_FROM_NAME y, opcional, MAIL_TO (separado por comas).
"""
import functools
import html
import json
import logging
import os
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("carga_masiva.correo")

DESTINATARIOS_POR_DEFECTO = "carla.noguera@digisalud.org,lorymar.guerra@digisalud.org"
MAX_DETALLES = 50  # máximo de líneas de errores/duplicados listadas en el correo


def _config():
    return {
        "host": os.getenv("MAIL_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("MAIL_PORT", "465")),
        "secure": os.getenv("MAIL_SECURE", "ssl").lower(),
        "user": os.getenv("MAIL_USER", ""),
        "password": os.getenv("MAIL_PASS", ""),
        "from": os.getenv("MAIL_FROM", os.getenv("MAIL_USER", "")),
        "from_name": os.getenv("MAIL_FROM_NAME", "Equipo Digisalud"),
        "to": [d.strip() for d in os.getenv("MAIL_TO", DESTINATARIOS_POR_DEFECTO).split(",") if d.strip()],
    }


def _lista_html(titulo, items):
    if not items:
        return ""
    items = [str(i) for i in items]
    extra = len(items) - MAX_DETALLES
    li = "".join(f"<li>{html.escape(i)}</li>" for i in items[:MAX_DETALLES])
    if extra > 0:
        li += f"<li><i>... y {extra} más</i></li>"
    return f"<h4>{titulo} ({len(items)})</h4><ul>{li}</ul>"


def _fila(k, v):
    return (f"<tr><td style='padding:4px 12px;background:#f3f4f6'><b>{html.escape(k)}</b></td>"
            f"<td style='padding:4px 12px'>{html.escape(str(v))}</td></tr>")


def _construir_correo(tipo, contexto, resultado):
    estado = str(resultado.get("status", "desconocido"))
    ok = estado == "ok"
    errores = resultado.get("errores") or []
    duplicados = resultado.get("duplicados") or []
    insertados = resultado.get("insertados")

    asunto = f"[Carga masiva] {tipo}: {'EXITOSA' if ok else 'CON ERRORES'}"

    filas = [
        _fila("Tipo de carga", tipo),
        _fila("Resultado", "Exitosa" if ok else "Con errores / no aplicada"),
        _fila("Fecha y hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    for k, v in contexto.items():
        filas.append(_fila(k, v))
    if resultado.get("mensaje"):
        filas.append(_fila("Mensaje", resultado["mensaje"]))
    if isinstance(insertados, dict):
        for k, v in insertados.items():
            filas.append(_fila(f"Insertados - {k}", v))
    elif insertados is not None:
        filas.append(_fila("Insertados", insertados))
    if "personas_existentes_reutilizadas" in resultado:
        filas.append(_fila("Personas existentes reutilizadas", resultado["personas_existentes_reutilizadas"]))
    filas.append(_fila("Duplicados", len(duplicados)))
    filas.append(_fila("Errores", len(errores)))

    cuerpo = (
        "<div style='font-family:Arial,sans-serif;font-size:14px'>"
        f"<h3>Resultado de carga masiva: {html.escape(tipo)}</h3>"
        f"<table style='border-collapse:collapse'>{''.join(filas)}</table>"
        f"{_lista_html('Errores', errores)}"
        f"{_lista_html('Duplicados', duplicados)}"
        "<p style='color:#6b7280;font-size:12px'>Mensaje automático, no responder.</p></div>"
    )
    return asunto, cuerpo


def _enviar(asunto, cuerpo_html):
    cfg = _config()
    if not cfg["user"] or not cfg["password"] or not cfg["to"]:
        logger.warning("Correo no enviado: faltan MAIL_USER/MAIL_PASS/destinatarios en .env")
        return
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = formataddr((cfg["from_name"], cfg["from"]))
    msg["To"] = ", ".join(cfg["to"])
    msg.set_content("Este mensaje requiere un cliente de correo con soporte HTML.")
    msg.add_alternative(cuerpo_html, subtype="html")
    try:
        if cfg["secure"] == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
            smtp.starttls()
        with smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        logger.info("Correo de resultado enviado: %s", asunto)
    except Exception:
        logger.exception("No se pudo enviar el correo de resultado de carga")


def _a_dict(respuesta):
    """Normaliza la respuesta del endpoint (dict o JSONResponse) a dict."""
    if isinstance(respuesta, dict):
        return respuesta
    cuerpo = getattr(respuesta, "body", None)
    if cuerpo:
        try:
            return json.loads(cuerpo)
        except Exception:
            pass
    return {"status": "error", "mensaje": "Respuesta no interpretable"}


def notificar_carga(tipo):
    """Decorador para endpoints de carga: envía el resultado por correo."""
    def decorador(func):
        @functools.wraps(func)
        async def envoltura(*args, **kwargs):
            # Import tardío para evitar import circular con main.py
            from backend.main import _usuario_autenticado

            request = kwargs.get("request")
            usuario = _usuario_autenticado(request) if request else None
            archivo = kwargs.get("file")
            nombre_archivo = getattr(archivo, "filename", "") if archivo else ""

            respuesta = await func(*args, **kwargs)

            if usuario:  # sin sesión (401) no es una carga real
                try:
                    contexto = {
                        "Usuario": usuario,
                        "Archivo": nombre_archivo,
                        "País": kwargs.get("pais", ""),
                        "Actividad": kwargs.get("actividad", ""),
                        "ID destino (jornada/centro)": kwargs.get("destino_id", ""),
                    }
                    if kwargs.get("institucion_id") is not None:
                        contexto["Institución ID"] = kwargs["institucion_id"]
                    asunto, cuerpo = _construir_correo(tipo, contexto, _a_dict(respuesta))
                    threading.Thread(target=_enviar, args=(asunto, cuerpo), daemon=True).start()
                except Exception:
                    logger.exception("Error preparando el correo de resultado")
            return respuesta
        return envoltura
    return decorador
