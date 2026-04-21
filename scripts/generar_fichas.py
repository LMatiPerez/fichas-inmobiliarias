from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import math
import re
import urllib.error
import urllib.request
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _cffi_requests = None  # type: ignore
    _CFFI_AVAILABLE = False

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BeautifulSoup = None  # type: ignore
    _BS4_AVAILABLE = False


ROOT_DIR = Path(__file__).resolve().parent.parent
SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "marca.json"
DEFAULT_CSV_PATH = ROOT_DIR / "data" / "propiedades.csv"
DEFAULT_PROPERTIES_DIR = ROOT_DIR / "properties"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output"
OSM_TILE_SIZE = 256
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


@dataclass
class Theme:
    accent_color: str
    text_color: str
    muted_color: str
    background_color: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Genera fichas inmobiliarias a partir de un CSV y una carpeta de "
            "fotos por propiedad."
        )
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help="Ruta al archivo CSV con los datos de las propiedades.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Ruta al archivo JSON con la configuración de marca.",
    )
    parser.add_argument(
        "--properties-dir",
        type=Path,
        default=DEFAULT_PROPERTIES_DIR,
        help="Directorio con una carpeta por propiedad.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directorio donde se exportan las fichas.",
    )
    parser.add_argument(
        "--slug",
        help="Si se indica, genera únicamente la propiedad con ese slug.",
    )
    parser.add_argument(
        "--mudafy-url",
        help="Si se indica, importa una ficha de Mudafy desde el link y genera la publicaciÃ³n sin usar el CSV.",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [normalize_row(row) for row in reader]


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {key.strip(): (value or "").strip() for key, value in row.items() if key}


def as_int(value: str) -> int | None:
    if value is None:
        return None
    cleaned = (
        str(value)
        .replace("U$D", "")
        .replace("USD", "")
        .replace("$", "")
        .replace(".", "")
        .replace(",", "")
        .replace("m2", "")
        .replace("m²", "")
        .strip()
    )
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def as_float(value: str) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_currency(currency: str, amount: str) -> str:
    amount_number = as_int(amount)
    if amount_number is None:
        return f"{currency} {amount}".strip()
    symbol = currency or "U$D"
    return f"{symbol} {amount_number:,}".replace(",", ".")


def format_area(value: str) -> str:
    area_number = as_int(value)
    if area_number is None:
        return value
    return f"{area_number:,} m²".replace(",", ".")


def format_value(value: str) -> str:
    return value if value else "-"


def has_value(value) -> bool:
    if value is None:
        return False
    return str(value).strip() not in ("", "-")


def format_expenses(value: str) -> str:
    if not has_value(value):
        return ""
    return format_currency("$", str(value))


def split_amenities(raw: str) -> list[str]:
    return [item.strip() for item in raw.split("|") if item.strip()]


PROMOTED_AMENITY_LABELS: list[tuple[str, str]] = [
    ("Balcón", "balcon"),
    ("Baulera", "baulera"),
]


def extract_promoted_amenities(amenities: list[str]) -> tuple[dict[str, bool], list[str]]:
    """Separa Balcón y Baulera del resto para mostrarlos en Información básica."""
    promoted_keys = {normalize_text_token(display): display for display, _ in PROMOTED_AMENITY_LABELS}
    promoted: dict[str, bool] = {}
    remaining: list[str] = []
    for item in amenities:
        key = normalize_text_token(item)
        if key in promoted_keys:
            promoted[promoted_keys[key]] = True
        else:
            remaining.append(item)
    return promoted, remaining


def normalize_text_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    collapsed = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text).strip().lower()
    return re.sub(r"\s+", " ", collapsed)


def slugify(value: str) -> str:
    token = normalize_text_token(value).replace(" ", "-")
    return token or "propiedad"


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def dedupe_strings(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        key = normalize_text_token(cleaned)
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def normalize_remote_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").strip()

    path = quote(parsed.path, safe="/:@%+-._~")
    query = quote(parsed.query, safe="=&/:+,%@-._~")
    fragment = quote(parsed.fragment, safe="=&/:+,%@-._~")
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, fragment))


def fetch_url_bytes(url: str) -> bytes:
    request = urllib.request.Request(normalize_remote_url(url), headers={"User-Agent": HTTP_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_url_text(url: str) -> str:
    return fetch_url_bytes(url).decode("utf-8", errors="replace")


def extract_remix_context(html: str) -> dict:
    marker = "window.__remixContext = "
    start = html.find(marker)
    if start < 0:
        raise ValueError("No encontrÃ© `window.__remixContext` en el HTML de Mudafy.")
    start += len(marker)
    end = html.find(";</script>", start)
    if end < 0:
        raise ValueError("No pude aislar el JSON interno de Mudafy.")
    return json.loads(html[start:end])


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from walk_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from walk_dicts(item)


def find_mudafy_listing_payload(remix_context: dict) -> dict:
    loader_data = remix_context.get("state", {}).get("loaderData", {})
    required_keys = {"photos", "priceProps", "propertyDetailsProps", "listingMapProps", "publicationTitleProps"}
    for candidate in walk_dicts(loader_data):
        if required_keys.issubset(candidate.keys()):
            return candidate
    modern_listing_keys = {"photos", "priceData", "coordinates", "mainDetails"}
    for candidate in walk_dicts(loader_data):
        listing_data = candidate.get("listingData")
        if isinstance(listing_data, dict) and modern_listing_keys.issubset(listing_data.keys()):
            return candidate
    raise ValueError("No encontrÃ© el bloque principal de datos de la ficha de Mudafy.")


def extract_mudafy_title_slug(url: str) -> str:
    parsed_url = urlparse(url)
    path_parts = [part for part in parsed_url.path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[0] == "ficha" and path_parts[1] == "propiedad":
        return path_parts[2]
    return path_parts[-1] if path_parts else ""


def mudafy_currency(value: str) -> str:
    mapping = {
        "USD": "U$D",
        "ARS": "$",
    }
    return mapping.get(str(value or "").upper(), str(value or "U$D"))


def mudafy_operation(value: str) -> str:
    mapping = {
        "sale": "Venta",
        "rent": "Alquiler",
        "temporary_rent": "Alquiler temporal",
    }
    return mapping.get(str(value or "").lower(), str(value or "Venta").title())


def mudafy_property_kind(value: str, subtitle: str = "") -> str:
    mapping = {
        "apartment": "Departamento",
        "house": "Casa",
        "ph": "PH",
        "land": "Terreno",
        "office": "Oficina",
        "commercial": "Local",
    }
    normalized = str(value or "").strip().lower()
    if normalized in mapping:
        return mapping[normalized]
    subtitle_token = normalize_text_token(subtitle)
    if subtitle_token.startswith("departamento "):
        return "Departamento"
    if subtitle_token.startswith("casa "):
        return "Casa"
    if subtitle_token.startswith("ph "):
        return "PH"
    return str(value or "Propiedad").title()


def expand_orientation(value: str) -> str:
    mapping = {
        "N": "Norte",
        "S": "Sur",
        "E": "Este",
        "O": "Oeste",
        "NE": "Noreste",
        "NO": "Noroeste",
        "SE": "Sudeste",
        "SO": "Suroeste",
    }
    cleaned = str(value or "").strip().upper()
    return mapping.get(cleaned, str(value or "").strip())


def extract_integer_string(value: str) -> str:
    match = re.search(r"(\d+(?:[.,]\d+)?)", str(value or ""))
    if not match:
        return ""
    number = match.group(1).replace(",", ".")
    parsed = float(number)
    if parsed.is_integer():
        return str(int(parsed))
    return str(parsed).replace(".", ",")


def extract_mudafy_reference_code(url: str, title_slug: str, payload: dict) -> str:
    slug_match = re.search(r"(\d+)(?!.*\d)", title_slug or "")
    if slug_match:
        return f"MUD-{slug_match.group(1)}"
    first_photo = next(iter(payload.get("photos", [])), {})
    first_photo_url = first_non_empty(first_photo.get("original_link"), first_photo.get("large_link"), first_photo.get("medium_link"))
    if first_photo_url:
        publication_match = re.search(r"/publications/(\d+)/", str(first_photo_url))
        if publication_match:
            return f"MUD-{publication_match.group(1)}"
    parsed = urlparse(url)
    fallback = slugify(Path(parsed.path).stem)
    return f"MUD-{fallback.upper()}"


def clean_mudafy_description(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\\r", "\r").replace("\\n", "\n").replace("\xa0", " ")
    stop_markers = (
        "las descripciones arquitectonicas",
        "inciso 8 del articulo 10",
        "todas las operaciones estan a cargo",
        "se encuentra prohibido cobrar",
        "los datos fueron proporcionados por el propietario",
    )
    selected_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = " ".join(raw_line.split()).strip(" /")
        if not line:
            continue
        token = normalize_text_token(line)
        if line.startswith("*") or any(marker in token for marker in stop_markers):
            break
        selected_lines.append(line)
    if not selected_lines:
        return " ".join(cleaned.split())
    return " ".join(selected_lines)


def build_mudafy_detail_map(payload: dict) -> dict[str, tuple[str, str]]:
    sections: list[dict] = []
    property_details = payload.get("propertyDetailsProps", {})
    development_details = payload.get("developmentsDetailsProps", {})
    for key in ("mainDetails", "secondaryDetails"):
        sections.extend(property_details.get(key, []) or [])
        sections.extend(development_details.get(key, []) or [])

    listing_data = payload.get("listingData", {})
    if isinstance(listing_data, dict):
        sections.extend(listing_data.get("mainDetails", []) or [])
        sections.extend(listing_data.get("secondaryDetails", []) or [])

    detail_map: dict[str, tuple[str, str]] = {}
    for detail in sections:
        title = str(detail.get("title", "")).strip()
        if not title:
            continue
        normalized = normalize_text_token(title)
        raw_value = detail.get("value", "")
        if isinstance(raw_value, bool):
            value = "Sí" if raw_value else ""
        else:
            value = str(raw_value or "").strip()
        if normalized not in detail_map or value:
            detail_map[normalized] = (title, value)
    return detail_map


def lookup_mudafy_detail(detail_map: dict[str, tuple[str, str]], *aliases: str) -> str:
    for alias in aliases:
        normalized = normalize_text_token(alias)
        if normalized in detail_map:
            return detail_map[normalized][1]
    return ""


def build_mudafy_amenities(payload: dict, detail_map: dict[str, tuple[str, str]]) -> list[str]:
    amenities = [
        option.get("name", "")
        for option in payload.get("amenitiesSectionProps", {}).get("options", []) or []
    ]
    amenities.extend(
        option.get("name", "")
        for option in payload.get("servicesSectionProps", {}).get("options", []) or []
    )

    listing_data = payload.get("listingData", {})
    if isinstance(listing_data, dict):
        amenities.extend(option.get("name", "") for option in listing_data.get("additionals", []) or [])
        amenities.extend(option.get("name", "") for option in listing_data.get("services", []) or [])

    for schema in payload.get("schemas", []) or []:
        for option in schema.get("amenityFeature") or []:
            if option.get("value", True):
                amenities.append(option.get("name", ""))

    for title, value in detail_map.values():
        if value == "Sí":
            amenities.append(title)
    return dedupe_strings(amenities)


def build_mudafy_map_payload(payload: dict) -> dict:
    if payload.get("listingMapProps"):
        return payload.get("listingMapProps", {}) or {}

    listing_data = payload.get("listingData", {})
    if isinstance(listing_data, dict):
        return {
            "coordinates": listing_data.get("coordinates", {}) or {},
            "apiKey": payload.get("mapKey", ""),
            "locationName": listing_data.get("locationName", ""),
        }

    return {}


def get_mudafy_photo_entries(payload: dict) -> list[dict]:
    if payload.get("photos"):
        return list(payload.get("photos", []) or [])

    listing_data = payload.get("listingData", {})
    if isinstance(listing_data, dict):
        return list(listing_data.get("photos", []) or [])

    return []


def build_mudafy_row(url: str, payload: dict, slug_override: str | None = None) -> dict[str, str]:
    title_slug = extract_mudafy_title_slug(url)

    publication_title = payload.get("publicationTitleProps", {})
    listing_data = payload.get("listingData", {})
    listing_map = build_mudafy_map_payload(payload)
    price_props = payload.get("priceProps", {})
    if isinstance(listing_data, dict) and not price_props:
        price_props = listing_data.get("priceData", {}) or {}
    agent_card = payload.get("agentCardProps", {})
    detail_map = build_mudafy_detail_map(payload)
    coordinates = listing_map.get("coordinates", {}) or {}

    if publication_title:
        title = str(publication_title.get("title", "")).strip()
        subtitle = str(publication_title.get("subtitle", "")).strip()
        location_name = str(listing_map.get("locationName", "")).strip()
        property_kind_value = agent_card.get("propertyKind", "")
        price_currency = price_props.get("price_currency", "U$D")
        price_amount = price_props.get("price_amount", "")
        description = payload.get("listingDescriptionProps", {}).get("description", "")
        operation_kind = price_props.get("operationKind", "")
        expenses_amount = price_props.get("expenses_amount", "")
    else:
        title = str(
            first_non_empty(
                listing_data.get("publicAddress"),
                listing_data.get("locationShortName"),
                listing_data.get("title"),
                "",
            )
        ).strip()
        subtitle = str(listing_data.get("subtitle", "")).strip()
        location_name = str(listing_data.get("locationName", "")).strip()
        property_kind_value = listing_data.get("propertyKind", "")
        price_currency = price_props.get("currency", "U$D")
        price_amount = price_props.get("value", "")
        description = listing_data.get("description", "")
        operation_kind = first_non_empty(listing_data.get("operationKind"), price_props.get("operationKind", ""))
        expenses_amount = price_props.get("expenses", "")

    slug = slug_override or title_slug or slugify(title or "propiedad")
    cocheras = lookup_mudafy_detail(detail_map, "Cocheras", "Estacionamientos", "Estacionamiento")
    garage = lookup_mudafy_detail(detail_map, "Garage", "Garaje", "Tipo de cochera")

    return {
        "slug": slugify(slug),
        "titulo": title,
        "ubicacion": location_name.replace(", ", " | ").strip(),
        "codigo": extract_mudafy_reference_code(url, title_slug, payload),
        "operacion": mudafy_operation(operation_kind),
        "tipo_inmueble": mudafy_property_kind(property_kind_value, subtitle),
        "moneda": mudafy_currency(price_currency),
        "precio": str(price_amount).strip(),
        "descripcion": clean_mudafy_description(description),
        "ambientes": lookup_mudafy_detail(detail_map, "Ambientes"),
        "dormitorios": lookup_mudafy_detail(detail_map, "Dormitorios"),
        "banos": lookup_mudafy_detail(detail_map, "BaÃ±os", "Banos"),
        "toilettes": lookup_mudafy_detail(detail_map, "Toilettes", "Toilette"),
        "garage": garage,
        "cocheras": cocheras,
        "antiguedad": lookup_mudafy_detail(detail_map, "Antigüedad", "Antiguedad"),
        "expensas": str(expenses_amount).strip(),
        "orientacion": expand_orientation(lookup_mudafy_detail(detail_map, "OrientaciÃ³n", "Orientacion")),
        "cubierta_m2": extract_integer_string(lookup_mudafy_detail(detail_map, "Sup. Cubierta", "Superficie cubierta", "Cubierta")),
        "semicubierta_m2": extract_integer_string(lookup_mudafy_detail(detail_map, "Sup. Semicubierta", "Superficie semicubierta", "Semicubierta")),
        "total_m2": extract_integer_string(lookup_mudafy_detail(detail_map, "Superficie total", "Sup. Total", "Total")),
        "terreno_m2": extract_integer_string(lookup_mudafy_detail(detail_map, "Superficie del terreno", "Sup. Terreno", "Terreno", "Lote")),
        "amenities": "|".join(build_mudafy_amenities(payload, detail_map)),
        "url": url,
        "lat": str(coordinates.get("latitude", "")).strip(),
        "lng": str(coordinates.get("longitude", "")).strip(),
    }


def save_remote_image_as_jpeg(url: str, destination: Path) -> None:
    data = fetch_url_bytes(url)
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="JPEG", quality=95, subsampling=0)


def pick_mudafy_photo_url(photo: dict) -> str:
    return str(
        first_non_empty(
            photo.get("original_link"),
            photo.get("large_link"),
            photo.get("medium_link"),
            photo.get("small_link"),
            "",
        )
    ).strip()


def download_mudafy_photos(payload: dict, folder: Path) -> None:
    photo_entries = [
        photo
        for photo in get_mudafy_photo_entries(payload)
        if photo.get("type") == "photo" and photo.get("is_enabled", True)
    ]
    target_names = ["foto_principal.jpg", "foto_1.jpg", "foto_2.jpg", "foto_3.jpg"]
    for target_name, photo in zip(target_names, photo_entries[:4]):
        url = pick_mudafy_photo_url(photo)
        if not url:
            continue
        save_remote_image_as_jpeg(url, folder / target_name)


def build_google_static_map_url(listing_map: dict, width: int, height: int) -> str:
    coordinates = listing_map.get("coordinates", {}) or {}
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")
    api_key = str(listing_map.get("apiKey", "")).strip()
    if latitude in (None, "") or longitude in (None, "") or not api_key:
        return ""
    params = [
        ("center", f"{latitude},{longitude}"),
        ("zoom", "15"),
        ("size", f"{width}x{height}"),
        ("scale", "2"),
        ("maptype", "roadmap"),
        ("markers", f"color:red|{latitude},{longitude}"),
        ("key", api_key),
    ]
    query = "&".join(f"{quote(key)}={quote(value, safe=':,|')}" for key, value in params)
    return f"https://maps.googleapis.com/maps/api/staticmap?{query}"


def download_mudafy_map(payload: dict, folder: Path, regular_font_path: Path) -> None:
    listing_map = build_mudafy_map_payload(payload)
    folder.mkdir(parents=True, exist_ok=True)
    map_path = folder / "mapa.png"

    map_url = build_google_static_map_url(listing_map, width=610, height=350)
    if map_url:
        try:
            data = fetch_url_bytes(map_url)
            with Image.open(io.BytesIO(data)) as source:
                image = source.convert("RGB")
            image.save(map_path, format="PNG")
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    coordinates = listing_map.get("coordinates", {}) or {}
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")
    if latitude in (None, "") or longitude in (None, ""):
        return
    generated_map = build_osm_map_image(float(latitude), float(longitude), (610, 350), regular_font_path)
    if generated_map is not None:
        generated_map.save(map_path, format="PNG")


def import_mudafy_listing(
    url: str,
    config: dict,
    properties_dir: Path,
    slug_override: str | None = None,
) -> dict[str, str]:
    html = fetch_url_text(url)
    remix_context = extract_remix_context(html)
    payload = find_mudafy_listing_payload(remix_context)
    row = build_mudafy_row(url, payload, slug_override=slug_override)
    folder = properties_dir / row["slug"]
    regular_font_path, _ = resolve_font_paths(config)
    download_mudafy_photos(payload, folder)
    download_mudafy_map(payload, folder, regular_font_path)
    return row


# ── ZonaProp ──────────────────────────────────────────────────────────────────

_ZP_FEATURE_MAP = {
    "CFT100":  "total_m2",
    "CFT101":  "cubierta_m2",
    "CFT1":    "ambientes",
    "CFT2":    "dormitorios",
    "CFT3":    "banos",
    "CFT4":    "toilettes",
    "CFT5":    "antiguedad",
    "1000016": "plantas",
    "1000019": "disposicion",
    "1000027": "luminosidad",
    "2000203": "semicubierta_m2",
}
_ZP_FEATURES_TEXTO = {"disposicion", "luminosidad"}

_ZP_TIPO_MAP = {
    "veclap": "Departamento", "veclph": "PH",       "veclca": "Casa",
    "vecllc": "Local",        "vecltr": "Terreno",   "vecloc": "Oficina",
    "vecled": "Edificio",     "veclde": "Deposito",  "veclga": "Cochera",
    "veclbg": "Galpón",       "ememve": "Emprendimiento",
}


def fetch_zonaprop_html(url: str) -> str:
    """Descarga la página de ZonaProp usando curl_cffi para evitar bloqueos."""
    if _CFFI_AVAILABLE:
        headers = {
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "es-AR,es;q=0.9",
        }
        resp = _cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=25)
        return resp.text
    return fetch_url_text(url)


def extract_zonaprop_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    last = path.split("/")[-1]
    last = re.sub(r"\.html$", "", last)
    return slugify(last) or "zonaprop-propiedad"


def parse_zonaprop_price(html: str) -> tuple[str, str, str]:
    """Retorna (moneda, precio, expensas)."""
    moneda, precio, expensas = "USD", "", ""
    m = re.search(r'"prices"\s*:\s*\[(\{[^\]]+\})\]', html)
    if m:
        try:
            price_obj = json.loads(m.group(1))
            moneda = price_obj.get("currency", price_obj.get("isoCode", "USD"))
            precio = str(price_obj.get("amount", price_obj.get("formattedAmount", ""))).replace(".", "")
        except Exception:
            pass
    if not precio:
        m2 = re.search(r"'price'\s*:\s*'([^']+)'", html)
        if m2:
            raw = m2.group(1)
            moneda = "USD" if "USD" in raw.upper() else "$"
            precio = re.sub(r"[^\d]", "", raw)
    m3 = re.search(r"'expenses'\s*:\s*'(\d+)'", html)
    if m3:
        expensas = m3.group(1)
    return moneda, precio, expensas


def extract_zonaprop_main_features(html: str) -> dict:
    """Extrae el objeto mainFeatures del JS embebido."""
    m = re.search(r'const mainFeatures\s*=\s*(\{.+?\})\s*;?\s*\n', html, re.DOTALL)
    if not m:
        m = re.search(r'mainFeatures\s*=\s*(\{[^;]{50,8000}\})\s*;', html, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def extract_zonaprop_coords(html: str) -> tuple[str, str]:
    lat_m = re.search(r'mapLatOf\s*=\s*"([^"]+)"', html)
    lng_m = re.search(r'mapLngOf\s*=\s*"([^"]+)"', html)
    lat = base64.b64decode(lat_m.group(1)).decode().strip() if lat_m else ""
    lng = base64.b64decode(lng_m.group(1)).decode().strip() if lng_m else ""
    return lat, lng


def extract_zonaprop_map_url(html: str) -> str:
    m = re.search(r'urlMapOf\s*=\s*"([^"]+)"', html)
    if m:
        try:
            return base64.b64decode(m.group(1)).decode().strip()
        except Exception:
            pass
    return ""


def extract_zonaprop_photos(html: str) -> list[str]:
    """Devuelve hasta 4 URLs de fotos en la resolución más alta disponible."""
    pat = re.compile(
        r'(https://imgar\.zonapropcdn\.com/avisos/(?:resize/)?'
        r'\d[\d/]+/(\d+x\d+)/(\d+)\.jpg[^\s"\'<]*)',
        re.IGNORECASE,
    )
    by_id: dict[str, tuple[int, str]] = {}
    for full_url, res_str, foto_id in pat.findall(html):
        w = int(res_str.split("x")[0])
        if foto_id not in by_id or w > by_id[foto_id][0]:
            by_id[foto_id] = (w, full_url)
    # Ordenar: la primera imagen (isFirstImage) primero, luego por orden de aparición
    first_url = ""
    first_pat = re.search(
        r'https://imgar\.zonapropcdn\.com/avisos/[^\s"\'<]+isFirstImage=true', html
    )
    if first_pat:
        first_url_raw = first_pat.group(0)
        foto_id_m = re.search(r'/(\d{8,})\.jpg', first_url_raw)
        if foto_id_m and foto_id_m.group(1) in by_id:
            first_id = foto_id_m.group(1)
            first_url = by_id.pop(first_id)[1]
    ordered = [first_url] if first_url else []
    ordered += [url for _, url in sorted(by_id.values(), key=lambda x: -x[0])]
    return ordered[:4]


def extract_zonaprop_amenities(html: str, datos: dict) -> list[str]:
    amenities: list[str] = []
    if datos.get("tiene_balcon"):
        amenities.append("Balcón")
    if datos.get("tiene_patio"):
        amenities.append("Patio")
    if datos.get("disposicion"):
        amenities.append(f"Disposición {datos['disposicion']}")
    if datos.get("luminosidad"):
        amenities.append(f"Luminosidad {datos['luminosidad']}")
    flags_m = re.search(r"'flagsFeatures'\s*:\s*(\[[^\]]+\])", html)
    if flags_m:
        try:
            flags = json.loads(flags_m.group(1).replace("'", '"'))
            for f in flags:
                label = f.get("label", "")
                if label:
                    amenities.append(label)
        except Exception:
            pass
    return dedupe_strings(amenities)


def parse_zonaprop_html(url: str, html: str) -> dict[str, str]:
    """Convierte el HTML de ZonaProp en un property_row listo para build_card."""
    datos: dict = {}

    # Schema.org
    if _BS4_AVAILABLE:
        soup = _BeautifulSoup(html, "html.parser")
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(sc.string or "{}")
                if ld.get("@type") in ("Apartment", "House", "RealEstateListing",
                                       "SingleFamilyResidence", "Accommodation"):
                    datos["titulo_completo"]      = re.sub(r"\s*-\s*Zonaprop$", "", ld.get("name", ""), flags=re.IGNORECASE).strip()
                    datos["descripcion_completa"] = ld.get("description", "")
                    addr = ld.get("address", {})
                    datos["direccion"]  = addr.get("streetAddress", "")
                    datos["zona"]       = addr.get("addressRegion", "")
                    datos["localidad"]  = addr.get("addressLocality", "").split(",")[0].strip()
                    fs = ld.get("floorSize", {})
                    if fs:
                        datos["total_m2"] = str(int(fs.get("value", 0) or 0)) if fs.get("value") else ""
                    datos["ambientes"]   = str(ld.get("numberOfRooms", "") or "")
                    datos["banos"]       = str(ld.get("numberOfBathroomsTotal", "") or "")
                    datos["dormitorios"] = str(ld.get("numberOfBedrooms", "") or "")
                    break
            except Exception:
                pass
    else:
        # Fallback sin bs4: regex sobre JSON-LD
        m = re.search(r'"@type"\s*:\s*"Apartment".*?"name"\s*:\s*"([^"]+)"', html, re.DOTALL)
        if m:
            datos["titulo_completo"] = re.sub(r"\s*-\s*Zonaprop$", "", m.group(1)).strip()

    # mainFeatures
    features_raw = extract_zonaprop_main_features(html)
    for feat_id, campo in _ZP_FEATURE_MAP.items():
        if feat_id in features_raw:
            val = features_raw[feat_id].get("value")
            if val is None:
                continue
            if campo not in _ZP_FEATURES_TEXTO:
                try:
                    num = float(str(val).replace(",", "."))
                    val = str(int(num)) if num == int(num) else str(num)
                except (ValueError, TypeError):
                    val = "0"
            datos[campo] = str(val)

    # Tipo de propiedad desde URL
    m_tipo = re.search(r'/([a-z]{4,6})in-', url.split("/")[-1])
    tipo = _ZP_TIPO_MAP.get(m_tipo.group(1), "") if m_tipo else ""

    # Código (posting ID)
    codigo_m = re.search(r"postingId\s*=\s*[\"']?(\d+)[\"']?", html)
    codigo = codigo_m.group(1) if codigo_m else ""

    # Balcón / patio desde texto combinado
    combined = (
        datos.get("titulo_completo", "") + " " + datos.get("descripcion_completa", "")
    ).lower()
    datos["tiene_balcon"] = bool(re.search(r"balc[oó]n|terraza", combined))
    datos["tiene_patio"]  = bool(re.search(r"patio|jard[ií]n", combined))

    moneda, precio, expensas = parse_zonaprop_price(html)
    lat, lng = extract_zonaprop_coords(html)

    ubicacion_parts = [p for p in [datos.get("direccion", ""), datos.get("zona", ""), datos.get("localidad", "")] if p]
    ubicacion = " | ".join(dict.fromkeys(ubicacion_parts))

    amenities = extract_zonaprop_amenities(html, datos)

    slug = extract_zonaprop_slug(url)

    return {
        "slug":           slug,
        "titulo":         datos.get("titulo_completo", slug),
        "ubicacion":      ubicacion,
        "codigo":         codigo,
        "operacion":      "Venta" if "venta" in url.lower() else "Alquiler",
        "tipo_inmueble":  tipo,
        "moneda":         moneda,
        "precio":         precio,
        "descripcion":    datos.get("descripcion_completa", ""),
        "ambientes":      datos.get("ambientes", ""),
        "dormitorios":    datos.get("dormitorios", ""),
        "banos":          datos.get("banos", ""),
        "toilettes":      datos.get("toilettes", ""),
        "garage":         "",
        "cocheras":       "",
        "antiguedad":     datos.get("antiguedad", ""),
        "expensas":       expensas,
        "orientacion":    "",
        "cubierta_m2":    datos.get("cubierta_m2", ""),
        "semicubierta_m2": datos.get("semicubierta_m2", ""),
        "total_m2":       datos.get("total_m2", ""),
        "terreno_m2":     "",
        "amenities":      "|".join(amenities),
        "url":            url,
        "lat":            lat,
        "lng":            lng,
    }


def import_zonaprop_listing(
    url: str,
    config: dict,
    properties_dir: Path,
    slug_override: str | None = None,
) -> dict[str, str]:
    html = fetch_zonaprop_html(url)
    row = parse_zonaprop_html(url, html)
    if slug_override:
        row["slug"] = slugify(slug_override)

    folder = properties_dir / row["slug"]
    folder.mkdir(parents=True, exist_ok=True)

    # Fotos
    photos = extract_zonaprop_photos(html)
    names = ["foto_principal.jpg", "foto_1.jpg", "foto_2.jpg", "foto_3.jpg"]
    for name, photo_url in zip(names, photos):
        try:
            save_remote_image_as_jpeg(normalize_remote_url(photo_url), folder / name)
        except Exception:
            pass

    # Mapa
    map_path = folder / "mapa.png"
    if not map_path.exists():
        map_url = extract_zonaprop_map_url(html)
        if map_url:
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


def detect_listing_source(url: str) -> str:
    """Devuelve 'mudafy', 'zonaprop' o 'unknown'."""
    host = urlparse(url).netloc.lower()
    if "mudafy" in host:
        return "mudafy"
    if "zonaprop" in host:
        return "zonaprop"
    return "unknown"


def import_listing(
    url: str,
    config: dict,
    properties_dir: Path,
    slug_override: str | None = None,
) -> dict[str, str]:
    """Importa desde Mudafy o ZonaProp detectando el origen automáticamente."""
    source = detect_listing_source(url)
    if source == "mudafy":
        return import_mudafy_listing(url, config, properties_dir, slug_override)
    if source == "zonaprop":
        return import_zonaprop_listing(url, config, properties_dir, slug_override)
    raise ValueError(f"URL no reconocida (no es Mudafy ni ZonaProp): {url}")


def resolve_font_paths(config: dict) -> tuple[Path, Path]:
    def resolve(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else ROOT_DIR / p

    regular_path = resolve(config.get("font_regular", "assets/fonts/RobotoCondensed-Regular.ttf"))
    bold_path = resolve(config.get("font_bold", "assets/fonts/RobotoCondensed-Bold.ttf"))
    if not regular_path.exists() or not bold_path.exists():
        raise FileNotFoundError(
            "No se encontraron las fuentes configuradas. Revisá `font_regular` y "
            "`font_bold` en config/marca.json."
        )
    return regular_path, bold_path


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def safe_asset_path(base_dir: Path, relative_or_empty: str) -> Path | None:
    if not relative_or_empty:
        return None
    asset_path = Path(relative_or_empty)
    if not asset_path.is_absolute():
        asset_path = base_dir / asset_path
    return asset_path if asset_path.exists() else None


def find_property_image(folder: Path, base_name: str) -> Path | None:
    for extension in SUPPORTED_IMAGE_EXTENSIONS:
        candidate = folder / f"{base_name}{extension}"
        if candidate.exists():
            return candidate
    return None


def open_and_fit_image(image_path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def open_and_contain_image(image_path: Path, size: tuple[int, int], background_color: str = "#FFFFFF") -> Image.Image:
    with Image.open(image_path) as source:
        image = source.convert("RGBA")

    image.thumbnail(size, Image.Resampling.LANCZOS)
    background = Image.new("RGBA", size, background_color)
    offset_x = (size[0] - image.width) // 2
    offset_y = (size[1] - image.height) // 2
    background.paste(image, (offset_x, offset_y), image)
    return background.convert("RGB")


def paste_contained_image(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    image_path: Path,
    background_color: str = "#FFFFFF",
) -> None:
    x, y, w, h = box
    image = open_and_contain_image(image_path, (w, h), background_color)
    canvas.paste(image, (x, y))


def lat_lng_to_tile(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    tiles = 2**zoom
    x = (lng + 180.0) / 360.0 * tiles
    y = (1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi) / 2.0 * tiles
    return x, y


def fetch_web_image(url: str) -> Image.Image | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "InmobiliariaTemplate/1.0 (+https://www.nasoarcepropiedades.com.ar)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
        with Image.open(io.BytesIO(data)) as source:
            return source.convert("RGBA")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def build_osm_map_image(
    lat: float,
    lng: float,
    size: tuple[int, int],
    regular_font_path: Path,
    zoom: int = 15,
) -> Image.Image | None:
    tile_x, tile_y = lat_lng_to_tile(lat, lng, zoom)
    center_pixel_x = tile_x * OSM_TILE_SIZE
    center_pixel_y = tile_y * OSM_TILE_SIZE
    half_width = size[0] / 2
    half_height = size[1] / 2

    left_pixel = center_pixel_x - half_width
    top_pixel = center_pixel_y - half_height
    right_pixel = center_pixel_x + half_width
    bottom_pixel = center_pixel_y + half_height

    min_tile_x = math.floor(left_pixel / OSM_TILE_SIZE)
    max_tile_x = math.floor(right_pixel / OSM_TILE_SIZE)
    min_tile_y = math.floor(top_pixel / OSM_TILE_SIZE)
    max_tile_y = math.floor(bottom_pixel / OSM_TILE_SIZE)

    tile_count = 2**zoom
    mosaic_width = (max_tile_x - min_tile_x + 1) * OSM_TILE_SIZE
    mosaic_height = (max_tile_y - min_tile_y + 1) * OSM_TILE_SIZE
    mosaic = Image.new("RGBA", (mosaic_width, mosaic_height), "#F3F3F3")

    fetched_any = False
    for current_x in range(min_tile_x, max_tile_x + 1):
        for current_y in range(min_tile_y, max_tile_y + 1):
            if current_y < 0 or current_y >= tile_count:
                continue

            normalized_x = current_x % tile_count
            tile_url = f"https://tile.openstreetmap.org/{zoom}/{normalized_x}/{current_y}.png"
            tile = fetch_web_image(tile_url)
            if tile is None:
                continue

            fetched_any = True
            paste_x = (current_x - min_tile_x) * OSM_TILE_SIZE
            paste_y = (current_y - min_tile_y) * OSM_TILE_SIZE
            mosaic.paste(tile, (paste_x, paste_y), tile)

    if not fetched_any:
        return None

    crop_x = int(round(left_pixel - min_tile_x * OSM_TILE_SIZE))
    crop_y = int(round(top_pixel - min_tile_y * OSM_TILE_SIZE))
    cropped = mosaic.crop((crop_x, crop_y, crop_x + size[0], crop_y + size[1])).convert("RGB")

    draw = ImageDraw.Draw(cropped)
    pin_x = size[0] // 2
    pin_y = size[1] // 2 - 10
    draw.ellipse([pin_x - 16, pin_y - 16, pin_x + 16, pin_y + 16], fill="#D63A3A", outline="#9E2020", width=3)
    draw.polygon(
        [(pin_x, pin_y + 26), (pin_x - 12, pin_y + 4), (pin_x + 12, pin_y + 4)],
        fill="#D63A3A",
        outline="#9E2020",
    )
    draw.ellipse([pin_x - 6, pin_y - 6, pin_x + 6, pin_y + 6], fill="#FFFFFF")

    attribution_font = load_font(regular_font_path, 16)
    attribution = "OpenStreetMap"
    text_width = draw.textlength(attribution, font=attribution_font)
    draw.rounded_rectangle(
        [12, size[1] - 34, 24 + text_width, size[1] - 10],
        radius=8,
        fill=(255, 255, 255),
    )
    draw.text((18, size[1] - 31), attribution, fill="#666666", font=attribution_font)
    return cropped


def draw_slot(
    canvas: Image.Image,
    slot: tuple[int, int, int, int],
    image_path: Path | None,
    placeholder: str,
    theme: Theme,
    font_regular_path: Path,
) -> None:
    x, y, w, h = slot
    draw = ImageDraw.Draw(canvas)
    if image_path and image_path.exists():
        image = open_and_fit_image(image_path, (w, h))
        canvas.paste(image, (x, y))
        return

    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=24,
        fill="#F4F4F4",
        outline=theme.accent_color,
        width=3,
    )
    font = load_font(font_regular_path, max(22, min(42, h // 7)))
    text = placeholder.upper()
    text_width = draw.textlength(text, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_height = bbox[3] - bbox[1]
    draw.text(
        (x + (w - text_width) / 2, y + (h - text_height) / 2 - 4),
        text,
        fill=theme.muted_color,
        font=font,
    )


def draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    font_path: Path,
    size_candidates: Iterable[int],
    fill: str,
    line_spacing: int = 8,
    align: str = "left",
    max_lines: int | None = None,
) -> tuple[int, int]:
    x, y, w, h = box
    cleaned_text = " ".join(text.split())

    for size in size_candidates:
        font = load_font(font_path, size)
        lines = wrap_text(draw, cleaned_text, font, w)
        if max_lines is not None:
            lines = truncate_lines(draw, lines, font, w, max_lines)

        bbox = draw.textbbox((0, 0), "Ag", font=font)
        line_height = bbox[3] - bbox[1]
        needed_height = len(lines) * line_height + max(0, len(lines) - 1) * line_spacing
        if needed_height <= h:
            draw_multiline_lines(
                draw=draw,
                lines=lines,
                x=x,
                y=y,
                width=w,
                font=font,
                fill=fill,
                align=align,
                line_spacing=line_spacing,
            )
            return size, needed_height

    fallback_size = min(size_candidates)
    font = load_font(font_path, fallback_size)
    lines = wrap_text(draw, cleaned_text, font, w)
    if max_lines is not None:
        lines = truncate_lines(draw, lines, font, w, max_lines)
    draw_multiline_lines(
        draw=draw,
        lines=lines,
        x=x,
        y=y,
        width=w,
        font=font,
        fill=fill,
        align=align,
        line_spacing=line_spacing,
    )
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    line_height = bbox[3] - bbox[1]
    used_height = len(lines) * line_height + max(0, len(lines) - 1) * line_spacing
    return fallback_size, used_height


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    if not text:
        return [""]

    words = text.split()
    lines: list[str] = []
    current = words[0]

    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def pick_font_for_single_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    size_candidates: Iterable[int],
    max_width: int,
) -> ImageFont.FreeTypeFont:
    for size in size_candidates:
        font = load_font(font_path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
    return load_font(font_path, min(size_candidates))


def truncate_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    if len(lines) <= max_lines:
        return lines

    truncated = lines[:max_lines]
    ellipsis = "..."
    last_line = truncated[-1]
    while last_line and draw.textlength(last_line + ellipsis, font=font) > max_width:
        last_line = last_line[:-1].rstrip()
    truncated[-1] = (last_line + ellipsis).strip()
    return truncated


def draw_multiline_lines(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[str],
    x: int,
    y: int,
    width: int,
    font: ImageFont.FreeTypeFont,
    fill: str,
    align: str,
    line_spacing: int,
) -> None:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    line_height = bbox[3] - bbox[1]
    current_y = y
    for line in lines:
        if align == "right":
            line_x = x + width - draw.textlength(line, font=font)
        elif align == "center":
            line_x = x + (width - draw.textlength(line, font=font)) / 2
        else:
            line_x = x
        draw.text((line_x, current_y), line, fill=fill, font=font)
        current_y += line_height + line_spacing


def draw_section_header(
    draw: ImageDraw.ImageDraw,
    title: str,
    x: int,
    y: int,
    width: int,
    theme: Theme,
    bold_font_path: Path,
) -> int:
    header_font = load_font(bold_font_path, 38)
    draw.text((x, y), title.upper(), fill=theme.text_color, font=header_font)
    line_y = y + 60
    draw.line((x, line_y, x + width, line_y), fill=theme.accent_color, width=2)
    return line_y


def draw_placeholder_logo(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    brand_short_name: str,
    theme: Theme,
    regular_font_path: Path,
    bold_font_path: Path,
) -> None:
    x, y, w, h = box
    draw.rounded_rectangle([x, y, x + w, y + h], radius=18, fill="#FBFBFB", outline="#E6E6E6", width=2)
    main_text = brand_short_name.strip() or "TU MARCA"
    words = main_text.upper().split()
    if len(words) > 1:
        top_text = " ".join(words[:-1])
        bottom_text = words[-1]
    else:
        top_text = main_text.upper()
        bottom_text = "PROPIEDADES"

    top_font = load_font(bold_font_path, 52)
    bottom_font = load_font(regular_font_path, 24)
    top_width = draw.textlength(top_text, font=top_font)
    bottom_width = draw.textlength(bottom_text, font=bottom_font)
    draw.text((x + (w - top_width) / 2, y + 18), top_text, fill=theme.accent_color, font=top_font)
    draw.text(
        (x + (w - bottom_width) / 2, y + h - 42),
        bottom_text,
        fill=theme.text_color,
        font=bottom_font,
    )


def draw_placeholder_qr(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    theme: Theme,
    regular_font_path: Path,
) -> None:
    x, y, w, h = box
    draw.rectangle([x, y, x + w, y + h], fill="#FFFFFF", outline=theme.text_color, width=4)
    step = max(8, w // 18)
    for row in range(1, 16):
        for col in range(1, 16):
            if (row * 7 + col * 11) % 5 in (0, 1):
                cell_x = x + col * step // 1
                cell_y = y + row * step // 1
                if cell_x + step < x + w - step and cell_y + step < y + h - step:
                    draw.rectangle([cell_x, cell_y, cell_x + step, cell_y + step], fill=theme.text_color)
    font = load_font(regular_font_path, 22)
    label = "QR"
    draw.text(
        (x + w / 2 - draw.textlength(label, font=font) / 2, y + h / 2 - 12),
        label,
        fill=theme.muted_color,
        font=font,
    )


def draw_empty_qr_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
) -> None:
    x, y, w, h = box
    draw.rectangle([x, y, x + w, y + h], fill="#FFFFFF", outline="#E4E4E4", width=2)


def draw_metadata_grid(
    draw: ImageDraw.ImageDraw,
    items: Sequence[tuple[str, str]],
    start_x: int,
    start_y: int,
    width: int,
    columns: int,
    theme: Theme,
    regular_font_path: Path,
    bold_font_path: Path,
    row_height: int = 50,
) -> int:
    regular_font = load_font(regular_font_path, 24)
    bold_font = load_font(bold_font_path, 24)
    column_width = width // columns

    for index, (label, value) in enumerate(items):
        col = index % columns
        row = index // columns
        x = start_x + col * column_width
        y = start_y + row * row_height

        label_text = f"{label}: "
        draw.text((x, y), label_text, fill=theme.text_color, font=regular_font)
        label_width = draw.textlength(label_text, font=regular_font)
        draw.text((x + label_width, y), format_value(value), fill=theme.text_color, font=bold_font)

    rows_used = math.ceil(len(items) / columns)
    return start_y + rows_used * row_height


def build_basic_info_items(property_row: dict[str, str]) -> list[tuple[str, str]]:
    amenities_raw = split_amenities(property_row.get("amenities", ""))
    promoted, _ = extract_promoted_amenities(amenities_raw)

    candidates = [
        ("Ambientes", property_row.get("ambientes", "")),
        ("Baños", property_row.get("banos", "")),
        ("Toilettes", property_row.get("toilettes", "")),
        ("Dormitorios", property_row.get("dormitorios", "")),
        ("Garage", property_row.get("garage", "")),
        ("Cocheras", property_row.get("cocheras", "")),
        ("Orientación", property_row.get("orientacion", "")),
        ("Antigüedad", property_row.get("antiguedad", "")),
        ("Expensas", format_expenses(property_row.get("expensas", ""))),
        ("Balcón", "Sí" if promoted.get("Balcón") else ""),
        ("Baulera", "Sí" if promoted.get("Baulera") else ""),
    ]
    visible_items = [(label, value) for label, value in candidates if has_value(value)]
    if visible_items:
        return visible_items
    return [(label, value) for label, value in candidates[:6]]


def draw_amenities(
    draw: ImageDraw.ImageDraw,
    amenities: Sequence[str],
    box: tuple[int, int, int, int],
    theme: Theme,
    regular_font_path: Path,
) -> int:
    x, y, w, h = box
    amenities = list(amenities) or ["Sin datos cargados"]

    columns = 3
    column_width = w // columns
    rows = math.ceil(len(amenities) / columns)
    column_groups = [amenities[index * rows : (index + 1) * rows] for index in range(columns)]

    wrapped_columns: list[list[list[str]]] = []
    line_height = 0
    item_spacing = 10

    for size in range(28, 17, -1):
        font = load_font(regular_font_path, size)
        bbox = draw.textbbox((0, 0), "Ag", font=font)
        line_height = bbox[3] - bbox[1]
        wrapped_columns = []
        column_heights: list[int] = []

        for group in column_groups:
            wrapped_group: list[list[str]] = []
            current_height = 0
            for amenity in group:
                wrapped = wrap_text(draw, amenity, font, column_width - 18)
                wrapped_group.append(wrapped)
                current_height += len(wrapped) * line_height
                current_height += item_spacing
            if current_height > 0:
                current_height -= item_spacing
            wrapped_columns.append(wrapped_group)
            column_heights.append(current_height)

        if max(column_heights or [0]) <= h:
            break

    max_bottom = y
    for column_index, wrapped_group in enumerate(wrapped_columns):
        current_x = x + column_index * column_width
        current_y = y
        for wrapped_amenity in wrapped_group:
            for line in wrapped_amenity:
                draw.text((current_x, current_y), line, fill=theme.text_color, font=font)
                current_y += line_height
            current_y += item_spacing
        max_bottom = max(max_bottom, current_y - item_spacing)
    return max_bottom


def draw_brand_block(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    config: dict,
    theme: Theme,
    regular_font_path: Path,
    bold_font_path: Path,
    footer_y: int,
    right_x: int,
    right_width: int,
) -> int:
    logo_width = int(config.get("brand_logo_width", 245))
    logo_height = int(config.get("brand_logo_height", 145))
    qr_size = int(config.get("qr_box_size", 190))
    brand_left_padding = int(config.get("brand_block_padding_left", 18))
    logo_text_gap = int(config.get("brand_logo_text_gap", 34))
    text_qr_gap = int(config.get("brand_text_qr_gap", 34))
    qr_offset_y = int(config.get("brand_qr_offset_y", 5))

    logo_box = (right_x + brand_left_padding, footer_y, logo_width, logo_height)
    text_box_x = logo_box[0] + logo_box[2] + logo_text_gap
    qr_box = (right_x + right_width - qr_size, footer_y + qr_offset_y, qr_size, qr_size)
    brand_text_width = max(320, qr_box[0] - text_box_x - text_qr_gap)

    logo_path = safe_asset_path(ROOT_DIR, config.get("logo_path", ""))
    qr_path = safe_asset_path(ROOT_DIR, config.get("qr_path", ""))

    if logo_path:
        paste_contained_image(canvas, logo_box, logo_path)
    else:
        draw_placeholder_logo(
            draw=draw,
            box=logo_box,
            brand_short_name=config.get("brand_short_name", ""),
            theme=theme,
            regular_font_path=regular_font_path,
            bold_font_path=bold_font_path,
        )

    detail_font_size = int(config.get("brand_detail_font_size", 26))
    detail_line_spacing = int(config.get("brand_detail_line_spacing", 38))
    info_y = footer_y + 8

    draw.text(
        (text_box_x, info_y),
        config.get("brand_name", "Tu inmobiliaria"),
        fill=theme.text_color,
        font=pick_font_for_single_line(
            draw,
            config.get("brand_name", "Tu inmobiliaria"),
            bold_font_path,
            range(32, 24, -1),
            brand_text_width,
        ),
    )
    info_font = load_font(regular_font_path, detail_font_size)
    custom_details = config.get("brand_detail_lines", [])
    if isinstance(custom_details, list):
        details = [str(detail).strip() for detail in custom_details if str(detail).strip()]
    else:
        details = []
    if not details:
        details = [
            config.get("agent_name", ""),
            config.get("agent_phone", ""),
            config.get("agent_email", ""),
            config.get("website", ""),
        ]
    current_y = info_y + 56
    for detail in details:
        if not detail:
            continue
        draw.text((text_box_x, current_y), detail, fill=theme.text_color, font=info_font)
        current_y += detail_line_spacing

    if qr_path:
        paste_contained_image(canvas, qr_box, qr_path)
    else:
        qr_mode = str(config.get("missing_qr_mode", "blank")).strip().lower()
        if qr_mode == "placeholder":
            draw_placeholder_qr(draw, qr_box, theme, regular_font_path)
        else:
            draw_empty_qr_box(draw, qr_box)
    return max(logo_box[1] + logo_box[3], qr_box[1] + qr_box[3], current_y)


def build_card(property_row: dict[str, str], config: dict, properties_dir: Path) -> Image.Image:
    width = int(config.get("output_width", 2480))
    height = int(config.get("output_height", 1754))
    theme = Theme(
        accent_color=config.get("accent_color", "#8BA86A"),
        text_color=config.get("text_color", "#2A2A2A"),
        muted_color=config.get("muted_color", "#6E6E6E"),
        background_color=config.get("background_color", "#FFFFFF"),
    )
    regular_font_path, bold_font_path = resolve_font_paths(config)

    canvas = Image.new("RGB", (width, height), theme.background_color)
    draw = ImageDraw.Draw(canvas)

    slug = property_row["slug"]
    folder = properties_dir / slug

    page_margin_x = int(config.get("page_margin_x", 108))
    right_margin_x = int(config.get("right_margin_x", page_margin_x))
    header_top_y = int(config.get("header_top_y", 84))
    header_subtitle_offset_y = int(config.get("header_subtitle_offset_y", 82))
    header_code_offset_y = int(config.get("header_code_offset_y", 118))
    price_y = int(config.get("price_y", 90))
    badge_y = int(config.get("badge_y", 162))
    left_x = page_margin_x
    left_width = int(config.get("left_column_width", 860))
    column_gutter_x = int(config.get("column_gutter_x", 142))
    section_start_y = int(config.get("section_start_y", 248))
    section_gap = int(config.get("section_gap_y", 28))
    subsection_gap = int(config.get("subsection_gap_y", 24))
    right_x = left_x + left_width + column_gutter_x
    right_width = width - right_x - right_margin_x

    gallery_top_y = int(config.get("gallery_top_y", 250))
    main_photo_height = int(config.get("gallery_main_height", 350))
    thumb_height = int(config.get("gallery_thumb_height", 215))
    gallery_gap_y = int(config.get("gallery_gap_y", 22))
    gallery_top_gap_x = int(config.get("gallery_top_gap_x", 30))
    gallery_thumb_gap_x = int(config.get("gallery_thumb_gap_x", 22))

    top_photo_content_width = right_width - gallery_top_gap_x
    main_photo_ratio = float(config.get("gallery_main_photo_ratio", 545 / 1155))
    main_photo_width = int(round(top_photo_content_width * main_photo_ratio))
    map_width = top_photo_content_width - main_photo_width

    thumb_content_width = right_width - (gallery_thumb_gap_x * 2)
    thumb_ratio_1 = float(config.get("gallery_thumb_ratio_1", 370 / 1141))
    thumb_ratio_2 = float(config.get("gallery_thumb_ratio_2", 370 / 1141))
    thumb_1_width = int(round(thumb_content_width * thumb_ratio_1))
    thumb_2_width = int(round(thumb_content_width * thumb_ratio_2))
    thumb_3_width = thumb_content_width - thumb_1_width - thumb_2_width
    thumbs_y = gallery_top_y + main_photo_height + gallery_gap_y

    title_text = property_row.get("titulo", "Sin titulo")
    title_font = pick_font_for_single_line(draw, title_text, bold_font_path, range(68, 49, -2), 1440)
    subtitle_font = load_font(regular_font_path, 24)
    code_font = load_font(regular_font_path, 22)
    price_font = pick_font_for_single_line(draw, format_currency(property_row.get("moneda", "U$D"), property_row.get("precio", "")), bold_font_path, range(58, 45, -1), 520)
    badge_font = load_font(bold_font_path, 28)

    draw.text((left_x, header_top_y), title_text, fill=theme.text_color, font=title_font)
    draw.text(
        (left_x, header_top_y + header_subtitle_offset_y),
        property_row.get("ubicacion", ""),
        fill=theme.text_color,
        font=subtitle_font,
    )
    draw.text(
        (left_x, header_top_y + header_code_offset_y),
        f"COD: {property_row.get('codigo', '-')}",
        fill=theme.text_color,
        font=code_font,
    )

    price_text = format_currency(property_row.get("moneda", "U$D"), property_row.get("precio", ""))
    price_width = draw.textlength(price_text, font=price_font)
    draw.text((width - right_margin_x - price_width, price_y), price_text, fill=theme.accent_color, font=price_font)

    badge_text = f"{property_row.get('tipo_inmueble', '').upper()} EN {property_row.get('operacion', '').upper()}"
    badge_width = draw.textlength(badge_text, font=badge_font)
    draw.text((width - right_margin_x - badge_width, badge_y), badge_text, fill=theme.text_color, font=badge_font)

    

    description_line_y = draw_section_header(draw, "Descripción", left_x, section_start_y, left_width, theme, bold_font_path)
    description_body_y = description_line_y + 20
    _, description_height = draw_wrapped_text(
        draw=draw,
        text=property_row.get("descripcion", ""),
        box=(left_x, description_body_y, left_width, 260),
        font_path=regular_font_path,
        size_candidates=range(30, 22, -1),
        fill=theme.text_color,
        line_spacing=8,
        max_lines=int(config.get("description_max_lines", 6)),
    )
    description_bottom = description_body_y + description_height

    info_header_y = description_bottom + section_gap
    info_line_y = draw_section_header(draw, "Información básica", left_x, info_header_y, left_width, theme, bold_font_path)
    info_items = build_basic_info_items(property_row)
    info_bottom = draw_metadata_grid(
        draw=draw,
        items=info_items,
        start_x=left_x,
        start_y=info_line_y + 18,
        width=left_width,
        columns=3,
        theme=theme,
        regular_font_path=regular_font_path,
        bold_font_path=bold_font_path,
        row_height=48,
    )

    surfaces_header_y = info_bottom + subsection_gap
    surfaces_line_y = draw_section_header(draw, "Superficies", left_x, surfaces_header_y, left_width, theme, bold_font_path)
    surface_items = [
        ("Cubierta", format_area(property_row.get("cubierta_m2", ""))),
        ("Total", format_area(property_row.get("total_m2", ""))),
        ("Semicubierta", format_area(property_row.get("semicubierta_m2", ""))),
        ("Del terreno", format_area(property_row.get("terreno_m2", ""))),
    ]
    surfaces_bottom = draw_metadata_grid(
        draw=draw,
        items=surface_items,
        start_x=left_x,
        start_y=surfaces_line_y + 18,
        width=left_width,
        columns=2,
        theme=theme,
        regular_font_path=regular_font_path,
        bold_font_path=bold_font_path,
        row_height=48,
    )

    all_amenities = split_amenities(property_row.get("amenities", ""))
    _, filtered_amenities = extract_promoted_amenities(all_amenities)

    amenities_header_y = surfaces_bottom + subsection_gap
    amenities_line_y = draw_section_header(draw, "Ambientes y servicios", left_x, amenities_header_y, left_width, theme, bold_font_path)
    amenities_bottom = draw_amenities(
        draw=draw,
        amenities=filtered_amenities,
        box=(left_x, amenities_line_y + 18, left_width, 400),
        theme=theme,
        regular_font_path=regular_font_path,
    )

    draw_slot(
        canvas,
        (right_x, gallery_top_y, main_photo_width, main_photo_height),
        find_property_image(folder, "foto_principal"),
        "Foto principal",
        theme,
        regular_font_path,
    )
    map_slot = (right_x + main_photo_width + gallery_top_gap_x, gallery_top_y, map_width, main_photo_height)
    map_image_path = find_property_image(folder, "mapa")
    if map_image_path:
        draw_slot(canvas, map_slot, map_image_path, "Mapa", theme, regular_font_path)
    else:
        lat = as_float(property_row.get("lat", ""))
        lng = as_float(property_row.get("lng", ""))
        generated_map = None
        if lat is not None and lng is not None:
            generated_map = build_osm_map_image(lat, lng, (map_slot[2], map_slot[3]), regular_font_path)

        if generated_map is not None:
            canvas.paste(generated_map, (map_slot[0], map_slot[1]))
        else:
            draw_slot(canvas, map_slot, None, "Mapa", theme, regular_font_path)
    draw_slot(
        canvas,
        (right_x, thumbs_y, thumb_1_width, thumb_height),
        find_property_image(folder, "foto_1"),
        "Foto 1",
        theme,
        regular_font_path,
    )
    draw_slot(
        canvas,
        (right_x + thumb_1_width + gallery_thumb_gap_x, thumbs_y, thumb_2_width, thumb_height),
        find_property_image(folder, "foto_2"),
        "Foto 2",
        theme,
        regular_font_path,
    )
    draw_slot(
        canvas,
        (
            right_x + thumb_1_width + thumb_2_width + (gallery_thumb_gap_x * 2),
            thumbs_y,
            thumb_3_width,
            thumb_height,
        ),
        find_property_image(folder, "foto_3"),
        "Foto 3",
        theme,
        regular_font_path,
    )

    thumbs_bottom = thumbs_y + thumb_height
    footer_gap = int(config.get("footer_gap_from_gallery", 22))
    footer_y = thumbs_bottom + footer_gap
    footer_bottom = draw_brand_block(
        canvas,
        draw,
        config,
        theme,
        regular_font_path,
        bold_font_path,
        footer_y,
        right_x,
        right_width,
    )

    if config.get("auto_trim_bottom", True):
        bottom_padding = int(config.get("bottom_padding", 70))
        content_bottom = max(
            description_bottom,
            info_bottom,
            surfaces_bottom,
            amenities_bottom,
            map_slot[1] + map_slot[3],
            thumbs_bottom,
            footer_bottom,
        )
        min_trimmed_height = int(config.get("min_trimmed_height", 1100))
        target_height = min(height, max(content_bottom + bottom_padding, min_trimmed_height))
        canvas = canvas.crop((0, 0, width, target_height))

    return canvas


def build_caption(property_row: dict[str, str], config: dict) -> str:
    extra_lines: list[str] = []
    if has_value(property_row.get("expensas", "")):
        extra_lines.append(f"Expensas: {format_expenses(property_row.get('expensas', ''))}")
    if has_value(property_row.get("antiguedad", "")):
        extra_lines.append(f"Antigüedad: {property_row.get('antiguedad', '')}")

    key_parts: list[str] = []
    if has_value(property_row.get("dormitorios", "")):
        key_parts.append(f"{property_row.get('dormitorios', '')} dorm.")
    if has_value(property_row.get("banos", "")):
        key_parts.append(f"{property_row.get('banos', '')} baños")
    if has_value(property_row.get("toilettes", "")):
        key_parts.append(f"{property_row.get('toilettes', '')} toilettes")
    if has_value(property_row.get("cocheras", "")):
        key_parts.append(f"{property_row.get('cocheras', '')} coch.")
    if has_value(property_row.get("total_m2", "")):
        key_parts.append(f"{format_area(property_row.get('total_m2', ''))} totales")
    if not key_parts:
        key_parts = [
            f"{format_value(property_row.get('dormitorios', ''))} dorm.",
            f"{format_value(property_row.get('banos', ''))} baños",
            f"{format_value(property_row.get('cocheras', ''))} coch.",
            f"{format_area(property_row.get('total_m2', ''))} totales",
        ]

    highlights = [
        f"{property_row.get('tipo_inmueble', 'Propiedad')} en {property_row.get('operacion', 'Venta').lower()}",
        property_row.get("titulo", ""),
        property_row.get("ubicacion", ""),
        f"Precio: {format_currency(property_row.get('moneda', 'U$D'), property_row.get('precio', ''))}",
        *extra_lines,
        "Datos clave: " + " | ".join(key_parts),
        property_row.get("descripcion", ""),
        f"COD: {property_row.get('codigo', '-')}",
    ]

    custom_details = config.get("brand_detail_lines", [])
    if isinstance(custom_details, list):
        detail_lines = [str(detail).strip() for detail in custom_details if str(detail).strip()]
    else:
        detail_lines = []
    if not detail_lines:
        detail_lines = [
            config.get("agent_name", ""),
            config.get("agent_phone", ""),
            config.get("agent_email", ""),
            config.get("website", ""),
        ]
    contact_lines = [
        config.get("brand_name", ""),
        *detail_lines,
        property_row.get("url", ""),
        config.get("caption_hashtags", ""),
    ]
    return "\n".join(line for line in highlights + [""] + contact_lines if line.strip())


def export_outputs(
    card: Image.Image,
    property_row: dict[str, str],
    config: dict,
    output_dir: Path,
) -> None:
    slug = property_row["slug"]
    output_dir.mkdir(parents=True, exist_ok=True)

    png_path = output_dir / f"{slug}.png"
    jpg_path = output_dir / f"{slug}.jpg"
    caption_path = output_dir / f"{slug}.txt"

    card.save(png_path, dpi=(300, 300))
    card.save(jpg_path, quality=94, dpi=(300, 300), subsampling=0)
    caption_path.write_text(build_caption(property_row, config), encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.mudafy_url:
        row = import_listing(
            url=args.mudafy_url,
            config=config,
            properties_dir=args.properties_dir,
            slug_override=args.slug,
        )
        card = build_card(row, config, args.properties_dir)
        export_outputs(card, row, config, args.output_dir)
        print(f"Generada: {args.output_dir / (row['slug'] + '.png')}")
        return

    rows = load_rows(args.csv)

    filtered_rows = rows
    if args.slug:
        filtered_rows = [row for row in rows if row.get("slug") == args.slug]
        if not filtered_rows:
            raise SystemExit(f"No se encontró ninguna propiedad con slug `{args.slug}`.")

    for row in filtered_rows:
        missing_slug = not row.get("slug")
        if missing_slug:
            raise SystemExit("Cada fila del CSV debe tener un valor en la columna `slug`.")
        card = build_card(row, config, args.properties_dir)
        export_outputs(card, row, config, args.output_dir)
        print(f"Generada: {args.output_dir / (row['slug'] + '.png')}")


if __name__ == "__main__":
    main()
