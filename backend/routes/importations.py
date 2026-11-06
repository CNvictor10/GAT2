"""Administración del control de importaciones."""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape
from werkzeug.utils import secure_filename

from backend.app import *  # noqa: F401,F403
from backend.routes.auth import admin_required


def _filtros_importaciones():
    query = request.args.get('q', '').strip()
    canal = request.args.get('canal', '').strip()
    condiciones = []
    parametros = []
    if query:
        condiciones.append(
            """(numero_importacion LIKE ? OR dua_dam LIKE ? OR proveedor LIKE ?
               OR agente_carga LIKE ? OR categoria_producto LIKE ?
               OR puerto_origen LIKE ? OR medio_transporte LIKE ?
               OR volumen_peso LIKE ? OR incidencias LIKE ? OR canal_sunat LIKE ?)"""
        )
        parametros.extend([f'%{query}%'] * 10)
    if canal in {'Verde', 'Naranja', 'Rojo'}:
        condiciones.append('canal_sunat = ?')
        parametros.append(canal)
    where_sql = f" WHERE {' AND '.join(condiciones)}" if condiciones else ''
    return query, canal, where_sql, parametros


CAMPOS_TEXTO = {
    'dua_dam': ('DUA / DAM', 180),
    'puerto_origen': ('Puerto de origen', 200),
    'medio_transporte': ('Medio de transporte', 300),
    'agente_carga': ('Agente de carga', 300),
    'canal_sunat': ('Canal SUNAT', 20),
    'incidencias': ('Incidencias / observaciones', 4000),
    'categoria_producto': ('Categoría de producto', 300),
    'volumen_peso': ('Volumen / peso', 180),
    'proveedor': ('Proveedor / fabricante', 300),
    'tipo_proveedor': ('Tipo de proveedor', 40),
}
FECHAS = {
    'fecha_emision_pedido': 'Fecha de emisión del pedido',
    'fecha_estimada_llegada': 'Fecha estimada de llegada',
    'fecha_real_llegada': 'Fecha real de llegada',
    'fecha_ingreso_callao': 'Fecha de ingreso al Callao',
}
COLUMNAS_EXCEL = [
    ('numero_importacion', 'N.º importación'),
    ('dua_dam', 'DUA / DAM'),
    ('fecha_emision_pedido', 'Fecha emisión pedido'),
    ('fecha_estimada_llegada', 'Fecha estimada llegada'),
    ('fecha_real_llegada', 'Fecha real llegada'),
    ('puerto_origen', 'Puerto de origen'),
    ('medio_transporte', 'Medio de transporte'),
    ('agente_carga', 'Agente de carga'),
    ('demora_dias', 'Demora (días)'),
    ('canal_sunat', 'Canal SUNAT'),
    ('fecha_ingreso_callao', 'Fecha ingreso Callao'),
    ('dias_levante', 'Días de levante'),
    ('incidencias', 'Incidencias / observaciones'),
    ('categoria_producto', 'Categoría de producto'),
    ('volumen_peso', 'Volumen / peso'),
    ('proveedor', 'Proveedor / fabricante'),
    ('tipo_proveedor', 'Tipo de proveedor'),
]


def _datos_formulario(formulario):
    datos = {
        campo: formulario.get(campo, '').strip()
        for campo in CAMPOS_TEXTO
    }
    datos['numero_importacion'] = formulario.get('numero_importacion', '').strip()

    if not datos['numero_importacion'] or len(datos['numero_importacion']) > 20:
        raise ValueError('El número de importación es obligatorio y no debe exceder 20 caracteres.')

    for campo, (etiqueta, maximo) in CAMPOS_TEXTO.items():
        if len(datos[campo]) > maximo:
            raise ValueError(f'{etiqueta}: máximo {maximo} caracteres.')

    if datos['canal_sunat'] and datos['canal_sunat'].casefold() not in {
        'verde', 'naranja', 'rojo'
    }:
        raise ValueError('Selecciona un canal SUNAT válido.')
    if datos['canal_sunat']:
        datos['canal_sunat'] = datos['canal_sunat'].capitalize()

    for campo, etiqueta in FECHAS.items():
        valor = formulario.get(campo, '').strip()
        if valor:
            try:
                datos[campo] = datetime.date.fromisoformat(valor).isoformat()
            except ValueError as exc:
                raise ValueError(f'{etiqueta}: usa una fecha válida.') from exc
        else:
            datos[campo] = None

    fechas_completas = (
        datos['fecha_estimada_llegada'],
        datos['fecha_real_llegada'],
        datos['fecha_ingreso_callao'],
    )
    if fechas_completas[0] and fechas_completas[1]:
        estimada = datetime.date.fromisoformat(fechas_completas[0])
        real = datetime.date.fromisoformat(fechas_completas[1])
        datos['demora_dias'] = (real - estimada).days
    else:
        datos['demora_dias'] = None
    if fechas_completas[1] and fechas_completas[2]:
        real = datetime.date.fromisoformat(fechas_completas[1])
        callao = datetime.date.fromisoformat(fechas_completas[2])
        datos['dias_levante'] = (real - callao).days
    else:
        datos['dias_levante'] = None
    return datos


def _siguiente_numero_importacion(conn):
    ultimo = conn.execute(
        """SELECT COALESCE(MAX(CAST(numero_importacion AS INTEGER)), 0)
           FROM importaciones
           WHERE numero_importacion NOT GLOB '*[^0-9]*'"""
    ).fetchone()[0]
    return str(ultimo + 1)


@app.route('/importaciones')
@admin_required
def listar_importaciones():
    conn = crear_conexion()
    query, canal, where_sql, parametros = _filtros_importaciones()

    indicadores = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(AVG(demora_dias), 0) AS demora_promedio,
                  COALESCE(AVG(dias_levante), 0) AS levante_promedio,
                  SUM(CASE WHEN canal_sunat IN ('Naranja', 'Rojo') THEN 1 ELSE 0 END) AS en_revision
           FROM importaciones"""
    ).fetchone()
    total_filtrado = conn.execute(
        f'SELECT COUNT(*) FROM importaciones{where_sql}', parametros
    ).fetchone()[0]
    por_pagina = 50
    paginas = max(1, (total_filtrado + por_pagina - 1) // por_pagina)
    pagina = request.args.get('pagina', 1, type=int) or 1
    pagina = min(max(pagina, 1), paginas)
    registros = conn.execute(
        f"""SELECT * FROM importaciones{where_sql}
            ORDER BY CAST(numero_importacion AS INTEGER) DESC, id DESC
            LIMIT ? OFFSET ?""",
        (*parametros, por_pagina, (pagina - 1) * por_pagina),
    ).fetchall()
    conn.close()
    return render_template(
        'importaciones/listar.html',
        importaciones=registros,
        indicadores=indicadores,
        busqueda=query,
        canal_seleccionado=canal,
        total_filtrado=total_filtrado,
        pagina=pagina,
        paginas=paginas,
    )


@app.route('/importaciones/nueva', methods=['GET', 'POST'])
@admin_required
def nueva_importacion():
    conn = crear_conexion()
    if request.method == 'POST':
        try:
            datos = _datos_formulario(request.form)
            conn.execute('BEGIN IMMEDIATE')
            numero = datos['numero_importacion']
            if numero.lower() == 'auto':
                numero = _siguiente_numero_importacion(conn)
            datos['numero_importacion'] = numero
            columnas = list(datos)
            conn.execute(
                f"INSERT INTO importaciones ({', '.join(columnas)}) "
                f"VALUES ({', '.join('?' for _ in columnas)})",
                [datos[campo] for campo in columnas],
            )
            conn.commit()
            flash('Importación registrada correctamente.', 'success')
            conn.close()
            return redirect(url_for('listar_importaciones'))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'danger')
        except sqlite3.IntegrityError:
            conn.rollback()
            flash('Ese número de importación ya está registrado.', 'danger')
        except Exception as exc:
            conn.rollback()
            app.logger.exception('No se pudo registrar la importación.')
            flash(f'No se pudo registrar la importación: {exc}', 'danger')
        conn.close()
        datos_formulario = request.form
    else:
        datos_formulario = {
            'numero_importacion': _siguiente_numero_importacion(conn),
            **{campo: '' for campo in CAMPOS_TEXTO},
            **{campo: '' for campo in FECHAS},
        }
    conn.close()
    return render_template(
        'importaciones/formulario.html',
        importacion=datos_formulario,
        titulo='Nueva importación',
        modo='nueva',
    )


@app.route('/importaciones/<int:importacion_id>/editar', methods=['GET', 'POST'])
@admin_required
def editar_importacion(importacion_id):
    conn = crear_conexion()
    existente = conn.execute(
        'SELECT * FROM importaciones WHERE id=?', (importacion_id,)
    ).fetchone()
    if not existente:
        conn.close()
        flash('No se encontró la importación.', 'warning')
        return redirect(url_for('listar_importaciones'))

    if request.method == 'POST':
        try:
            datos = _datos_formulario(request.form)
            conn.execute('BEGIN IMMEDIATE')
            columnas = [campo for campo in datos if campo != 'numero_importacion']
            conn.execute(
                f"UPDATE importaciones SET "
                f"{', '.join(f'{campo}=?' for campo in columnas)}, "
                "fecha_actualizacion=CURRENT_TIMESTAMP WHERE id=?",
                [*[datos[campo] for campo in columnas], importacion_id],
            )
            conn.commit()
            flash('Importación actualizada correctamente.', 'success')
            conn.close()
            return redirect(url_for('listar_importaciones'))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            conn.rollback()
            app.logger.exception('No se pudo actualizar la importación.')
            flash(f'No se pudo actualizar la importación: {exc}', 'danger')
        conn.close()
        datos_formulario = request.form
    else:
        datos_formulario = dict(existente)
    conn.close()
    return render_template(
        'importaciones/formulario.html',
        importacion=datos_formulario,
        titulo=f"Editar importación {existente['numero_importacion']}",
        modo='editar',
    )


@app.route('/importaciones/exportar_excel')
@admin_required
def exportar_importaciones_excel():
    conn = crear_conexion()
    _, _, where_sql, parametros = _filtros_importaciones()
    registros = conn.execute(
        f"""SELECT {', '.join(campo for campo, _ in COLUMNAS_EXCEL)}
            FROM importaciones{where_sql}
            ORDER BY CAST(numero_importacion AS INTEGER), id""",
        parametros,
    ).fetchall()
    conn.close()

    workbook = Workbook()
    hoja = workbook.active
    hoja.title = 'Importaciones'
    hoja.append([titulo for _, titulo in COLUMNAS_EXCEL])
    for registro in registros:
        hoja.append([registro[campo] for campo, _ in COLUMNAS_EXCEL])
    hoja.freeze_panes = 'A2'
    hoja.auto_filter.ref = hoja.dimensions
    for cell in hoja[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F4E79')
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for indice, (campo, titulo) in enumerate(COLUMNAS_EXCEL, start=1):
        letra = get_column_letter(indice)
        if campo.startswith('fecha_'):
            for celda in hoja[letra][1:]:
                celda.number_format = 'dd/mm/yyyy'
        max_largo = max(
            [len(titulo)]
            + [min(len(str(hoja.cell(fila, indice).value or '')), 55)
               for fila in range(2, hoja.max_row + 1)]
        )
        hoja.column_dimensions[letra].width = min(max(max_largo + 2, 14), 42)
    hoja.row_dimensions[1].height = 32

    archivo = BytesIO()
    workbook.save(archivo)
    archivo.seek(0)
    return send_file(
        archivo,
        as_attachment=True,
        download_name=secure_filename('control_importaciones.xlsx'),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/importaciones/exportar_pdf')
@admin_required
def exportar_importaciones_pdf():
    conn = crear_conexion()
    _, canal, where_sql, parametros = _filtros_importaciones()
    registros = conn.execute(
        f"""SELECT numero_importacion, dua_dam, proveedor, categoria_producto,
                   fecha_emision_pedido, fecha_estimada_llegada, fecha_real_llegada,
                   canal_sunat, demora_dias, dias_levante, incidencias
            FROM importaciones{where_sql}
            ORDER BY CAST(numero_importacion AS INTEGER), id""",
        parametros,
    ).fetchall()
    conn.close()

    archivo = BytesIO()
    documento = SimpleDocTemplate(
        archivo,
        pagesize=landscape(letter),
        rightMargin=28,
        leftMargin=28,
        topMargin=30,
        bottomMargin=28,
        title='Reporte de control de importaciones',
    )
    estilos = getSampleStyleSheet()
    titulo = ParagraphStyle(
        'TituloImportaciones',
        parent=estilos['Title'],
        fontName='Helvetica-Bold',
        fontSize=17,
        leading=21,
        alignment=TA_LEFT,
        textColor=colors.HexColor('#16324f'),
        spaceAfter=5,
    )
    detalle = ParagraphStyle(
        'DetalleImportaciones',
        parent=estilos['Normal'],
        fontName='Helvetica',
        fontSize=7,
        leading=9,
        alignment=TA_LEFT,
        wordWrap='CJK',
    )
    encabezado = ParagraphStyle(
        'EncabezadoImportaciones',
        parent=detalle,
        fontName='Helvetica-Bold',
        textColor=colors.white,
        alignment=TA_CENTER,
    )

    historia = [
        Paragraph('Reporte de control de importaciones', titulo),
        Paragraph(
            f"Generado: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')} "
            f"&nbsp;&nbsp;|&nbsp;&nbsp; Registros: {len(registros)}"
            + (f" &nbsp;&nbsp;|&nbsp;&nbsp; Canal SUNAT: {escape(canal)}" if canal else ''),
            detalle,
        ),
        Spacer(1, 12),
    ]
    titulos = [
        'N.º', 'DUA / DAM', 'Proveedor', 'Mercancía', 'Pedido',
        'Llegada estimada', 'Llegada real', 'Canal', 'Demora', 'Levante',
        'Incidencias / observaciones',
    ]
    filas = [[Paragraph(escape(text), encabezado) for text in titulos]]
    for registro in registros:
        fila = []
        for campo in (
            'numero_importacion', 'dua_dam', 'proveedor', 'categoria_producto',
            'fecha_emision_pedido', 'fecha_estimada_llegada', 'fecha_real_llegada',
            'canal_sunat', 'demora_dias', 'dias_levante', 'incidencias',
        ):
            valor = registro[campo]
            if campo.startswith('fecha_') and valor:
                valor = f'{valor[8:10]}/{valor[5:7]}/{valor[:4]}'
            if campo == 'incidencias' and valor and len(str(valor)) > 350:
                valor = f'{str(valor)[:347]}...'
            fila.append(Paragraph(escape(str(valor if valor is not None else '—')), detalle))
        filas.append(fila)

    tabla = Table(
        filas,
        colWidths=[35, 57, 78, 78, 48, 57, 57, 43, 40, 40, 145],
        repeatRows=1,
        hAlign='LEFT',
    )
    tabla.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#16324f')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (7, 1), (9, -1), 'CENTER'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f3f6fa')]),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#d6dee8')),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    historia.append(tabla)
    documento.build(historia)
    archivo.seek(0)
    return send_file(
        archivo,
        as_attachment=True,
        download_name=secure_filename('reporte_control_importaciones.pdf'),
        mimetype='application/pdf',
    )


@app.route('/api/dashboard_importaciones')
@admin_required
def api_dashboard_importaciones():
    conn = crear_conexion()
    resumen = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(AVG(demora_dias), 0) AS demora_promedio,
                  COALESCE(AVG(dias_levante), 0) AS levante_promedio,
                  SUM(CASE WHEN canal_sunat IN ('Naranja', 'Rojo') THEN 1 ELSE 0 END) AS en_revision
           FROM importaciones"""
    ).fetchone()
    canales = conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(canal_sunat), ''), 'Sin asignar') AS canal,
                  COUNT(*) AS cantidad
           FROM importaciones
           GROUP BY COALESCE(NULLIF(TRIM(canal_sunat), ''), 'Sin asignar')
           ORDER BY CASE canal
               WHEN 'Verde' THEN 1 WHEN 'Naranja' THEN 2
               WHEN 'Rojo' THEN 3 ELSE 4 END"""
    ).fetchall()
    conn.close()
    return jsonify({
        'success': True,
        'data': {
            'total': int(resumen['total'] or 0),
            'demora_promedio': round(float(resumen['demora_promedio'] or 0), 1),
            'levante_promedio': round(float(resumen['levante_promedio'] or 0), 1),
            'en_revision': int(resumen['en_revision'] or 0),
            'canales': [
                {'nombre': fila['canal'], 'cantidad': int(fila['cantidad'])}
                for fila in canales
            ],
        },
    })
