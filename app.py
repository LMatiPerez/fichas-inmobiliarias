from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "scripts"))

for _d in ["output", "properties", "data"]:
    (ROOT_DIR / _d).mkdir(parents=True, exist_ok=True)

from generar_fichas import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CSV_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROPERTIES_DIR,
    as_float,
    build_card,
    build_caption,
    build_osm_map_image,
    export_outputs,
    extract_zonaprop_map_url,
    extract_zonaprop_photos,
    fetch_url_bytes,
    import_listing,
    load_config,
    load_rows,
    normalize_remote_url,
    parse_zonaprop_html,
    resolve_font_paths,
    save_remote_image_as_jpeg,
    slugify,
)

st.set_page_config(
    page_title="Generador de Fichas Inmobiliarias",
    page_icon="🏠",
    layout="wide",
)

config = load_config(DEFAULT_CONFIG_PATH)


def card_to_bytes(card: Image.Image) -> bytes:
    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()


def show_result(key: str) -> None:
    state = st.session_state.get(key)
    if not state:
        return
    st.success(f"Ficha generada: **{state['slug']}**")
    st.image(state["png_bytes"], use_container_width=True)
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "⬇️ Descargar PNG",
            data=state["png_bytes"],
            file_name=f"{state['slug']}.png",
            mime="image/png",
            use_container_width=True,
            key=f"{key}_dl_png",
        )
    with col2:
        st.download_button(
            "📋 Descargar caption",
            data=state["caption"].encode("utf-8"),
            file_name=f"{state['slug']}.txt",
            mime="text/plain",
            use_container_width=True,
            key=f"{key}_dl_txt",
        )
    with st.expander("Ver caption para redes sociales"):
        st.text_area("Caption", value=state["caption"], height=180,
                     label_visibility="collapsed", key=f"{key}_caption_view")
    with st.expander("Ver datos importados"):
        st.json(state["row"])


def generate_and_store(row: dict, state_key: str) -> None:
    try:
        card = build_card(row, config, DEFAULT_PROPERTIES_DIR)
    except Exception as exc:
        st.error(f"Error al generar la ficha: {exc}")
        return
    export_outputs(card, row, config, DEFAULT_OUTPUT_DIR)
    caption = build_caption(row, config)
    st.session_state[state_key] = {
        "slug": row["slug"],
        "png_bytes": card_to_bytes(card),
        "caption": caption,
        "row": row,
    }


def process_zonaprop_from_html(html: str, url: str) -> dict | None:
    try:
        row = parse_zonaprop_html(url, html)
        folder = DEFAULT_PROPERTIES_DIR / row["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        for name, photo_url in zip(
            ["foto_principal.jpg", "foto_1.jpg", "foto_2.jpg", "foto_3.jpg"],
            extract_zonaprop_photos(html),
        ):
            try:
                save_remote_image_as_jpeg(normalize_remote_url(photo_url), folder / name)
            except Exception:
                pass
        map_path = folder / "mapa.png"
        map_url = extract_zonaprop_map_url(html)
        if map_url and not map_path.exists():
            try:
                data = fetch_url_bytes(map_url)
                with Image.open(io.BytesIO(data)) as src:
                    src.convert("RGB").save(map_path, format="PNG")
            except Exception:
                pass
        if not map_path.exists():
            lat = as_float(row.get("lat", ""))
            lng = as_float(row.get("lng", ""))
            if lat is not None and lng is not None:
                regular_font_path, _ = resolve_font_paths(config)
                generated = build_osm_map_image(lat, lng, (610, 350), regular_font_path)
                if generated:
                    generated.save(map_path, format="PNG")
        return row
    except Exception as exc:
        st.error(f"Error procesando los datos: {exc}")
        return None


def make_bookmarklet(app_url: str) -> str:
    """Genera el código JavaScript del bookmarklet para ZonaProp."""
    app_url = app_url.rstrip("/")
    js = r"""(function(){
try{
var d={};
if(location.href.indexOf('zonaprop')<0){alert('Abrí primero una propiedad en zonaprop.com.ar');return;}
var scripts=document.getElementsByTagName('script');
var big='';
for(var i=0;i<scripts.length;i++){if(scripts[i].innerHTML.length>big.length)big=scripts[i].innerHTML;}

function rx(pat,src){var m=(src||big).match(pat);return m?m[1]:'';}
function rxj(pat){try{return JSON.parse(rx(pat));}catch(e){return null;}}

d.url=location.href;

var ld=null;
var ldtags=document.querySelectorAll('script[type="application/ld+json"]');
for(var j=0;j<ldtags.length;j++){try{var o=JSON.parse(ldtags[j].innerHTML);if(o['@type']==='Apartment'||o['@type']==='House'){ld=o;break;}}catch(e){}}
if(ld){
  d.titulo=(ld.name||'').replace(/\s*-\s*Zonaprop$/i,'').trim();
  d.descripcion=ld.description||'';
  var addr=ld.address||{};
  d.direccion=addr.streetAddress||'';
  d.zona=addr.addressRegion||'';
  d.localidad=(addr.addressLocality||'').split(',')[0].trim();
  d.ambientes=String(ld.numberOfRooms||'');
  d.banos=String(ld.numberOfBathroomsTotal||'');
  d.dormitorios=String(ld.numberOfBedrooms||'');
  if(ld.floorSize)d.total_m2=String(ld.floorSize.value||'');
}

var m=big.match(/"prices"\s*:\s*\[(\{[^\]]+\})\]/);
if(m){try{var p=JSON.parse(m[1]);d.moneda=p.currency||p.isoCode||'USD';d.precio=String(p.amount||'');}catch(e){}}
if(!d.precio){var ps=rx(/'price'\s*:\s*'([^']+)'/);d.moneda=ps.indexOf('USD')>=0?'USD':'$';d.precio=ps.replace(/[^\d]/g,'');}
var exp=rx(/'expenses'\s*:\s*'(\d+)'/);d.expensas=exp;

var cid=rx(/postingId\s*=\s*["']?(\d+)["']?/);d.codigo=cid;

var mf=rx(/mainFeatures\s*=\s*(\{)/);
if(mf){
  var start=big.indexOf('mainFeatures');var bi=big.indexOf('{',start);var dep=0;var end=bi;
  for(var k=bi;k<bi+20000&&k<big.length;k++){if(big[k]==='{')dep++;else if(big[k]==='}'){dep--;if(dep===0){end=k+1;break;}}}
  try{
    var mfobj=JSON.parse(big.slice(bi,end));
    var fm={'CFT100':'total_m2','CFT101':'cubierta_m2','CFT1':'ambientes','CFT2':'dormitorios','CFT3':'banos','CFT4':'toilettes','CFT5':'antiguedad','2000203':'semicubierta_m2','1000019':'disposicion','1000027':'luminosidad'};
    for(var fk in fm){if(mfobj[fk]){var fv=mfobj[fk].value;if(fv!=null)d[fm[fk]]=String(fv);}}
  }catch(e){}
}

var latb=rx(/mapLatOf\s*=\s*"([^"]+)"/);var lngb=rx(/mapLngOf\s*=\s*"([^"]+)"/);
try{d.lat=atob(latb).trim();d.lng=atob(lngb).trim();}catch(e){}
try{d.map_url=atob(rx(/urlMapOf\s*=\s*"([^"]+)"/)).trim();}catch(e){}

var pat=/(https:\/\/imgar\.zonapropcdn\.com\/avisos\/(?:resize\/)?\d[\d\/]+\/(\d+x\d+)\/(\d+)\.jpg[^\s"'<]*)/g;
var byId={};var mm;
while((mm=pat.exec(document.documentElement.innerHTML))!==null){
  var fw=parseInt(mm[2].split('x')[0]);var fid=mm[3];
  if(!byId[fid]||fw>byId[fid][0])byId[fid]=[fw,mm[1]];
}
var firstUrl='';var fi=document.querySelector('img[src*="isFirstImage"]');
if(fi){var fm2=fi.src.match(/\/(\d{8,})\.jpg/);if(fm2&&byId[fm2[1]]){firstUrl=byId[fm2[1]][1];delete byId[fm2[1]];}}
var photos=[firstUrl].concat(Object.values(byId).sort(function(a,b){return b[0]-a[0];}).map(function(x){return x[1];})).filter(Boolean).slice(0,4);
d.photos=photos;

var gfs=rx(/generalFeatures\s*[=:]\s*(\{)/);
var amenities=[];
if(gfs){
  var gs=big.indexOf('generalFeatures');var gb=big.indexOf('{',gs);var gd=0;var ge=gb;
  for(var gi=gb;gi<gb+20000&&gi<big.length;gi++){if(big[gi]==='{')gd++;else if(big[gi]==='}'){gd--;if(gd===0){ge=gi+1;break;}}}
  try{
    var gfobj=JSON.parse(big.slice(gb,ge));
    var skip=['cantidad plantas','superficie semicubierta'];
    for(var cat in gfobj){for(var fid2 in gfobj[cat]){var lbl=gfobj[cat][fid2].label||'';if(lbl&&!skip.some(function(s){return lbl.toLowerCase().indexOf(s)>=0;}))amenities.push(lbl);}}
  }catch(e){}
}
if(d.disposicion)amenities.push('Disposición '+d.disposicion);
var flags=rx(/'flagsFeatures'\s*:\s*(\[[^\]]+\])/);
try{var fa=JSON.parse(flags.replace(/'/g,'"'));fa.forEach(function(f){if(f.label)amenities.push(f.label);});}catch(e){}
d.amenities=amenities.filter(function(v,i,a){return a.indexOf(v)===i;}).join('|');

var encoded=btoa(unescape(encodeURIComponent(JSON.stringify(d))));
var safe=encoded.replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
var appUrl='APP_URL_PLACEHOLDER';
location.href=appUrl+'?zp='+safe;
}catch(ex){alert('Error: '+ex.message);}
})();"""
    js = js.replace("APP_URL_PLACEHOLDER", app_url)
    # Renombrar fm2 → fiMatch para evitar conflicto con fm (feature map)
    js = js.replace("var fm2=", "var fiMatch=").replace("if(fm2&&", "if(fiMatch&&").replace("byId[fm2[1]]", "byId[fiMatch[1]]")
    js_min = " ".join(js.split())
    return "javascript:" + js_min


# ── Procesar datos del bookmarklet (query param ?zp=...) ─────────────────────

qp = st.query_params
if "zp" in qp and "zp_processed" not in st.session_state:
    try:
        # URL-safe base64: revertir - → + y _ → / y agregar padding
        raw_b64 = qp["zp"].replace("-", "+").replace("_", "/")
        raw_b64 += "=" * (-len(raw_b64) % 4)
        raw = base64.b64decode(raw_b64).decode("utf-8")
        zp_data = json.loads(raw)
        st.session_state["zp_bookmarklet_data"] = zp_data
        st.session_state["zp_processed"] = True
        st.query_params.clear()
    except Exception as e:
        st.session_state["zp_decode_error"] = str(e)

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🏠 Generador de Fichas Inmobiliarias")
st.caption("Mudafy Nativa · Daniel Manuel Pérez")

# ── Auto-procesar datos del bookmarklet ───────────────────────────────────────

if "zp_bookmarklet_data" in st.session_state and "result_url" not in st.session_state:
    zp_data = st.session_state.pop("zp_bookmarklet_data")
    st.info(f"Datos recibidos desde ZonaProp: **{zp_data.get('titulo', '')}**")
    with st.spinner("Generando ficha..."):
        # Reconstruir HTML mínimo para reutilizar parse_zonaprop_html
        # En realidad usamos los datos directamente
        url = zp_data.get("url", "")
        slug = slugify(zp_data.get("titulo", url.split("/")[-1]))

        ubicacion_parts = [p for p in [
            zp_data.get("direccion", ""),
            zp_data.get("zona", ""),
            zp_data.get("localidad", ""),
        ] if p]
        ubicacion = " | ".join(dict.fromkeys(ubicacion_parts))

        row = {
            "slug":            slug,
            "titulo":          zp_data.get("titulo", ""),
            "ubicacion":       ubicacion,
            "codigo":          zp_data.get("codigo", ""),
            "operacion":       "Venta" if "venta" in url.lower() else "Alquiler",
            "tipo_inmueble":   zp_data.get("tipo_inmueble", "Departamento"),
            "moneda":          zp_data.get("moneda", "USD"),
            "precio":          zp_data.get("precio", ""),
            "descripcion":     zp_data.get("descripcion", ""),
            "ambientes":       zp_data.get("ambientes", ""),
            "dormitorios":     zp_data.get("dormitorios", ""),
            "banos":           zp_data.get("banos", ""),
            "toilettes":       zp_data.get("toilettes", ""),
            "garage":          "",
            "cocheras":        "",
            "antiguedad":      zp_data.get("antiguedad", ""),
            "expensas":        zp_data.get("expensas", ""),
            "orientacion":     "",
            "cubierta_m2":     zp_data.get("cubierta_m2", ""),
            "semicubierta_m2": zp_data.get("semicubierta_m2", ""),
            "total_m2":        zp_data.get("total_m2", ""),
            "terreno_m2":      "",
            "amenities":       zp_data.get("amenities", ""),
            "url":             url,
            "lat":             zp_data.get("lat", ""),
            "lng":             zp_data.get("lng", ""),
        }

        folder = DEFAULT_PROPERTIES_DIR / slug
        folder.mkdir(parents=True, exist_ok=True)

        for name, photo_url in zip(
            ["foto_principal.jpg", "foto_1.jpg", "foto_2.jpg", "foto_3.jpg"],
            zp_data.get("photos", []),
        ):
            try:
                save_remote_image_as_jpeg(normalize_remote_url(photo_url), folder / name)
            except Exception:
                pass

        map_path = folder / "mapa.png"
        map_url = zp_data.get("map_url", "")
        if map_url and not map_path.exists():
            try:
                data_bytes = fetch_url_bytes(map_url)
                with Image.open(io.BytesIO(data_bytes)) as src:
                    src.convert("RGB").save(map_path, format="PNG")
            except Exception:
                pass
        if not map_path.exists():
            lat = as_float(row.get("lat", ""))
            lng = as_float(row.get("lng", ""))
            if lat is not None and lng is not None:
                regular_font_path, _ = resolve_font_paths(config)
                generated = build_osm_map_image(lat, lng, (610, 350), regular_font_path)
                if generated:
                    generated.save(map_path, format="PNG")

        generate_and_store(row, "result_url")


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_url, tab_csv, tab_manual, tab_bookmarklet = st.tabs([
    "🔗 Desde URL",
    "📋 Desde CSV (lote)",
    "✏️ Carga manual",
    "🔖 Instalar Bookmarklet",
])

# ── Tab URL ───────────────────────────────────────────────────────────────────

with tab_url:
    st.subheader("Generar desde una publicación online")
    st.markdown(
        "Pegá el link de cualquier propiedad de **Mudafy** o **ZonaProp**. "
        "Para ZonaProp en la nube, usá el **bookmarklet** (pestaña 🔖)."
    )

    url_input = st.text_input(
        "URL",
        placeholder="https://mudafy.com.ar/... o https://www.zonaprop.com.ar/...",
        label_visibility="collapsed",
        key="url_input",
    )

    is_zonaprop = "zonaprop" in url_input.lower()

    if st.button("Generar ficha", type="primary", use_container_width=True, key="btn_url"):
        if url_input.strip():
            with st.spinner("Descargando datos y fotos..."):
                try:
                    row = import_listing(url_input.strip(), config, DEFAULT_PROPERTIES_DIR)
                    fetch_error = None
                except Exception as exc:
                    row = None
                    fetch_error = exc
            if row:
                with st.spinner("Generando ficha..."):
                    generate_and_store(row, "result_url")
                st.session_state.pop("zp_paste_visible", None)
            elif fetch_error:
                if is_zonaprop and ("403" in str(fetch_error) or "Forbidden" in str(fetch_error)):
                    st.warning(
                        "ZonaProp bloqueó la descarga desde este servidor. "
                        "Usá el **bookmarklet** en la pestaña 🔖 para generar la ficha "
                        "directamente desde tu navegador con un solo clic."
                    )
                else:
                    st.error(f"No se pudo importar la publicación: {fetch_error}")
        else:
            st.warning("Ingresá una URL válida.")

    show_result("result_url")


# ── Tab CSV ───────────────────────────────────────────────────────────────────

with tab_csv:
    st.subheader("Generar fichas desde el CSV")

    if DEFAULT_CSV_PATH.exists():
        rows = load_rows(DEFAULT_CSV_PATH)
        if rows:
            slugs = [r.get("slug", f"fila-{i}") for i, r in enumerate(rows)]
            selected_slugs = st.multiselect(
                "Seleccioná una o más propiedades",
                options=slugs,
                default=[],
                placeholder="Elegí propiedades del CSV...",
                key="csv_multiselect",
            )
            generate_all = st.checkbox("Generar TODAS las propiedades del CSV", key="csv_all")

            if st.button("Generar fichas", type="primary", use_container_width=True, key="btn_csv"):
                targets = rows if generate_all else [r for r in rows if r.get("slug") in selected_slugs]
                if not targets:
                    st.warning("Seleccioná al menos una propiedad.")
                elif len(targets) == 1:
                    with st.spinner(f"Generando {targets[0].get('slug')}..."):
                        generate_and_store(targets[0], "result_csv_single")
                    st.session_state.pop("result_csv_batch", None)
                else:
                    progress = st.progress(0, text="Generando fichas...")
                    generated: list[tuple[str, bytes]] = []
                    for i, row in enumerate(targets):
                        slug = row.get("slug", f"fila-{i}")
                        try:
                            card = build_card(row, config, DEFAULT_PROPERTIES_DIR)
                            export_outputs(card, row, config, DEFAULT_OUTPUT_DIR)
                            generated.append((slug, card_to_bytes(card)))
                        except Exception as exc:
                            st.warning(f"Error en {slug}: {exc}")
                        progress.progress((i + 1) / len(targets), text=f"Generando {slug}...")
                    progress.empty()
                    zip_buf = io.BytesIO()
                    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for slug, png_bytes in generated:
                            zf.writestr(f"{slug}.png", png_bytes)
                    st.session_state["result_csv_batch"] = {
                        "count": len(generated),
                        "zip_bytes": zip_buf.getvalue(),
                    }
                    st.session_state.pop("result_csv_single", None)

            batch = st.session_state.get("result_csv_batch")
            if batch:
                st.success(f"Se generaron {batch['count']} fichas.")
                st.download_button(
                    "⬇️ Descargar todas como ZIP",
                    data=batch["zip_bytes"],
                    file_name="fichas.zip",
                    mime="application/zip",
                    use_container_width=True,
                    key="dl_zip",
                )

            show_result("result_csv_single")
        else:
            st.info("El CSV está vacío. Agregá filas con propiedades.")
    else:
        st.error(f"No se encontró el CSV en `{DEFAULT_CSV_PATH}`.")

    st.markdown("---")
    st.caption(f"📂 CSV actual: `{DEFAULT_CSV_PATH}`")
    if DEFAULT_CSV_PATH.exists():
        st.download_button(
            "Descargar CSV para editar",
            data=DEFAULT_CSV_PATH.read_bytes(),
            file_name="propiedades.csv",
            mime="text/csv",
            key="dl_csv",
        )


# ── Tab Manual ────────────────────────────────────────────────────────────────

with tab_manual:
    st.subheader("Carga manual de una propiedad")

    with st.form("form_manual"):
        c1, c2 = st.columns(2)
        with c1:
            titulo = st.text_input("Título / Dirección *", placeholder="Ej: Rivadavia 1200")
            ubicacion = st.text_input("Ubicación", placeholder="Ej: Mar del Plata | Buenos Aires")
            codigo = st.text_input("Código", placeholder="Ej: 211296")
            descripcion = st.text_area("Descripción", height=120)
        with c2:
            col_op, col_tipo = st.columns(2)
            with col_op:
                operacion = st.selectbox("Operación", ["Venta", "Alquiler", "Alquiler temporario"])
            with col_tipo:
                tipo_inmueble = st.selectbox("Tipo", ["Casa", "Departamento", "PH", "Local", "Oficina", "Terreno", "Cochera", "Otro"])
            col_mon, col_precio = st.columns(2)
            with col_mon:
                moneda = st.selectbox("Moneda", ["USD", "$"])
            with col_precio:
                precio = st.text_input("Precio", placeholder="150000")
            expensas = st.text_input("Expensas ($)", placeholder="Ej: 45000")
            antiguedad = st.text_input("Antigüedad", placeholder="Ej: 10 años")

        st.markdown("##### Características")
        c3, c4, c5 = st.columns(3)
        with c3:
            ambientes = st.text_input("Ambientes", placeholder="3")
            dormitorios = st.text_input("Dormitorios", placeholder="2")
            banos = st.text_input("Baños", placeholder="1")
        with c4:
            toilettes = st.text_input("Toilettes", placeholder="1")
            cocheras = st.text_input("Cocheras", placeholder="1")
            orientacion = st.text_input("Orientación", placeholder="Norte")
        with c5:
            cubierta_m2 = st.text_input("Cubierta m²", placeholder="75")
            total_m2 = st.text_input("Total m²", placeholder="90")
            terreno_m2 = st.text_input("Terreno m²", placeholder="")

        amenities = st.text_input("Amenities (separados por |)", placeholder="Balcón|Baulera|Aire acondicionado|Pileta")
        url_prop = st.text_input("URL de la publicación (opcional)", placeholder="https://...")

        st.markdown("##### Fotos")
        foto_principal = st.file_uploader("Foto principal *", type=["jpg", "jpeg", "png", "webp"])
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            foto_1 = st.file_uploader("Foto 1", type=["jpg", "jpeg", "png", "webp"])
        with col_f2:
            foto_2 = st.file_uploader("Foto 2", type=["jpg", "jpeg", "png", "webp"])
        with col_f3:
            foto_3 = st.file_uploader("Foto 3", type=["jpg", "jpeg", "png", "webp"])
        mapa = st.file_uploader("Mapa (opcional)", type=["jpg", "jpeg", "png", "webp"])

        submitted = st.form_submit_button("Generar ficha", type="primary", use_container_width=True)

    if submitted:
        if not titulo.strip():
            st.error("El título es obligatorio.")
        elif not foto_principal:
            st.error("La foto principal es obligatoria.")
        else:
            slug = slugify(titulo)
            folder = DEFAULT_PROPERTIES_DIR / slug
            folder.mkdir(parents=True, exist_ok=True)

            def save_upload(upload, name: str) -> None:
                if upload:
                    with Image.open(upload) as img:
                        img.convert("RGB").save(folder / name, format="JPEG", quality=95)

            save_upload(foto_principal, "foto_principal.jpg")
            save_upload(foto_1, "foto_1.jpg")
            save_upload(foto_2, "foto_2.jpg")
            save_upload(foto_3, "foto_3.jpg")
            save_upload(mapa, "mapa.jpg")

            row = {
                "slug": slug, "titulo": titulo, "ubicacion": ubicacion,
                "codigo": codigo, "operacion": operacion, "tipo_inmueble": tipo_inmueble,
                "moneda": moneda, "precio": precio, "descripcion": descripcion,
                "ambientes": ambientes, "dormitorios": dormitorios, "banos": banos,
                "toilettes": toilettes, "garage": "", "cocheras": cocheras,
                "antiguedad": antiguedad, "expensas": expensas, "orientacion": orientacion,
                "cubierta_m2": cubierta_m2, "semicubierta_m2": "", "total_m2": total_m2,
                "terreno_m2": terreno_m2, "amenities": amenities, "url": url_prop,
                "lat": "", "lng": "",
            }
            with st.spinner("Generando ficha..."):
                generate_and_store(row, "result_manual")

    show_result("result_manual")


# ── Tab Bookmarklet ───────────────────────────────────────────────────────────

with tab_bookmarklet:
    st.subheader("Bookmarklet para ZonaProp")
    st.markdown(
        "El bookmarklet te permite generar fichas de ZonaProp con **un solo clic** "
        "desde tu navegador, sin copiar ni pegar nada. "
        "Lo instalás una sola vez."
    )

    st.markdown("#### Paso 1 — Ingresá la URL de esta app")
    app_url_input = st.text_input(
        "URL de la app",
        value=st.session_state.get("app_url_saved", ""),
        placeholder="https://fichas-inmobiliarias.streamlit.app",
        key="app_url_input",
        help="Es la URL que usás para abrir esta app en el navegador.",
    )
    if app_url_input.strip():
        st.session_state["app_url_saved"] = app_url_input.strip()

    if app_url_input.strip():
        bm_code = make_bookmarklet(app_url_input.strip())

        st.markdown("#### Paso 2 — Instalá el bookmarklet")
        st.markdown(
            "Copiá el código de abajo, luego creá un **nuevo favorito** en tu navegador "
            "(clic derecho en la barra de favoritos → *Agregar página*), "
            "poné el nombre que quieras (ej: *Generar Ficha ZP*) y pegá el código en el campo **URL**."
        )
        st.code(bm_code[:120] + "...", language=None)
        st.download_button(
            "📋 Copiar código del bookmarklet",
            data=bm_code,
            file_name="bookmarklet.txt",
            mime="text/plain",
            use_container_width=True,
        )

        st.markdown("#### Paso 3 — Usarlo")
        st.markdown(
            "1. Navegá a cualquier propiedad en **zonaprop.com.ar**\n"
            "2. Hacé clic en el favorito **Generar Ficha ZP**\n"
            "3. Se abre esta app automáticamente con todos los datos y fotos cargados\n"
            "4. La ficha se genera sola — solo descargás el resultado"
        )

        st.info("Las fotos se descargan desde los servidores de ZonaProp directamente, sin pasar por el bloqueo.")
    else:
        st.info("Ingresá la URL de la app arriba para generar el bookmarklet.")
