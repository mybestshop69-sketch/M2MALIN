from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import select

from services import MetaClient, SocialApiError, TikTokClient

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", secrets.token_urlsafe(48))
database_url = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'm2malin_social.sqlite'}")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://") and "+psycopg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["WTF_CSRF_TIME_LIMIT"] = None

db = SQLAlchemy(app)
csrf = CSRFProtect(app)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def fernet() -> Fernet:
    configured = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode())
        except (ValueError, TypeError):
            digest = hashlib.sha256(configured.encode()).digest()
            return Fernet(base64.urlsafe_b64encode(digest))
    digest = hashlib.sha256(app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    try:
        return fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Impossible de dechiffrer le jeton. Verifiez TOKEN_ENCRYPTION_KEY."
        ) from exc


class Connection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(30), unique=True, nullable=False)
    account_name = db.Column(db.String(255), nullable=False)
    access_token_encrypted = db.Column(db.Text, nullable=False)
    page_id = db.Column(db.String(100))
    instagram_user_id = db.Column(db.String(100))
    open_id = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class SocialPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    caption = db.Column(db.Text, nullable=False)
    media_url = db.Column(db.Text)
    media_type = db.Column(db.String(20), default="image", nullable=False)
    platforms = db.Column(db.String(120), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(30), default="draft", nullable=False)
    result_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    published_at = db.Column(db.DateTime)


from messenger_assistant import init_messenger_assistant


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected_user = os.getenv("ADMIN_USERNAME", "admin")
        expected_password = os.getenv("ADMIN_PASSWORD", "")
        auth = request.authorization

        if (
            not expected_password
            or not auth
            or not secrets.compare_digest(auth.username or "", expected_user)
            or not secrets.compare_digest(auth.password or "", expected_password)
        ):
            return Response(
                "Authentification requise. Configurez ADMIN_PASSWORD.",
                401,
                {"WWW-Authenticate": 'Basic realm="M2Malin Social Manager"'},
            )
        return view(*args, **kwargs)

    return wrapped


@app.before_request
def protect_dashboard():
    if request.endpoint in {"health", "static", "meta_webhook_verify", "meta_webhook_receive"}:
        return None
    return require_admin(lambda: None)()


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.template_filter("paris_time")
def paris_time(value: datetime) -> str:
    local_tz = ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "Europe/Paris"))
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(local_tz).strftime(
        "%d/%m/%Y %H:%M"
    )


@app.get("/")
def index():
    posts = db.session.scalars(
        select(SocialPost).order_by(SocialPost.scheduled_at.desc()).limit(100)
    ).all()
    connections = {
        item.platform: item
        for item in db.session.scalars(select(Connection)).all()
    }
    return render_template(
        "index.html",
        posts=posts,
        connections=connections,
        auto_mode=env_bool("AUTO_MODE_ENABLED", False),
        timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Paris"),
    )


@app.route("/posts/new", methods=["GET", "POST"])
def new_post():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        caption = request.form.get("caption", "").strip()
        media_url = request.form.get("media_url", "").strip() or None
        media_type = request.form.get("media_type", "image")
        platforms = request.form.getlist("platforms")
        scheduled_raw = request.form.get("scheduled_at", "").strip()

        if not title or not caption or not platforms or not scheduled_raw:
            flash("Remplissez tous les champs obligatoires.", "error")
            return render_template("new_post.html")

        try:
            local_tz = ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "Europe/Paris"))
            local_dt = datetime.fromisoformat(scheduled_raw).replace(tzinfo=local_tz)
            scheduled_utc = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        except ValueError:
            flash("Date de programmation invalide.", "error")
            return render_template("new_post.html")

        auto_mode = env_bool("AUTO_MODE_ENABLED", False)
        approval_required = env_bool("HUMAN_APPROVAL_REQUIRED", True)
        status = "approved" if auto_mode and not approval_required else "draft"

        post = SocialPost(
            title=title,
            caption=caption,
            media_url=media_url,
            media_type=media_type,
            platforms=",".join(platforms),
            scheduled_at=scheduled_utc,
            status=status,
        )
        db.session.add(post)
        db.session.commit()
        flash("Publication enregistree.", "success")
        return redirect(url_for("index"))

    return render_template("new_post.html")


@app.post("/posts/<int:post_id>/approve")
def approve_post(post_id: int):
    post = db.get_or_404(SocialPost, post_id)
    post.status = "approved"
    post.result_message = None
    db.session.commit()
    flash("Publication approuvee.", "success")
    return redirect(url_for("index"))


@app.post("/posts/<int:post_id>/publish")
def publish_now(post_id: int):
    post = db.get_or_404(SocialPost, post_id)
    post.status = "approved"
    post.scheduled_at = datetime.utcnow()
    db.session.commit()
    publish_post(post.id)
    flash("Tentative de publication terminee.", "success")
    return redirect(url_for("index"))


@app.post("/posts/<int:post_id>/delete")
def delete_post(post_id: int):
    post = db.get_or_404(SocialPost, post_id)
    db.session.delete(post)
    db.session.commit()
    flash("Publication supprimee.", "success")
    return redirect(url_for("index"))


@app.get("/connect/meta")
def connect_meta():
    app_id = os.getenv("META_APP_ID", "")
    redirect_uri = os.getenv(
        "META_REDIRECT_URI",
        f"{os.getenv('APP_BASE_URL', 'http://localhost:5000')}/oauth/meta/callback",
    )
    if not app_id:
        flash("META_APP_ID n'est pas configure.", "error")
        return redirect(url_for("index"))

    state = secrets.token_urlsafe(32)
    session["meta_oauth_state"] = state
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": ",".join(
            [
                "pages_show_list",
                "pages_read_engagement",
                "pages_manage_posts",
                "instagram_basic",
                "instagram_content_publish",
                "business_management",
                "pages_messaging",
                "pages_manage_metadata",
            ]
        ),
    }
    return redirect(f"https://www.facebook.com/dialog/oauth?{urlencode(params)}")


@app.get("/oauth/meta/callback")
def meta_callback():
    if request.args.get("state") != session.pop("meta_oauth_state", None):
        flash("Echec de securite OAuth Meta.", "error")
        return redirect(url_for("index"))

    code = request.args.get("code")
    if not code:
        flash("Meta n'a pas renvoye de code d'autorisation.", "error")
        return redirect(url_for("index"))

    version = os.getenv("META_GRAPH_VERSION", "v23.0")
    redirect_uri = os.getenv(
        "META_REDIRECT_URI",
        f"{os.getenv('APP_BASE_URL', 'http://localhost:5000')}/oauth/meta/callback",
    )
    token_response = requests.get(
        f"https://graph.facebook.com/{version}/oauth/access_token",
        params={
            "client_id": os.getenv("META_APP_ID"),
            "client_secret": os.getenv("META_APP_SECRET"),
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    payload = token_response.json()
    user_token = payload.get("access_token")
    if not token_response.ok or not user_token:
        flash(f"Connexion Meta impossible : {payload}", "error")
        return redirect(url_for("index"))

    try:
        pages = MetaClient(version, user_token).get_pages()
        if not pages:
            raise SocialApiError("Aucune Page Facebook administree n'a ete trouvee.")
        page = pages[0]
        page_token = page["access_token"]
        instagram_id = MetaClient(version, page_token).get_instagram_account(
            page["id"], page_token
        )
    except (KeyError, SocialApiError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    connection = db.session.scalar(
        select(Connection).where(Connection.platform == "meta")
    )
    if connection is None:
        connection = Connection(
            platform="meta",
            account_name=page.get("name", "Page Facebook"),
            access_token_encrypted=encrypt_secret(page_token),
        )
        db.session.add(connection)

    connection.account_name = page.get("name", "Page Facebook")
    connection.access_token_encrypted = encrypt_secret(page_token)
    connection.page_id = page["id"]
    connection.instagram_user_id = instagram_id
    db.session.commit()
    flash("Facebook et Instagram sont connectes.", "success")
    return redirect(url_for("index"))


@app.get("/connect/tiktok")
def connect_tiktok():
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "")
    redirect_uri = os.getenv(
        "TIKTOK_REDIRECT_URI",
        f"{os.getenv('APP_BASE_URL', 'http://localhost:5000')}/oauth/tiktok/callback",
    )
    if not client_key:
        flash("TIKTOK_CLIENT_KEY n'est pas configure.", "error")
        return redirect(url_for("index"))

    state = secrets.token_urlsafe(32)
    session["tiktok_oauth_state"] = state
    params = {
        "client_key": client_key,
        "scope": os.getenv(
            "TIKTOK_SCOPES", "user.info.basic,video.publish,video.upload"
        ),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return redirect(f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}")


@app.get("/oauth/tiktok/callback")
def tiktok_callback():
    if request.args.get("state") != session.pop("tiktok_oauth_state", None):
        flash("Echec de securite OAuth TikTok.", "error")
        return redirect(url_for("index"))

    code = request.args.get("code")
    if not code:
        flash("TikTok n'a pas renvoye de code d'autorisation.", "error")
        return redirect(url_for("index"))

    redirect_uri = os.getenv(
        "TIKTOK_REDIRECT_URI",
        f"{os.getenv('APP_BASE_URL', 'http://localhost:5000')}/oauth/tiktok/callback",
    )
    response = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": os.getenv("TIKTOK_CLIENT_KEY"),
            "client_secret": os.getenv("TIKTOK_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    payload = response.json()
    access_token = payload.get("access_token")
    if not response.ok or not access_token:
        flash(f"Connexion TikTok impossible : {payload}", "error")
        return redirect(url_for("index"))

    connection = db.session.scalar(
        select(Connection).where(Connection.platform == "tiktok")
    )
    if connection is None:
        connection = Connection(
            platform="tiktok",
            account_name=payload.get("open_id", "Compte TikTok"),
            access_token_encrypted=encrypt_secret(access_token),
        )
        db.session.add(connection)

    connection.account_name = payload.get("open_id", "Compte TikTok")
    connection.open_id = payload.get("open_id")
    connection.access_token_encrypted = encrypt_secret(access_token)
    db.session.commit()
    flash("TikTok est connecte.", "success")
    return redirect(url_for("index"))


@app.post("/connections/<platform>/disconnect")
def disconnect(platform: str):
    connection = db.session.scalar(
        select(Connection).where(Connection.platform == platform)
    )
    if connection:
        db.session.delete(connection)
        db.session.commit()
    flash(f"Connexion {platform} supprimee.", "success")
    return redirect(url_for("index"))


def publish_post(post_id: int) -> None:
    with app.app_context():
        post = db.session.get(SocialPost, post_id)
        if post is None or post.status not in {"approved", "failed"}:
            return

        post.status = "publishing"
        db.session.commit()

        results: list[str] = []
        failures: list[str] = []
        requested_platforms = {
            item.strip() for item in post.platforms.split(",") if item.strip()
        }

        meta_connection = db.session.scalar(
            select(Connection).where(Connection.platform == "meta")
        )
        tiktok_connection = db.session.scalar(
            select(Connection).where(Connection.platform == "tiktok")
        )

        for platform in requested_platforms:
            try:
                if platform in {"facebook", "instagram"}:
                    if not meta_connection:
                        raise SocialApiError("Compte Meta non connecte.")
                    token = decrypt_secret(meta_connection.access_token_encrypted)
                    client = MetaClient(
                        os.getenv("META_GRAPH_VERSION", "v23.0"), token
                    )
                    if platform == "facebook":
                        result = client.publish_facebook(
                            meta_connection.page_id or "",
                            post.caption,
                            post.media_url,
                            post.media_type,
                        )
                    else:
                        if not meta_connection.instagram_user_id:
                            raise SocialApiError(
                                "Aucun compte Instagram professionnel lie a la Page."
                            )
                        result = client.publish_instagram(
                            meta_connection.instagram_user_id,
                            post.caption,
                            post.media_url or "",
                            post.media_type,
                        )
                elif platform == "tiktok":
                    if not tiktok_connection:
                        raise SocialApiError("Compte TikTok non connecte.")
                    result = TikTokClient(
                        decrypt_secret(tiktok_connection.access_token_encrypted)
                    ).publish(
                        post.caption,
                        post.media_url or "",
                        post.media_type,
                    )
                else:
                    raise SocialApiError(f"Plateforme inconnue : {platform}")

                results.append(f"{platform}: {result}")
            except Exception as exc:  # Keep scheduler alive; error is stored for review.
                failures.append(f"{platform}: {exc}")

        if failures:
            post.status = "failed"
            post.result_message = " | ".join(results + failures)
        else:
            post.status = "published"
            post.published_at = datetime.utcnow()
            post.result_message = " | ".join(results)
        db.session.commit()


def process_due_posts() -> None:
    with app.app_context():
        due_ids = db.session.scalars(
            select(SocialPost.id).where(
                SocialPost.status == "approved",
                SocialPost.scheduled_at <= datetime.utcnow(),
            )
        ).all()
    for post_id in due_ids:
        publish_post(post_id)


messenger_assistant = init_messenger_assistant(
    app=app,
    db=db,
    csrf=csrf,
    connection_model=Connection,
    decrypt_secret=decrypt_secret,
    encrypt_secret=encrypt_secret,
    env_bool=env_bool,
)


with app.app_context():
    db.create_all()
    messenger_assistant["reset_stuck_processing"]()
    db.session.commit()

scheduler = BackgroundScheduler(
    timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Paris"),
    daemon=True,
)
scheduler.add_job(
    process_due_posts,
    "interval",
    minutes=1,
    id="publish-due-posts",
    max_instances=1,
    coalesce=True,
)
scheduler.add_job(
    messenger_assistant["process_pending"],
    "interval",
    seconds=15,
    id="messenger-pending-messages",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=30,
)
scheduler.add_job(
    messenger_assistant["refresh_site_knowledge"],
    "interval",
    hours=6,
    id="messenger-refresh-site-knowledge",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)
if not app.testing and os.getenv("DISABLE_SCHEDULER", "false").lower() != "true":
    scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
