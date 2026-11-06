"""Rutas de quotes."""

# Registramos estas vistas en la instancia Flask existente para conservar las URL.
from backend.app import *  # noqa: F401,F403
from backend.routes.auth import role_required, admin_required
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from werkzeug.utils import secure_filename

@app.route('/cotizaciones')
@role_required('ventas')
def listar_cotizaciones():
    conn = crear_conexion()
    cursor = conn.cursor()
    search = request.args.get('search_query', '').strip()

    sql = '''SELECT c.id, c.cliente_nombre, c.fecha, c.total, c.estado, u.nombre, c.tipo_pago,
                    COALESCE(c.razon_social_nombre, ''), COALESCE(c.numero_cotizacion, '')
             FROM cotizaciones c JOIN usuarios u ON c.usuario_id = u.id'''
    if search:
        sql += " WHERE c.cliente_nombre LIKE ? OR c.numero_cotizacion LIKE ? ORDER BY c.fecha DESC"
        cursor.execute(sql, ('%' + search + '%', '%' + search + '%'))
    else:
        sql += " ORDER BY c.fecha DESC"
        cursor.execute(sql)

    lista = []
    for row in cursor.fetchall():
        r = list(row)
        try:
            r[2] = datetime.datetime.strptime(str(r[2]).split('.')[0], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y')
        except Exception:
            pass
        lista.append(tuple(r))
    conn.close()
    
    return render_template('cotizaciones/listar.html', cotizaciones=lista, search_query=search)

@app.route('/cotizaciones/buscar_cliente')
@role_required('ventas')
def buscar_cliente_cotizacion():
    tipo = request.args.get('tipo', '').strip().lower()
    if tipo not in ('celular', 'ruc', 'nombre'):
        return jsonify({'encontrado': False}), 400

    conn = crear_conexion()
    try:
        if tipo in ('celular', 'ruc'):
            columna = {'celular': 'cliente_celular', 'ruc': 'cliente_ruc'}[tipo]
            valor = ''.join(caracter for caracter in request.args.get('valor', '') if caracter.isdigit())
            if len(valor) < 8:
                return jsonify({'encontrado': False})

            normalizado = (
                f"replace(replace(replace(replace(replace(replace(COALESCE({columna}, ''), "
                "' ', ''), '-', ''), '+', ''), '(', ''), ')', ''), '.', '')"
            )
            cliente = conn.execute(
                f"SELECT cliente_nombre, cliente_celular, cliente_direccion, cliente_ruc "
                f"FROM cotizaciones WHERE {normalizado} = ? ORDER BY id DESC LIMIT 1",
                (valor,),
            ).fetchone()
            if not cliente:
                return jsonify({'encontrado': False})

            historial = conn.execute(
                f"""SELECT id, numero_cotizacion, fecha, estado, total,
                          COALESCE(observacion_rechazo, '') AS observacion_rechazo
                   FROM cotizaciones WHERE {normalizado} = ?
                   ORDER BY id DESC LIMIT 10""",
                (valor,),
            ).fetchall()

        elif tipo == 'nombre':
            valor = request.args.get('valor', '').strip()
            if len(valor) < 2:
                return jsonify({'encontrado': False})

            cliente = conn.execute(
                """SELECT cliente_nombre, cliente_celular, cliente_direccion, cliente_ruc
                   FROM cotizaciones
                   WHERE LOWER(TRIM(cliente_nombre)) = LOWER(TRIM(?))
                      OR LOWER(cliente_nombre) LIKE LOWER(?)
                   ORDER BY CASE WHEN LOWER(TRIM(cliente_nombre)) = LOWER(TRIM(?)) THEN 0 ELSE 1 END, id DESC
                   LIMIT 1""",
                (valor, f"%{valor}%", valor),
            ).fetchone()

            if not cliente:
                return jsonify({'encontrado': False})

            historial = conn.execute(
                """SELECT id, numero_cotizacion, fecha, estado, total,
                          COALESCE(observacion_rechazo, '') AS observacion_rechazo
                   FROM cotizaciones
                   WHERE LOWER(TRIM(cliente_nombre)) = LOWER(TRIM(?))
                   ORDER BY id DESC LIMIT 10""",
                (cliente[0],),
            ).fetchall()

        return jsonify({
            'encontrado': True,
            'nombre': (cliente[0] or '').strip(),
            'celular': (cliente[1] or '').strip(),
            'direccion': (cliente[2] or '').strip(),
            'ruc': (cliente[3] or '').strip(),
            'historial': [dict(item) for item in historial],
        })
    finally:
        conn.close()


@app.route('/aprobar_cotizacion/<int:cotizacion_id>', methods=['POST'])
@role_required('ventas', 'caja', 'usuario')
def route_aprobar_cotizacion(cotizacion_id):
    pago = request.form.get('tipo_pago', 'Efectivo')
    monto = request.form.get('monto', type=float)
    if monto is None:
        flash('Ingresa el importe recibido para registrar el pago.', 'danger')
        return redirect(url_for('listar_pagos'))
    res = registrar_pago_cotizacion(cotizacion_id, monto, pago)
    flash(res['message'], 'success' if res['success'] else 'danger')
    return redirect(url_for('listar_pagos'))


@app.route('/pagos')
@role_required('ventas', 'caja', 'usuario')
def listar_pagos():
    conn = crear_conexion()
    conn.row_factory = sqlite3.Row
    hoy_iso = datetime.date.today().isoformat()
    fecha_param = request.args.get('fecha', '').strip()
    fecha_filtro = fecha_param if fecha_param else hoy_iso

    abiertas = conn.execute('''
        SELECT c.id, c.cliente_nombre, c.fecha, c.total, c.estado, u.nombre,
               COALESCE(SUM(mc.ingreso_total), 0) AS abonado
        FROM cotizaciones c
        JOIN usuarios u ON u.id=c.usuario_id
        LEFT JOIN movimientos_caja mc ON mc.cotizacion_id=c.id
        WHERE c.estado IN ('Pendiente', 'Parcial')
        GROUP BY c.id
        ORDER BY c.fecha ASC, c.id ASC
    ''').fetchall()

    pagos_dia_rows = conn.execute('''
        SELECT mc.id, mc.fecha, mc.ingreso_total, mc.tipo_pago,
               c.id AS cotizacion_id, c.numero_cotizacion, c.cliente_nombre,
               c.cliente_celular, c.cliente_ruc, c.total AS total_cotizacion,
               u.nombre AS vendedor
        FROM movimientos_caja mc
        JOIN cotizaciones c ON c.id = mc.cotizacion_id
        LEFT JOIN usuarios u ON u.id = c.usuario_id
        WHERE date(mc.fecha) = ?
        ORDER BY mc.id DESC
    ''', (fecha_filtro,)).fetchall()

    total_dia = 0.0
    desglose_dia = {}
    conteo_medios_dia = {}
    pagos_dia = []
    for row in pagos_dia_rows:
        item = dict(row)
        monto = float(item.get('ingreso_total') or 0)
        total_dia += monto
        medio = (item.get('tipo_pago') or 'Efectivo').strip()
        desglose_dia[medio] = desglose_dia.get(medio, 0.0) + monto
        conteo_medios_dia[medio] = conteo_medios_dia.get(medio, 0) + 1
        f_str = str(item.get('fecha') or '')
        item['hora'] = f_str[11:16] if len(f_str) >= 16 else ''
        pagos_dia.append(item)

    fechas_disponibles_rows = conn.execute('''
        SELECT date(fecha) AS f, COUNT(*) AS cantidad, SUM(ingreso_total) AS total
        FROM movimientos_caja
        GROUP BY date(fecha)
        ORDER BY date(fecha) DESC
        LIMIT 30
    ''').fetchall()
    fechas_disponibles = [dict(f) for f in fechas_disponibles_rows]

    pagos = conn.execute('''
        SELECT mc.fecha, c.id, c.numero_cotizacion, c.cliente_nombre, mc.ingreso_total, mc.tipo_pago
        FROM movimientos_caja mc
        JOIN cotizaciones c ON c.id=mc.cotizacion_id
        ORDER BY mc.id DESC
        LIMIT 100
    ''').fetchall()
    conn.close()

    try:
        fecha_filtro_formateada = datetime.datetime.strptime(fecha_filtro, '%Y-%m-%d').strftime('%d/%m/%Y')
    except Exception:
        fecha_filtro_formateada = fecha_filtro

    return render_template(
        'pagos/listar.html',
        cotizaciones=abiertas,
        pagos=pagos,
        pagos_dia=pagos_dia,
        total_dia=total_dia,
        desglose_dia=desglose_dia,
        conteo_medios_dia=conteo_medios_dia,
        fecha_filtro=fecha_filtro,
        fecha_filtro_formateada=fecha_filtro_formateada,
        fechas_disponibles=fechas_disponibles,
        es_hoy=(fecha_filtro == hoy_iso)
    )


@app.route('/pagos/reporte-pdf')
@role_required('ventas', 'caja', 'usuario')
def imprimir_reporte_pagos_dia():
    fecha_param = request.args.get('fecha', '').strip()
    hoy_iso = datetime.date.today().isoformat()
    fecha_filtro = fecha_param if fecha_param else hoy_iso

    conn = crear_conexion()
    conn.row_factory = sqlite3.Row

    razon = conn.execute(
        "SELECT nombre, ruc, telefono FROM razones_sociales WHERE activa=1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    empresa_nombre = razon['nombre'] if razon else 'DING FENG IMPORT AND EXPORT TRADE S.A.C.'
    empresa_ruc = razon['ruc'] if razon else '20608796859'
    empresa_telefono = razon['telefono'] if razon else '929 877 778'

    pagos_dia_rows = conn.execute('''
        SELECT mc.id, mc.fecha, mc.ingreso_total, mc.tipo_pago,
               c.id AS cotizacion_id, c.numero_cotizacion, c.cliente_nombre,
               c.cliente_celular, c.cliente_ruc, u.nombre AS vendedor,
               COALESCE(c.razon_social_nombre, '') AS razon_social_nombre,
               COALESCE(c.razon_social_ruc, '') AS razon_social_ruc,
               COALESCE(c.razon_social_telefono, '') AS razon_social_telefono
        FROM movimientos_caja mc
        JOIN cotizaciones c ON c.id = mc.cotizacion_id
        LEFT JOIN usuarios u ON u.id = c.usuario_id
        WHERE date(mc.fecha) = ?
        ORDER BY mc.fecha ASC, mc.id ASC
    ''', (fecha_filtro,)).fetchall()
    conn.close()

    empresas_pago = {
        row['razon_social_nombre'].strip(): row
        for row in pagos_dia_rows
        if row['razon_social_nombre'].strip()
    }
    if len(empresas_pago) == 1:
        empresa = next(iter(empresas_pago.values()))
        empresa_nombre = empresa['razon_social_nombre']
        empresa_ruc = empresa['razon_social_ruc']
        empresa_telefono = empresa['razon_social_telefono']
    elif len(empresas_pago) > 1:
        empresa_nombre = 'Reporte consolidado de cobranzas'
        empresa_ruc = ''
        empresa_telefono = ''

    nombre_empresa_normalizado = empresa_nombre.upper().replace(' ', '')
    es_dingfeng = 'DINGFENG' in nombre_empresa_normalizado
    es_wangnuke = 'WANGNUKE' in nombre_empresa_normalizado
    color_acento = '#6D28D9' if es_dingfeng else '#9A6700' if es_wangnuke else '#0056b3'
    color_acento_claro = '#F3E8FF' if es_dingfeng else '#FFF4CC' if es_wangnuke else '#e0f2fe'
    color_acento_texto = '#581C87' if es_dingfeng else '#714F00' if es_wangnuke else '#0369a1'

    total_recaudado = 0.0
    desglose = {}
    conteo_medios = {}
    pagos = []
    for row in pagos_dia_rows:
        item = dict(row)
        monto = float(item.get('ingreso_total') or 0)
        total_recaudado += monto
        medio = (item.get('tipo_pago') or 'Efectivo').strip()
        desglose[medio] = desglose.get(medio, 0.0) + monto
        conteo_medios[medio] = conteo_medios.get(medio, 0) + 1
        f_str = str(item.get('fecha') or '')
        item['hora'] = f_str[11:16] if len(f_str) >= 16 else ''
        pagos.append(item)

    try:
        fecha_formateada = datetime.datetime.strptime(fecha_filtro, '%Y-%m-%d').strftime('%d/%m/%Y')
    except Exception:
        fecha_formateada = fecha_filtro

    responsable = getattr(flask_login.current_user, 'nombre', 'Administrador')
    hora_impresion = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    html = render_template(
        'pagos/reporte_diario_pdf.html',
        fecha_filtro=fecha_filtro,
        fecha_formateada=fecha_formateada,
        pagos=pagos,
        total_recaudado=total_recaudado,
        desglose=desglose,
        conteo_medios=conteo_medios,
        empresa_nombre=empresa_nombre,
        empresa_ruc=empresa_ruc,
        empresa_telefono=empresa_telefono,
        color_acento=color_acento,
        color_acento_claro=color_acento_claro,
        color_acento_texto=color_acento_texto,
        responsable=responsable,
        hora_impresion=hora_impresion
    )

    pdf = io.BytesIO()
    from xhtml2pdf import pisa

    def resolver_recurso(uri, _):
        prefijos_estaticos = ('/static/', 'static/')
        for prefijo in prefijos_estaticos:
            if uri.startswith(prefijo):
                ruta = os.path.realpath(os.path.join(app.static_folder, uri[len(prefijo):]))
                raiz_estatica = os.path.realpath(app.static_folder)
                if os.path.commonpath([raiz_estatica, ruta]) == raiz_estatica:
                    return ruta
                break
        return uri

    pisa.CreatePDF(html, dest=pdf, encoding='UTF-8', link_callback=resolver_recurso)
    pdf.seek(0)

    return send_file(
        pdf,
        as_attachment=False,
        mimetype='application/pdf',
        download_name=f'Reporte_Pagos_{fecha_filtro}.pdf'
    )

@app.route('/cotizaciones/nueva', methods=['GET', 'POST'])
@role_required('ventas')
def nueva_cotizacion():
    def mostrar_formulario(datos=None, productos_iniciales=None):
        inventario = obtener_inventario_desde_bd()
        productos_inventario = [(p['id'], p['nombre'], p['precio_unitario']) for p in inventario]
        conn_formulario = crear_conexion()
        razones = conn_formulario.execute(
            'SELECT id, nombre, ruc FROM razones_sociales WHERE activa=1 ORDER BY nombre COLLATE NOCASE'
        ).fetchall()
        clientes_query = conn_formulario.execute(
            '''SELECT DISTINCT TRIM(cliente_nombre)
               FROM cotizaciones
               WHERE cliente_nombre IS NOT NULL AND TRIM(cliente_nombre) != ''
               ORDER BY TRIM(cliente_nombre) COLLATE NOCASE'''
        ).fetchall()
        clientes_guardados = [fila[0] for fila in clientes_query if fila[0]]
        telefonos_query = conn_formulario.execute(
            '''SELECT DISTINCT TRIM(cliente_celular)
               FROM cotizaciones
               WHERE cliente_celular IS NOT NULL AND TRIM(cliente_celular) != ''
               ORDER BY TRIM(cliente_celular)'''
        ).fetchall()
        telefonos_guardados = [fila[0] for fila in telefonos_query if fila[0]]
        conn_formulario.close()
        return render_template(
            'cotizaciones/nueva.html', productos_inventario=productos_inventario,
            razones_sociales=razones,
            clientes_guardados=clientes_guardados,
            telefonos_guardados=telefonos_guardados,
            datos_formulario=datos or {},
            productos_iniciales=productos_iniciales or [],
            fecha_hoy=datetime.date.today().isoformat(),
        )

    if request.method == 'POST':
        datos_formulario = {
            campo: request.form.get(campo, '')
            for campo in ('razon_social_id', 'cliente_nombre', 'cliente_celular', 'cliente_direccion', 'cliente_ruc', 'fecha')
        }
        try:
            productos_iniciales = json.loads(request.form.get('productos_seleccionados', '[]'))
            if not isinstance(productos_iniciales, list):
                productos_iniciales = []
        except (TypeError, json.JSONDecodeError):
            productos_iniciales = []

        conn = crear_conexion()
        cursor = conn.cursor()
        try:
            fecha_elegida = datetime.datetime.strptime(datos_formulario.get('fecha', ''), '%Y-%m-%d').date()
            hora_actual = datetime.datetime.now().strftime('%H:%M:%S')
            fecha = f'{fecha_elegida.isoformat()} {hora_actual}'
            razon_id = request.form.get('razon_social_id', type=int)
            razon = cursor.execute(
                'SELECT id, nombre, ruc, telefono, medios_pago FROM razones_sociales WHERE id=? AND activa=1',
                (razon_id,),
            ).fetchone()
            if not razon:
                flash('Selecciona una razón social activa antes de cotizar.', 'danger')
                return mostrar_formulario(datos_formulario, productos_iniciales)

            cliente = request.form['cliente_nombre']
            celular = request.form.get('cliente_celular')
            direccion = request.form.get('cliente_direccion', '').strip()
            ruc = request.form.get('cliente_ruc')
            # Las cotizaciones se crean pendientes; estado y pagos se gestionan exclusivamente desde Pagos.
            pago = ''
            estado = 'Pendiente'
            prods = productos_iniciales

            if not prods:
                flash('Agregue productos', 'danger')
                return mostrar_formulario(datos_formulario, productos_iniciales)

            inventario = obtener_inventario_desde_bd()
            dict_inv = {p['id']: p for p in inventario}

            total = 0
            items_listos = []

            for p in prods:
                pid = int(p['id'])
                if pid not in dict_inv:
                    raise Exception(f"Producto ID {pid} no existe.")
                
                producto = dict_inv[pid]
                stock_actual = producto['cantidad']
                costo_adq = producto.get('costo_adquisicion', 0)
                p_nombre = producto['nombre']
                unidad = str(p.get('unidad') or 'und').strip().lower()
                if unidad not in ('caja', 'und', 'paquete', 'par'):
                    raise Exception('Selecciona una unidad válida para cada producto.')
                
                if estado in ('Aprobada', 'Pagada') and int(p['cantidad']) > stock_actual:
                    raise Exception(f"Stock insuficiente: {p_nombre}")
                
                sub = float(p['precio']) * int(p['cantidad'])
                total += sub
                items_listos.append({
                    'id': pid, 
                    'cant': int(p['cantidad']), 
                    'unidad': unidad,
                    'precio': float(p['precio']), 
                    'costo': costo_adq or 0
                })

            conn.execute('BEGIN IMMEDIATE')
            periodo_folio = fecha[:7].replace('-', '')
            ultimo_folio = cursor.execute(
                "SELECT COALESCE(MAX(CAST(substr(numero_cotizacion, 8) AS INTEGER)), 0) "
                "FROM cotizaciones WHERE numero_cotizacion LIKE ?",
                (f'{periodo_folio}-%',),
            ).fetchone()[0]
            numero_cotizacion = f'{periodo_folio}-{ultimo_folio + 1:04d}'
            cursor.execute("""INSERT INTO cotizaciones (
                              cliente_nombre, cliente_celular, cliente_direccion, cliente_email, cliente_ruc,
                              fecha, total, estado, usuario_id, tipo_pago, razon_social_id,
                              razon_social_nombre, razon_social_ruc, razon_social_telefono,
                              razon_social_medios_pago, numero_cotizacion
                              ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (cliente, celular, direccion, None, ruc, fecha, total, estado,
                            flask_login.current_user.id, pago, razon['id'], razon['nombre'],
                            razon['ruc'], razon['telefono'], razon['medios_pago'], numero_cotizacion))
            cot_id = cursor.lastrowid

            costo_total = 0
            for it in items_listos:
                cursor.execute(
                    "INSERT INTO cotizacion_detalles (cotizacion_id, producto_id, cantidad, descripcion, precio_unitario_cotizado, costo_unitario, unidad) VALUES (?,?,?,?,?,?,?)",
                    (cot_id, it['id'], it['cant'], '', it['precio'], it['costo'], it['unidad']))
                
                if estado in ('Aprobada', 'Pagada'):
                    cursor.execute("UPDATE inventario SET cantidad = cantidad - ? WHERE id = ?", (it['cant'], it['id']))
                    costo_total += it['costo'] * it['cant']

            if estado in ('Aprobada', 'Pagada'):
                cursor.execute(
                    "INSERT INTO movimientos_caja (cotizacion_id, fecha, ingreso_total, costo_total, tipo_pago) VALUES (?,?,?,?,?)",
                    (cot_id, fecha, total, costo_total, pago))

            conn.commit()
            flash('Cotización creada correctamente', 'success')
            return redirect(url_for('listar_cotizaciones'))
            
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            return mostrar_formulario(datos_formulario, productos_iniciales)
        finally:
            conn.close()
    return mostrar_formulario()

@app.route('/cotizaciones/detalle/<int:id>')
@role_required('ventas')
def detalle_cotizacion(id):
    conn = crear_conexion()
    cursor = conn.cursor()
    cursor.execute('''SELECT c.id, c.cliente_nombre, c.cliente_celular, c.cliente_email, c.cliente_ruc, 
                      c.fecha, c.total, c.estado, u.nombre, c.tipo_pago,
                      COALESCE(c.razon_social_nombre, ''), COALESCE(c.razon_social_ruc, ''),
                      COALESCE(c.razon_social_telefono, ''), COALESCE(c.cliente_direccion, ''),
                      COALESCE((SELECT SUM(mc.ingreso_total) FROM movimientos_caja mc WHERE mc.cotizacion_id=c.id), 0),
                      COALESCE(c.observacion_rechazo, ''), c.razon_social_id,
                      COALESCE(c.numero_cotizacion, '')
                      FROM cotizaciones c JOIN usuarios u ON c.usuario_id = u.id WHERE c.id = ?''', (id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        flash('No encontrada', 'danger')
        return redirect(url_for('listar_cotizaciones'))

    # Desde la lista, "Ver" abre directamente todos los campos editables de
    # cotizaciones sin pago. Después de guardar se muestra la ficha actualizada.
    cot = list(row)
    try:
        fecha_iso = datetime.datetime.strptime(str(cot[5]).split('.')[0], '%Y-%m-%d %H:%M:%S').date().isoformat()
    except Exception:
        fecha_iso = datetime.date.today().isoformat()
    try:
        cot[5] = datetime.datetime.strptime(str(cot[5]).split('.')[0], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')
    except Exception:
        pass

    cursor.execute('''
        SELECT cd.cantidad, cd.producto_id, cd.precio_unitario_cotizado,
               COALESCE(i.nombre, 'Producto #' || cd.producto_id),
               COALESCE(cd.costo_unitario, i.costo_adquisicion, 0), COALESCE(cd.unidad, 'und')
        FROM cotizacion_detalles cd
        LEFT JOIN inventario i ON cd.producto_id = i.id
        WHERE cd.cotizacion_id = ?
    ''', (id,))
    detalles_bd = cursor.fetchall()

    detalles = []
    for item in detalles_bd:
        detalles.append({
            'cantidad': item[0],
            'producto_id': item[1],
            'nombre': item[3],
            'precio': item[2],
            'costo': item[4] or 0,
            'unidad': item[5] or 'und'
        })

    razones = conn.execute(
        'SELECT id, nombre, ruc, activa FROM razones_sociales WHERE activa=1 OR id=? ORDER BY nombre COLLATE NOCASE',
        (cot[16],),
    ).fetchall()
    inventario = obtener_inventario_desde_bd()
    productos_inventario = [
        (p['id'], p['nombre'], p['precio_unitario'], p.get('costo_adquisicion', 0))
        for p in inventario
    ]
    conn.close()

    return render_template(
        'cotizaciones/detalle.html', cotizacion=cot, detalles=detalles,
        razones_sociales=razones, productos_inventario=productos_inventario, fecha_iso=fecha_iso,
        puede_editar=cot[7] in ('Pendiente', 'Rechazado', 'Rechazada'),
    )

@app.route('/cotizaciones/editar/<int:id>', methods=['GET', 'POST'])
@role_required('ventas')
def editar_cotizacion(id):
    conn = crear_conexion()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT estado, total, razon_social_id, fecha FROM cotizaciones WHERE id = ?", (id,))
    row_ant = cursor.fetchone()
    if not row_ant:
        conn.close()
        return redirect(url_for('listar_cotizaciones'))

    estado_anterior = row_ant['estado']
    if estado_anterior in ('Aprobada', 'Pagada', 'Parcial'):
        conn.close()
        flash('Las cotizaciones pagadas ya no se pueden editar.', 'warning')
        return redirect(url_for('listar_cotizaciones'))

    if request.method == 'POST':
        try:
            cliente = request.form['cliente_nombre']
            celular = request.form.get('cliente_celular')
            direccion = request.form.get('cliente_direccion', '').strip()
            ruc = request.form.get('cliente_ruc')
            fecha_texto = request.form.get('fecha', '').strip()
            if fecha_texto:
                fecha_elegida = datetime.datetime.strptime(fecha_texto, '%Y-%m-%d').date()
                try:
                    hora_guardada = datetime.datetime.fromisoformat(str(row_ant['fecha']).split('.')[0]).strftime('%H:%M:%S')
                except (TypeError, ValueError):
                    hora_guardada = datetime.datetime.now().strftime('%H:%M:%S')
                fecha = f'{fecha_elegida.isoformat()} {hora_guardada}'
            else:
                fecha = row_ant['fecha']
            razon_id = request.form.get('razon_social_id', type=int)
            razon = conn.execute(
                'SELECT id, nombre, ruc, telefono, medios_pago FROM razones_sociales '
                'WHERE id=? AND (activa=1 OR id=?)',
                (razon_id, row_ant['razon_social_id']),
            ).fetchone()
            if not razon:
                raise Exception('Selecciona una razón social activa.')
            nuevos_productos = json.loads(request.form.get('productos_seleccionados', '[]'))

            if not nuevos_productos:
                raise Exception("Debe haber al menos un producto.")

            conn.execute('BEGIN TRANSACTION;')

            inventario = obtener_inventario_desde_bd()
            dict_stock = {p['id']: p for p in inventario}
            
            cursor.execute("""
                UPDATE cotizaciones SET cliente_nombre=?, cliente_celular=?, cliente_direccion=?, cliente_email=NULL, fecha=?,
                cliente_ruc=?, razon_social_id=?, razon_social_nombre=?,
                razon_social_ruc=?, razon_social_telefono=?, razon_social_medios_pago=? WHERE id=?
            """, (cliente, celular, direccion, fecha, ruc, razon['id'], razon['nombre'],
                  razon['ruc'], razon['telefono'], razon['medios_pago'], id))

            cursor.execute("DELETE FROM cotizacion_detalles WHERE cotizacion_id=?", (id,))

            total_venta = 0

            for p in nuevos_productos:
                pid = int(p['id'])
                cant = int(p['cantidad'])
                precio = float(p['precio'])
                costo_unit = float(p.get('costo', dict_stock.get(pid, {}).get('costo_adquisicion', 0)) or 0)
                if precio <= 0 or costo_unit < 0:
                    raise Exception('El precio debe ser mayor que cero y el costo no puede ser negativo.')
                unidad = str(p.get('unidad') or 'und').strip().lower()
                if unidad not in ('caja', 'und', 'paquete', 'par'):
                    raise Exception('Selecciona una unidad válida para cada producto.')

                total_venta += precio * cant

                cursor.execute("""
                    INSERT INTO cotizacion_detalles (cotizacion_id, producto_id, cantidad, descripcion, precio_unitario_cotizado, costo_unitario, unidad)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (id, pid, cant, '', precio, costo_unit, unidad))

            cursor.execute("UPDATE cotizaciones SET total=? WHERE id=?", (total_venta, id))

            conn.commit()
            flash('Cotización actualizada correctamente.', 'success')
            if request.form.get('accion') == 'guardar_pdf':
                return redirect(url_for('exportar_cotizacion_pdf', id=id))
            return redirect(url_for('detalle_cotizacion', id=id, guardado=1))
            
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            if request.form.get('origen_detalle'):
                # Indica al detalle que debe recuperar el borrador guardado en el navegador.
                return redirect(url_for('detalle_cotizacion', id=id, guardado='error'))
            return redirect(url_for('editar_cotizacion', id=id))
        finally:
            conn.close()

    # GET
    cursor.execute("SELECT * FROM cotizaciones WHERE id=?", (id,))
    cot = cursor.fetchone()
    razones = conn.execute(
        'SELECT id, nombre, ruc, activa FROM razones_sociales WHERE activa=1 OR id=? ORDER BY nombre COLLATE NOCASE',
        (cot['razon_social_id'],),
    ).fetchall()
    
    cursor.execute('''
        SELECT cd.producto_id, cd.cantidad, cd.precio_unitario_cotizado,
               COALESCE(i.nombre, 'Producto #' || cd.producto_id),
               COALESCE(cd.costo_unitario, i.costo_adquisicion, 0), COALESCE(cd.unidad, 'und')
        FROM cotizacion_detalles cd
        LEFT JOIN inventario i ON cd.producto_id = i.id
        WHERE cd.cotizacion_id = ?
    ''', (id,))
    detalles_bd = cursor.fetchall()
    
    detalles = []
    for item in detalles_bd:
        detalles.append({
            'producto_id': item[0],
            'nombre_producto': item[3],
            'cantidad': item[1],
            'precio_unitario_cotizado': item[2],
            'costo_unitario': item[4],
            'unidad': item[5] or 'und'
        })
    
    inventario = obtener_inventario_desde_bd()
    productos_inventario = [(p['id'], p['nombre'], p['precio_unitario'], p.get('costo_adquisicion', 0)) for p in inventario]
    
    conn.close()
    return render_template('cotizaciones/editar.html', 
                         cotizacion=cot, 
                         detalles=detalles, 
                         productos_inventario=productos_inventario,
                         razones_sociales=razones)

@app.route('/cotizaciones/eliminar/<int:id>', methods=['POST'])
@admin_required
def eliminar_cotizacion(id):
    conn = crear_conexion()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT estado FROM cotizaciones WHERE id=?", (id,))
        est = cursor.fetchone()[0]
        conn.execute("BEGIN TRANSACTION;")
        
        if est in ('Aprobada', 'Pagada', 'Parcial'):
            cursor.execute("SELECT producto_id, cantidad FROM cotizacion_detalles WHERE cotizacion_id=?", (id,))
            for pid, cant in cursor.fetchall():
                cursor.execute("UPDATE inventario SET cantidad = cantidad + ? WHERE id = ?", (cant, pid))
        
        cursor.execute("DELETE FROM movimientos_caja WHERE cotizacion_id=?", (id,))
        cursor.execute("DELETE FROM cotizacion_detalles WHERE cotizacion_id=?", (id,))
        cursor.execute("DELETE FROM cotizaciones WHERE id=?", (id,))
        conn.commit()
        flash('Cotización eliminada correctamente', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('listar_cotizaciones'))

@app.route('/cotizaciones/rechazar/<int:id>', methods=['POST'])
@role_required('ventas', 'caja', 'usuario')
def rechazar_cotizacion(id):
    observacion = request.form.get('observacion_rechazo', '').strip()
    if not observacion:
        flash('Escribe una observacion para registrar el rechazo.', 'danger')
        return redirect(url_for('listar_pagos'))
    if len(observacion) > 1000:
        flash('La observacion no puede superar 1000 caracteres.', 'danger')
        return redirect(url_for('listar_pagos'))

    conn = crear_conexion()
    try:
        conn.execute('BEGIN TRANSACTION;')
        cursor = conn.cursor()
        cotizacion = cursor.execute(
            'SELECT estado FROM cotizaciones WHERE id=?', (id,)
        ).fetchone()
        abonos = cursor.execute(
            'SELECT COALESCE(SUM(ingreso_total), 0) FROM movimientos_caja WHERE cotizacion_id=?',
            (id,),
        ).fetchone()[0]

        if not cotizacion or cotizacion['estado'] != 'Pendiente' or (abonos or 0) > 0:
            conn.rollback()
            flash('Solo se pueden rechazar cotizaciones pendientes y sin abonos.', 'warning')
            return redirect(url_for('listar_pagos'))

        cursor.execute(
            "UPDATE cotizaciones SET estado='Rechazado', observacion_rechazo=? WHERE id=?",
            (observacion, id),
        )
        conn.commit()
        flash('Rechazo y observacion registrados.', 'info')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('listar_pagos'))

@app.route('/cotizaciones/exportar_pdf/<int:id>')
@role_required('ventas')
def exportar_cotizacion_pdf(id):
    conn = crear_conexion()
    cursor = conn.cursor()
    
    cursor.execute('''SELECT c.id, c.cliente_nombre, c.cliente_celular, c.cliente_email, c.cliente_ruc, 
                      c.fecha, c.total, c.estado, u.nombre, c.tipo_pago,
                      COALESCE(c.razon_social_nombre, ''), COALESCE(c.razon_social_ruc, ''),
                      COALESCE(c.razon_social_telefono, ''), COALESCE(c.razon_social_medios_pago, ''),
                      COALESCE(c.cliente_direccion, ''), c.razon_social_id,
                      COALESCE(u.celular, '')
                      FROM cotizaciones c JOIN usuarios u ON c.usuario_id = u.id WHERE c.id = ?''', (id,))
    cot_data = cursor.fetchone()
    
    if not cot_data:
        conn.close()
        flash('Cotización no encontrada', 'danger')
        return redirect(url_for('listar_cotizaciones'))

    cotizacion = {
        'id': cot_data[0],
        'cliente_nombre': cot_data[1],
        'vendedor': cot_data[8] or '',
        'cliente_celular': cot_data[2] or '',
        'cliente_ruc': cot_data[4] or '',
        'fecha': cot_data[5],
        'total': cot_data[6],
        'tipo_pago': cot_data[9],
        'razon_social_nombre': cot_data[10] or 'Razón social no asignada (cotización anterior)',
        'razon_social_ruc': cot_data[11],
        'razon_social_telefono': cot_data[12],
        'razon_social_medios_pago': cot_data[13],
        'cliente_direccion': cot_data[14] or '',
        'razon_social_id': cot_data[15],
        'vendedor_celular': cot_data[16] or '',
    }

    cursor.execute('''
        SELECT cd.cantidad, cd.producto_id, cd.precio_unitario_cotizado,
               COALESCE(i.nombre, 'Producto #' || cd.producto_id), COALESCE(cd.unidad, 'und')
        FROM cotizacion_detalles cd
        LEFT JOIN inventario i ON cd.producto_id = i.id
        WHERE cd.cotizacion_id = ?
    ''', (id,))
    detalles_bd = cursor.fetchall()
    conn.close()

    detalles = []
    for item in detalles_bd:
        detalles.append({
            'cantidad': item[0],
            'nombre': item[3],
            'precio': item[2],
            'unidad': item[4] or 'und'
        })

    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=50, rightMargin=50, topMargin=20, bottomMargin=30)
        story = []
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name='TableData', fontName='Helvetica-Bold', fontSize=12, leading=15))
        styles.add(ParagraphStyle(name='QuoteInfo', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=13, leading=17))
        styles.add(ParagraphStyle(name='CompanyInfo', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=12, leading=15, alignment=TA_CENTER))
        styles.add(ParagraphStyle(name='VendorInfo', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=13, leading=16))
        styles.add(ParagraphStyle(name='QuoteTotal', parent=styles['Normal'], fontSize=14, leading=17,
                                  alignment=TA_RIGHT, fontName='Helvetica-Bold'))
        styles.add(ParagraphStyle(name='MediosPagoHeader', fontName='Helvetica-Bold', fontSize=12, leading=15, alignment=TA_RIGHT))
        nombre_empresa = cotizacion['razon_social_nombre']
        nombre_empresa_upper = nombre_empresa.upper()
        colores_razon_social = (
            '#1F4E79', '#7A263A', '#426B52', '#654C78', '#9B5B13', '#3E6470',
        )
        razon_social_id = cotizacion['razon_social_id'] or 1
        nombre_empresa_normalizado = nombre_empresa_upper.replace(' ', '')
        if 'DINGFENG' in nombre_empresa_normalizado:
            color_empresa_hex = '#6D28D9'
        elif 'WANGNUKE' in nombre_empresa_normalizado:
            color_empresa_hex = '#9A6700'
        else:
            color_empresa_hex = colores_razon_social[(razon_social_id - 1) % len(colores_razon_social)]
        color_empresa = colors.HexColor(color_empresa_hex)
        styles.add(ParagraphStyle(name='QuoteTitle', parent=styles['Heading2'], fontSize=16,
                                  leading=19, alignment=TA_CENTER, spaceBefore=0, spaceAfter=2,
                                  textColor=color_empresa))

        logo_path = os.path.join(app.static_folder, 'images', 'logo2.png')
        logo = Image(logo_path, width=150, height=65) if os.path.exists(logo_path) else Spacer(1, 1)

        def etiqueta_pdf(texto):
            return f'<font color="{color_empresa_hex}"><b>{texto}</b></font>'

        def dato_pdf(texto):
            return f'<font color="#000000">{xml_escape(str(texto or ""))}</font>'

        if 'DINGFENG' in nombre_empresa_upper:
            ancho_nombre = pdfmetrics.stringWidth(nombre_empresa, 'Helvetica-Bold', 11)
            tamano_nombre = min(11, 178 * 11 / max(ancho_nombre, 1))
            nombre_empresa_pdf = (
                f'<font color="{color_empresa_hex}" size="{tamano_nombre:.2f}">'
                f'<b>{xml_escape(nombre_empresa)}</b></font>'
            )
        else:
            nombre_empresa_pdf = etiqueta_pdf(xml_escape(nombre_empresa))

        datos_empresa = [nombre_empresa_pdf]
        es_empresa_gat = 'DINGFENG' in nombre_empresa_upper or 'WANGNUKE' in nombre_empresa_upper
        if cotizacion['razon_social_ruc']:
            datos_empresa.append(f"{etiqueta_pdf('RUC:')} {dato_pdf(cotizacion['razon_social_ruc'])}")
        celular_empresa = '900654001' if es_empresa_gat else cotizacion['razon_social_telefono']
        if celular_empresa:
            datos_empresa.append(f"{etiqueta_pdf('Celular:')} {dato_pdf(celular_empresa)}")
        info_empresa = Paragraph('<br/>'.join(datos_empresa), styles['CompanyInfo'])

        if cotizacion['razon_social_medios_pago']:
            lineas_pago = []
            for linea in cotizacion['razon_social_medios_pago'].splitlines():
                banco, separador, cuenta = linea.partition(':')
                if separador and banco.strip() and cuenta.strip():
                    lineas_pago.append(
                        f'<font color="{color_empresa_hex}"><b>{xml_escape(banco.strip())}:</b></font> '
                        f'<font color="#000000"><b>{xml_escape(cuenta.strip())}</b></font>'
                    )
                else:
                    lineas_pago.append(f'<font color="#000000"><b>{xml_escape(linea)}</b></font>')
            medios_pago = Paragraph(
                f"{etiqueta_pdf('MEDIOS DE PAGO')}<br/>{'<br/>'.join(lineas_pago)}",
                styles['MediosPagoHeader'],
            )
        else:
            medios_pago = Spacer(1, 1)

        header_table = Table([[logo, info_empresa, medios_pago]], colWidths=[160, 190, 150])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (1, 0), 'CENTER'),
            ('ALIGN', (2, 0), (2, 0), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (0, 0), (0, 0), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 1))

        fecha_cotizacion_pdf = datetime.datetime.strptime(str(cotizacion['fecha']).split('.')[0], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y') if cotizacion['fecha'] else ''
        story.append(Paragraph('<b>COTIZACI\u00d3N</b>', styles['QuoteTitle']))
        datos_contacto = [
            f"{etiqueta_pdf(etiqueta)} {dato_pdf(valor)}"
            for etiqueta, valor in (
                ('RUC:', cotizacion['cliente_ruc']),
                ('Celular:', cotizacion['cliente_celular']),
                ('Dirección:', cotizacion['cliente_direccion']),
            )
            if valor
        ]
        info_line = (
            f"{etiqueta_pdf('Fecha:')} {dato_pdf(fecha_cotizacion_pdf)}"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; {etiqueta_pdf('Pago:')} {dato_pdf(cotizacion['tipo_pago'])}<br/>"
            f"{etiqueta_pdf('Cliente:')} {dato_pdf(cotizacion['cliente_nombre'])}"
        )
        if datos_contacto:
            info_line += f"<br/>{' &nbsp;&nbsp;|&nbsp;&nbsp; '.join(datos_contacto)}"
        vendedor_texto = dato_pdf(cotizacion['vendedor'])
        if cotizacion['vendedor_celular']:
            vendedor_texto += f" | {etiqueta_pdf('Celular:')} {dato_pdf(cotizacion['vendedor_celular'])}"
        story.append(Paragraph(info_line, styles['QuoteInfo']))
        story.append(Spacer(1, 5))

        data = [['#', 'Producto', 'Unidad', 'Cant', 'P. Unit', 'Subtotal']]
        total_acumulado = 0

        if not detalles:
            data.append(['', '⚠️ No hay productos en esta cotización', '', '', '', ''])
        else:
            for i, item in enumerate(detalles, 1):
                cant = item['cantidad']
                nombre = item['nombre']
                precio = item['precio']
                subtotal = cant * precio
                total_acumulado += subtotal
                data.append([
                    str(i),
                    Paragraph(f"<b>{xml_escape(str(nombre))}</b>", styles['TableData']),
                    item.get('unidad') or 'und',
                    str(cant),
                    f"S/ {precio:,.2f}",
                    f"S/ {subtotal:,.2f}"
                ])

        table = Table(data, colWidths=[30, 150, 55, 40, 85, 130])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), color_empresa),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7'))
        ]))
        story.append(table)
        story.append(Spacer(1, 12))

        total_display = total_acumulado if detalles else 0
        total_table = Table(
            [[
                Paragraph(f"{etiqueta_pdf('Vendedor:')} {vendedor_texto}", styles['VendorInfo']),
                Paragraph(f"TOTAL: S/ {total_display:,.2f}", styles['QuoteTotal']),
            ]],
            colWidths=[330, 160]
        )
        total_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(total_table)

        doc.build(story)
        buffer.seek(0)
        return send_file(
            buffer,
            mimetype='application/pdf',
            as_attachment=False,
            download_name=f'{secure_filename(cotizacion["cliente_nombre"]) or "Cliente"}.pdf'
        )
    except Exception as e:
        flash(f'Error al generar PDF: {e}', 'danger')
        return redirect(url_for('listar_cotizaciones'))

# =============================================================================
# RUTAS: CIERRE DE CAJA
# =============================================================================
