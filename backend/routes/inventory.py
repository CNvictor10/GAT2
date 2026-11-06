"""Rutas de inventory."""

# Registramos estas vistas en la instancia Flask existente para conservar las URL.
from backend.app import *  # noqa: F401,F403
from backend.routes.auth import role_required
from werkzeug.utils import secure_filename
import os
import uuid


def guardar_imagen_producto(archivo):
    """Guarda únicamente imágenes raster permitidas y devuelve su ruta estática."""
    if not archivo or not archivo.filename:
        return ''
    extension = os.path.splitext(secure_filename(archivo.filename))[1].lower()
    firmas = {
        '.jpg': lambda data: data.startswith(b'\xff\xd8\xff'),
        '.jpeg': lambda data: data.startswith(b'\xff\xd8\xff'),
        '.png': lambda data: data.startswith(b'\x89PNG\r\n\x1a\n'),
        '.webp': lambda data: len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP',
    }
    archivo.stream.seek(0)
    firma = archivo.stream.read(16)
    archivo.stream.seek(0)
    if extension not in firmas or not firmas[extension](firma):
        raise ValueError('La imagen debe ser JPG, PNG o WEBP válida.')
    directorio = os.path.join(app.static_folder, 'uploads', 'products')
    os.makedirs(directorio, exist_ok=True)
    nombre = f"{uuid.uuid4().hex}{extension}"
    archivo.save(os.path.join(directorio, nombre))
    return f"uploads/products/{nombre}"


def eliminar_imagen_producto(ruta):
    if not ruta or not ruta.startswith('uploads/products/'):
        return
    directorio = os.path.realpath(os.path.join(app.static_folder, 'uploads', 'products'))
    archivo = os.path.realpath(os.path.join(app.static_folder, ruta))
    if os.path.commonpath([directorio, archivo]) == directorio and os.path.isfile(archivo):
        os.remove(archivo)


@app.route('/inventario/importar-excel', methods=['POST'])
@role_required('inventario')
def importar_inventario_excel():
    archivo = request.files.get('archivo_excel')
    nombre_archivo = (archivo.filename or '').strip() if archivo else ''
    if not archivo or not nombre_archivo:
        flash('Selecciona un archivo Excel .xlsx.', 'warning')
        return redirect(url_for('listar_inventario'))
    if not nombre_archivo.lower().endswith('.xlsx'):
        flash('El archivo debe tener formato .xlsx.', 'danger')
        return redirect(url_for('listar_inventario'))

    resultado = sincronizar_inventario_desde_excel(archivo)
    flash(
        resultado.get('message', 'No se pudo importar el Excel.'),
        'success' if resultado.get('success') else 'danger',
    )
    return redirect(url_for('listar_inventario'))

@app.route('/inventario')
@role_required('inventario')
def listar_inventario():
    search = request.args.get('search_query', '').strip()
    
    if search:
        productos = buscar_productos_bd(search)
    else:
        productos = obtener_inventario_desde_bd()
    
    bajos = get_productos_bajo_stock_bd()
    
    return render_template('inventario/listar.html', 
                         inventario=productos, 
                         alertas_stock=len(bajos),
                         productos_bajo_stock=bajos, 
                         search_query=search)

@app.route('/inventario/agregar', methods=['GET', 'POST'])
@role_required('inventario')
def agregar_inventario():
    if request.method == 'POST':
        try:
            imagen = guardar_imagen_producto(request.files.get('imagen'))
            precio_unitario = float(request.form.get('precio_unitario') or request.form.get('precio_unidad_nuevo') or 0)
            conn = crear_conexion()
            cursor = conn.cursor()
            
            cursor.execute("SELECT MAX(id) FROM inventario")
            max_id = cursor.fetchone()[0] or 0
            nuevo_id = max_id + 1
            
            nombre_es = request.form['nombre']
            nombre_en = request.form.get('nombre_en', '')
            nombre_zh = request.form.get('nombre_zh', '')
            
            cursor.execute("""
                INSERT INTO inventario (
                    id, nombre, nombre_en, nombre_zh, descripcion, 
                    cantidad, precio_unitario, precio_unidad, umbral_stock, costo_adquisicion, unidades_por_caja, imagen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                nuevo_id,
                nombre_es,
                nombre_en,
                nombre_zh,
                request.form['descripcion'],
                int(request.form['cantidad']),
                precio_unitario,
                precio_unitario,
                int(request.form['umbral_stock']),
                0,
                0,
                imagen
            ))
            
            conn.commit()
            conn.close()
            flash('Producto agregado correctamente', 'success')
            return redirect(url_for('listar_inventario'))
            
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    
    return render_template('inventario/agregar.html')

@app.route('/inventario/editar/<int:id>', methods=['GET', 'POST'])
@role_required('inventario')
def editar_inventario(id):
    producto = obtener_producto_por_id_bd(id)
    
    if not producto:
        flash('Producto no encontrado', 'danger')
        return redirect(url_for('listar_inventario'))
    
    if request.method == 'POST':
        try:
            imagen_actual = producto.get('imagen', '')
            archivo_imagen = request.files.get('imagen')
            precio_unitario = float(request.form.get('precio_unitario') or request.form.get('precio_unidad_nuevo') or 0)
            nueva_imagen = guardar_imagen_producto(archivo_imagen) if archivo_imagen and archivo_imagen.filename else imagen_actual
            if request.form.get('quitar_imagen') == '1':
                nueva_imagen = ''
            conn = crear_conexion()
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE inventario SET 
                    nombre = ?,
                    descripcion = ?,
                    cantidad = ?,
                    precio_unitario = ?,
                    precio_unidad = ?,
                    umbral_stock = ?,
                    costo_adquisicion = ?,
                    unidades_por_caja = ?,
                    imagen = ?,
                    fecha_actualizacion = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                request.form['nombre'],
                request.form['descripcion'],
                int(request.form['cantidad']),
                precio_unitario,
                precio_unitario,
                int(request.form['umbral_stock']),
                # El costo es interno y este formulario no lo modifica.
                float(producto.get('costo_adquisicion', 0)),
                0,
                nueva_imagen,
                id
            ))
            
            conn.commit()
            conn.close()
            if nueva_imagen != imagen_actual:
                eliminar_imagen_producto(imagen_actual)
            flash('Producto actualizado correctamente', 'success')
            return redirect(url_for('listar_inventario'))
            
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    
    return render_template('inventario/editar.html', producto=producto)

@app.route('/inventario/eliminar/<int:id>', methods=['POST'])
@role_required('inventario')
def eliminar_inventario(id):
    try:
        producto = obtener_producto_por_id_bd(id)
        conn = crear_conexion()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM inventario WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        if producto:
            eliminar_imagen_producto(producto.get('imagen', ''))
        flash('Producto eliminado correctamente', 'success')
    except Exception as e:
        flash(f'Error al eliminar: {e}', 'danger')
    
    return redirect(url_for('listar_inventario'))

@app.route('/inventario/exportar_excel')
@role_required('inventario')
def exportar_inventario_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.page import PageMargins

    search = request.args.get('search_query', '').strip()
    productos = buscar_productos_bd(search) if search else obtener_inventario_desde_bd()
    idioma = idioma_de_solicitud()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Conteo de inventario'
    sheet.sheet_view.showGridLines = False

    headers = [
        'Producto', 'Código', 'Stock disponible', 'Precio por unidad', 'Mínimo',
        'Conteo físico', 'Diferencia', 'Observaciones',
    ]
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    title = sheet.cell(row=1, column=1, value='CONTEO FÍSICO DE INVENTARIO')
    title.font = Font(name='Arial', size=16, bold=True, color='FFFFFF')
    title.fill = PatternFill('solid', fgColor='1F4E79')
    title.alignment = Alignment(horizontal='left', vertical='center')
    sheet.row_dimensions[1].height = 30

    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    subtitle = sheet.cell(
        row=2,
        column=1,
        value=f'Listado de productos para conteo manual · Total: {len(productos)}',
    )
    subtitle.font = Font(name='Arial', size=10, italic=True, color='526579')
    subtitle.alignment = Alignment(vertical='center')
    sheet.row_dimensions[2].height = 22

    header_row = 4
    thin_gray = Side(style='thin', color='D8E0E8')
    for column, header in enumerate(headers, 1):
        cell = sheet.cell(row=header_row, column=column, value=header)
        cell.font = Font(name='Arial', size=10, bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='355C7D')
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = Border(bottom=Side(style='medium', color='1F4E79'))
    sheet.row_dimensions[header_row].height = 30

    for row_number, producto in enumerate(productos, header_row + 1):
        if idioma == 'en' and producto['nombre_en']:
            nombre = producto['nombre_en']
        elif idioma == 'zh' and producto['nombre_zh']:
            nombre = producto['nombre_zh']
        else:
            nombre = producto['nombre']

        values = [
            nombre,
            f"#{producto['id']}",
            producto['cantidad'],
            producto['precio_unitario'],
            producto['umbral_stock'],
            None,
            f'=IF(F{row_number}="","",F{row_number}-C{row_number})',
            None,
        ]
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row=row_number, column=column, value=value)
            cell.font = Font(name='Arial', size=10, color='172B43')
            cell.alignment = Alignment(
                horizontal='left' if column in (1, 8) else 'center',
                vertical='center',
                wrap_text=column in (1, 8),
            )
            cell.border = Border(bottom=thin_gray)
            if column in (6, 8):
                cell.fill = PatternFill('solid', fgColor='FFF8E7')
            if column == 4:
                cell.number_format = '"S/ " #,##0.00'
            elif column in (3, 5, 6, 7):
                cell.number_format = '#,##0.##;[Red]-#,##0.##'
        sheet.row_dimensions[row_number].height = 27

    widths = {'A': 42, 'B': 12, 'C': 19, 'D': 20, 'E': 13, 'F': 17, 'G': 14, 'H': 30}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    last_row = header_row + len(productos)
    sheet.freeze_panes = 'A5'
    sheet.auto_filter.ref = f'A{header_row}:H{max(header_row, last_row)}'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.page_margins = PageMargins(left=0.25, right=0.25, top=0.5, bottom=0.5, header=0.2, footer=0.2)
    sheet.print_title_rows = f'1:{header_row}'
    sheet.print_area = f'A1:H{max(header_row, last_row)}'
    sheet.oddFooter.center.text = 'Página &[Page] de &[Pages]'

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='Conteo_inventario.xlsx',
    )

@app.route('/inventario/exportar_pdf')
@role_required('inventario')
def exportar_inventario_pdf():
    productos = obtener_inventario_desde_bd()
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [Paragraph("<b>Reporte Inventario</b>", styles['Heading1']), Spacer(1, 12)]
    
    t_data = [['ID', 'Nombre', 'Desc', 'Cant', 'Precio', 'Min']]
    for p in productos:
        t_data.append([str(p['id']), p['nombre'], p['descripcion'][:50] if p['descripcion'] else '', 
                      str(p['cantidad']), f"{p['precio_unitario']:.2f}", str(p['umbral_stock'])])
    
    table = Table(t_data)
    table.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 1, colors.black)]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    
    return make_response(buffer.getvalue(), 200, {
        'Content-Type': 'application/pdf', 
        'Content-Disposition': 'attachment; filename=inventario.pdf'
    })

# =============================================================================
# RUTAS: COTIZACIONES (COMPLETAS)
# =============================================================================
