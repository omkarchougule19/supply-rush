"""
auth.py — Instructor HTTP Basic Auth + passwordless student email verification

Two independent gates:
  - Instructor: a static username/password from env vars (INSTRUCTOR_USERNAMES /
    INSTRUCTOR_PASSWORDS), checked via HTTPBasic — the browser's native login
    popup. No database, no accounts.
  - Students: a 6-digit one-time code emailed to a school address, restricted
    to ALLOWED_STUDENT_EMAIL_DOMAIN. No passwords are ever created or stored.
    Once verified, a signed cookie carries the email for SESSION_MAX_AGE_SECONDS
    so the student doesn't need to re-verify on every request.

Both gates are OFF by default (student verification via REQUIRE_STUDENT_VERIFICATION,
defaulting to "false") so local development never needs Resend/env vars configured
just to exercise the rest of the app.
"""

import os
import re
import hashlib
import secrets
import logging
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy.orm import Session

from database import SessionLocal
from models import VerificationCode, AppSecret

logger = logging.getLogger("uvicorn.error")

# In-process cache so each fallback secret only needs one DB round-trip per
# worker's lifetime, not one per request.
_secret_cache: dict = {}


def _get_or_create_persistent_secret(key: str, generator) -> str:
    """
    Returns a secret shared across every process (gunicorn worker) touching
    this DB, generating and persisting it once if it doesn't exist yet.
    Only used as a fallback when the corresponding env var is unset — using
    a fresh random value per worker process would make instructor logins
    and student session cookies work on one worker and fail on the next.
    """
    if key in _secret_cache:
        return _secret_cache[key]
    db = SessionLocal()
    try:
        row = db.query(AppSecret).filter(AppSecret.key == key).first()
        if row is None:
            value = generator()
            db.add(AppSecret(key=key, value=value))
            try:
                db.commit()
            except Exception:
                # Another worker won the race to insert this key first — use its value.
                db.rollback()
                row = db.query(AppSecret).filter(AppSecret.key == key).first()
                value = row.value if row else value
        else:
            value = row.value
        _secret_cache[key] = value
        return value
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Instructor — HTTP Basic Auth
# ─────────────────────────────────────────────────────────────────────────────
security = HTTPBasic()

# Only used when INSTRUCTOR_USERNAMES/PASSWORDS are both left unset —
# DB-persisted (not the well-known "admin"/"password", and not a fresh
# random value per process — under multi-worker gunicorn every worker must
# agree on the same fallback or logins would only work on some of them),
# and logged once per worker so local dev without any env vars can find it.
_fallback_password_logged = False


def authenticate_instructor(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    global _fallback_password_logged
    usernames_env = os.getenv("INSTRUCTOR_USERNAMES")
    passwords_env = os.getenv("INSTRUCTOR_PASSWORDS")

    if not usernames_env and not passwords_env:
        fallback_password = _get_or_create_persistent_secret(
            "instructor_fallback_password", lambda: secrets.token_urlsafe(12)
        )
        if not _fallback_password_logged:
            logger.warning(
                f"[DEV — INSTRUCTOR_USERNAMES/PASSWORDS not set] Instructor login: admin / {fallback_password}"
            )
            _fallback_password_logged = True
        allowed_users = ["admin"]
        allowed_passwords = [fallback_password]
    elif not usernames_env or not passwords_env:
        # Exactly one of the two is set — never silently treat the missing
        # one as a blank-string credential (a much weaker, unintended login).
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Instructor auth misconfigured: INSTRUCTOR_USERNAMES and INSTRUCTOR_PASSWORDS must both be set together.",
        )
    else:
        allowed_users = usernames_env.split(",")
        allowed_passwords = passwords_env.split(",")
        if len(allowed_users) != len(allowed_passwords):
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Instructor auth misconfigured: INSTRUCTOR_USERNAMES and INSTRUCTOR_PASSWORDS must have the same number of comma-separated entries.",
            )
    user_map = dict(zip(allowed_users, allowed_passwords))

    correct_password = user_map.get(credentials.username)
    if not correct_password or not secrets.compare_digest(credentials.password, correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            # Critical — instructs the browser to show the login popup
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ─────────────────────────────────────────────────────────────────────────────
#  Students — signed session cookie
# ─────────────────────────────────────────────────────────────────────────────
SESSION_COOKIE_NAME = "sr_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days

# Only used when SESSION_SECRET is unset — DB-persisted (not a fixed
# source-visible string, and not a fresh random value per process — under
# multi-worker gunicorn every worker must sign/verify with the same secret
# or a cookie minted by one worker gets rejected by the next).
def _fallback_session_secret() -> str:
    return _get_or_create_persistent_secret(
        "session_fallback_secret", lambda: secrets.token_urlsafe(32)
    )


def _serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("SESSION_SECRET") or _fallback_session_secret()
    return URLSafeTimedSerializer(secret, salt="sr-student-session")


def set_session_cookie(response: Response, email: str) -> None:
    token = _serializer().dumps({"email": email})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "true").lower() == "true",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)


def get_session_email(request: Request) -> str:
    """Returns the verified email from the session cookie, or None if absent/invalid/expired."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("email")
    except (BadSignature, SignatureExpired):
        return None


def verification_required() -> bool:
    return os.getenv("REQUIRE_STUDENT_VERIFICATION", "false").lower() == "true"


def require_student_email(request: Request) -> str:
    """
    FastAPI dependency: the verified student email for this request.
    When REQUIRE_STUDENT_VERIFICATION is off (the default), returns a stable
    placeholder so every downstream membership check becomes a no-op — the
    app behaves exactly as it did before this feature existed.
    """
    if not verification_required():
        return "unverified@local"
    email = get_session_email(request)
    if not email:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please verify your email to continue.")
    return email


# ─────────────────────────────────────────────────────────────────────────────
#  Email domain restriction
# ─────────────────────────────────────────────────────────────────────────────
def allowed_email_domain() -> str:
    # Strip an accidentally-included leading "@" (ALLOWED_STUDENT_EMAIL_DOMAIN=
    # @illinois.edu instead of illinois.edu) — without this, is_allowed_email's
    # endswith("@" + domain) can never match any real address, silently
    # locking out every student with no startup-time check to catch the typo.
    return os.getenv("ALLOWED_STUDENT_EMAIL_DOMAIN", "illinois.edu").strip().lower().lstrip("@")


def is_allowed_email(email: str) -> bool:
    domain = allowed_email_domain()
    if not domain:
        return True  # no restriction configured
    return email.strip().lower().endswith("@" + domain)


# ─────────────────────────────────────────────────────────────────────────────
#  One-time verification codes
# ─────────────────────────────────────────────────────────────────────────────
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_COOLDOWN_SECONDS = 60


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email.lower()}:{code}".encode()).hexdigest()


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def create_verification_code(db: Session, email: str) -> str:
    """Generates, sends, and stores (hashed) a new 6-digit code for `email`.
    Rate-limited so one address can't be spammed with resend requests. The
    email is sent BEFORE the code row is persisted — a failed send (Resend
    outage, bad API key) must not start the resend cooldown, or the student
    would be locked out of retrying for a code that never arrived."""
    email = email.strip().lower()
    now = datetime.utcnow()

    recent = (
        db.query(VerificationCode)
        .filter(VerificationCode.email == email)
        .order_by(VerificationCode.created_at.desc())
        .first()
    )
    if recent and (now - recent.created_at).total_seconds() < CODE_RESEND_COOLDOWN_SECONDS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait a moment before requesting another code.")

    code = _generate_code()
    send_verification_email(email, code)  # raises before any DB write on failure

    db.add(VerificationCode(
        email=email,
        code_hash=_hash_code(email, code),
        created_at=now,
        expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
        attempts_remaining=CODE_MAX_ATTEMPTS,
        consumed=False,
    ))
    db.commit()
    return code


def verify_code(db: Session, email: str, code: str) -> bool:
    """Checks `code` against every still-valid unconsumed code for `email`
    (not just the newest — a student may still be holding an earlier email
    after requesting a resend). Consumes the matching one on success;
    decrements attempts_remaining on every live candidate on a wrong guess,
    so a resend can't multiply the total guess budget across codes."""
    email = email.strip().lower()
    now = datetime.utcnow()
    candidates = (
        db.query(VerificationCode)
        .filter(
            VerificationCode.email == email,
            VerificationCode.consumed == False,  # noqa: E712
            VerificationCode.expires_at >= now,
            VerificationCode.attempts_remaining > 0,
        )
        .order_by(VerificationCode.created_at.desc())
        .all()
    )
    if not candidates:
        return False
    code_hash = _hash_code(email, code)
    for vc in candidates:
        if secrets.compare_digest(vc.code_hash, code_hash):
            vc.consumed = True
            db.commit()
            return True
    for vc in candidates:
        vc.attempts_remaining -= 1
    db.commit()
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Sending the code — Brevo or SendGrid (single-sender verified address, no
#  domain ownership needed) or Resend (requires a verified domain to reach
#  arbitrary recipients). Falls back to a log line when none are configured.
# ─────────────────────────────────────────────────────────────────────────────
def _parse_from_address(raw: str):
    """'Name <email@x.com>' -> (name, email); a bare address -> (None, address)."""
    raw = raw.strip()
    m = re.match(r'^(.*)<(.+)>$', raw)
    if m:
        name = m.group(1).strip().strip('"')
        return (name or None), m.group(2).strip()
    return None, raw


def _send_via_brevo(email: str, code: str, api_key: str, from_addr: str) -> None:
    name, addr = _parse_from_address(from_addr)
    sender_obj = {"email": addr, **({"name": name} if name else {})}

    import requests
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": api_key, "Content-Type": "application/json", "accept": "application/json"},
        json={
            "sender": sender_obj,
            "to": [{"email": email}],
            "subject": "Your Supply Rush verification code",
            "textContent": f"Your verification code is: {code}\n\nIt expires in {CODE_TTL_MINUTES} minutes.",
        },
        timeout=10,
    )
    if resp.status_code >= 300:
        logger.error(f"Brevo API error sending to {email}: {resp.status_code} {resp.text}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send verification email. Please try again.")


def _send_via_sendgrid(email: str, code: str, api_key: str, from_addr: str) -> None:
    name, addr = _parse_from_address(from_addr)
    from_obj = {"email": addr, **({"name": name} if name else {})}

    import requests
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "personalizations": [{"to": [{"email": email}]}],
            "from": from_obj,
            "subject": "Your Supply Rush verification code",
            "content": [{
                "type": "text/plain",
                "value": f"Your verification code is: {code}\n\nIt expires in {CODE_TTL_MINUTES} minutes.",
            }],
        },
        timeout=10,
    )
    if resp.status_code >= 300:
        logger.error(f"SendGrid API error sending to {email}: {resp.status_code} {resp.text}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send verification email. Please try again.")


def _send_via_resend(email: str, code: str, api_key: str, from_addr: str) -> None:
    import requests
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": from_addr,
            "to": [email],
            "subject": "Your Supply Rush verification code",
            "text": f"Your verification code is: {code}\n\nIt expires in {CODE_TTL_MINUTES} minutes.",
        },
        timeout=10,
    )
    if resp.status_code >= 300:
        logger.error(f"Resend API error sending to {email}: {resp.status_code} {resp.text}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send verification email. Please try again.")


def send_verification_email(email: str, code: str) -> None:
    brevo_key = os.getenv("BREVO_API_KEY")
    sendgrid_key = os.getenv("SENDGRID_API_KEY")
    resend_key = os.getenv("RESEND_API_KEY")
    from_addr = os.getenv("EMAIL_FROM", "Supply Rush <onboarding@resend.dev>")

    if brevo_key:
        _send_via_brevo(email, code, brevo_key, from_addr)
        return
    if sendgrid_key:
        _send_via_sendgrid(email, code, sendgrid_key, from_addr)
        return
    if resend_key:
        _send_via_resend(email, code, resend_key, from_addr)
        return

    # Local/dev fallback: log instead of sending. This only matters when
    # REQUIRE_STUDENT_VERIFICATION is explicitly turned on without any email
    # provider configured — normal local dev never reaches this code path.
    logger.warning(f"[DEV — no BREVO_API_KEY/SENDGRID_API_KEY/RESEND_API_KEY set] Verification code for {email}: {code}")
