from __future__ import annotations

from email.message import EmailMessage
from typing import Literal

import logging
import asyncio
import smtplib

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "development"
    frontend_origin: str = "http://localhost:5173"

    mail_host: str = "smtp.gmail.com"
    mail_port: int = 587
    mail_user: str = ""
    mail_password: str = ""
    mail_to: str = ""
    mail_from: str = ""
    mail_use_tls: bool = True

    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_recipient_phone: str = ""
    whatsapp_template_name: str = ""
    whatsapp_template_language: str = "en_US"

logger = logging.getLogger("uvicorn.error")
settings = Settings()

app = FastAPI(title="Dafna Medical API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in settings.frontend_origin.split(",") if x.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ContactRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=5, max_length=60)
    treatment: str = Field(min_length=1, max_length=120)
    message: str = Field(default="", max_length=5000)
    consent: bool
    locale: Literal["en", "uk", "de", "he"] = "en"


async def send_email(payload: ContactRequest) -> bool:
    if not all([settings.mail_host, settings.mail_to, settings.mail_from]):
        return False

    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = settings.mail_to
    msg["Reply-To"] = payload.email
    msg["Subject"] = f"Dafna Medical — new enquiry from {payload.name}"
    msg.set_content(
        "New Dafna Medical enquiry\n\n"
        f"Name: {payload.name}\n"
        f"Email: {payload.email}\n"
        f"Phone: {payload.phone}\n"
        f"Treatment: {payload.treatment}\n"
        f"Locale: {payload.locale}\n\n"
        f"Message:\n{payload.message or '(no message)'}\n"
    )
    def _send() -> None:
        with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=20) as server:
            if settings.mail_use_tls:
                server.starttls()
            if settings.mail_user:
                server.login(settings.mail_user, settings.mail_password)
            server.send_message(msg)

    await asyncio.to_thread(_send)
    return True


async def send_whatsapp(payload: ContactRequest) -> bool:
    base_configured = all([
        settings.whatsapp_phone_number_id,
        settings.whatsapp_access_token,
        settings.whatsapp_recipient_phone,
    ])
    if not base_configured:
        return False

    url = f"https://graph.facebook.com/v23.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    # For notifications outside the 24-hour customer-service window, set an approved
    # WhatsApp template. Without a template, the code uses a normal text message,
    # which is appropriate when the recipient conversation is in the active window.
    if settings.whatsapp_template_name:
        data = {
            "messaging_product": "whatsapp",
            "to": settings.whatsapp_recipient_phone,
            "type": "template",
            "template": {
                "name": settings.whatsapp_template_name,
                "language": {"code": settings.whatsapp_template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": payload.name},
                            {"type": "text", "text": payload.phone},
                            {"type": "text", "text": payload.email},
                            {"type": "text", "text": payload.treatment},
                            {"type": "text", "text": payload.message or "-"},
                        ],
                    }
                ],
            },
        }
    else:
        text = (
            "New Dafna Medical enquiry\n"
            f"Name: {payload.name}\n"
            f"Email: {payload.email}\n"
            f"Phone: {payload.phone}\n"
            f"Treatment: {payload.treatment}\n"
            f"Locale: {payload.locale}\n"
            f"Message: {payload.message or '(no message)'}"
        )
        data = {
            "messaging_product": "whatsapp",
            "to": settings.whatsapp_recipient_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, json=data, headers=headers)
        response.raise_for_status()
    return True


@app.get("/api/ping")
async def ping():
    return { "status": "ok" }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "email_configured": bool(settings.mail_host and settings.mail_to and settings.mail_from),
        "whatsapp_configured": bool(
            settings.whatsapp_phone_number_id
            and settings.whatsapp_access_token
            and settings.whatsapp_recipient_phone
        ),
        "whatsapp_template_mode": bool(settings.whatsapp_template_name),
    }


@app.post("/api/contact")
async def contact(payload: ContactRequest):
    if not payload.consent:
        raise HTTPException(status_code=422, detail="Consent is required")

    email_sent = False
    whatsapp_sent = False
    errors: list[str] = []

    try:
        email_sent = await send_email(payload)
    except (smtplib.SMTPException, OSError) as exc:
        errors.append(f"email: {exc}")

    try:
        whatsapp_sent = await send_whatsapp(payload)
    except httpx.HTTPError as exc:
        errors.append(f"whatsapp: {exc}")

    production = settings.app_env.lower() == "production"
    both_configured = (
        bool(settings.mail_host and settings.mail_to and settings.mail_from)
        and bool(settings.whatsapp_phone_number_id and settings.whatsapp_access_token and settings.whatsapp_recipient_phone)
    )

    if production:
        if not both_configured:
            raise HTTPException(status_code=503, detail="Email and WhatsApp delivery must both be configured")
        if not (email_sent or whatsapp_sent):
            raise HTTPException(status_code=502, detail="Unable to send")

    if not email_sent and not whatsapp_sent:
        logger.warning("[DEV] Contact request:", payload.model_dump())
    elif settings.app_env.lower() != "production" and errors:
        logger.warning("[DEV] delivery errors:", errors)

    return { "ok": True, "email_sent": email_sent, "whatsapp_sent": whatsapp_sent }
