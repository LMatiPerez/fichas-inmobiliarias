# Generador de fichas inmobiliarias

Este proyecto genera fichas de inmuebles con un formato fijo, inspirado en las referencias que pasaste, para usarlas en WhatsApp, Instagram o impresión.

## Cómo está organizado

La lógica usa dos fuentes de información:

1. Una planilla `CSV` con los datos del inmueble.
2. Una carpeta por propiedad con nombres de archivo estandarizados para las imágenes.

### Estructura recomendada

```text
INMOBILIARIA/
├─ config/
│  └─ marca.json
├─ data/
│  └─ propiedades.csv
├─ output/
├─ properties/
│  └─ daniel-50-sierra-de-los-padres/
│     ├─ foto_principal.jpg
│     ├─ mapa.jpg
│     ├─ foto_1.jpg
│     ├─ foto_2.jpg
│     └─ foto_3.jpg
└─ scripts/
   └─ generar_fichas.py
```

## Convención de nombres

Cada propiedad debe tener un `slug` único en la planilla. Ese mismo `slug` se usa como nombre de carpeta dentro de `properties/`.

Ejemplo:

- `slug`: `daniel-50-sierra-de-los-padres`
- Carpeta: `properties/daniel-50-sierra-de-los-padres/`

Dentro de esa carpeta, el script busca automáticamente estos nombres:

- `foto_principal.jpg` o `.jpeg` o `.png`
- `mapa.jpg` o `.jpeg` o `.png`
- `foto_1.jpg`
- `foto_2.jpg`
- `foto_3.jpg`

Si falta alguna imagen, el generador coloca un bloque placeholder para que no se rompa la maqueta.
Si no existe `mapa.jpg` pero la fila tiene `lat` y `lng`, el script intenta generar un mapa automáticamente usando OpenStreetMap.

## Qué editar todos los días

### 1. La planilla

Editá [data/propiedades.csv](/c:/Users/Matil/Desktop/CLAUDE_CODE/INMOBILIARIA/data/propiedades.csv:1) desde Excel o Google Sheets.

Campos principales:

- `slug`: identificador único y nombre de carpeta
- `titulo`: título grande arriba a la izquierda
- `ubicacion`: línea secundaria
- `codigo`: código interno
- `operacion`: por ejemplo `Venta`
- `tipo_inmueble`: por ejemplo `Casa`, `Lote`, `Departamento`
- `precio`: solo número, por ejemplo `179000`
- `descripcion`: texto descriptivo
- `ambientes`, `dormitorios`, `banos`, `garage`, `cocheras`, `orientacion`
- `cubierta_m2`, `semicubierta_m2`, `total_m2`, `terreno_m2`
- `amenities`: separados con `|`
- `url`: opcional, se agrega al caption exportado
- `lat`, `lng`: opcionales, permiten generar el mapa automáticamente

### 2. La marca

Editá [config/marca.json](/c:/Users/Matil/Desktop/CLAUDE_CODE/INMOBILIARIA/config/marca.json:1) para cambiar:

- nombre de la inmobiliaria
- asesor o corredor
- teléfono
- email
- web
- colores
- fuentes
- logo y QR

Si querés usar logo o QR reales:

1. Guardalos en `assets/`
2. Completá `logo_path` y `qr_path` en `config/marca.json`

Ejemplo:

```json
"logo_path": "assets/logo.png",
"qr_path": "assets/qr.png"
```

Si una ficha no tiene QR disponible y querés conservar ese espacio vacío:

```json
"missing_qr_mode": "blank"
```

Si preferís un placeholder visual para pruebas:

```json
"missing_qr_mode": "placeholder"
```

Si querés que la imagen final recorte automáticamente el espacio blanco inferior:

```json
"auto_trim_bottom": true,
"bottom_padding": 80
```

## Cómo generar las fichas

Desde la raíz del proyecto:

```powershell
python scripts/generar_fichas.py
```

Eso genera en `output/`:

- una ficha `.png`
- una ficha `.jpg`
- un `.txt` con caption base para WhatsApp o Instagram

Para generar solo una propiedad:

```powershell
python scripts/generar_fichas.py --slug daniel-50-sierra-de-los-padres
```

## Recomendación operativa

Para automatizar sin fricción, conviene trabajar siempre así:

1. Crear una fila nueva en la planilla con el `slug`.
2. Crear la carpeta de esa propiedad dentro de `properties/`.
3. Guardar las imágenes con los nombres fijos.
4. Ejecutar el script.
5. Revisar el resultado en `output/`.

## Próximo paso útil

La base ya quedó lista para escalar. El siguiente paso lógico sería agregar:

- lectura desde Excel `.xlsx`
- generación automática de mapa desde una URL
- variantes de formato para `post cuadrado`, `story` e `imagen para estado`
- exportación por lote para todo el catálogo
