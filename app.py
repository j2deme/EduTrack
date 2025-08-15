# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
from flask_pymongo import PyMongo
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import os
from bson.objectid import ObjectId
from datetime import datetime, date, timedelta
from config import Config
import calendar

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
        from datetime import date, timedelta

        tutor_id_str = str(user['_id'])

        # 1. Obtener grupos asignados al tutor
        grupos = list(mongo.db.grupos.find({"tutor_id": tutor_id_str}))
        grupo_ids = [str(g['_id']) for g in grupos]

        # 2. Obtener IDs de estudiantes en esos grupos
        student_ids = []
        for grupo in grupos:
            # Ya son strings
            student_ids.extend(grupo.get('estudiante_ids', []))

        # 3. Contar totales
        total_grupos = len(grupos)
        total_estudiantes = len(student_ids)

        # 4. Obtener datos básicos de estudiantes
        estudiantes = list(mongo.db.usuarios.find(
            {"_id": {"$in": [ObjectId(sid) for sid in student_ids]}},
            {"nombre_completo": 1, "numero_control": 1}
        ))

        # 5. --- NUEVO: Obtener estadísticas resumidas para el dashboard ---
        stats_resumen = {
            'total_grupos': total_grupos,
            'total_estudiantes': total_estudiantes,
            'promedio_cumplimiento_grupal': 0.0,
            'grupos_ranking': [],  # Top 3 grupos por cumplimiento
            'ultimos_registros': [],  # Últimos 5 registros de estudiantes
            'estudiantes_sin_actividad': []  # Estudiantes sin registro en los últimos 3 días
        }

        if grupo_ids and student_ids:
            # a. Promedio de cumplimiento grupal (simplificado: últimos 7 días)
            hace_7_dias = date.today() - timedelta(days=7)

            # Obtener hábitos base activos para el cálculo
            habitos_base_ids = [str(h['_id']) for h in mongo.db.habitos.find(
                {"activo": True, "tipo": "base"}, {"_id": 1})]
            total_habitos_base = len(
                habitos_base_ids) if habitos_base_ids else 1

            # Registros de los últimos 7 días de los estudiantes del tutor
            registros_7_dias = list(mongo.db.registros_habitos.find({
                "usuario_id": {"$in": student_ids},
                "habito_id": {"$in": habitos_base_ids},
                "fecha": {"$gte": hace_7_dias.isoformat()}
            }))

            # Calcular promedio grupal
            total_registros_esperados = total_estudiantes * total_habitos_base * 7
            total_registros_reales = len(registros_7_dias)
            if total_registros_esperados > 0:
                stats_resumen['promedio_cumplimiento_grupal'] = round(
                    (total_registros_reales / total_registros_esperados) * 100, 2)
            else:
                stats_resumen['promedio_cumplimiento_grupal'] = 0.0

            # b. Ranking de grupos (simplificado)
            if habitos_base_ids:
                cumplimiento_por_grupo = {}
                for grupo in grupos:
                    g_id = str(grupo['_id'])
                    estudiantes_grupo = grupo.get('estudiante_ids', [])
                    if not estudiantes_grupo:
                        cumplimiento_por_grupo[g_id] = {
                            'nombre': grupo['nombre'], 'promedio': 0.0}
                        continue

                    registros_grupo = [
                        r for r in registros_7_dias if r['usuario_id'] in estudiantes_grupo]
                    total_esperado_grupo = len(
                        estudiantes_grupo) * total_habitos_base * 7
                    total_real_grupo = len(registros_grupo)
                    if total_esperado_grupo > 0:
                        promedio_grupo = (total_real_grupo /
                                          total_esperado_grupo) * 100
                    else:
                        promedio_grupo = 0.0
                    cumplimiento_por_grupo[g_id] = {
                        'nombre': grupo['nombre'], 'promedio': round(promedio_grupo, 2)}

                # Ordenar y tomar top 3
                grupos_ordenados = sorted(cumplimiento_por_grupo.items(
                ), key=lambda item: item[1]['promedio'], reverse=True)
                stats_resumen['grupos_ranking'] = grupos_ordenados[:3]

            # c. Últimos registros (5 más recientes)
            # Obtener los últimos 5 registros ordenados por fecha (desc) y luego invertir para mostrarlos en orden
            ultimos_5_registros_cursor = mongo.db.registros_habitos.find(
                {"usuario_id": {"$in": student_ids}}
            ).sort("fecha", -1).limit(5)  # -1 para orden descendente

            ultimos_5_registros = list(ultimos_5_registros_cursor)
            # Obtener info de estudiantes y hábitos para mostrar nombres
            ultimos_estudiante_ids = list(
                set(r['usuario_id'] for r in ultimos_5_registros))
            ultimos_habito_ids = list(set(r['habito_id']
                                      for r in ultimos_5_registros))

            ultimos_estudiantes_dict = {str(e['_id']): e for e in mongo.db.usuarios.find(
                {"_id": {"$in": [ObjectId(sid)
                                 for sid in ultimos_estudiante_ids]}},
                {"nombre_completo": 1, "numero_control": 1}
            )}
            ultimos_habitos_dict = {str(h['_id']): h for h in mongo.db.habitos.find(
                {"_id": {"$in": [ObjectId(hid)
                                 for hid in ultimos_habito_ids]}},
                {"nombre": 1}
            )}

            for registro in ultimos_5_registros:
                est_info = ultimos_estudiantes_dict.get(registro['usuario_id'])
                hab_info = ultimos_habitos_dict.get(registro['habito_id'])
                stats_resumen['ultimos_registros'].append({
                    'estudiante_nombre': est_info['nombre_completo'] if est_info else 'Desconocido',
                    'estudiante_numero_control': est_info['numero_control'] if est_info else 'N/A',
                    'habito_nombre': hab_info['nombre'] if hab_info else 'Desconocido',
                    'fecha': registro['fecha'],
                    'estado': registro['estado']
                })
            # Ordenar por fecha ascendente (más viejo primero en la lista)
            stats_resumen['ultimos_registros'].sort(key=lambda x: x['fecha'])

            # d. Estudiantes sin actividad en los últimos 3 días
            hace_3_dias = date.today() - timedelta(days=3)
            # Encontrar estudiantes que tienen registros en los últimos 3 días
            estudiantes_activos_reciente = set()
            registros_3_dias = mongo.db.registros_habitos.find({
                "usuario_id": {"$in": student_ids},
                "fecha": {"$gte": hace_3_dias.isoformat()}
            })
            for r in registros_3_dias:
                estudiantes_activos_reciente.add(r['usuario_id'])

            # Estudiantes totales - estudiantes activos reciente = estudiantes inactivos
            estudiantes_sin_actividad_ids = [
                sid for sid in student_ids if sid not in estudiantes_activos_reciente]

            # Obtener datos de los estudiantes inactivos
            if estudiantes_sin_actividad_ids:
                estudiantes_sin_actividad_cursor = mongo.db.usuarios.find(
                    {"_id": {"$in": [ObjectId(sid)
                                     for sid in estudiantes_sin_actividad_ids]}},
                    {"nombre_completo": 1, "numero_control": 1,
                        "email": 1}  # Puedes incluir más info
                )
                # Limitar a 5 para no abrumar
                stats_resumen['estudiantes_sin_actividad'] = list(
                    estudiantes_sin_actividad_cursor)[:5]

        # Pasar todos los datos a la plantilla
        return render_template(
            'dashboard.html',
            user=user,
            grupos=grupos,
            estudiantes=estudiantes,
            dashboard_type='tutor',
            stats_resumen=stats_resumen
        )

    elif user['rol'] == 'estudiante':
        # Obtener hábitos activos base y personales
        habitos_base = list(mongo.db.habitos.find(
            {"activo": True, "tipo": "base"}))
        habitos_personales = list(mongo.db.habitos.find({
            "usuario_id": str(user['_id']),
            "tipo": "personal",
            "activo": True
        }))

        # --- NUEVO: Obtener registros del estudiante para hoy ---
        from datetime import date
        hoy = date.today().isoformat()
        registros_hoy_cursor = mongo.db.registros_habitos.find({
            "usuario_id": str(user['_id']),
            "fecha": hoy
        })

        # Convertir los registros a un diccionario para fácil acceso {habito_id: registro}
        registros_hoy_dict = {}
        for registro in registros_hoy_cursor:
            # Guardamos todo el documento del registro
            registros_hoy_dict[registro['habito_id']] = registro

        # Pasar los registros a la plantilla
        return render_template('dashboard.html',
                               user=user,
                               habitos_base=habitos_base,
                               habitos_personales=habitos_personales,
                               registros_hoy=registros_hoy_dict,  # Nuevo argumento
                               dashboard_type='estudiante')

    else:
        flash('Rol de usuario no reconocido.', 'error')
        return redirect(url_for('logout'))


@app.route('/calendar')
def calendar_view():
    """Muestra la vista de calendario para el estudiante."""
    user = get_current_user()
    if not user or user['rol'] != 'estudiante':
        flash('Acceso denegado.', 'error')
        return redirect(url_for('login'))

    # Obtener el mes y año actuales
    today = date.today()
    year = today.year
    month = today.month

    # 1. Obtener el primer y último día del mes
    # CORRECCION: Usar el módulo calendar correctamente
    first_day = date(year, month, 1)
    # Usar calendar.monthrange (del MODULO calendar) para obtener el último día
    last_day_num = calendar.monthrange(
        year, month)[1]  # <--- Aquí estaba el error
    last_day = date(year, month, last_day_num)

    # 2. Obtener registros del estudiante para todo el mes
    registros_mes_cursor = mongo.db.registros_habitos.find({
        "usuario_id": str(user['_id']),
        "fecha": {
            "$gte": first_day.isoformat(),
            "$lte": last_day.isoformat()
        }
    })

    # 3. Procesar registros
    registros_por_dia = {}
    fechas_con_registros = set()

    # Obtener hábitos activos
    habitos_activos_base_ids = [str(h['_id']) for h in mongo.db.habitos.find(
        {"activo": True, "tipo": "base"}, {"_id": 1})]
    habitos_activos_personal_ids = [str(h['_id']) for h in mongo.db.habitos.find({
        "usuario_id": str(user['_id']),
        "tipo": "personal",
        "activo": True
    }, {"_id": 1})]
    total_habitos_activos = len(
        habitos_activos_base_ids) + len(habitos_activos_personal_ids)

    # Manejar caso de división por cero si no hay hábitos activos
    if total_habitos_activos == 0:
        total_habitos_activos = 1  # Evitar división por cero, el progreso será 0% o 100%

    for registro in registros_mes_cursor:
        fecha_str = registro['fecha']
        if fecha_str not in registros_por_dia:
            registros_por_dia[fecha_str] = []
        registros_por_dia[fecha_str].append(registro)
        fechas_con_registros.add(fecha_str)

    # 4. Generar datos del calendario usando el MODULO calendar
    cal_matrix = calendar.monthcalendar(year, month)  # <--- También aquí

    # Información para la plantilla
    mes_nombre = first_day.strftime('%B').capitalize()
    mes_nombre_es = {
        'January': 'Enero', 'February': 'Febrero', 'March': 'Marzo',
        'April': 'Abril', 'May': 'Mayo', 'June': 'Junio',
        'July': 'Julio', 'August': 'Agosto', 'September': 'Septiembre',
        'October': 'Octubre', 'November': 'Noviembre', 'December': 'Diciembre'
    }.get(mes_nombre, mes_nombre)

    dias_semana_es = ['Lu', 'Ma', 'Mi', 'Ju', 'Vi', 'Sa', 'Do']

    return render_template(
        'student_calendar.html',
        user=user,
        year=year,
        month=month,
        mes_nombre_es=mes_nombre_es,
        dias_semana_es=dias_semana_es,
        calendar_matrix=cal_matrix,
        registros_por_dia=registros_por_dia,
        fechas_con_registros=fechas_con_registros,
        total_habitos_activos=total_habitos_activos,
        today=today
    )


@app.route('/stats')
def stats():
    """Muestra estadísticas generales para el tutor (por grupo)."""
    user = get_current_user()
    if not user or user['rol'] != 'tutor':
        flash('Acceso denegado.', 'error')
        return redirect(url_for('login'))

    # Obtener grupos asignados al tutor
    grupos = list(mongo.db.grupos.find({"tutor_id": str(user['_id'])}))

    # Para cada grupo, calcular estadísticas
    stats_por_grupo = []
    for grupo in grupos:
        grupo_id = str(grupo['_id'])
        nombre_grupo = grupo['nombre']

        # Obtener IDs de estudiantes del grupo
        student_ids_str = grupo.get('estudiante_ids', [])
        if not student_ids_str:
            # Si no hay estudiantes, estadísticas vacías
            stats_por_grupo.append({
                'grupo_id': grupo_id,
                'nombre_grupo': nombre_grupo,
                'num_estudiantes': 0,
                'promedio_cumplimiento': 0.0,
                'estudiantes_data': []  # Para detalles si se expande
            })
            continue

        student_ids_oid = [ObjectId(sid) for sid in student_ids_str]

        # Obtener datos de estudiantes
        estudiantes_grupo = list(mongo.db.usuarios.find(
            {"_id": {"$in": student_ids_oid}},
            {"nombre_completo": 1, "numero_control": 1}
        ))

        # Obtener hábitos activos base (los personales son muy individuales para stats grupales)
        habitos_activos_base_ids = [str(h['_id']) for h in mongo.db.habitos.find(
            {"activo": True, "tipo": "base"}, {"_id": 1})]
        total_habitos_base = len(habitos_activos_base_ids)

        if total_habitos_base == 0:
            total_habitos_base = 1  # Evitar división por cero

        # Obtener registros de los últimos 30 días para estos estudiantes y hábitos base
        hace_30_dias = date.today() - timedelta(days=30)
        registros_recientes = list(mongo.db.registros_habitos.find({
            "usuario_id": {"$in": student_ids_str},
            "habito_id": {"$in": habitos_activos_base_ids},
            "fecha": {"$gte": hace_30_dias.isoformat()}
        }))

        # Calcular estadísticas
        # 1. Promedio de cumplimiento del grupo
        total_registros_esperados = len(
            student_ids_str) * total_habitos_base * 30  # Aproximación
        total_registros_reales = len(registros_recientes)

        if total_registros_esperados > 0:
            promedio_cumplimiento = (
                total_registros_reales / total_registros_esperados) * 100
        else:
            promedio_cumplimiento = 0.0

        # 2. Datos por estudiante (para futuras visualizaciones)
        estudiantes_data = []
        for estudiante in estudiantes_grupo:
            est_id = str(estudiante['_id'])
            # Contar registros de este estudiante en el periodo
            registros_estudiante = [
                r for r in registros_recientes if r['usuario_id'] == est_id]
            # Calcular su propio promedio
            registros_esperados_est = total_habitos_base * 30
            if registros_esperados_est > 0:
                promedio_estudiante = (
                    len(registros_estudiante) / registros_esperados_est) * 100
            else:
                promedio_estudiante = 0.0

            estudiantes_data.append({
                'id': est_id,
                'nombre': estudiante['nombre_completo'],
                'numero_control': estudiante['numero_control'],
                'promedio': round(promedio_estudiante, 2)
            })

        stats_por_grupo.append({
            'grupo_id': grupo_id,
            'nombre_grupo': nombre_grupo,
            'num_estudiantes': len(estudiantes_grupo),
            'promedio_cumplimiento': round(promedio_cumplimiento, 2),
            'estudiantes_data': estudiantes_data
        })

    return render_template('tutor_stats.html', user=user, stats_por_grupo=stats_por_grupo)


@app.route('/stats/user/<user_id>')
def stats_user(user_id):
    """Muestra estadísticas detalladas de un estudiante específico."""
    tutor = get_current_user()
    if not tutor or tutor['rol'] != 'tutor':
        flash('Acceso denegado.', 'error')
        return redirect(url_for('login'))

    try:
        # Verificar que el estudiante pertenece a un grupo del tutor
        # 1. Obtener grupos del tutor
        grupos_tutor_ids = [g['_id'] for g in mongo.db.grupos.find(
            {"tutor_id": str(tutor['_id'])}, {"_id": 1})]
        # 2. Verificar si el estudiante está en alguno de esos grupos
        estudiante_obj = mongo.db.usuarios.find_one({
            "_id": ObjectId(user_id),
            "rol": "estudiante"
        })
        if not estudiante_obj:
            flash('Estudiante no encontrado.', 'error')
            return redirect(url_for('stats'))  # O al dashboard

        # Verificar pertenencia a grupo
        pertenece_al_tutor = mongo.db.grupos.find_one({
            "_id": {"$in": grupos_tutor_ids},
            "estudiante_ids": user_id
        })
        if not pertenece_al_tutor:
            flash(
                'No tienes permiso para ver las estadísticas de este estudiante.', 'error')
            return redirect(url_for('stats'))

        # --- Recopilar datos del estudiante ---
        estudiante = estudiante_obj

        # 1. Obtener hábitos activos base y personales del estudiante
        habitos_base = list(mongo.db.habitos.find(
            {"activo": True, "tipo": "base"}))
        habitos_personales = list(mongo.db.habitos.find({
            "usuario_id": user_id,
            "tipo": "personal",
            "activo": True
        }))

        # 2. Obtener registros de los últimos 30 días
        hace_30_dias = date.today() - timedelta(days=30)
        registros_30_dias = list(mongo.db.registros_habitos.find({
            "usuario_id": user_id,
            "fecha": {"$gte": hace_30_dias.isoformat()}
        }).sort("fecha", 1))  # Ordenar por fecha

        # 3. Procesar datos para la vista
        # a. Conteo por estado en los últimos 30 días
        conteo_estados = {'cumplido': 0, 'incumplido': 0, 'no_aplica': 0}
        for registro in registros_30_dias:
            estado = registro.get('estado')
            if estado in conteo_estados:
                conteo_estados[estado] += 1

        # b. Progreso por día (para gráfico)
        # Crear un diccionario {fecha: conteo_cumplidos}
        registros_por_fecha = {}
        for registro in registros_30_dias:
            fecha = registro['fecha']
            if fecha not in registros_por_fecha:
                registros_por_fecha[fecha] = {'cumplido': 0, 'total': 0}
            registros_por_fecha[fecha]['total'] += 1
            if registro['estado'] == 'cumplido':
                registros_por_fecha[fecha]['cumplido'] += 1

        # Convertir a listas ordenadas para el gráfico
        fechas_chart = sorted(registros_por_fecha.keys())
        cumplidos_chart = [registros_por_fecha[f]['cumplido']
                           for f in fechas_chart]
        totales_chart = [registros_por_fecha[f]['total'] for f in fechas_chart]

        # c. Progreso por hábito (últimos 7 días como ejemplo)
        hace_7_dias = date.today() - timedelta(days=7)
        registros_7_dias = [
            r for r in registros_30_dias if r['fecha'] >= hace_7_dias.isoformat()]

        progreso_por_habito = {}
        # Inicializar con todos los hábitos activos
        for habito in habitos_base + habitos_personales:
            progreso_por_habito[str(habito['_id'])] = {
                'nombre': habito['nombre'],
                'categoria': habito['categoria'],
                'tipo': habito['tipo'],
                'cumplido': 0,
                'total': 0
            }
        # Contar registros
        for registro in registros_7_dias:
            habito_id = registro['habito_id']
            if habito_id in progreso_por_habito:
                progreso_por_habito[habito_id]['total'] += 1
                if registro['estado'] == 'cumplido':
                    progreso_por_habito[habito_id]['cumplido'] += 1

        # Calcular porcentajes
        for habito_id, data in progreso_por_habito.items():
            if data['total'] > 0:
                data['porcentaje'] = round(
                    (data['cumplido'] / data['total']) * 100, 2)
            else:
                data['porcentaje'] = 0.0

    except Exception as e:
        app.logger.error(f"Error al obtener stats de usuario {user_id}: {e}")
        flash('Ocurrió un error al cargar las estadísticas.', 'error')
        return redirect(url_for('stats'))

    return render_template(
        'tutor_stats_user.html',
        tutor=tutor,
        estudiante=estudiante,
        conteo_estados=conteo_estados,
        fechas_chart=fechas_chart,
        cumplidos_chart=cumplidos_chart,
        totales_chart=totales_chart,
        progreso_por_habito=progreso_por_habito
    )

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

# --- Rutas para gestión de tutores (ADMIN)


@app.route('/admin/tutores')
@admin_required
def admin_gestionar_tutores():
    """Muestra la lista de tutores."""
    # Obtener todos los usuarios con rol 'tutor'
    tutores = list(mongo.db.usuarios.find({"rol": "tutor"}))
    return render_template('admin_tutores.html', tutores=tutores)


@app.route('/admin/tutores/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_nuevo_tutor():
    """Crea un nuevo usuario tutor."""
    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        rfc = request.form.get('rfc', '').strip().upper()  # RFC en mayúsculas
        area_adscripcion = request.form.get('area_adscripcion', '').strip()
        acreditacion = 'acreditacion' in request.form  # Checkbox

        # Validaciones básicas
        errors = []
        if not nombre_completo:
            errors.append("El nombre completo es obligatorio.")
        if not email:
            errors.append("El email es obligatorio.")
        else:
            # Validar formato de email básico (puedes usar librerías como 'email-validator' para más robustez)
            if "@" not in email or "." not in email:
                errors.append("El formato del email no es válido.")
            # Verificar unicidad de email
            elif mongo.db.usuarios.find_one({"email": email}):
                errors.append("Ya existe un usuario con ese email.")

        if not password:
            errors.append("La contraseña es obligatoria.")
        elif len(password) < 6:  # Ejemplo de validación de longitud
            errors.append("La contraseña debe tener al menos 6 caracteres.")

        # Puedes agregar validaciones para RFC si es necesario

        if errors:
            for error in errors:
                flash(error, 'error')
            # Volver a mostrar el formulario con los datos ingresados
            return render_template('admin_nuevo_tutor.html',
                                   nombre_completo=nombre_completo,
                                   email=email,
                                   rfc=rfc,
                                   area_adscripcion=area_adscripcion,
                                   acreditacion=acreditacion)

        # Si todo es válido, crear el tutor
        try:
            hashed_password = generate_password_hash(password)

            nuevo_tutor = {
                "nombre_completo": nombre_completo,
                "email": email,
                "password": hashed_password,
                "rol": "tutor",
                "rfc": rfc,
                "area_adscripcion": area_adscripcion,
                "acreditacion": acreditacion
            }

            result = mongo.db.usuarios.insert_one(nuevo_tutor)
            if result.inserted_id:
                flash(
                    f'Tutor "{nombre_completo}" creado exitosamente.', 'success')
                return redirect(url_for('admin_gestionar_tutores'))
            else:
                raise Exception("No se pudo insertar el documento.")

        except Exception as e:
            app.logger.error(f"Error al crear tutor: {e}")
            flash('Ocurrió un error al crear el tutor.', 'error')
            return render_template('admin_nuevo_tutor.html',
                                   nombre_completo=nombre_completo,
                                   email=email,
                                   rfc=rfc,
                                   area_adscripcion=area_adscripcion,
                                   acreditacion=acreditacion)

    # Si es GET, mostrar el formulario vacío
    return render_template('admin_nuevo_tutor.html')


@app.route('/admin/tutores/editar/<tutor_id>', methods=['GET', 'POST'])
@admin_required
def admin_editar_tutor(tutor_id):
    """Edita un usuario tutor existente."""
    try:
        tutor = mongo.db.usuarios.find_one(
            {"_id": ObjectId(tutor_id), "rol": "tutor"})
        if not tutor:
            flash('Tutor no encontrado.', 'error')
            return redirect(url_for('admin_gestionar_tutores'))
    except Exception:
        flash('ID de tutor inválido.', 'error')
        return redirect(url_for('admin_gestionar_tutores'))

    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        email = request.form.get('email', '').strip().lower()
        # La contraseña NO se edita aquí por seguridad. Se podría hacer en un flujo separado.
        rfc = request.form.get('rfc', '').strip().upper()
        area_adscripcion = request.form.get('area_adscripcion', '').strip()
        acreditacion = 'acreditacion' in request.form  # Checkbox

        # Validaciones básicas (menos la de unicidad de email, a menos que cambie)
        errors = []
        if not nombre_completo:
            errors.append("El nombre completo es obligatorio.")
        if not email:
            errors.append("El email es obligatorio.")
        else:
            if "@" not in email or "." not in email:
                errors.append("El formato del email no es válido.")
            # Verificar unicidad de email (excluyendo al propio tutor)
            elif mongo.db.usuarios.find_one({"email": email, "_id": {"$ne": ObjectId(tutor_id)}}):
                errors.append("Ya existe otro usuario con ese email.")

        if errors:
            for error in errors:
                flash(error, 'error')
            # Volver a mostrar el formulario con los datos ingresados
            return render_template('admin_editar_tutor.html', tutor=tutor)

        # Si todo es válido, actualizar el tutor
        try:
            update_data = {
                "nombre_completo": nombre_completo,
                "email": email,
                "rfc": rfc,
                "area_adscripcion": area_adscripcion,
                "acreditacion": acreditacion
                # No actualizamos la contraseña aquí
            }

            # Si se proporciona una nueva contraseña, se puede manejar aquí.
            # nueva_password = request.form.get('nueva_password', '')
            # if nueva_password:
            #     if len(nueva_password) < 6:
            #          flash('La nueva contraseña debe tener al menos 6 caracteres.', 'error')
            #          return render_template('admin_editar_tutor.html', tutor=tutor)
            #     update_data['password'] = generate_password_hash(nueva_password)

            result = mongo.db.usuarios.update_one(
                {"_id": ObjectId(tutor_id)},
                {"$set": update_data}
            )
            if result.matched_count > 0:
                flash(
                    f'Tutor "{nombre_completo}" actualizado exitosamente.', 'success')
                return redirect(url_for('admin_gestionar_tutores'))
            else:
                flash('No se encontró el tutor para actualizar.', 'error')

        except Exception as e:
            app.logger.error(f"Error al actualizar tutor: {e}")
            flash('Ocurrió un error al actualizar el tutor.', 'error')
            return render_template('admin_editar_tutor.html', tutor=tutor)

    # Si es GET, mostrar el formulario con los datos actuales del tutor
    return render_template('admin_editar_tutor.html', tutor=tutor)


# Usualmente DELETE, pero POST es más compatible con formularios simples
@app.route('/admin/tutores/eliminar/<tutor_id>', methods=['POST'])
@admin_required
def admin_eliminar_tutor(tutor_id):
    """Elimina un usuario tutor (CUIDADO: Esto es irreversible)."""
    """Opcionalmente, podrías desactivarlo en lugar de eliminarlo."""
    try:
        # Verificar que el tutor exista y sea de rol 'tutor'
        tutor = mongo.db.usuarios.find_one(
            {"_id": ObjectId(tutor_id), "rol": "tutor"})
        if not tutor:
            flash('Tutor no encontrado.', 'error')
            return redirect(url_for('admin_gestionar_tutores'))

        # Verificar si el tutor tiene grupos asignados
        grupos_asignados = mongo.db.grupos.count_documents(
            {"tutor_id": tutor_id})
        if grupos_asignados > 0:
            flash(
                f'No se puede eliminar el tutor "{tutor["nombre_completo"]}" porque tiene {grupos_asignados} grupo(s) asignado(s). Reasigna o elimina los grupos primero.', 'error')
            return redirect(url_for('admin_gestionar_tutores'))

        # Proceder con la eliminación (¡Esto es irreversible!)
        result = mongo.db.usuarios.delete_one({"_id": ObjectId(tutor_id)})
        if result.deleted_count > 0:
            flash(
                f'Tutor "{tutor["nombre_completo"]}" eliminado exitosamente.', 'success')
        else:
            flash('No se pudo eliminar el tutor.', 'error')
    except Exception as e:
        app.logger.error(f"Error al eliminar tutor: {e}")
        flash('Ocurrió un error al eliminar el tutor.', 'error')

    return redirect(url_for('admin_gestionar_tutores'))

# --- Rutas para gestión de grupos (ADMIN)


@app.route('/admin/grupos')
@admin_required
def admin_gestionar_grupos():
    """Muestra la lista de grupos."""
    try:
        # Obtener todos los grupos
        grupos_cursor = mongo.db.grupos.find()
        grupos = []
        for grupo in grupos_cursor:
            # Obtener información del tutor
            tutor = mongo.db.usuarios.find_one({"_id": ObjectId(grupo['tutor_id'])}, {
                                               "nombre_completo": 1}) if grupo.get('tutor_id') else None
            grupo['tutor_nombre'] = tutor['nombre_completo'] if tutor else 'No asignado'

            # Contar estudiantes
            grupo['num_estudiantes'] = len(grupo.get('estudiante_ids', []))

            grupos.append(grupo)

        return render_template('admin_grupos.html', grupos=grupos)
    except Exception as e:
        app.logger.error(f"Error al obtener grupos: {e}")
        flash('Ocurrió un error al cargar la lista de grupos.', 'error')
        return render_template('admin_grupos.html', grupos=[])


@app.route('/admin/grupos/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_nuevo_grupo():
    """Crea un nuevo grupo."""
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        ciclo_escolar = request.form.get(
            'ciclo_escolar', '').strip()  # Ej: 2024-A

        # Validaciones básicas
        if not nombre:
            flash('El nombre del grupo es obligatorio.', 'error')
            return render_template('admin_nuevo_grupo.html', nombre=nombre, ciclo_escolar=ciclo_escolar)

        # Verificar unicidad de nombre (opcional, pero buena práctica)
        if mongo.db.grupos.find_one({"nombre": nombre}):
            flash(f'Ya existe un grupo con el nombre "{nombre}".', 'error')
            return render_template('admin_nuevo_grupo.html', nombre=nombre, ciclo_escolar=ciclo_escolar)

        nuevo_grupo = {
            "nombre": nombre,
            "ciclo_escolar": ciclo_escolar,
            "tutor_id": None,  # Se asigna después
            "estudiante_ids": []  # Lista vacía inicialmente
        }

        try:
            result = mongo.db.grupos.insert_one(nuevo_grupo)
            if result.inserted_id:
                flash(f'Grupo "{nombre}" creado exitosamente.', 'success')
                return redirect(url_for('admin_gestionar_grupos'))
            else:
                raise Exception("No se pudo insertar el documento.")
        except Exception as e:
            app.logger.error(f"Error al crear grupo: {e}")
            flash('Ocurrió un error al crear el grupo.', 'error')
            return render_template('admin_nuevo_grupo.html', nombre=nombre, ciclo_escolar=ciclo_escolar)

    # Si es GET, mostrar el formulario vacío
    return render_template('admin_nuevo_grupo.html')


@app.route('/admin/grupos/editar/<grupo_id>', methods=['GET', 'POST'])
@admin_required
def admin_editar_grupo(grupo_id):
    """Edita un grupo existente (nombre, ciclo)."""
    try:
        grupo = mongo.db.grupos.find_one({"_id": ObjectId(grupo_id)})
        if not grupo:
            flash('Grupo no encontrado.', 'error')
            return redirect(url_for('admin_gestionar_grupos'))
    except Exception:
        flash('ID de grupo inválido.', 'error')
        return redirect(url_for('admin_gestionar_grupos'))

    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        ciclo_escolar = request.form.get('ciclo_escolar', '').strip()

        # Validaciones básicas
        if not nombre:
            flash('El nombre del grupo es obligatorio.', 'error')
            return render_template('admin_editar_grupo.html', grupo=grupo)

        # Verificar unicidad de nombre (excluyendo el grupo actual)
        if mongo.db.grupos.find_one({"nombre": nombre, "_id": {"$ne": ObjectId(grupo_id)}}):
            flash(f'Ya existe otro grupo con el nombre "{nombre}".', 'error')
            return render_template('admin_editar_grupo.html', grupo=grupo)

        try:
            update_data = {
                "nombre": nombre,
                "ciclo_escolar": ciclo_escolar
            }

            result = mongo.db.grupos.update_one(
                {"_id": ObjectId(grupo_id)},
                {"$set": update_data}
            )
            if result.matched_count > 0:
                flash(f'Grupo "{nombre}" actualizado exitosamente.', 'success')
                return redirect(url_for('admin_gestionar_grupos'))
            else:
                flash('No se encontró el grupo para actualizar.', 'error')

        except Exception as e:
            app.logger.error(f"Error al actualizar grupo: {e}")
            flash('Ocurrió un error al actualizar el grupo.', 'error')
            return render_template('admin_editar_grupo.html', grupo=grupo)

    # Si es GET, mostrar el formulario con los datos actuales del grupo
    return render_template('admin_editar_grupo.html', grupo=grupo)


@app.route('/admin/grupos/eliminar/<grupo_id>', methods=['POST'])
@admin_required
def admin_eliminar_grupo(grupo_id):
    """Elimina un grupo."""
    try:
        grupo = mongo.db.grupos.find_one({"_id": ObjectId(grupo_id)})
        if not grupo:
            flash('Grupo no encontrado.', 'error')
            return redirect(url_for('admin_gestionar_grupos'))

        # Proceder con la eliminación
        result = mongo.db.grupos.delete_one({"_id": ObjectId(grupo_id)})
        if result.deleted_count > 0:
            flash(
                f'Grupo "{grupo["nombre"]}" eliminado exitosamente.', 'success')
        else:
            flash('No se pudo eliminar el grupo.', 'error')
    except Exception as e:
        app.logger.error(f"Error al eliminar grupo: {e}")
        flash('Ocurrió un error al eliminar el grupo.', 'error')

    return redirect(url_for('admin_gestionar_grupos'))


@app.route('/admin/grupos/asignar_tutor/<grupo_id>', methods=['GET', 'POST'])
@admin_required
def admin_asignar_tutor(grupo_id):
    """Asigna o cambia el tutor de un grupo."""
    try:
        grupo = mongo.db.grupos.find_one({"_id": ObjectId(grupo_id)})
        if not grupo:
            flash('Grupo no encontrado.', 'error')
            return redirect(url_for('admin_gestionar_grupos'))
    except Exception:
        flash('ID de grupo inválido.', 'error')
        return redirect(url_for('admin_gestionar_grupos'))

    if request.method == 'POST':
        tutor_id = request.form.get('tutor_id')

        if not tutor_id or tutor_id == 'none':
            # Desasignar tutor
            try:
                mongo.db.grupos.update_one(
                    {"_id": ObjectId(grupo_id)},
                    {"$set": {"tutor_id": None}}
                )
                flash('Tutor desasignado del grupo correctamente.', 'success')
                return redirect(url_for('admin_gestionar_grupos'))
            except Exception as e:
                app.logger.error(f"Error al desasignar tutor: {e}")
                flash('Ocurrió un error al desasignar el tutor.', 'error')
                return redirect(url_for('admin_asignar_tutor', grupo_id=grupo_id))

        # Verificar que el tutor_id sea válido y sea un tutor
        try:
            tutor_obj = mongo.db.usuarios.find_one(
                {"_id": ObjectId(tutor_id), "rol": "tutor"})
            if not tutor_obj:
                flash('Tutor seleccionado inválido.', 'error')
                # Recargar la página para mostrar el error
                return redirect(url_for('admin_asignar_tutor', grupo_id=grupo_id))
        except Exception:
            flash('ID de tutor inválido.', 'error')
            return redirect(url_for('admin_asignar_tutor', grupo_id=grupo_id))

        try:
            # Asignar el tutor
            mongo.db.grupos.update_one(
                {"_id": ObjectId(grupo_id)},
                {"$set": {"tutor_id": tutor_id}}
            )
            flash(
                f'Tutor {tutor_obj["nombre_completo"]} asignado al grupo {grupo["nombre"]} correctamente.', 'success')
            return redirect(url_for('admin_gestionar_grupos'))
        except Exception as e:
            app.logger.error(f"Error al asignar tutor: {e}")
            flash('Ocurrió un error al asignar el tutor.', 'error')
            return redirect(url_for('admin_asignar_tutor', grupo_id=grupo_id))

    # Si es GET, mostrar el formulario
    # Obtener lista de tutores disponibles
    tutores = list(mongo.db.usuarios.find(
        {"rol": "tutor"}, {"nombre_completo": 1}))
    # Obtener el tutor actual asignado
    tutor_actual_id = grupo.get('tutor_id')

    return render_template('admin_asignar_tutor.html', grupo=grupo, tutores=tutores, tutor_actual_id=tutor_actual_id)


@app.route('/admin/grupos/gestionar_estudiantes/<grupo_id>', methods=['GET', 'POST'])
@admin_required
def admin_gestionar_estudiantes(grupo_id):
    """Agrega o elimina estudiantes de un grupo."""
    try:
        grupo = mongo.db.grupos.find_one({"_id": ObjectId(grupo_id)})
        if not grupo:
            flash('Grupo no encontrado.', 'error')
            return redirect(url_for('admin_gestionar_grupos'))
    except Exception:
        flash('ID de grupo inválido.', 'error')
        return redirect(url_for('admin_gestionar_grupos'))

    if request.method == 'POST':
        action = request.form.get('action')  # 'agregar' o 'eliminar'
        estudiante_id = request.form.get('estudiante_id')

        if not estudiante_id or not action:
            flash('Datos inválidos.', 'error')
            return redirect(url_for('admin_gestionar_estudiantes', grupo_id=grupo_id))

        try:
            estudiante_oid = ObjectId(estudiante_id)
            grupo_oid = ObjectId(grupo_id)
        except Exception:
            flash('ID de estudiante o grupo inválido.', 'error')
            return redirect(url_for('admin_gestionar_estudiantes', grupo_id=grupo_id))

        if action == 'agregar':
            # Verificar que el estudiante exista y sea de rol 'estudiante'
            estudiante = mongo.db.usuarios.find_one(
                {"_id": estudiante_oid, "rol": "estudiante"})
            if not estudiante:
                flash('Estudiante no encontrado o no es válido.', 'error')
                return redirect(url_for('admin_gestionar_estudiantes', grupo_id=grupo_id))

            # Verificar que no esté ya en el grupo
            if estudiante_id in grupo.get('estudiante_ids', []):
                flash(
                    f'El estudiante {estudiante["nombre_completo"]} ya está en este grupo.', 'warning')
                return redirect(url_for('admin_gestionar_estudiantes', grupo_id=grupo_id))

            # Agregar al estudiante
            try:
                mongo.db.grupos.update_one(
                    {"_id": grupo_oid},
                    # $addToSet evita duplicados
                    {"$addToSet": {"estudiante_ids": estudiante_id}}
                )
                flash(
                    f'Estudiante {estudiante["nombre_completo"]} agregado al grupo.', 'success')
            except Exception as e:
                app.logger.error(f"Error al agregar estudiante: {e}")
                flash('Ocurrió un error al agregar el estudiante.', 'error')

        elif action == 'eliminar':
            # Verificar que el estudiante esté en el grupo
            if estudiante_id not in grupo.get('estudiante_ids', []):
                flash('El estudiante no está en este grupo.', 'warning')
                return redirect(url_for('admin_gestionar_estudiantes', grupo_id=grupo_id))

            # Eliminar del estudiante
            try:
                mongo.db.grupos.update_one(
                    {"_id": grupo_oid},
                    # $pull elimina el elemento
                    {"$pull": {"estudiante_ids": estudiante_id}}
                )
                flash('Estudiante eliminado del grupo.', 'success')
            except Exception as e:
                app.logger.error(f"Error al eliminar estudiante: {e}")
                flash('Ocurrió un error al eliminar el estudiante.', 'error')

        # Redirigir a la misma página para refrescar la lista
        return redirect(url_for('admin_gestionar_estudiantes', grupo_id=grupo_id))

    # Si es GET, mostrar el formulario
    # Obtener lista de estudiantes en el grupo
    estudiante_ids = [ObjectId(sid) for sid in grupo.get('estudiante_ids', [])]
    estudiantes_en_grupo = []
    if estudiante_ids:
        estudiantes_en_grupo = list(mongo.db.usuarios.find(
            {"_id": {"$in": estudiante_ids}},
            {"nombre_completo": 1, "numero_control": 1}
        ))

    # Obtener IDs de estudiantes que ya están en algún grupo
    grupos = list(mongo.db.grupos.find({}, {"estudiante_ids": 1}))
    estudiantes_asignados = set()
    for g in grupos:
        estudiantes_asignados.update(g.get('estudiante_ids', []))

    # Obtener lista de estudiantes NO asignados a ningún grupo
    estudiantes_disponibles = list(mongo.db.usuarios.find(
        {
            "rol": "estudiante",
            "_id": {"$nin": [ObjectId(sid) for sid in estudiantes_asignados]}
        },
        {"nombre_completo": 1, "numero_control": 1}
    ))

    return render_template('admin_gestionar_estudiantes.html',
                           grupo=grupo,
                           estudiantes_en_grupo=estudiantes_en_grupo,
                           estudiantes_disponibles=estudiantes_disponibles)

# -- Rutas para gestión de estudiantes (ADMIN)


@app.route('/admin/estudiantes')
@admin_required
def admin_gestionar_estudiantes_generales():
    """Muestra la lista de todos los estudiantes."""
    try:
        # Obtener todos los usuarios con rol 'estudiante'
        estudiantes = list(mongo.db.usuarios.find({"rol": "estudiante"}))
        return render_template('admin_estudiantes.html', estudiantes=estudiantes)
    except Exception as e:
        app.logger.error(f"Error al obtener estudiantes: {e}")
        flash('Ocurrió un error al cargar la lista de estudiantes.', 'error')
        return render_template('admin_estudiantes.html', estudiantes=[])


@app.route('/admin/estudiantes/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_nuevo_estudiante():
    """Crea un nuevo usuario estudiante."""
    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        email = request.form.get('email', '').strip().lower()
        # Contraseña temporal o generada
        password = request.form.get('password', '')
        numero_control = request.form.get(
            'numero_control', '').strip().upper()  # En mayúsculas
        carrera = request.form.get('carrera', '').strip()
        semestre = request.form.get('semestre', '').strip()
        generacion = request.form.get('generacion', '').strip()

        # Validaciones básicas
        errors = []
        if not nombre_completo:
            errors.append("El nombre completo es obligatorio.")
        if not email:
            errors.append("El email es obligatorio.")
        else:
            if "@" not in email or "." not in email:
                errors.append("El formato del email no es válido.")
            elif mongo.db.usuarios.find_one({"email": email}):
                errors.append("Ya existe un usuario con ese email.")
        # El número de control debería ser único también
        if not numero_control:
            errors.append("El número de control es obligatorio.")
        elif mongo.db.usuarios.find_one({"numero_control": numero_control, "rol": "estudiante"}):
            errors.append("Ya existe un estudiante con ese número de control.")

        # Validar semestre como número (opcional)
        if semestre:
            try:
                int(semestre)
            except ValueError:
                errors.append("El semestre debe ser un número.")

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('admin_nuevo_estudiante.html',
                                   nombre_completo=nombre_completo,
                                   email=email,
                                   numero_control=numero_control,
                                   carrera=carrera,
                                   semestre=semestre,
                                   generacion=generacion)

        # Si todo es válido, crear el estudiante
        # Generar una contraseña temporal o usar la proporcionada
        if not password:
            password = numero_control  # Usar el número de control como contraseña inicial
            flash(
                'Se ha establecido el número de control como contraseña temporal.', 'info')

        try:
            hashed_password = generate_password_hash(password)

            nuevo_estudiante = {
                "nombre_completo": nombre_completo,
                "email": email,
                "password": hashed_password,
                "rol": "estudiante",
                "numero_control": numero_control,
                "carrera": carrera,
                "semestre": semestre,
                "generacion": generacion
            }

            result = mongo.db.usuarios.insert_one(nuevo_estudiante)
            if result.inserted_id:
                flash(
                    f'Estudiante "{nombre_completo}" ({numero_control}) creado exitosamente.', 'success')
                # Opcional: Redirigir a la lista de estudiantes o al grupo desde donde se llamó
                # Por ahora, redirigimos a la lista general
                return redirect(url_for('admin_gestionar_estudiantes_generales'))
            else:
                raise Exception("No se pudo insertar el documento.")

        except Exception as e:
            app.logger.error(f"Error al crear estudiante: {e}")
            flash('Ocurrió un error al crear el estudiante.', 'error')
            return render_template('admin_nuevo_estudiante.html',
                                   nombre_completo=nombre_completo,
                                   email=email,
                                   numero_control=numero_control,
                                   carrera=carrera,
                                   semestre=semestre,
                                   generacion=generacion)

    # Si es GET, mostrar el formulario vacío
    return render_template('admin_nuevo_estudiante.html')

# Placeholder para carga por lotes


@app.route('/admin/estudiantes/cargar_lote', methods=['GET', 'POST'])
@admin_required
def admin_cargar_lote_estudiantes():
    """Placeholder para la funcionalidad de carga por lotes."""
    if request.method == 'POST':
        # Aquí iría la lógica para procesar un archivo CSV/XLSX
        # Por ahora, solo mostramos un mensaje.
        flash('Funcionalidad de carga por lotes aún no implementada. Esta es una demostración.', 'warning')
        return redirect(url_for('admin_gestionar_estudiantes_generales'))

    return render_template('admin_cargar_lote_estudiantes.html')


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


@app.context_processor
def inject_user():
    """Hace que 'current_user' esté disponible en todas las plantillas Jinja2."""
    # Obtener el usuario actual desde 'g'
    return dict(current_user=g.current_user)


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
