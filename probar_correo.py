"""Prueba aislada del correo (sin subir Excel). Ejecutar desde la raíz:
    python probar_correo.py
Muestra el diálogo SMTP completo y el error exacto si falla."""
import os
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()
host = os.getenv("MAIL_HOST", "smtp.gmail.com")
port = int(os.getenv("MAIL_PORT", "465"))
secure = os.getenv("MAIL_SECURE", "ssl").lower()
user = os.getenv("MAIL_USER", "")
pwd = os.getenv("MAIL_PASS", "")
to = [d.strip() for d in os.getenv("MAIL_TO", "").split(",") if d.strip()]

print(f"host={host} port={port} secure={secure} user={user!r} "
      f"pass_len={len(pwd)} to={to}")
if not pwd or pwd == "MICLAVE_SECRETA":
    print("AVISO: MAIL_PASS vacío o aún es el placeholder")

msg = EmailMessage()
msg["Subject"] = "Prueba correo carga masiva"
msg["From"] = f'{os.getenv("MAIL_FROM_NAME", "")} <{os.getenv("MAIL_FROM", user)}>'
msg["To"] = ", ".join(to)
msg.set_content("Prueba de configuración SMTP.")

smtp = smtplib.SMTP_SSL(host, port, timeout=20) if secure == "ssl" else smtplib.SMTP(host, port, timeout=20)
smtp.set_debuglevel(1)
if secure != "ssl":
    smtp.starttls()
smtp.login(user, pwd)
smtp.send_message(msg)
smtp.quit()
print("OK: correo enviado")
