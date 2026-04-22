from __future__ import annotations

import base64
import html
import io
import json
import sys
import urllib.parse
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
    build_mudafy_row,
    download_mudafy_map,
    export_outputs,
    extract_remix_context,
    extract_zonaprop_map_url,
    extract_zonaprop_photos,
    fetch_url_bytes,
    fetch_url_text,
    find_mudafy_listing_payload,
    get_mudafy_photo_entries,
    import_listing,
    load_config,
    load_rows,
    normalize_remote_url,
    parse_zonaprop_html,
    pick_mudafy_photo_url,
    resolve_font_paths,
    save_remote_image_as_jpeg,
    slugify,
)

def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk_dicts(v)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _collect_mudafy_photo_urls(remix_context: dict) -> list[str]:
    """Busca TODAS las fotos en todo el remix_context, sin límite."""
    seen: set[str] = set()
    urls: list[str] = []
    for d in _walk_dicts(remix_context):
        if not isinstance(d, dict):
            continue
        if d.get("is_enabled") is False:
            continue
        if d.get("type") in ("video", "floor_plan", "blueprint"):
            continue
        url = pick_mudafy_photo_url(d)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def fetch_mudafy_listing_preview(
    url: str, config: dict, properties_dir: Path
) -> tuple:
    page_html = fetch_url_text(url)
    remix_context = extract_remix_context(page_html)
    payload = find_mudafy_listing_payload(remix_context)
    row = build_mudafy_row(url, payload)
    folder = properties_dir / row["slug"]
    folder.mkdir(parents=True, exist_ok=True)
    regular_font_path, _ = resolve_font_paths(config)
    download_mudafy_map(payload, folder, regular_font_path)
    photo_urls = _collect_mudafy_photo_urls(remix_context)
    # fallback: si la búsqueda amplia no encontró nada, usar el método estándar
    if not photo_urls:
        photo_urls = [
            u for u in (pick_mudafy_photo_url(e) for e in get_mudafy_photo_entries(payload))
            if u
        ]
    return row, photo_urls

def _extract_all_zonaprop_photos(html_str: str) -> list[str]:
    """Extrae TODAS las URLs de fotos de ZonaProp sin límite de cantidad."""
    import re as _re
    pat = _re.compile(
        r'(https://imgar\.zonapropcdn\.com/avisos/(?:resize/)?'
        r'\d[\d/]+/(\d+x\d+)/(\d+)\.jpg[^\s"\'<]*)',
        _re.IGNORECASE,
    )
    by_id: dict[str, tuple[int, str]] = {}
    for full_url, res_str, foto_id in pat.findall(html_str):
        w = int(res_str.split("x")[0])
        if foto_id not in by_id or w > by_id[foto_id][0]:
            by_id[foto_id] = (w, full_url)
    first_url = ""
    first_m = _re.search(
        r'https://imgar\.zonapropcdn\.com/avisos/[^\s"\'<]+isFirstImage=true', html_str
    )
    if first_m:
        fid_m = _re.search(r'/(\d{8,})\.jpg', first_m.group(0))
        if fid_m and fid_m.group(1) in by_id:
            first_url = by_id.pop(fid_m.group(1))[1]
    ordered = ([first_url] if first_url else []) + [
        url for _, url in sorted(by_id.values(), key=lambda x: -x[0])
    ]
    return ordered


st.set_page_config(
    page_title="Generador de Fichas Inmobiliarias",
    page_icon="🏠",
    layout="wide",
)

config = load_config(DEFAULT_CONFIG_PATH)


def card_to_bytes(card: Image.Image, image_format: str = "PNG", **save_kwargs) -> bytes:
    buf = io.BytesIO()
    card.save(buf, format=image_format, **save_kwargs)
    return buf.getvalue()


def build_listing_zip_bytes(items: list[dict[str, str | bytes]]) -> bytes:
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            slug = str(item["slug"])
            zf.writestr(f"{slug}.png", item["png_bytes"])
            zf.writestr(f"{slug}.jpg", item["jpg_bytes"])
            zf.writestr(f"{slug}.txt", str(item["caption"]).encode("utf-8"))
    return zip_buf.getvalue()


def show_result(key: str) -> None:
    state = st.session_state.get(key)
    if not state:
        return
    st.success(f"Ficha generada: **{state['slug']}**")
    st.image(state["png_bytes"], use_container_width=True)
    col1, col2, col3 = st.columns(3)
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
    with col3:
        st.download_button(
            "🗜️ Descargar ZIP",
            data=state["zip_bytes"],
            file_name=f"{state['slug']}.zip",
            mime="application/zip",
            use_container_width=True,
            key=f"{key}_dl_zip",
        )
    wa_text = urllib.parse.quote(state["caption"])
    wa_url = f"https://wa.me/?text={wa_text}"
    wa_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
        'viewBox="0 0 24 24" fill="white" style="vertical-align:middle;margin-right:7px;">'
        '<path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15'
        "-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475"
        "-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52"
        ".149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207"
        "-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297"
        "-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487"
        ".709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413"
        ".248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87"
        " 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26"
        "c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893"
        " 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495"
        " 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882"
        ' 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/>'
        "</svg>"
    )
    st.markdown(
        f'<div style="margin-top:10px;margin-bottom:4px;">'
        f'<a href="{wa_url}" target="_blank" rel="noopener noreferrer" '
        f'style="display:inline-flex;align-items:center;padding:8px 20px;'
        f'background-color:#25D366;color:white;text-decoration:none;'
        f'border-radius:6px;font-weight:600;font-size:0.95em;">'
        f'{wa_svg}Compartir por WhatsApp</a>'
        f'&nbsp;&nbsp;<span style="color:#888;font-size:0.82em;">'
        f'Abre WhatsApp con el caption · descargá la imagen arriba y adjuntala</span>'
        f"</div>",
        unsafe_allow_html=True,
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
    png_bytes = card_to_bytes(card)
    jpg_bytes = card_to_bytes(card, image_format="JPEG", quality=94, subsampling=0)
    caption = build_caption(row, config)
    st.session_state[state_key] = {
        "slug": row["slug"],
        "png_bytes": png_bytes,
        "jpg_bytes": jpg_bytes,
        "caption": caption,
        "zip_bytes": build_listing_zip_bytes([{
            "slug": row["slug"],
            "png_bytes": png_bytes,
            "jpg_bytes": jpg_bytes,
            "caption": caption,
        }]),
        "row": row,
    }


def preview_zonaprop_from_html(html_str: str, url: str) -> tuple[dict, list[str]] | None:
    """Parses ZonaProp HTML. Returns (row, all_photo_urls). Downloads map but NOT photos."""
    try:
        row = parse_zonaprop_html(url, html_str)
        folder = DEFAULT_PROPERTIES_DIR / row["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        photo_urls = _extract_all_zonaprop_photos(html_str)
        map_path = folder / "mapa.png"
        map_url = extract_zonaprop_map_url(html_str)
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
        return row, photo_urls
    except Exception as exc:
        st.error(f"Error procesando los datos: {exc}")
        return None


# ── Formulario de edición + selector de fotos ────────────────────────────────

_EDIT_FIELDS: list[tuple] = [
    ("titulo",       "Título",                  "text",     None),
    ("ubicacion",    "Ubicación",               "text",     None),
    ("operacion",    "Operación",               "select",   ["Venta", "Alquiler"]),
    ("tipo_inmueble","Tipo de inmueble",         "text",     None),
    ("moneda",       "Moneda",                  "select",   ["USD", "$"]),
    ("precio",       "Precio",                  "text",     None),
    ("expensas",     "Expensas",                "text",     None),
    ("ambientes",    "Ambientes",               "text",     None),
    ("dormitorios",  "Dormitorios",             "text",     None),
    ("banos",        "Baños",                   "text",     None),
    ("toilettes",    "Toilettes",               "text",     None),
    ("cubierta_m2",  "Sup. cubierta m²",        "text",     None),
    ("total_m2",     "Sup. total m²",           "text",     None),
    ("antiguedad",   "Antigüedad",              "text",     None),
    ("descripcion",  "Descripción",             "textarea", None),
    ("amenities",    "Amenities (separar con |)", "textarea", None),
]


def _ekey(pk: str, field: str) -> str:
    return f"{pk}_edit_{field}"


def _init_edit_state(pk: str, row: dict) -> None:
    for fname, _lbl, wtype, options in _EDIT_FIELDS:
        k = _ekey(pk, fname)
        if k not in st.session_state:
            val = str(row.get(fname, "") or "")
            if wtype == "select" and options and val not in options:
                val = options[0]
            st.session_state[k] = val


def _clear_edit_state(pk: str) -> None:
    for fname, *_ in _EDIT_FIELDS:
        st.session_state.pop(_ekey(pk, fname), None)
    for k in list(st.session_state.keys()):
        if k.startswith(f"{pk}_ph_"):
            del st.session_state[k]


def _apply_selected_photos(folder: Path, photo_urls: list[str], selected_indices: list[int]) -> None:
    target_names = ["foto_principal.jpg", "foto_1.jpg", "foto_2.jpg", "foto_3.jpg"]
    chosen = [photo_urls[i] for i in selected_indices[:4]]
    for name, url in zip(target_names, chosen):
        try:
            save_remote_image_as_jpeg(normalize_remote_url(url), folder / name)
        except Exception as exc:
            st.warning(f"No se pudo descargar {name}: {exc}")


def show_edit_form(pk: str, result_key: str) -> None:
    """Renders the edit form + photo picker for a pending import. Generates on confirm."""
    pending = st.session_state.get(pk)
    if not pending:
        return

    row = pending["row"]
    photo_urls = pending.get("photo_urls", [])
    folder: Path = pending["folder"]

    _init_edit_state(pk, row)

    st.divider()
    st.markdown("### ✏️ Revisar y editar datos")

    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Título", key=_ekey(pk, "titulo"))
    with c2:
        st.text_input("Ubicación", key=_ekey(pk, "ubicacion"))

    c1, c2, c3 = st.columns(3)
    with c1:
        st.selectbox("Operación", ["Venta", "Alquiler"], key=_ekey(pk, "operacion"))
    with c2:
        st.text_input("Tipo de inmueble", key=_ekey(pk, "tipo_inmueble"))
    with c3:
        st.selectbox("Moneda", ["USD", "$"], key=_ekey(pk, "moneda"))

    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Precio", key=_ekey(pk, "precio"))
    with c2:
        st.text_input("Expensas", key=_ekey(pk, "expensas"))

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.text_input("Ambientes", key=_ekey(pk, "ambientes"))
    with c2:
        st.text_input("Dormitorios", key=_ekey(pk, "dormitorios"))
    with c3:
        st.text_input("Baños", key=_ekey(pk, "banos"))
    with c4:
        st.text_input("Toilettes", key=_ekey(pk, "toilettes"))

    c1, c2, c3 = st.columns(3)
    with c1:
        st.text_input("Sup. cubierta m²", key=_ekey(pk, "cubierta_m2"))
    with c2:
        st.text_input("Sup. total m²", key=_ekey(pk, "total_m2"))
    with c3:
        st.text_input("Antigüedad", key=_ekey(pk, "antiguedad"))

    st.text_area("Descripción", height=120, key=_ekey(pk, "descripcion"))
    st.text_area("Amenities (separar con |)", height=80, key=_ekey(pk, "amenities"))

    # ── Photo picker ────────────────────────────────────────────────────────────
    st.markdown("### 📸 Elegir fotos")
    if photo_urls:
        n = len(photo_urls)
        st.caption(
            f"{n} foto{'s' if n != 1 else ''} disponible{'s' if n != 1 else ''}. "
            "Marcá hasta 4 en orden — la primera marcada será la foto principal. "
            "Si no marcás ninguna se usan las primeras 4."
        )
        COLS = 5
        for row_i in range((n + COLS - 1) // COLS):
            thumb_cols = st.columns(COLS)
            for ci in range(COLS):
                idx = row_i * COLS + ci
                if idx >= n:
                    break
                with thumb_cols[ci]:
                    try:
                        st.image(photo_urls[idx], use_container_width=True)
                    except Exception:
                        st.caption("–")
                    st.checkbox(f"#{idx + 1}", key=f"{pk}_ph_{idx}")
    else:
        st.info("No hay fotos disponibles para seleccionar.")

    # ── Generate button ──────────────────────────────────────────────────────────
    if st.button("🏠 Generar ficha", type="primary", use_container_width=True, key=f"{pk}_btn_gen"):
        edited_row = dict(row)
        for fname, _lbl, _wtype, _opts in _EDIT_FIELDS:
            edited_row[fname] = str(st.session_state.get(_ekey(pk, fname), "") or "")

        if photo_urls:
            selected = [
                i for i in range(len(photo_urls))
                if st.session_state.get(f"{pk}_ph_{i}", False)
            ]
            if not selected:
                selected = list(range(min(4, len(photo_urls))))
            elif len(selected) > 4:
                st.warning("Solo se usan las primeras 4 fotos marcadas.")
                selected = selected[:4]
        else:
            selected = []

        with st.spinner("Descargando fotos seleccionadas..."):
            _apply_selected_photos(folder, photo_urls, selected)

        with st.spinner("Generando ficha..."):
            generate_and_store(edited_row, result_key)

        _clear_edit_state(pk)
        del st.session_state[pk]
        st.rerun()


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
d.photos=[firstUrl].concat(Object.values(byId).sort(function(a,b){return b[0]-a[0];}).map(function(x){return x[1];})).filter(Boolean);

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

if "zp_bookmarklet_data" in st.session_state:
    zp_data = st.session_state.pop("zp_bookmarklet_data")
    st.info(f"Datos recibidos desde ZonaProp: **{zp_data.get('titulo', '')}** — revisá y editá antes de generar.")

    bm_url = zp_data.get("url", "")
    bm_slug = slugify(zp_data.get("titulo", bm_url.split("/")[-1]))

    ubicacion_parts = [p for p in [
        zp_data.get("direccion", ""),
        zp_data.get("zona", ""),
        zp_data.get("localidad", ""),
    ] if p]
    bm_ubicacion = " | ".join(dict.fromkeys(ubicacion_parts))

    bm_row = {
        "slug":            bm_slug,
        "titulo":          zp_data.get("titulo", ""),
        "ubicacion":       bm_ubicacion,
        "codigo":          zp_data.get("codigo", ""),
        "operacion":       "Venta" if "venta" in bm_url.lower() else "Alquiler",
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
        "url":             bm_url,
        "lat":             zp_data.get("lat", ""),
        "lng":             zp_data.get("lng", ""),
    }

    bm_folder = DEFAULT_PROPERTIES_DIR / bm_slug
    bm_folder.mkdir(parents=True, exist_ok=True)

    bm_map_path = bm_folder / "mapa.png"
    bm_map_url = zp_data.get("map_url", "")
    if bm_map_url and not bm_map_path.exists():
        try:
            bm_map_bytes = fetch_url_bytes(bm_map_url)
            with Image.open(io.BytesIO(bm_map_bytes)) as src:
                src.convert("RGB").save(bm_map_path, format="PNG")
        except Exception:
            pass
    if not bm_map_path.exists():
        bm_lat = as_float(bm_row.get("lat", ""))
        bm_lng = as_float(bm_row.get("lng", ""))
        if bm_lat is not None and bm_lng is not None:
            bm_font, _ = resolve_font_paths(config)
            bm_gen = build_osm_map_image(bm_lat, bm_lng, (610, 350), bm_font)
            if bm_gen:
                bm_gen.save(bm_map_path, format="PNG")

    _clear_edit_state("pending_url")
    st.session_state["pending_url"] = {
        "row": bm_row,
        "photo_urls": zp_data.get("photos", []),
        "folder": bm_folder,
    }
    st.session_state.pop("result_url", None)


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

    if st.button("Importar publicación", type="primary", use_container_width=True, key="btn_url"):
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
                        row, photo_urls = fetch_mudafy_listing_preview(
                            url_input.strip(), config, DEFAULT_PROPERTIES_DIR
                        )
                        fetch_error = None
                    except Exception as exc:
                        row = None
                        photo_urls = []
                        fetch_error = exc
                if row:
                    folder = DEFAULT_PROPERTIES_DIR / row["slug"]
                    _clear_edit_state("pending_url")
                    st.session_state["pending_url"] = {
                        "row": row,
                        "photo_urls": photo_urls,
                        "folder": folder,
                    }
                    st.session_state.pop("result_url", None)
                elif fetch_error:
                    st.error(f"No se pudo importar la publicación: {fetch_error}")
        else:
            st.warning("Ingresá una URL válida.")

    show_edit_form("pending_url", "result_url")
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

    if st.button("Importar desde HTML de ZonaProp", type="primary", use_container_width=True, key="btn_zp_html_tab"):
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
                result = preview_zonaprop_from_html(source_html, source_url)
            if result:
                row, photo_urls = result
                folder = DEFAULT_PROPERTIES_DIR / row["slug"]
                _clear_edit_state("pending_zp")
                st.session_state["pending_zp"] = {
                    "row": row,
                    "photo_urls": photo_urls,
                    "folder": folder,
                }
                st.session_state.pop("result_zonaprop_html", None)

    show_edit_form("pending_zp", "result_zonaprop_html")
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
                    generated: list[dict[str, str | bytes]] = []
                    for i, row in enumerate(targets):
                        slug = row.get("slug", f"fila-{i}")
                        try:
                            card = build_card(row, config, DEFAULT_PROPERTIES_DIR)
                            export_outputs(card, row, config, DEFAULT_OUTPUT_DIR)
                            generated.append({
                                "slug": slug,
                                "png_bytes": card_to_bytes(card),
                                "jpg_bytes": card_to_bytes(card, image_format="JPEG", quality=94, subsampling=0),
                                "caption": build_caption(row, config),
                            })
                        except Exception as exc:
                            st.warning(f"Error en {slug}: {exc}")
                        progress.progress((i + 1) / len(targets), text=f"Generando {slug}...")
                    progress.empty()
                    if generated:
                        st.session_state["result_csv_batch"] = {
                            "count": len(generated),
                            "zip_bytes": build_listing_zip_bytes(generated),
                        }
                        st.session_state.pop("result_csv_single", None)
                    else:
                        st.session_state.pop("result_csv_batch", None)
                        st.error("No se pudo generar ninguna ficha del lote.")

            batch = st.session_state.get("result_csv_batch")
            if batch:
                st.success(f"Se generaron {batch['count']} fichas.")
                st.download_button(
                    "🗜️ Descargar lote ZIP",
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
