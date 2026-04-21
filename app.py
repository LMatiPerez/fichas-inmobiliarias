from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "scripts"))

from generar_fichas import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CSV_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROPERTIES_DIR,
    build_card,
    build_caption,
    export_outputs,
    import_mudafy_listing,
    load_config,
    load_rows,
    slugify,
)

st.set_page_config(
    page_title="Generador de Fichas Inmobiliarias",
    page_icon="🏠",
    layout="wide",
)

st.title("🏠 Generador de Fichas Inmobiliarias")
st.caption("Mudafy Nativa · Daniel Manuel Pérez")

config = load_config(DEFAULT_CONFIG_PATH)


def card_to_bytes(card: Image.Image) -> bytes:
    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()


def show_result(key: str) -> None:
    """Muestra la ficha guardada en session_state[key] con sus botones de descarga."""
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
        st.text_area("Caption", value=state["caption"], height=180, label_visibility="collapsed", key=f"{key}_caption_view")
    with st.expander("Ver datos importados"):
        st.json(state["row"])


def generate_and_store(row: dict, state_key: str) -> None:
    """Genera la ficha, la guarda en session_state y en disco."""
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


# ── Tab URL ──────────────────────────────────────────────────────────────────

tab_url, tab_csv, tab_manual = st.tabs(
    ["🔗 Desde URL de Mudafy", "📋 Desde CSV (lote)", "✏️ Carga manual"]
)

with tab_url:
    st.subheader("Generar desde una publicación de Mudafy")
    st.markdown(
        "Pegá el link de cualquier propiedad de Mudafy y el sistema descarga "
        "los datos y las fotos automáticamente."
    )
    url_input = st.text_input(
        "URL de Mudafy",
        placeholder="https://mudafy.com.ar/ficha/propiedad/...",
        label_visibility="collapsed",
        key="url_input",
    )
    if st.button("Generar ficha", type="primary", use_container_width=True, key="btn_url"):
        if url_input.strip():
            with st.spinner("Descargando datos y fotos desde Mudafy..."):
                try:
                    row = import_mudafy_listing(url_input.strip(), config, DEFAULT_PROPERTIES_DIR)
                except Exception as exc:
                    st.error(f"No se pudo importar la publicación: {exc}")
                    row = None
            if row:
                with st.spinner("Generando ficha..."):
                    generate_and_store(row, "result_url")
        else:
            st.warning("Ingresá una URL válida de Mudafy.")

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

            # Resultado lote
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
    st.markdown("Completá los datos a mano y subí las fotos desde tu computadora.")

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
                tipo_inmueble = st.selectbox(
                    "Tipo",
                    ["Casa", "Departamento", "PH", "Local", "Oficina", "Terreno", "Cochera", "Otro"],
                )
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

        amenities = st.text_input(
            "Amenities (separados por |)",
            placeholder="Balcón|Baulera|Aire acondicionado|Pileta",
        )
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
                "slug": slug,
                "titulo": titulo,
                "ubicacion": ubicacion,
                "codigo": codigo,
                "operacion": operacion,
                "tipo_inmueble": tipo_inmueble,
                "moneda": moneda,
                "precio": precio,
                "descripcion": descripcion,
                "ambientes": ambientes,
                "dormitorios": dormitorios,
                "banos": banos,
                "toilettes": toilettes,
                "garage": "",
                "cocheras": cocheras,
                "antiguedad": antiguedad,
                "expensas": expensas,
                "orientacion": orientacion,
                "cubierta_m2": cubierta_m2,
                "semicubierta_m2": "",
                "total_m2": total_m2,
                "terreno_m2": terreno_m2,
                "amenities": amenities,
                "url": url_prop,
                "lat": "",
                "lng": "",
            }

            with st.spinner("Generando ficha..."):
                generate_and_store(row, "result_manual")

    show_result("result_manual")
