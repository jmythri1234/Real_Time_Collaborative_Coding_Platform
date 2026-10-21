from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from flask_socketio import SocketIO, join_room, emit
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import os

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", secrets.token_hex(32)
)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///coding_platform.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# ---------------- DATABASE MODELS ----------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)


class CodingRoom(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_code = db.Column(
        db.String(12), unique=True, nullable=False
    )
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.Text, default="")
    language = db.Column(db.String(30), default="python")
    owner_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False
    )


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------- HOME ----------------

@app.route("/")
def home():
    return render_template("index.html")


# ---------------- REGISTER ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Please fill in all fields.", "error")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
            return redirect(url_for("register"))

        existing = User.query.filter(
            (User.username == username) | (User.email == email)
        ).first()

        if existing:
            flash("Username or email already exists.", "error")
            return redirect(url_for("register"))

        user = User(
            username=username,
            email=email,
            password=generate_password_hash(password)
        )

        db.session.add(user)
        db.session.commit()

        flash("Registration successful! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


# ---------------- LOGOUT ----------------

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    rooms = CodingRoom.query.filter_by(
        owner_id=current_user.id
    ).order_by(CodingRoom.id.desc()).all()

    return render_template("dashboard.html", rooms=rooms)


# ---------------- CREATE ROOM ----------------

@app.route("/create-room", methods=["POST"])
@login_required
def create_room():
    name = request.form.get("name", "").strip()
    language = request.form.get("language", "python")

    allowed_languages = {"python", "javascript", "java", "cpp"}

    if not name:
        flash("Please enter a room name.", "error")
        return redirect(url_for("dashboard"))

    if len(name) > 100 or language not in allowed_languages:
        flash("Invalid room details.", "error")
        return redirect(url_for("dashboard"))

    room_code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]

    room = CodingRoom(
        room_code=room_code,
        name=name,
        language=language,
        owner_id=current_user.id
    )

    db.session.add(room)
    db.session.commit()

    return redirect(url_for("coding_room", room_code=room_code))


# ---------------- JOIN ROOM ----------------

@app.route("/join-room", methods=["POST"])
@login_required
def join_existing_room():
    room_code = request.form.get("room_code", "").strip()

    room = CodingRoom.query.filter_by(room_code=room_code).first()

    if not room:
        flash("Coding room not found.", "error")
        return redirect(url_for("dashboard"))

    return redirect(url_for("coding_room", room_code=room.room_code))


# ---------------- CODING ROOM ----------------

@app.route("/room/<room_code>")
@login_required
def coding_room(room_code):
    room = CodingRoom.query.filter_by(room_code=room_code).first_or_404()

    return render_template("coding_room.html", room=room)


# ---------------- REAL-TIME CODE SYNC ----------------

@socketio.on("join_room")
def handle_join_room(data):
    if not current_user.is_authenticated:
        return

    room_code = data.get("room_code", "")
    room = CodingRoom.query.filter_by(room_code=room_code).first()

    if not room:
        return

    join_room(room_code)

    emit(
        "load_code",
        {
            "code": room.code,
            "language": room.language
        },
        to=request.sid
    )


@socketio.on("code_change")
def handle_code_change(data):
    if not current_user.is_authenticated:
        return

    room_code = data.get("room_code", "")
    code = data.get("code", "")

    if not isinstance(code, str) or len(code) > 100000:
        return

    room = CodingRoom.query.filter_by(room_code=room_code).first()

    if not room:
        return

    # Only users who joined this room can update its code.
    from flask_socketio import rooms as socket_rooms
    if room_code not in socket_rooms():
        return

    room.code = code
    db.session.commit()

    emit(
        "code_update",
        {"code": code},
        to=room_code,
        include_self=False
    )


# ---------------- REAL-TIME CHAT ----------------

@socketio.on("chat_message")
def handle_chat_message(data):
    if not current_user.is_authenticated:
        return

    room_code = data.get("room_code", "")
    message = data.get("message", "")

    if not isinstance(message, str):
        return

    message = message.strip()

    if not message or len(message) > 500:
        return

    room = CodingRoom.query.filter_by(room_code=room_code).first()

    if not room:
        return

    from flask_socketio import rooms as socket_rooms
    if room_code not in socket_rooms():
        return

    emit(
        "new_message",
        {
            "username": current_user.username,
            "message": message
        },
        to=room_code
    )


# ---------------- START APPLICATION ----------------

with app.app_context():
    db.create_all()


if __name__ == "__main__":
    socketio.run(
        app,
        debug=True,
        host="127.0.0.1",
        port=5000
    )