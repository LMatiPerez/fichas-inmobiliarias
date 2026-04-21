from __future__ import annotations

import base64
import html
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


def read_uploaded_text(uploaded_file) -> str:
    if not uploaded_file:
        return ""
    data = uploaded_file.getvalue()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def make_bookmarklet(app_url: str) -> str:
    """Genera el código JavaScript del bookmarklet para ZonaProp."""
    app_url = app_url.rstrip("/")
    js = r"""(function(){
try{
if(location.href.indexOf('zonaprop')<0){alert('Abrí primero una propiedad en zonaprop.com.ar');return;}
var d={};
// Usar TODO el HTML como fuente de búsqueda
var big=document.documentElement.innerHTML;
function rx(pat){var m=big.match(pat);return m?m[1]:'';}

d.url=location.href;

// Schema.org JSON-LD
var ld=null;
var ldtags=document.querySelectorAll('script[type="application/ld+json"]');
for(var j=0;j<ldtags.length;j++){
  try{var o=JSON.parse(ldtags[j].innerHTML);
  if(o['@type']==='Apartment'||o['@type']==='House'||o['@type']==='SingleFamilyResidence'){ld=o;break;}}catch(e){}
}
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

// Precio desde pricesData
var pm=big.match(/"prices"\s*:\s*\[\s*(\{(?:[^{}]|\{[^{}]*\})*\})\s*\]/);
if(pm){try{var p=JSON.parse(pm[1]);d.moneda=p.currency||p.isoCode||'USD';d.precio=String(p.amount||'');}catch(e){}}
// Fallback: 'price': 'USD 155.000'
if(!d.precio){
  var ps=rx(/'price'\s*:\s*'([^']+)'/);
  if(ps){d.moneda=ps.indexOf('USD')>=0?'USD':'$';d.precio=ps.replace(/\D/g,'');}
}
// Expensas
d.expensas=rx(/'expenses'\s*:\s*'(\d+)'/)||rx(/"expenses"\s*:\s*"?(\d+)"?/);
// Código
d.codigo=rx(/postingId\s*=\s*["'](\d+)["']/)||rx(/['"]idAviso['"]\s*:\s*['"](\d+)['"]/);

// mainFeatures — buscar en todos los scripts
var allScripts=document.querySelectorAll('script:not([src]):not([type])');
var mfRaw='';
for(var si=0;si<allScripts.length;si++){
  if(allScripts[si].innerHTML.indexOf('mainFeatures')>=0){mfRaw=allScripts[si].innerHTML;break;}
}
if(!mfRaw)mfRaw=big;
var mfStart=mfRaw.indexOf('mainFeatures');
if(mfStart>=0){
  var mfBrace=mfRaw.indexOf('{',mfStart);
  var mfDepth=0;var mfEnd=mfBrace;
  for(var mi=mfBrace;mi<mfBrace+30000&&mi<mfRaw.length;mi++){
    if(mfRaw[mi]==='{')mfDepth++;
    else if(mfRaw[mi]==='}'){mfDepth--;if(mfDepth===0){mfEnd=mi+1;break;}}
  }
  try{
    var mfobj=JSON.parse(mfRaw.slice(mfBrace,mfEnd));
    var fmap={'CFT100':'total_m2','CFT101':'cubierta_m2','CFT1':'ambientes','CFT2':'dormitorios',
              'CFT3':'banos','CFT4':'toilettes','CFT5':'antiguedad','2000203':'semicubierta_m2',
              '1000019':'disposicion','1000027':'luminosidad'};
    for(var fk in fmap){if(mfobj[fk]!=null){var fv=mfobj[fk].value;if(fv!=null)d[fmap[fk]]=String(fv);}}
  }catch(e){}
}

// Lat/Lng y mapa
try{d.lat=atob(rx(/mapLatOf\s*=\s*"([^"]+)"/).trim()).trim();}catch(e){}
try{d.lng=atob(rx(/mapLngOf\s*=\s*"([^"]+)"/).trim()).trim();}catch(e){}
try{d.map_url=atob(rx(/urlMapOf\s*=\s*"([^"]+)"/).trim()).trim();}catch(e){}

// Fotos HD
var fpat=/(https:\/\/imgar\.zonapropcdn\.com\/avisos\/(?:resize\/)?\d[\d\/]+\/(\d+x\d+)\/(\d+)\.jpg[^\s"'<]*)/g;
var byId={};var fmm;
while((fmm=fpat.exec(big))!==null){
  var fw=parseInt(fmm[2].split('x')[0]);var fid=fmm[3];
  if(!byId[fid]||fw>byId[fid][0])byId[fid]=[fw,fmm[1]];
}
var firstUrl='';var fi=document.querySelector('img[src*="isFirstImage"]');
if(fi){var fiMatch=fi.src.match(/\/(\d{8,})\.jpg/);if(fiMatch&&byId[fiMatch[1]]){firstUrl=byId[fiMatch[1]][1];delete byId[fiMatch[1]];}}
d.photos=[firstUrl].concat(Object.values(byId).sort(function(a,b){return b[0]-a[0];}).map(function(x){return x[1];})).filter(Boolean).slice(0,4);

// Amenities desde generalFeatures
var amenities=[];
var gfStart=mfRaw.indexOf('generalFeatures');
if(gfStart<0)gfStart=big.indexOf('generalFeatures');
if(gfStart>=0){
  var gfSrc=gfStart===big.indexOf('generalFeatures')?big:mfRaw;
  var gfBrace=gfSrc.indexOf('{',gfStart);
  var gfDepth=0;var gfEnd=gfBrace;
  for(var gi=gfBrace;gi<gfBrace+30000&&gi<gfSrc.length;gi++){
    if(gfSrc[gi]==='{')gfDepth++;
    else if(gfSrc[gi]==='}'){gfDepth--;if(gfDepth===0){gfEnd=gi+1;break;}}
  }
  try{
    var gfobj=JSON.parse(gfSrc.slice(gfBrace,gfEnd));
    var skip=['cantidad plantas','superficie semicubierta'];
    for(var cat in gfobj){for(var fid2 in gfobj[cat]){
      var lbl=(gfobj[cat][fid2].label||'').trim();
      if(lbl&&!skip.some(function(s){return lbl.toLowerCase().indexOf(s)>=0;}))amenities.push(lbl);
    }}
  }catch(e){}
}
if(d.disposicion)amenities.push('Disposición '+d.disposicion);
var flagsM=big.match(/'flagsFeatures'\s*:\s*(\[[^\]]+\])/);
if(flagsM){try{JSON.parse(flagsM[1].replace(/'/g,'"')).forEach(function(f){if(f.label)amenities.push(f.label);});}catch(e){}}
d.amenities=amenities.filter(function(v,i,a){return a.indexOf(v)===i;}).join('|');

var encoded=btoa(unescape(encodeURIComponent(JSON.stringify(d))));
var safe=encoded.replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
var appUrl='APP_URL_PLACEHOLDER';
var sep=appUrl.indexOf('?')>=0?'&':'?';
location.href=appUrl+sep+'zp='+safe;
}catch(ex){alert('Error: '+ex.message);}
})();"""
    js = js.replace("APP_URL_PLACEHOLDER", app_url)
    js_payload = " ".join(
        line.strip()
        for line in js.strip().splitlines()
        if not line.strip().startswith("//")
    )
    return "javascript:" + js_payload


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
if "zp_decode_error" in st.session_state:
    st.error(f"No pude leer los datos del bookmarklet: {st.session_state.pop('zp_decode_error')}")

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

tab_url, tab_zonaprop, tab_csv, tab_manual, tab_bookmarklet = st.tabs([
    "🔗 Desde URL",
    "🏘️ ZonaProp (HTML)",
    "📋 Desde CSV (lote)",
    "✏️ Carga manual",
    "🔖 Instalar Bookmarklet",
])

# ── Tab URL ───────────────────────────────────────────────────────────────────

with tab_url:
    st.subheader("Generar desde una publicación online")
    st.markdown(
        "Pegá el link de una propiedad de **Mudafy**. "
        "Para **ZonaProp**, usá la pestaña **🏘️ ZonaProp (HTML)** porque la descarga directa suele devolver `Forbidden`."
    )

    url_input = st.text_input(
        "URL",
        placeholder="https://mudafy.com.ar/...",
        label_visibility="collapsed",
        key="url_input",
    )

    is_zonaprop = "zonaprop" in url_input.lower()
    if is_zonaprop and url_input.strip():
        st.session_state["zp_source_url"] = url_input.strip()
        st.info(
            "Para ZonaProp, el método más estable ahora es la pestaña **🏘️ ZonaProp (HTML)**. "
            "Si la descarga directa falla, seguí por ahí."
        )

    if st.button("Generar ficha", type="primary", use_container_width=True, key="btn_url"):
        if url_input.strip():
            if is_zonaprop:
                st.warning(
                    "ZonaProp bloquea la importación directa por URL con `Forbidden / 403`. "
                    "Seguí por la pestaña **🏘️ ZonaProp (HTML)** usando esta misma URL."
                )
                st.session_state["zp_source_url"] = url_input.strip()
            else:
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
                    st.error(f"No se pudo importar la publicación: {fetch_error}")
        else:
            st.warning("Ingresá una URL válida.")

    show_result("result_url")


# ── Tab ZonaProp HTML ───────────────────────────────────────────────────────────

with tab_zonaprop:
    st.subheader("ZonaProp desde HTML")
    st.markdown(
        "Este es el método más estable para ZonaProp cuando la descarga directa o el bookmarklet fallan. "
        "Necesitás la URL de la publicación y el código fuente HTML."
    )
    st.markdown("#### Paso a paso")
    st.markdown(
        "1. Abrí la publicación en **ZonaProp** en tu navegador.\n"
        "2. Copiá la URL de la publicación con **Ctrl+L** y después **Ctrl+C**.\n"
        "3. Abrí el código fuente con **Ctrl+U**.\n"
        "4. En la pestaña del código fuente hacé **Ctrl+A** y después **Ctrl+C** para copiar todo el HTML.\n"
        "5. Volvé a esta app.\n"
        "6. Pegá la URL en el campo **URL de la publicación de ZonaProp**.\n"
        "7. Pegá el HTML completo en **O pegar el HTML completo**.\n"
        "8. Tocá **Generar ficha desde HTML de ZonaProp**."
    )
    st.info(
        "Si preferís, en vez de pegar el HTML podés guardar el código fuente como archivo "
        "y subirlo en `Subir archivo HTML`."
    )
    st.code(
        "ZonaProp\\n"
        "Ctrl+L\\n"
        "Ctrl+C\\n"
        "Ctrl+U\\n"
        "Ctrl+A\\n"
        "Ctrl+C\\n"
        "Volver a la app\\n"
        "Pegar URL\\n"
        "Pegar HTML\\n"
        "Generar ficha",
        language="text",
    )

    zp_html_url = st.text_input(
        "URL de la publicación de ZonaProp",
        value=st.session_state.get("zp_source_url", ""),
        placeholder="https://www.zonaprop.com.ar/propiedades/...",
        key="zp_html_url",
    )
    zp_html_file = st.file_uploader(
        "Subir archivo HTML",
        type=["html", "txt"],
        key="zp_html_file",
        help="Podés guardar el código fuente como archivo y subirlo acá.",
    )
    zp_html_text = st.text_area(
        "O pegar el HTML completo",
        height=260,
        placeholder="Pegá aquí el código fuente completo de la página de ZonaProp...",
        key="zp_html_text",
    )

    if st.button("Generar ficha desde HTML de ZonaProp", type="primary", use_container_width=True, key="btn_zp_html_tab"):
        source_url = zp_html_url.strip()
        source_html = read_uploaded_text(zp_html_file) or zp_html_text.strip()

        if not source_url:
            st.warning("Ingresá primero la URL de la publicación de ZonaProp.")
        elif "zonaprop" not in source_url.lower():
            st.warning("La URL tiene que ser una publicación de ZonaProp.")
        elif not source_html:
            st.warning("Pegá el HTML completo o subí un archivo HTML antes de generar.")
        else:
            st.session_state["zp_source_url"] = source_url
            with st.spinner("Procesando HTML de ZonaProp..."):
                row = process_zonaprop_from_html(source_html, source_url)
            if row:
                with st.spinner("Generando ficha..."):
                    generate_and_store(row, "result_zonaprop_html")

    show_result("result_zonaprop_html")


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
        "Este método es **opcional / experimental**. "
        "Si no te funciona en tu navegador, usá la pestaña **🏘️ ZonaProp (HTML)**, "
        "que ahora es el camino más estable."
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
            "Arrastrá el botón de abajo directo a tu **barra de favoritos**. "
            "Si no ves la barra, activala con **Ctrl+Shift+B**."
        )

        # Link arrastrable con href escapado para que el navegador no rompa el bookmarklet.
        bm_escaped = html.escape(bm_code, quote=True)
        st.markdown(
            f"""
<div style="text-align:center; padding: 24px 0;">
  <a href="{bm_escaped}"
     draggable="true"
     style="display:inline-block; padding:14px 28px; background:#4A7C59; color:white;
            font-size:18px; font-weight:bold; border-radius:8px; text-decoration:none;
            border: 3px dashed #2E5240; cursor:grab;"
     onclick="alert('No hagas clic acá — arrastralo a tu barra de favoritos'); return false;">
    🔖 Generar Ficha ZP
  </a>
  <p style="color:#666; margin-top:12px; font-size:14px;">
    ☝️ Arrastrá este botón a tu barra de favoritos
  </p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("#### Paso 2 bis — Instalación manual")
        st.markdown(
            "Si el arrastre no funciona en tu navegador, copiá el código completo de abajo "
            "y guardalo como un favorito nuevo en el campo **URL**."
        )
        st.text_area(
            "Código completo del bookmarklet",
            value=bm_code,
            height=180,
            key="bookmarklet_code_full",
        )
        st.download_button(
            "Descargar código completo",
            data=bm_code,
            file_name="bookmarklet-zonaprop.txt",
            mime="text/plain",
            use_container_width=True,
            key="bookmarklet_code_download",
        )

        st.markdown("#### Paso 3 — Usarlo")
        st.markdown(
            "1. Navegá a cualquier propiedad en **zonaprop.com.ar**\n"
            "2. Hacé clic en el favorito **Generar Ficha ZP** de tu barra\n"
            "3. La app se abre automáticamente con todos los datos y fotos cargados\n"
            "4. La ficha se genera sola — solo descargás el resultado"
        )

        st.info(
            "Si al hacer clic en el favorito no pasa nada, no hace falta insistir: "
            "usá la pestaña **🏘️ ZonaProp (HTML)** y generás la ficha pegando el código fuente."
        )
    else:
        st.info("Ingresá la URL de la app arriba para generar el bookmarklet.")
