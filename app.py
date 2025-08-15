# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
from flask_pymongo import PyMongo
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import os
from bson.objectid import ObjectId
from datetime import datetime, date
from config import Config

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = app.config['SECRET_KEY']

# Inicializar PyMongo
mongo = PyMongo(app)

_initialized = False

# --- Funciones auxiliares ---


def get_avatar_url(email):
    """Genera la URL del avatar con DiceBear."""
    seed = hashlib.sha256(email.encode('utf-8')).hexdigest()
    return f"https://api.dicebear.com/9.x/thumbs/svg?seed={seed}&background=%23ffffff"


def get_current_user():
    """Obtiene el usuario actual desde la sesión."""
    if 'user_id' in session:
        user = mongo.db.usuarios.find_one(
            {"_id": ObjectId(session['user_id'])})
        if user:
            user['avatar_url'] = get_avatar_url(user['email'])
            return user
    return None

# --- Rutas ---


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        # Buscar usuario por email
        user = mongo.db.usuarios.find_one({"email": email})

        if user and check_password_hash(user['password'], password):
            # Iniciar sesión usando Flask session [[17]]
            session['user_id'] = str(user['_id'])
            flash('Inicio de sesión exitoso.', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Credenciales inválidas.', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash('Has cerrado sesión.', 'info')
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    if request.method == 'POST':
        # Actualizar datos del perfil (excepto email y rol)
        update_data = {
            "nombre_completo": request.form.get('nombre_completo'),
            # Agrega aquí otros campos editables como teléfono, etc.
        }

        # Solo el admin debería poder cambiar rol/email, por seguridad.
        # Si se permite cambiar email, se debe regenerar el avatar_url.

        mongo.db.usuarios.update_one(
            {"_id": ObjectId(user['_id'])}, {"$set": update_data})
        flash('Perfil actualizado correctamente.', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html', user=user)


@app.route('/dashboard')
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    # Lógica para mostrar contenido específico por rol
    if user['rol'] == 'administrador':
        # Contar tutores, grupos, estudiantes
        stats = {
            "tutores": mongo.db.usuarios.count_documents({"rol": "tutor"}),
            "grupos": mongo.db.grupos.estimated_document_count(),
            "estudiantes": mongo.db.usuarios.count_documents({"rol": "estudiante"})
        }
        return render_template('dashboard.html', user=user, stats=stats, dashboard_type='admin')

    elif user['rol'] == 'tutor':
        # Obtener grupos asignados al tutor
        grupos = list(mongo.db.grupos.find({"tutor_id": str(user['_id'])}))
        # Obtener IDs de estudiantes en esos grupos
        student_ids = []
        for grupo in grupos:
            student_ids.extend([ObjectId(sid)
                               for sid in grupo.get('estudiante_ids', [])])

        # Obtener datos básicos de estudiantes
        estudiantes = list(mongo.db.usuarios.find(
            {"_id": {"$in": student_ids}},
            {"nombre_completo": 1, "numero_control": 1}
        ))
        return render_template('dashboard.html', user=user, grupos=grupos, estudiantes=estudiantes, dashboard_type='tutor')

    elif user['rol'] == 'estudiante':
        # Obtener hábitos activos base y personales
        habitos_base = list(mongo.db.habitos.find({"activo": True}))
        habitos_personales = list(mongo.db.habitos.find({
            "usuario_id": str(user['_id']),
            "tipo": "personal",
            "activo": True
        }))
        return render_template('dashboard.html', user=user, habitos_base=habitos_base, habitos_personales=habitos_personales, dashboard_type='estudiante')

    else:
        flash('Rol de usuario no reconocido.', 'error')
        return redirect(url_for('logout'))

# -- Rutas para gestión de hábitos (ADMIN)


def admin_required(f):
    """Decorador para requerir rol de administrador."""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash('Debes iniciar sesión.', 'error')
            return redirect(url_for('login'))
        if user.get('rol') != 'administrador':
            flash('Acceso denegado. Se requiere rol de administrador.', 'error')
            # Redirigir al dashboard del rol del usuario o a una página de error
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/admin/habitos')
@admin_required
def admin_habitos():
    """Muestra la lista de hábitos base para gestión."""
    # Obtener todos los hábitos base
    habitos = list(mongo.db.habitos.find({"tipo": "base"}))
    return render_template('admin_habitos.html', habitos=habitos)


@app.route('/admin/habitos/toggle/<habit_id>', methods=['POST'])
@admin_required
def admin_toggle_habito(habit_id):
    """Activa o desactiva un hábito base."""
    try:
        habit = mongo.db.habitos.find_one(
            {"_id": ObjectId(habit_id), "tipo": "base"})
        if not habit:
            flash('Hábito no encontrado.', 'error')
            return redirect(url_for('admin_habitos'))

        nuevo_estado = not habit.get('activo', True)
        mongo.db.habitos.update_one(
            {"_id": ObjectId(habit_id)},
            {"$set": {"activo": nuevo_estado}}
        )

        estado_str = "activado" if nuevo_estado else "desactivado"
        flash(
            f'Hábito "{habit["nombre"]}" {estado_str} correctamente.', 'success')
    except Exception as e:
        app.logger.error(f"Error al cambiar estado del hábito: {e}")
        flash('Ocurrió un error al cambiar el estado del hábito.', 'error')

    return redirect(url_for('admin_habitos'))


@app.route('/admin/habitos/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_nuevo_habito():
    """Crea un nuevo hábito base."""
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        clave = request.form.get('clave', '').strip().lower().replace(
            ' ', '_')  # Generar clave simple
        categoria = request.form.get('categoria', '')

        # Validaciones básicas
        if not nombre or not clave or categoria not in ['Académico', 'Bienestar']:
            flash('Por favor, completa todos los campos correctamente.', 'error')
            return render_template('admin_nuevo_habito.html', nombre=nombre, clave=clave, categoria=categoria)

        # Verificar si la clave ya existe
        if mongo.db.habitos.find_one({"clave": clave, "tipo": "base"}):
            flash(f'Ya existe un hábito con la clave "{clave}".', 'error')
            return render_template('admin_nuevo_habito.html', nombre=nombre, clave=clave, categoria=categoria)

        nuevo_habito = {
            "nombre": nombre,
            "clave": clave,
            "categoria": categoria,
            "activo": True,
            "tipo": "base"  # Especificar que es un hábito base
        }

        try:
            result = mongo.db.habitos.insert_one(nuevo_habito)
            if result.inserted_id:
                flash(f'Hábito "{nombre}" creado exitosamente.', 'success')
                return redirect(url_for('admin_habitos'))
            else:
                raise Exception("No se pudo insertar el documento.")
        except Exception as e:
            app.logger.error(f"Error al crear hábito: {e}")
            flash('Ocurrió un error al crear el hábito.', 'error')
            return render_template('admin_nuevo_habito.html', nombre=nombre, clave=clave, categoria=categoria)

    # Si es GET, mostrar el formulario vacío
    return render_template('admin_nuevo_habito.html')

# --- API Endpoints ---


@app.route('/api/registrar', methods=['POST'])
def api_registrar():
    user = get_current_user()
    if not user or user['rol'] != 'estudiante':
        return jsonify({"error": "Acceso denegado"}), 403

    data = request.get_json()
    habit_id = data.get('habit_id')
    status = data.get('status')  # 'cumplido', 'incumplido', 'no_aplica'
    nota = data.get('nota', '')  # Opcional

    if status not in ['cumplido', 'incumplido', 'no_aplica']:
        return jsonify({"error": "Estado inválido"}), 400

    # Asegurarse de que el hábito pertenece al usuario o es base
    habit = mongo.db.habitos.find_one({
        "$and": [
            {"_id": ObjectId(habit_id)},
            {"$or": [{"usuario_id": str(user['_id'])}, {"tipo": "base"}]}
        ]
    })
    if not habit:
        return jsonify({"error": "Hábito no encontrado"}), 404

    # Registrar en la colección de registros
    registro = {
        "usuario_id": str(user['_id']),
        "habito_id": habit_id,
        "fecha": date.today().isoformat(),
        "estado": status,
        "nota": nota
    }

    # Upsert: Si ya existe un registro para ese usuario, hábito y fecha, lo actualiza. Si no, lo crea.
    mongo.db.registros_habitos.replace_one(
        {
            "usuario_id": str(user['_id']),
            "habito_id": habit_id,
            "fecha": date.today().isoformat()
        },
        registro,
        upsert=True
    )

    return jsonify({"message": "Registro actualizado"}), 200


@app.route('/api/toggle-habito', methods=['POST'])
def api_toggle_habito():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Acceso denegado"}), 403

    data = request.get_json()
    habit_id = data.get('habit_id')
    action = data.get('action')  # 'toggle'

    if action != 'toggle':
        return jsonify({"error": "Acción inválida"}), 400

    # Para hábitos base, solo el admin puede desactivarlos globalmente.
    # Para hábitos personales, el estudiante puede activar/desactivar.
    habit = mongo.db.habitos.find_one({"_id": ObjectId(habit_id)})
    if not habit:
        return jsonify({"error": "Hábito no encontrado"}), 404

    if habit.get('tipo') == 'base':
        if user['rol'] != 'administrador':
            return jsonify({"error": "Solo el administrador puede modificar hábitos base."}), 403
        # Toggle global para hábitos base
        nuevo_estado = not habit.get('activo', True)
        mongo.db.habitos.update_one({"_id": ObjectId(habit_id)}, {
                                    "$set": {"activo": nuevo_estado}})

    elif habit.get('tipo') == 'personal' and habit.get('usuario_id') == str(user['_id']):
        # Toggle para hábito personal del usuario
        nuevo_estado = not habit.get('activo', True)
        mongo.db.habitos.update_one({"_id": ObjectId(habit_id)}, {
                                    "$set": {"activo": nuevo_estado}})
    else:
        return jsonify({"error": "Permiso denegado para modificar este hábito."}), 403

    return jsonify({"message": "Estado del hábito actualizado", "nuevo_estado": nuevo_estado}), 200


@app.route('/api/add-personal', methods=['POST'])
def api_add_personal():
    user = get_current_user()
    if not user or user['rol'] != 'estudiante':
        return jsonify({"error": "Acceso denegado"}), 403

    data = request.get_json()
    nombre = data.get('nombre', '').strip()
    categoria = data.get('categoria', '')  # 'Académico' o 'Bienestar'

    if not nombre or categoria not in ['Académico', 'Bienestar']:
        return jsonify({"error": "Datos inválidos"}), 400

    # Verificar límite de 2 hábitos personales
    count = mongo.db.habitos.count_documents({
        "usuario_id": str(user['_id']),
        "tipo": "personal"
    })
    if count >= 2:
        return jsonify({"error": "Límite de 2 hábitos personales alcanzado"}), 400

    nuevo_habito = {
        "nombre": nombre,
        "categoria": categoria,
        "tipo": "personal",  # Para distinguir de los base
        "usuario_id": str(user['_id']),  # Relación con el usuario
        "activo": True
    }
    result = mongo.db.habitos.insert_one(nuevo_habito)

    return jsonify({"message": "Hábito personal creado", "id": str(result.inserted_id)}), 201


@app.context_processor
def inject_now():
    from datetime import datetime
    return {'now': datetime.now}  # Hora local


# Hacer que el usuario actual esté disponible en todas las plantillas
@app.before_request
def load_logged_in_user():
    g.current_user = get_current_user()


@app.before_request
def initialize_app():
    global _initialized
    if not _initialized:
        # --- Llamada a la función de inicialización ---
        create_initial_data()
        _initialized = True


def create_initial_data():
    # Crear hábitos base si no existen
    habitos_base = [
        {"clave": "estudio_diario", "nombre": "Estudio diario",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "lectura_materiales", "nombre": "Lectura de materiales",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "organizacion_tareas", "nombre": "Organización de tareas",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "entrega_oportuna", "nombre": "Entrega oportuna",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "participacion_clase", "nombre": "Participación en clase",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "resolucion_dudas", "nombre": "Resolución de dudas",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "uso_horario_tutorias", "nombre": "Uso de horario de tutorías",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "autoevaluacion_semanal", "nombre": "Autoevaluación semanal",
            "categoria": "Académico", "activo": True, "tipo": "base"},
        {"clave": "sueno_regular", "nombre": "Sueño regular",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "hidratacion", "nombre": "Hidratación",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "alimentacion_balanceada", "nombre": "Alimentación balanceada",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "ejercicio_fisico", "nombre": "Ejercicio físico",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "desconexion_digital", "nombre": "Desconexión digital",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "tiempo_ocio", "nombre": "Tiempo de ocio",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "manejo_estres", "nombre": "Manejo del estrés",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
        {"clave": "conexion_social", "nombre": "Conexión social",
            "categoria": "Bienestar", "activo": True, "tipo": "base"},
    ]

    for habito in habitos_base:
        existing = mongo.db.habitos.find_one({"clave": habito["clave"]})
        if not existing:
            mongo.db.habitos.insert_one(habito)

    # Crear usuario administrador por defecto (si no existe)
    admin_email = "admin@tecnm.mx"
    existing_admin = mongo.db.usuarios.find_one({"email": admin_email})
    if not existing_admin:
        admin_data = {
            "nombre_completo": "Administrador del Sistema",
            "email": admin_email,
            # Cambia esta contraseña por defecto
            "password": generate_password_hash("Admin123"),
            "rol": "administrador"
        }
        mongo.db.usuarios.insert_one(admin_data)
        print("Usuario administrador creado por defecto. Email: admin@tecnm.mx, Contraseña: Admin123")


if __name__ == '__main__':
    app.run(debug=True)
