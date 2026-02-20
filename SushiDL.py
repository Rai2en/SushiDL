# -*- coding: utf-8 -*-
"""
SushiDL - Application de téléchargement de mangas depuis SushiScan.fr/net
Fonctionnalités principales :
- Contournement de la protection Cloudflare via les cookies cf_clearance
- Authentification manuelle via cookies `.fr` / `.net` et User-Agent
- Téléchargement multi-thread des images
- Conversion automatique WebP vers JPG
- Archivage CBZ des chapitres
- Interface graphique intuitive avec suivi de progression
"""

import os
import re
import html
import json
import shutil
import threading
import time
import datetime
import queue
import sys
import unicodedata
import webbrowser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image, ImageTk
from curl_cffi import requests
from zipfile import ZipFile


def configure_console_io():
    """Configure la sortie console pour limiter les problèmes d'encodage."""
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


configure_console_io()


class DownloadCancelled(Exception):
    """Erreur levée lorsqu'une annulation utilisateur est demandée."""


class ImageDownloadError(Exception):
    """Erreur de téléchargement enrichie avec type et code HTTP."""

    def __init__(self, message, status_code=None, kind="retryable", phase="direct"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.phase = phase


def get_status_code_from_exception(exc):
    """Extrait un code HTTP depuis une exception réseau si disponible."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    return status_code


def classify_download_failure(status_code=None, message=""):
    """Classe les échecs de téléchargement pour piloter la stratégie de retry."""
    if status_code in (404, 410):
        return "missing"
    if status_code in (401, 403, 429, 500, 502, 503, 504):
        return "blocked_or_retryable"

    lower = (message or "").lower()
    if any(marker in lower for marker in ("cloudflare", "just a moment", "attention required", "captcha")):
        return "blocked_or_retryable"
    return "retryable"


def interruptible_sleep(cancel_event, duration):
    """Attend `duration` secondes, interrompu si annulation demandée."""
    if duration <= 0:
        return False
    if cancel_event is None:
        time.sleep(duration)
        return False
    return cancel_event.wait(duration)


def normalize_tome_label(label):
    """Normalise l'affichage des labels en remplaçant 'Volume' par 'Tome'."""
    cleaned = (label or "").strip()
    if not cleaned:
        return ""
    return re.sub(r"(?i)\bvolume\b", "Tome", cleaned)


def normalize_image_url(url):
    """Normalise les URLs d'images (https forcé, schéma manquant géré)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw.replace("http://", "https://")


def get_sushiscan_domain_from_host(host):
    """Retourne 'fr' ou 'net' pour un host SushiScan (racine ou sous-domaine)."""
    value = (host or "").strip().lower()
    if not value:
        return ""
    if value.endswith(".sushiscan.fr") or value == "sushiscan.fr":
        return "fr"
    if value.endswith(".sushiscan.net") or value == "sushiscan.net":
        return "net"
    return ""


def get_sushiscan_domain_from_url(url):
    """Retourne 'fr' ou 'net' depuis une URL SushiScan (racine ou sous-domaine)."""
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        host = ""
    return get_sushiscan_domain_from_host(host)


def robust_download_image(img_url, headers, max_try=4, delay=2, cancel_event=None):
    """
    Télécharge une image de manière robuste avec plusieurs tentatives.
    Contourne les protections Cloudflare et vérifie l'intégrité des images.
    
    Args:
        img_url (str): URL de l'image à télécharger
        headers (dict): En-têtes HTTP à utiliser
        max_try (int): Nombre maximum de tentatives
        delay (int): Délai initial entre les tentatives (augmente exponentiellement)
    
    Returns:
        bytes: Contenu brut de l'image
    
    Raises:
        Exception: Après échec de toutes les tentatives
    """
    last_exc = None
    for attempt in range(1, max_try + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        try:
            # Essaye d'abord avec curl_cffi.requests si dispo (bypass cloudflare)
            try:
                import curl_cffi.requests as cffi_requests
                r = cffi_requests.get(
                    img_url,
                    headers=headers,
                    impersonate="chrome",
                    timeout=20
                )
                status_code = getattr(r, "status_code", None)
                if status_code and status_code >= 400:
                    kind = classify_download_failure(status_code, f"HTTP Error {status_code}")
                    raise ImageDownloadError(
                        f"HTTP Error {status_code}",
                        status_code=status_code,
                        kind=kind,
                        phase="direct",
                    )
                r.raise_for_status()
                raw = r.content
            except ImportError:
                import requests as cffi_requests
                r = cffi_requests.get(
                    img_url,
                    headers=headers,
                    timeout=20
                )
                status_code = getattr(r, "status_code", None)
                if status_code and status_code >= 400:
                    kind = classify_download_failure(status_code, f"HTTP Error {status_code}")
                    raise ImageDownloadError(
                        f"HTTP Error {status_code}",
                        status_code=status_code,
                        kind=kind,
                        phase="direct",
                    )
                r.raise_for_status()
                raw = r.content

            # Détection HTML (Cloudflare/captcha au lieu d'une image)
            if raw[:6] == b'<html>' or b'<html' in raw[:1024].lower():
                raise ImageDownloadError(
                    "Réponse HTML (protection serveur ou Cloudflare)",
                    kind="blocked_or_retryable",
                    phase="direct",
                )

            # Vérifie si c'est bien une image (fail si corrompue/invalide)
            try:
                Image.open(BytesIO(raw))
            except Exception as test_e:
                runtime_log(
                    f"Tentative {attempt}: contenu non reconnu comme image: {test_e}",
                    level="warning",
                    context={"action": "image_integrity"},
                )
                last_exc = ImageDownloadError(
                    f"Contenu non image: {test_e}",
                    kind="retryable",
                    phase="direct",
                )
                if interruptible_sleep(cancel_event, delay * attempt):
                    raise DownloadCancelled("Téléchargement annulé.")
                continue

            # Succès - retourne les données brutes de l'image
            return raw

        except DownloadCancelled:
            raise
        except ImageDownloadError as e:
            runtime_log(
                f"Tentative {attempt} échouée pour {img_url}: {e}",
                level="warning",
                context={"action": "image_retry"},
            )
            last_exc = e
            if e.kind == "missing":
                raise e
            sleep_time = min(delay * (2 ** attempt), 60) if e.status_code in (403, 429) else (delay * attempt)
            if interruptible_sleep(cancel_event, sleep_time):
                raise DownloadCancelled("Téléchargement annulé.")
        except Exception as e:
            runtime_log(
                f"Tentative {attempt} échouée pour {img_url}: {e}",
                level="warning",
                context={"action": "image_retry"},
            )
            status_code = get_status_code_from_exception(e)
            kind = classify_download_failure(status_code, str(e))
            wrapped = ImageDownloadError(
                str(e),
                status_code=status_code,
                kind=kind,
                phase="direct",
            )
            last_exc = wrapped
            if kind == "missing":
                raise wrapped

            # Backoff exponentiel pour les erreurs 403/429
            if status_code in (403, 429):
                sleep_time = min(delay * (2 ** attempt), 60)  # Max 60 secondes
                if interruptible_sleep(cancel_event, sleep_time):
                    raise DownloadCancelled("Téléchargement annulé.")
            else:
                if interruptible_sleep(cancel_event, delay * attempt):
                    raise DownloadCancelled("Téléchargement annulé.")
    if isinstance(last_exc, Exception):
        raise last_exc
    raise ImageDownloadError(
        f"Impossible de télécharger l'image {img_url} après {max_try} tentatives.",
        kind="retryable",
        phase="direct",
    )


# Expressions régulières et constantes globales
APP_NAME = "SushiDL"
APP_VERSION = "11.1.4"
REGEX_URL = r"^https://sushiscan\.(fr|net)/catalogue/[a-z0-9-]+/$"  # Format des URLs valides
ROOT_FOLDER = "DL SushiScan"  # Dossier racine pour les téléchargements
THREADS = 3  # Nombre de threads pour le téléchargement parallèle
BASE_DIR = Path(__file__).resolve().parent
COOKIE_CACHE_PATH = BASE_DIR / "cookie_cache.json"  # Fichier de cache pour les cookies
CONFIG_PATH = BASE_DIR / "config.json"  # Configuration globale de l'application
COOKIE_MAX_AGE_SECONDS = 3600  # Durée indicative de validité cookie (Cloudflare évolue)
COOKIE_REVIEW_AGE_SECONDS = 86400  # Au-delà: statut "A contrôler"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
DIRECT_USER_AGENT_DEFAULT = DEFAULT_USER_AGENT
DEFAULT_APP_CONFIG = {
    "auth_mode": "manual",
    "manual_links": {
        "cookie_fr": "https://sushiscan.fr",
        "cookie_net": "https://sushiscan.net",
        "user_agent": "https://httpbin.org/user-agent",
        "cookie_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
        "cloudflare_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
    },
}
CF_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "__cf_chl",
    "challenge-platform",
    "attention required",
)
LOG_LEVELS = ("debug", "info", "success", "warning", "error", "cbz")
LOG_EMOJIS = {
    "debug": "🔎",
    "info": "💬",
    "success": "✅",
    "warning": "⚠️",
    "error": "🔴",
    "cbz": "📦",
}
LOG_ANSI_COLORS = {
    "debug": "\033[90m",
    "info": "\033[36m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "cbz": "\033[35m",
}
ANSI_RESET = "\033[0m"
CONSOLE_USE_EMOJI = False
GUI_USE_EMOJI = False


def _merge_config(default_cfg, user_cfg):
    """Fusionne user_cfg dans default_cfg sans perdre les clés par défaut."""
    if not isinstance(default_cfg, dict):
        return user_cfg
    merged = {}
    safe_user = user_cfg if isinstance(user_cfg, dict) else {}
    for key, value in default_cfg.items():
        if isinstance(value, dict):
            merged[key] = _merge_config(value, safe_user.get(key, {}))
        elif isinstance(value, list):
            user_value = safe_user.get(key, value)
            merged[key] = user_value if isinstance(user_value, list) else list(value)
        else:
            merged[key] = safe_user.get(key, value)
    for key, value in safe_user.items():
        if key not in merged:
            merged[key] = value
    return merged


def _write_json_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_app_config():
    """Charge config.json et applique les valeurs par défaut manquantes."""
    if not CONFIG_PATH.exists():
        cfg = dict(DEFAULT_APP_CONFIG)
        _write_json_file(CONFIG_PATH, cfg)
        return cfg
    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        merged = _merge_config(DEFAULT_APP_CONFIG, raw)
        if merged != raw:
            _write_json_file(CONFIG_PATH, merged)
        return merged
    except Exception as exc:
        try:
            print(f"[WARN] Erreur lecture config.json ({exc}), valeurs par défaut utilisées.")
        except Exception:
            pass
        return dict(DEFAULT_APP_CONFIG)


APP_CONFIG = load_app_config()


def get_manual_link(config_key, default_value):
    """Retourne un lien manuel depuis config.json (ou valeur par defaut)."""
    links = APP_CONFIG.get("manual_links", {}) if isinstance(APP_CONFIG, dict) else {}
    if not isinstance(links, dict):
        return default_value
    raw = (links.get(config_key) or "").strip()
    return raw or default_value


def strip_console_unsafe_chars(text):
    """Retire certains symboles non ASCII (notamment emojis) en console Windows."""
    if os.name != "nt":
        return text
    # Supprime les emojis puis translittère en ASCII pour éviter tout mojibake.
    value = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\ufe0f]", "", text or "")
    value = unicodedata.normalize("NFKD", value)
    return value.encode("ascii", errors="ignore").decode("ascii", errors="ignore")


def normalize_log_level(level):
    """Normalise un niveau de log supporté."""
    candidate = (level or "info").strip().lower()
    return candidate if candidate in LOG_LEVELS else "info"


def format_log_context(context):
    """Formate un contexte de log lisible et stable."""
    if not context:
        return ""
    if isinstance(context, str):
        value = context.strip()
        return f" [{value}]" if value else ""
    if isinstance(context, dict):
        ordered_keys = ("domain", "tome", "action")
        parts = []
        for key in ordered_keys:
            value = str(context.get(key, "")).strip()
            if value:
                parts.append(f"{key}={value}")
        for key, value in context.items():
            if key in ordered_keys:
                continue
            value_txt = str(value).strip()
            if value_txt:
                parts.append(f"{key}={value_txt}")
        if parts:
            return " [" + " | ".join(parts) + "]"
    return ""


def console_supports_color():
    """Retourne True si la console semble supporter ANSI."""
    if os.getenv("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def format_console_line(message, level="info", context=None, timestamp=None, with_emoji=True):
    """Construit une ligne de log homogène pour la console."""
    lvl = normalize_log_level(level)
    ts = timestamp or time.strftime("%H:%M:%S")
    emoji = (LOG_EMOJIS.get(lvl, "") + " ") if with_emoji else ""
    safe_message = strip_console_unsafe_chars(message)
    ctx = format_log_context(context)
    return f"[{ts}] {emoji}{safe_message}{ctx}"


def emit_console_log(message, level="info", context=None, timestamp=None, with_emoji=None):
    """Écrit un log homogène en console, avec couleur si possible."""
    if with_emoji is None:
        with_emoji = CONSOLE_USE_EMOJI
    line = format_console_line(
        message=message,
        level=level,
        context=context,
        timestamp=timestamp,
        with_emoji=with_emoji,
    )
    lvl = normalize_log_level(level)
    if console_supports_color():
        color = LOG_ANSI_COLORS.get(lvl, "")
        if color:
            print(f"{color}{line}{ANSI_RESET}")
            return
    print(line)


def runtime_log(message, level="info", context=None):
    """
    Route un message vers le logger GUI quand disponible,
    sinon vers la console uniquement.
    """
    text = str(message or "").strip()
    if not text:
        return

    app_cls = globals().get("MangaApp")
    app = getattr(app_cls, "current_instance", None) if app_cls is not None else None
    if app is not None and hasattr(app, "log"):
        app.log(text, level=level, context=context)
        return
    emit_console_log(text, level=level, context=context)


def is_cloudflare_challenge_page(content):
    """Détecte une page de challenge Cloudflare."""
    text = (content or "").lower()
    if not text:
        return True
    return any(marker in text for marker in CF_CHALLENGE_MARKERS)


def strip_html_tags(text):
    """Supprime les balises HTML d'une chaîne."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


# --- Fonctions utilitaires ---
def sanitize_folder_name(name):
    """Nettoie les noms de dossier en supprimant les caractères invalids"""
    return re.sub(r'[<>:"/\\|?*\n\r]', "_", name).strip()


def make_request(url, cookie, ua):
    """Effectue une requête HTTP avec les cookies et l'user-agent appropriés"""
    headers = {
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": ua or DEFAULT_USER_AGENT,
        "Connection": "close",  # Important pour éviter les fuites de mémoire
    }
    domain = get_sushiscan_domain_from_url(url)
    if domain in ("fr", "net"):
        headers["Referer"] = f"https://sushiscan.{domain}/"

    cookie_header = ""
    app = getattr(MangaApp, "current_instance", None)
    if app and hasattr(app, "get_cookie_header_for_url"):
        try:
            cookie_header = app.get_cookie_header_for_url(url, fallback_cookie=cookie)
        except Exception:
            cookie_header = ""

    if not cookie_header and cookie:
        cookie_header = f"cf_clearance={cookie}"
    if cookie_header:
        headers["Cookie"] = cookie_header
    return requests.get(url, headers=headers, impersonate="chrome", timeout=10)


def detect_local_user_agent():
    """
    Tente de générer un User-Agent local cohérent avec le navigateur principal.
    Retourne (ua, source).
    """
    # Base stable pour Chrome/Edge sur Windows.
    if os.name == "nt":
        browser_keys = [
            ("chrome", r"SOFTWARE\Google\Chrome\BLBeacon"),
            ("edge", r"SOFTWARE\Microsoft\Edge\BLBeacon"),
        ]
        version = ""
        source = "fallback"
        try:
            import winreg

            for browser_name, reg_path in browser_keys:
                for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(root, reg_path) as key:
                            raw_version, _ = winreg.QueryValueEx(key, "version")
                            clean = str(raw_version or "").strip()
                            if clean:
                                version = clean
                                source = f"registre:{browser_name}"
                                break
                    except Exception:
                        continue
                if version:
                    break
        except Exception:
            pass

        if not version:
            version = "127.0.0.0"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )
        return ua, source

    # Fallback non Windows
    return DEFAULT_USER_AGENT, "fallback"


def parse_lr(text, left, right, recursive, unescape=True):
    """
    Parse le texte entre deux délimiteurs (left et right)
    
    Args:
        text (str): Texte à parser
        left (str): Délimiteur gauche
        right (str): Délimiteur droit
        recursive (bool): Récupère toutes les occurrences si True
        unescape (bool): Décode les entités HTML si True
    
    Returns:
        str/list: Résultat du parsing selon le mode
    """
    pattern = re.escape(left) + "(.*?)" + re.escape(right)
    matches = re.findall(pattern, text)
    if unescape:
        matches = [html.unescape(match) for match in matches]
    return matches if recursive else matches[0] if matches else None


def test_cookie_validity(domain, cookie, ua, probe_url=None):
    """
    Vérifie si un cookie cf_clearance est encore valide
    
    Args:
        domain (str): Domaine à tester (.fr ou .net)
        cookie (str): Valeur du cookie cf_clearance
        ua (str): User-Agent à utiliser
    
    Returns:
        bool: True si le cookie est valide, False sinon
    """
    if not cookie:
        return False
    status = evaluate_cookie_and_challenge(domain, cookie, ua, probe_url=probe_url)
    return bool(status.get("cookie_valid", False))


def evaluate_cookie_and_challenge(domain, cookie, ua, probe_url=None):
    """
    Evalue l'etat cookie + challenge Cloudflare.
    Retourne:
      - cookie_valid: bool
      - challenge_state: "present" | "absent" | "unknown"
      - http_status: int|None
    """
    result = {"cookie_valid": False, "challenge_state": "unknown", "http_status": None}
    if domain not in ("fr", "net"):
        return result

    test_url = probe_url or f"https://sushiscan.{domain}/"
    expected_host = f"sushiscan.{domain}"
    if expected_host not in test_url:
        test_url = f"https://{expected_host}/"

    try:
        r = make_request(test_url, cookie or "", ua)
        status_code = int(getattr(r, "status_code", 0) or 0)
        result["http_status"] = status_code or None
        text = (getattr(r, "text", "") or "").lower()

        has_content_markers = any(
            marker in text
            for marker in (
                "sushiscan",
                "entry-title",
                "wp-manga",
                "chapternum",
                "readerarea",
                "ts_reader.run",
            )
        )
        challenge_blocking = is_cloudflare_challenge_page(text) and not has_content_markers

        if status_code == 200 and not challenge_blocking:
            result["challenge_state"] = "absent"
            result["cookie_valid"] = bool((cookie or "").strip())
            return result

        if challenge_blocking or status_code in (401, 403, 429, 503):
            result["challenge_state"] = "present"
        else:
            result["challenge_state"] = "unknown"
        result["cookie_valid"] = False
        return result
    except Exception:
        return result


def interpret_curl_error(message):
    """Traduit les erreurs cURL en messages compréhensibles"""
    if "curl: (6)" in message:
        return "Nom d'hôte introuvable (DNS)."
    elif "curl: (7)" in message:
        return "Connexion refusée ou impossible (serveur hors ligne ?)."
    elif "curl: (28)" in message:
        return "Délai d'attente dépassé (timeout réseau)."
    elif "curl: (35)" in message:
        return "Erreur SSL/TLS lors de la connexion sécurisée."
    elif "curl: (56)" in message:
        return "Connexion interrompue (réponse incomplète ou terminée prématurément)."
    else:
        return None


def archive_cbz(folder_path, title, volume):
    """
    Crée une archive CBZ à partir d'un dossier d'images
    
    Args:
        folder_path (str): Chemin du dossier contenant les images
        title (str): Titre du manga
        volume (str): Libellé tome/chapitre
    
    Returns:
        bool: True si l'archivage a réussi, False sinon
    """
    clean_title = sanitize_folder_name(title)
    clean_volume = sanitize_folder_name(normalize_tome_label(volume))
    parent_dir = os.path.dirname(folder_path)
    cbz_name = os.path.join(parent_dir, f"{clean_title} - {clean_volume}.cbz")
    
    # Création de l'archive ZIP
    with ZipFile(cbz_name, "w") as cbz:
        for root, _, files in os.walk(folder_path):
            for file in sorted(files):  # Tri alphabétique pour l'ordre des pages
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, folder_path)
                cbz.write(full_path, arcname)
    
    # Vérification de l'intégrité de l'archive
    try:
        with ZipFile(cbz_name, "r") as test_zip:
            test_zip.testzip()
    except Exception:
        return False
    
    # Suppression du dossier original si l'archive est valide
    if os.path.exists(cbz_name) and os.path.getsize(cbz_name) > 10000:
        shutil.rmtree(folder_path)
        return True
    return False


def download_image(
    url, folder, cookie, ua, i, number_len, cancel_event, failed_downloads,
    progress_callback=None, referer_url=None, webp2jpg_enabled=False
):
    """
    Télécharge une image unique avec gestion d'erreurs et conversion optionnelle
    
    Args:
        url (str): URL de l'image
        folder (str): Dossier de destination
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
        i (int): Index de l'image (pour le nom de fichier)
        number_len (int): Longueur du padding numérique (ex: 003.jpg)
        cancel_event (threading.Event): Événement d'annulation
        failed_downloads (list): Liste des échecs à remplir
        progress_callback (func): Callback de progression
        referer_url (str): URL Referer à utiliser
        webp2jpg_enabled (bool): Activer la conversion WebP->JPG
    """
    import os

    normalized_url = normalize_image_url(url)

    def register_failure(kind, reason, status_code=None):
        failed_downloads.append(
            {
                "url": normalized_url,
                "kind": kind,
                "status_code": status_code,
                "reason": str(reason),
            }
        )

    if cancel_event.is_set():
        register_failure("cancelled", "Annulation demandée avant téléchargement.")
        return

    # Configuration des en-têtes HTTP
    image_domain = get_sushiscan_domain_from_url(normalized_url)
    referer = referer_url or (f"https://sushiscan.{image_domain}/" if image_domain in ("fr", "net") else "https://sushiscan.net/")
    app = getattr(MangaApp, "current_instance", None)
    cookie_header = ""
    if app and hasattr(app, "get_cookie_header_for_url"):
        try:
            cookie_header = app.get_cookie_header_for_url(normalized_url, fallback_cookie=cookie)
        except Exception:
            cookie_header = ""
    if not cookie_header and cookie:
        cookie_header = f"cf_clearance={cookie}"

    headers = {
        "Accept": "image/webp,image/jpeg,image/png,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": ua,
        "Referer": referer,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    # Détermination de l'extension et du nom de fichier
    parsed_path = (urlparse(normalized_url).path or "").lower()
    ext = parsed_path.rsplit(".", 1)[-1] if "." in parsed_path else "jpg"
    if ext not in {"jpg", "jpeg", "png", "webp", "avif"}:
        ext = "jpg"
    filename = os.path.join(folder, f"{str(i + 1).zfill(number_len)}.{ext}")

    # Téléchargement direct prioritaire
    try:
        raw = robust_download_image(normalized_url, headers, cancel_event=cancel_event)
        with open(filename, "wb") as f:
            f.write(raw)

        # Conversion WebP vers JPG si activée
        if webp2jpg_enabled and filename.lower().endswith(".webp"):
            try:
                img = Image.open(filename).convert("RGB")
                new_path = filename[:-5] + ".jpg"
                img.save(new_path, "JPEG", quality=90)
                os.remove(filename)
                filename = new_path
            except Exception as conv_e:
                runtime_log(f"Erreur conversion WebP->JPG: {conv_e}", level="warning", context={"action": "webp2jpg"})

        # Mise à jour de la progression
        if progress_callback:
            progress_callback(i + 1)
        if hasattr(MangaApp, "current_instance") and hasattr(MangaApp.current_instance, "log"):
            MangaApp.current_instance.log(f"Image {i + 1} téléchargee : {os.path.basename(filename)}", level="info")
        return

    except DownloadCancelled:
        register_failure("cancelled", "Annulation demandée pendant téléchargement direct.")
        return
    except ImageDownloadError as e:
        if e.kind == "missing":
            register_failure("missing", str(e), status_code=e.status_code)
            runtime_log(
                f"Image absente côté serveur (HTTP {e.status_code}): {normalized_url}",
                level="info",
                context={"action": "download", "url": normalized_url},
            )
            return
        register_failure(e.kind, str(e), status_code=e.status_code)
        runtime_log(
            f"Echec direct après retries: {e}",
            level="warning",
            context={"action": "download", "url": normalized_url},
        )
        return
    except Exception as e:
        status_code = get_status_code_from_exception(e)
        kind = classify_download_failure(status_code, str(e))
        register_failure(kind, str(e), status_code=status_code)
        runtime_log(
            f"Echec direct après retries: {e}",
            level="warning",
            context={"action": "download", "url": normalized_url},
        )
        return


def parse_manga_data_from_html(url, html_content):
    """
    Parse le HTML du catalogue et retourne (title, pairs).
    """
    html_content = html_content or ""

    soup = BeautifulSoup(html_content, "html.parser")

    # Extraction du titre (plus robuste entre .fr / .net)
    title = ""
    title_tag = soup.select_one("h1.entry-title")
    if title_tag:
        title = title_tag.get_text(" ", strip=True)
    if not title:
        parsed_title = parse_lr(
            html_content, '<h1 class="entry-title" itemprop="name">', "</h1>", False
        )
        title = html.unescape(parsed_title) if parsed_title else ""
    if not title:
        title = url.rstrip("/").split("/")[-1].replace("-", " ").strip() or "Sans titre"

    expected_domain = get_sushiscan_domain_from_url(url) or ("net" if "sushiscan.net" in url else "fr")
    expected_host = f"sushiscan.{expected_domain}"
    base_url = f"https://{expected_host}/"
    pairs = []

    # 1) Structure classique avec span.chapternum
    matches = re.findall(
        r'<a href="([^"]+)">\s*<span class="chapternum">(.*?)</span>',
        html_content,
        re.IGNORECASE | re.DOTALL,
    )
    for href, label in matches:
        full_link = urljoin(base_url, href.strip())
        parsed = urlparse(full_link)
        if get_sushiscan_domain_from_host(parsed.hostname) != expected_domain:
            continue
        clean_label = normalize_tome_label(strip_html_tags(html.unescape(label)))
        if clean_label:
            pairs.append((clean_label, full_link))

    # 2) Fallback sur liste de chapitres
    if not pairs:
        for a in soup.select("li.wp-manga-chapter a[href], .listing-chapters_wrap a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            full_link = urljoin(base_url, href)
            parsed = urlparse(full_link)
            if get_sushiscan_domain_from_host(parsed.hostname) != expected_domain:
                continue
            label = normalize_tome_label(a.get_text(" ", strip=True))
            if label:
                pairs.append((label, full_link))

    # Élimination des doublons
    seen = set()
    unique_pairs = []
    for label, link in pairs:
        if link in seen:
            continue
        seen.add(link)
        unique_pairs.append((label, link))

    if not unique_pairs:
        raise Exception("Aucun tome/chapitre détecté (page protégée ou structure modifiée).")

    unique_pairs.reverse()  # Pour afficher dans l'ordre croissant
    runtime_log(
        f"{len(unique_pairs)} tomes/chapitres détectés",
        level="info",
        context={"action": "parse_catalogue"},
    )
    return title, unique_pairs


def fetch_manga_data(url, cookie, ua):
    """
    Récupère les données d'un manga : titre et liste des tomes/chapitres
    
    Args:
        url (str): URL de la page catalogue du manga
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
    
    Returns:
        tuple: (titre, liste de tuples (label, url))
    """
    r = make_request(url, cookie, ua)
    if r.status_code != 200:
        final_url = getattr(r, "url", "") or ""
        detail = f"HTTP {r.status_code}"
        if final_url and final_url != url:
            detail += f" -> {final_url}"
        if int(getattr(r, "status_code", 0) or 0) == 403:
            detail += " | Vérifie le User-Agent"
        raise Exception(f"Accès refusé ou URL invalide ({detail})")

    return parse_manga_data_from_html(url, r.text or "")


def get_images(link, cookie, ua, retries=2, delay=5, debug_mode=True):
    """
    Récupère la liste des URLs d'images pour un volume/chapitre
    
    Args:
        link (str): URL de la page du volume
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
        retries (int): Tentatives de récupération
        delay (int): Délai entre les tentatives
        debug_mode (bool): Activer le mode debug
    
    Returns:
        list: Liste des URLs d'images
    """
    def clean_parasites(images, domain):
        """Filtre les images parasites (logos, pubs) pour sushiscan.fr"""
        if domain != "fr":
            return images

        PARASITE_KEYWORDS = ["ads", "sponsor", "banner", "footer", "cover", "logo", "pub"]
        filtered = []
        for img in images:
            if any(keyword in img.lower() for keyword in PARASITE_KEYWORDS):
                continue
            if "sushiscan.fr/wp-content/uploads/" in img:
                continue
            filtered.append(img)

        removed = len(images) - len(filtered)
        if removed > 0:
            runtime_log(
                f"{removed} image(s) parasite(s) supprimée(s) dynamiquement.",
                level="debug",
                context={"action": "image_filter", "domain": domain},
            )
        return filtered

    def extract_images(r_text, domain):
        """Extrait les URLs d'images depuis le contenu HTML"""
        # Étape 1 — Extraction depuis le JSON ts_reader.run
        json_str = parse_lr(r_text, "ts_reader.run(", ");</script>", False)
        if json_str:
            try:
                data = json.loads(json_str)
                images = [
                    normalize_image_url(img)
                    for img in data["sources"][0]["images"]
                ]
                if images:
                    runtime_log(
                        f"{len(images)} images détectées via ts_reader.run.",
                        level="info",
                        context={"action": "extract_images", "domain": domain},
                    )
                    images = clean_parasites(images, domain)
                    runtime_log(
                        f"{len(images)} images finales après filtrage.",
                        level="info",
                        context={"action": "extract_images", "domain": domain},
                    )
                    return images
            except Exception as e:
                runtime_log(f"Erreur parsing JSON images: {e}", level="warning", context={"action": "extract_images"})

        # Étape 2 — Fallback : balises img dans #readerarea
        soup = BeautifulSoup(r_text, "html.parser")

        # Supprimer les divs inutiles pour .fr
        if domain == "fr":
            for div in soup.find_all("div", class_="bixbox"):
                div.decompose()

        reader = soup.find("div", id="readerarea")
        if reader:
            images = []
            for img in reader.find_all("img"):
                src = img.get("data-src") or img.get("src")
                if not src:
                    continue
                if src.startswith("data:"):
                    continue
                if src.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
                    normalized_src = normalize_image_url(src)
                    if normalized_src:
                        images.append(normalized_src)
            if images:
                images = clean_parasites(images, domain)
                runtime_log(
                    f"{len(images)} images finales après filtrage.",
                    level="info",
                    context={"action": "extract_images", "domain": domain},
                )
                return images

        # Étape 3 — Fallback regex brut
        img_urls = re.findall(
            r'<img[^>]+(?:src|data-src)=["\'](https://[^"\'>]+\.(?:webp|jpg|jpeg|jpe|png|avif))["\']',
            r_text,
            re.IGNORECASE,
        )
        img_urls = [normalize_image_url(url) for url in img_urls if not url.startswith("data:")]
        img_urls = list(dict.fromkeys(img_urls))  # Supprime les doublons
        if img_urls:
            img_urls = clean_parasites(img_urls, domain)
            runtime_log(
                f"{len(img_urls)} images finales après filtrage.",
                level="info",
                context={"action": "extract_images", "domain": domain},
            )
        return img_urls

    # --- Phase 1 : tentative directe sans FlareSolverr ---
    try:
        time.sleep(1)
        r = make_request(link, cookie, ua)
        runtime_log(
            f"Requête HTTP directe reçue (len={len(r.text)}).",
            level="debug",
            context={"action": "get_images"},
        )
        domain = get_sushiscan_domain_from_url(link) or ("fr" if "sushiscan.fr" in link else "net")

        # Sauvegarde debug si activé
        if debug_mode:
            debug_file = f"debug_sushiscan_{domain}.log"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(r.text)
            runtime_log(f"Fichier debug généré: {debug_file}", level="debug", context={"action": "debug_dump"})

        images = extract_images(r.text, domain)
        if images:
            return images
        else:
            runtime_log(
                "Aucune image trouvée en accès direct.",
                level="warning",
                context={"action": "get_images"},
            )
    except Exception as e:
        message = str(e)
        interpretation = interpret_curl_error(message)
        if interpretation:
            runtime_log(
                interpretation,
                level="warning",
                context={"action": "get_images"},
            )
        else:
            runtime_log(
                f"Erreur directe: {message}.",
                level="warning",
                context={"action": "get_images"},
            )

    runtime_log(f"Impossible d'extraire des images depuis: {link}", level="error", context={"action": "get_images"})
    return []


def download_volume(
    volume,
    images,
    title,
    cookie,
    ua,
    logger,
    cancel_event,
    cbz_enabled=True,
    update_progress=None,
    webp2jpg_enabled=True,
    referer_url=None,
):
    """
    Télécharge un volume complet avec gestion de progression et archivage.

    Returns:
        bool|None:
            True  -> téléchargement OK (y compris pages manquantes 404/410)
            False -> échec bloquant (blocage/réseau/CBZ)
            None  -> annulation demandée
    """
    if cancel_event.is_set():
        return None

    tome_label = normalize_tome_label(volume)

    # Préparation des chemins
    clean_title = sanitize_folder_name(title)
    clean_tome = sanitize_folder_name(tome_label)
    folder = os.path.join(ROOT_FOLDER, clean_title, clean_tome)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        logger(f"Erreur création dossier: {str(e)}", level="error")
        return False

    # Calcul du padding pour les noms de fichiers
    number_len = max(1, len(str(len(images))))
    failed_downloads = []

    # Téléchargement parallèle avec ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        progress_counter = {"done": 0}
        lock = threading.Lock()

        for i, url in enumerate(images):
            if cancel_event.wait(0.1):
                break

            def progress_callback(_idx):
                """Callback de progression thread-safe."""
                with lock:
                    progress_counter["done"] += 1
                    if update_progress:
                        update_progress(progress_counter["done"], len(images))

            futures.append(
                executor.submit(
                    download_image,
                    url,
                    folder,
                    cookie,
                    ua,
                    i,
                    number_len,
                    cancel_event,
                    failed_downloads,
                    progress_callback=progress_callback,
                    referer_url=referer_url,
                    webp2jpg_enabled=webp2jpg_enabled,
                )
            )

        # Attente de la complétion des threads
        for future in as_completed(futures):
            if cancel_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                future.result()
            except Exception as thread_e:
                failed_downloads.append(
                    {
                        "url": "",
                        "kind": "retryable",
                        "status_code": None,
                        "reason": f"Exception thread: {thread_e}",
                    }
                )

    if cancel_event.is_set():
        logger(f"Téléchargement annulé pour {tome_label}.", level="warning")
        return None

    normalized_failures = []
    for fail in failed_downloads:
        if isinstance(fail, dict):
            normalized_failures.append(
                {
                    "url": (fail.get("url") or "").strip(),
                    "kind": (fail.get("kind") or "retryable").strip(),
                    "status_code": fail.get("status_code"),
                    "reason": (fail.get("reason") or "Échec inconnu").strip(),
                }
            )
        else:
            normalized_failures.append(
                {
                    "url": str(fail).strip(),
                    "kind": "retryable",
                    "status_code": None,
                    "reason": "Échec non typé",
                }
            )

    missing_failures = [f for f in normalized_failures if f["kind"] == "missing"]
    hard_failures = [f for f in normalized_failures if f["kind"] not in ("missing", "cancelled")]

    if missing_failures:
        sample_missing = missing_failures[0].get("url") or "URL inconnue"
        logger(
            f"{len(missing_failures)} page(s) absente(s) (404/410) sur {tome_label}. Exemple: {sample_missing}",
            level="warning",
        )
        logger("ℹ️ CBZ maintenu: les pages manquantes sont ignorées, sans relance forcée.", level="info")

    # Échecs bloquants: retry cookie puis proposition manuelle
    if hard_failures:
        sample_hard = hard_failures[0]
        sample_reason = sample_hard.get("reason") or "cause inconnue"
        logger(
            f"{len(hard_failures)} image(s) bloquee(s)/non telechargeable(s) sur {tome_label}. Exemple: {sample_reason}",
            level="warning",
        )
        if cancel_event.is_set():
            return None

        if cancel_event.is_set():
            return None

        try:
            app = getattr(MangaApp, "current_instance", None)
            if app and hasattr(app, "ask_yes_no"):
                res = app.ask_yes_no(
                    "Erreur de téléchargement",
                    "Des images ont échoué. Voulez-vous modifier le cookie et relancer le téléchargement complet de ce tome ?",
                )
            else:
                res = messagebox.askyesno(
                    "Erreur de téléchargement",
                    "Des images ont échoué. Voulez-vous modifier le cookie et relancer le téléchargement complet de ce tome ?",
                )
            if cancel_event.is_set():
                return None
            if res:
                if app and hasattr(app, "ask_string"):
                    new_cookie = app.ask_string(
                        "Nouveau cookie",
                        "Entrez le nouveau cookie cf_clearance :",
                    )
                else:
                    import tkinter.simpledialog as simpledialog
                    new_cookie = simpledialog.askstring(
                        "Nouveau cookie",
                        "Entrez le nouveau cookie cf_clearance :",
                    )
                if cancel_event.is_set():
                    return None
                if new_cookie:
                    shutil.rmtree(folder, ignore_errors=True)
                    logger("Ancien dossier supprime. Relancement du telechargement avec le nouveau cookie...", level="info")
                    return download_volume(
                        volume,
                        images,
                        title,
                        new_cookie,
                        ua,
                        logger,
                        cancel_event,
                        cbz_enabled,
                        update_progress,
                        webp2jpg_enabled,
                        referer_url,
                    )
                logger("Aucun cookie saisi. Le tome ne sera pas complete.", level="error")
        except Exception as e:
            logger(f"Erreur durant la relance : {e}", level="error")
        return False

    # Archivage CBZ si suffisamment d'images présentes
    if cancel_event.is_set():
        return None
    if not os.path.exists(folder):
        return False

    file_count = sum(len(files) for _, _, files in os.walk(folder))
    if file_count == 0:
        logger(f"Aucune image telechargee pour {tome_label}.", level="error")
        return False

    if cbz_enabled:
        if archive_cbz(folder, title, tome_label):
            cbz_path = os.path.join(
                ROOT_FOLDER, clean_title, f"{clean_title} - {clean_tome}.cbz"
            )
            size_mb = round(os.path.getsize(cbz_path) / (1024 * 1024), 2)
            logger("", level="info")  # ligne vide
            logger(f"CBZ créé : {cbz_path} ({size_mb} MB)", level="cbz")
            return True
        logger(f"Échec de création CBZ pour {clean_tome}", level="warning")
        return False

    logger(f"CBZ non créé pour {clean_tome} (option décochée)", level="info")
    return True

def save_cookie_cache(
    cookies_dict,
    ua,
    cbz,
    webp2jpg_enabled,
    verbose_logs=True,
    cookie_sources=None,
    cookie_user_agents=None,
    cookie_headers=None,
):
    """
    Sauvegarde les paramètres dans un fichier JSON
    
    Args:
        cookies_dict (dict): Cookies par domaine
        ua (str): User-Agent
        cbz (bool): Préférence CBZ
        webp2jpg_enabled (bool): Préférence conversion
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    normalized_cookies = {
        "fr": (cookies_dict.get("fr") or "").strip(),
        "net": (cookies_dict.get("net") or "").strip(),
    }
    normalized_sources = {
        "fr": (cookie_sources or {}).get("fr", ""),
        "net": (cookie_sources or {}).get("net", ""),
    }
    normalized_cookie_uas = {
        "fr": (cookie_user_agents or {}).get("fr", ""),
        "net": (cookie_user_agents or {}).get("net", ""),
    }
    normalized_cookie_headers = {
        "fr": (cookie_headers or {}).get("fr", ""),
        "net": (cookie_headers or {}).get("net", ""),
    }
    existing_cookies = {"fr": "", "net": ""}
    existing_updated_at = {"fr": "", "net": ""}
    if COOKIE_CACHE_PATH.exists():
        try:
            with COOKIE_CACHE_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            raw_existing_cookies = existing.get("cookies", {}) if isinstance(existing, dict) else {}
            raw_existing_updated = existing.get("cookie_updated_at", {}) if isinstance(existing, dict) else {}
            if isinstance(raw_existing_cookies, dict):
                existing_cookies = {
                    "fr": (raw_existing_cookies.get("fr") or "").strip(),
                    "net": (raw_existing_cookies.get("net") or "").strip(),
                }
            if isinstance(raw_existing_updated, dict):
                existing_updated_at = {
                    "fr": (raw_existing_updated.get("fr") or "").strip(),
                    "net": (raw_existing_updated.get("net") or "").strip(),
                }
        except Exception:
            pass

    cookie_updated_at = {"fr": "", "net": ""}
    for domain in ("fr", "net"):
        current_cookie = normalized_cookies[domain]
        previous_cookie = existing_cookies.get(domain, "")
        previous_ts = existing_updated_at.get(domain, "")
        if not current_cookie:
            cookie_updated_at[domain] = ""
        elif current_cookie == previous_cookie and previous_ts:
            cookie_updated_at[domain] = previous_ts
        else:
            cookie_updated_at[domain] = now_iso

    data = {
        "cookies": normalized_cookies,
        "ua": (ua or DEFAULT_USER_AGENT).strip(),
        "cbz_enabled": bool(cbz),
        "last_url": MangaApp.last_url_used,
        "timestamp": now_iso,
        "cookie_updated_at": cookie_updated_at,
        "cookie_sources": normalized_sources,
        "cookie_user_agents": normalized_cookie_uas,
        "cookie_headers": normalized_cookie_headers,
        "webp2jpg_enabled": bool(webp2jpg_enabled),
        "verbose_logs": bool(verbose_logs),
    }
    COOKIE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = COOKIE_CACHE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, COOKIE_CACHE_PATH)
    return cookie_updated_at


def load_cookie_cache():
    """Charge les paramètres depuis le fichier cache"""
    default_cbz = True
    default_webp2jpg = True
    default_verbose_logs = True
    
    if not COOKIE_CACHE_PATH.exists():
        return (
            {"fr": "", "net": ""},
            DEFAULT_USER_AGENT,
            default_cbz,
            "",
            default_webp2jpg,
            default_verbose_logs,
            {"fr": "", "net": ""},
            {"fr": "", "net": ""},
            {"fr": "", "net": ""},
            {"fr": "", "net": ""},
        )
    
    try:
        with COOKIE_CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)

        cookies = data.get("cookies", {})
        if not isinstance(cookies, dict):
            cookies = {}

        # Les préférences (UA, FlareSolverr, etc.) ne dépendent pas
        # de la validité temporelle du cookie Cloudflare.
        cookie_sources = data.get("cookie_sources", {})
        if not isinstance(cookie_sources, dict):
            cookie_sources = {}

        cookie_user_agents = data.get("cookie_user_agents", {})
        if not isinstance(cookie_user_agents, dict):
            cookie_user_agents = {}
        cookie_headers = data.get("cookie_headers", {})
        if not isinstance(cookie_headers, dict):
            cookie_headers = {}
        cookie_updated_at = data.get("cookie_updated_at", {})
        if not isinstance(cookie_updated_at, dict):
            cookie_updated_at = {}

        return (
            {
                "fr": (cookies.get("fr") or "").strip(),
                "net": (cookies.get("net") or "").strip(),
            },
            (data.get("ua") or DEFAULT_USER_AGENT).strip(),
            data.get("cbz_enabled", default_cbz),
            (data.get("last_url") or "").strip(),
            data.get("webp2jpg_enabled", default_webp2jpg),
            bool(data.get("verbose_logs", default_verbose_logs)),
            {
                "fr": (cookie_sources.get("fr") or "").strip(),
                "net": (cookie_sources.get("net") or "").strip(),
            },
            {
                "fr": (cookie_user_agents.get("fr") or "").strip(),
                "net": (cookie_user_agents.get("net") or "").strip(),
            },
            {
                "fr": (cookie_headers.get("fr") or "").strip(),
                "net": (cookie_headers.get("net") or "").strip(),
            },
            {
                "fr": (cookie_updated_at.get("fr") or "").strip(),
                "net": (cookie_updated_at.get("net") or "").strip(),
            },
        )
    except Exception as e:
        runtime_log(f"Erreur lecture cache cookie : {e}", level="warning")
    
    return (
        {"fr": "", "net": ""},
        DEFAULT_USER_AGENT,
        default_cbz,
        "",
        default_webp2jpg,
        default_verbose_logs,
        {"fr": "", "net": ""},
        {"fr": "", "net": ""},
        {"fr": "", "net": ""},
        {"fr": "", "net": ""},
    )


def get_cover_image(r_text):
    """
    Récupère et affiche l'image de couverture d'un manga
    
    Args:
        r_text (str): Contenu HTML de la page
    """
    runtime_log("Analyse de la couverture en cours.", level="debug", context={"action": "cover"})
    soup = BeautifulSoup(r_text, "html.parser")
    img = soup.select_one("div.thumb img[src], div.thumb-container img[src]")
    img_url = None

    # Recherche de l'URL de l'image
    if img and img.get("src", "").startswith("http"):
        img_url = img["src"]
    else:
        # Fallback aux balises meta
        meta_tags = soup.find_all("meta", attrs={"property": True})
        for tag in meta_tags:
            if tag["property"] in ["og:image", "og:image:secure_url"]:
                candidate = tag.get("content")
                if candidate and candidate.startswith("http"):
                    img_url = candidate
                    break

    # Téléchargement et affichage de l'image
    if img_url:
        if hasattr(MangaApp, 'current_instance'):
            MangaApp.current_instance.cover_url = img_url
            try:
                domain = get_sushiscan_domain_from_url(img_url) or ("net" if "sushiscan.net" in img_url else "fr")
                referer_url = MangaApp.current_instance.url.get().strip()
                if not referer_url:
                    referer_url = f"https://sushiscan.{domain}/"

                cookie = MangaApp.current_instance.get_cookie(img_url)
                cookie_header = MangaApp.current_instance.get_cookie_header_for_url(
                    img_url, fallback_cookie=cookie
                )
                headers = {
                    "User-Agent": MangaApp.current_instance.get_request_user_agent_for_url(img_url),
                    "Referer": referer_url,
                }
                if cookie_header:
                    headers["Cookie"] = cookie_header

                raw = b""
                try:
                    raw = robust_download_image(
                        normalize_image_url(img_url),
                        headers,
                        max_try=2,
                        delay=1,
                    )
                    runtime_log(
                        "Téléchargement couverture OK via accès direct.",
                        level="debug",
                        context={"action": "cover"},
                    )
                except Exception as direct_err:
                    raise RuntimeError(f"Couverture non récupérable en direct: {direct_err}")
                
                # Création de la prévisualisation
                image = Image.open(BytesIO(raw))
                if image.format == "WEBP":
                    image = image.convert("RGB")
                
                # Calcul du ratio pour conserver les proportions
                width, height = image.size
                ratio = min(120/width, 180/height)
                new_width = int(width * ratio)
                new_height = int(height * ratio)
                
                # Redimensionnement avec LANCZOS pour une meilleure qualité
                image = image.resize((new_width, new_height), Image.LANCZOS)
                
                # Création de l'image avec fond blanc
                new_image = Image.new("RGB", (120, 180), (255, 255, 255))
                offset = ((120 - new_width) // 2, (180 - new_height) // 2)
                new_image.paste(image, offset)
                
                MangaApp.current_instance.cover_preview = ImageTk.PhotoImage(new_image)
                MangaApp.current_instance.cover_label.configure(image=MangaApp.current_instance.cover_preview)
                MangaApp.current_instance.cover_label.image = MangaApp.current_instance.cover_preview
            except Exception as err:
                runtime_log(f"Erreur affichage couverture: {err}", level="error", context={"action": "cover"})
        return img_url

    return None


class MangaApp:
    """
    Classe principale de l'application - Interface graphique Tkinter
    Gère l'ensemble de l'UI et la logique de téléchargement
    """
    last_url_used = ""

    def run_on_ui(self, callback, *args, wait=False, default=None, **kwargs):
        """
        Exécute une fonction sur le thread UI.
        - wait=False : asynchrone
        - wait=True  : synchrone (bloque le thread appelant jusqu'au résultat)
        """
        if threading.current_thread() is threading.main_thread():
            return callback(*args, **kwargs)

        if wait:
            done = threading.Event()
            holder = {"result": default, "error": None}

            def wrapped():
                try:
                    holder["result"] = callback(*args, **kwargs)
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    done.set()

            self.ui_queue.put(wrapped)
            done.wait()
            if holder["error"] is not None:
                raise holder["error"]
            return holder["result"]

        self.ui_queue.put(lambda: callback(*args, **kwargs))
        return default

    def process_ui_queue(self):
        """Traite les actions UI planifiées depuis les threads de fond."""
        try:
            for _ in range(200):
                action = self.ui_queue.get_nowait()
                try:
                    action()
                except Exception as exc:
                    emit_console_log(f"Erreur action UI planifiée: {exc}", level="error", context={"action": "ui_queue"})
        except queue.Empty:
            pass
        finally:
            self.root.after(30, self.process_ui_queue)

    def _set_progress_ui(self, percent):
        self.progress.set(percent)
        self.progress_label.config(text=f"{int(percent)}%")

    def _set_download_controls(self, is_running):
        if is_running:
            self.dl_button.config(text="Téléchargement...", state="disabled")
            self.cancel_button.config(state="normal")
            self.filter_entry.config(state="disabled")
            self.clear_filter_button.config(state="disabled")
            if hasattr(self, "invert_button"):
                self.invert_button.config(state="disabled")
            if hasattr(self, "master_toggle_button"):
                self.master_toggle_button.config(state="disabled")
        else:
            self.dl_button.config(text="Télécharger", state="normal")
            self.cancel_button.config(state="disabled")
            self.filter_entry.config(state="normal")
            self.clear_filter_button.config(state="normal")
            if hasattr(self, "invert_button"):
                self.invert_button.config(state="normal")
            if hasattr(self, "master_toggle_button"):
                self.master_toggle_button.config(state="normal")
            if hasattr(self, "set_filter_placeholder") and not self.filter_text.get().strip():
                self.set_filter_placeholder()

    def ask_yes_no(self, title, prompt):
        return self.run_on_ui(
            lambda: messagebox.askyesno(title, prompt, parent=self.root),
            wait=True,
            default=False,
        )

    def ask_string(self, title, prompt):
        import tkinter.simpledialog as simpledialog
        return self.run_on_ui(
            simpledialog.askstring,
            title,
            prompt,
            wait=True,
            default=None,
            parent=self.root,
        )

    def _schedule_auth_status_update(self, *_args):
        """Rafraîchit rapidement les badges/labels auth sans revalidation réseau agressive."""
        if not hasattr(self, "cookie_sources"):
            return
        self.ua_runtime_validity = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))

    def _source_to_display(self, source):
        _ = source
        return ""

    def _set_auth_badge(self, widget, state):
        """Applique un badge visuel pour statut auth: valide / invalide / a_controler."""
        if isinstance(state, bool):
            normalized = "valid" if state else "invalid"
        else:
            normalized = str(state or "").strip().lower()
        if normalized in ("a_controler", "review", "warning", "check"):
            widget.config(text="A Contrôler", bg="#FFC067", fg="#1f2937")
        elif normalized in ("valid", "ok", "true", "1"):
            widget.config(text="Valide", bg="#ADEBB3", fg="#1f2937")
        else:
            widget.config(text="Invalide", bg="#FA003F", fg="#ffffff")

    def _mark_cookie_updated(self, domain, cookie_value):
        """Met à jour le timestamp local de changement cookie pour le domaine."""
        if domain not in ("fr", "net"):
            return
        if not hasattr(self, "cookie_updated_at") or not isinstance(self.cookie_updated_at, dict):
            self.cookie_updated_at = {"fr": "", "net": ""}
        value = (cookie_value or "").strip()
        if value:
            self.cookie_updated_at[domain] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        else:
            self.cookie_updated_at[domain] = ""

    def _is_cookie_stale_for_review(self, domain):
        """Retourne True si le cookie dépasse le seuil d'ancienneté de contrôle."""
        if domain not in ("fr", "net"):
            return False
        cookie_value = getattr(self, f"cookie_{domain}").get().strip()
        if not cookie_value:
            return False
        ts_raw = (getattr(self, "cookie_updated_at", {}).get(domain) or "").strip()
        if not ts_raw:
            return False
        normalized = ts_raw.replace("Z", "+00:00")
        try:
            ts = datetime.datetime.fromisoformat(normalized)
        except Exception:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        age_seconds = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
        return age_seconds >= COOKIE_REVIEW_AGE_SECONDS

    def _refresh_auth_labels(self, active_domain=None):
        """Met à jour les intitulés auth en mode manuel."""
        _ = active_domain
        self.cookie_fr_label_var.set("Cookie (.fr) :")
        self.cookie_net_label_var.set("Cookie (.net) :")
        self.ua_label_var.set("User-Agent :")

    def update_cookie_status(self, validate=True):
        """Met à jour badges et libellés de source pour cookies/UA."""
        try:
            if not hasattr(self, "cookie_sources"):
                return
            current_url = self.url.get().strip()
            active_domain = self.get_domain_from_url(current_url)
            self._refresh_auth_labels(active_domain=active_domain)
            if not all(hasattr(self, name) for name in ("cookie_fr_status", "cookie_net_status", "ua_status")):
                return

            for domain in ("fr", "net"):
                cookie = getattr(self, f"cookie_{domain}").get().strip()
                valid = False
                if cookie:
                    should_validate = validate and ((not active_domain) or active_domain == domain)
                    if should_validate:
                        probe_url = current_url if active_domain == domain else None
                        ua = self.get_request_user_agent_for_domain(domain)
                        eval_state = evaluate_cookie_and_challenge(domain, cookie, ua, probe_url=probe_url)
                        valid = bool(eval_state.get("cookie_valid", False))
                    else:
                        # Domaine non actif: on ne reteste pas immédiatement en réseau.
                        # Si un cookie est présent, on garde un état "valide provisoire"
                        # pour éviter un badge rouge trompeur au démarrage.
                        if validate:
                            valid = True
                        else:
                            valid = bool(self.auth_validity.get(domain, True))
                else:
                    valid = False
                self.auth_validity[domain] = valid
                badge = self.cookie_fr_status if domain == "fr" else self.cookie_net_status
                badge_state = "valid" if valid else "invalid"
                if valid and self._is_cookie_stale_for_review(domain):
                    badge_state = "a_controler"
                self._set_auth_badge(badge, badge_state)

            raw_ua = self.run_on_ui(self.ua.get, wait=True, default="").strip()
            ua_domain = active_domain if active_domain in ("fr", "net") else ""
            if not ua_domain:
                ua_domain = "net" if self.cookie_net.get().strip() else "fr"
            domain_cookie = getattr(self, f"cookie_{ua_domain}").get().strip() if ua_domain in ("fr", "net") else ""
            _ = ua_domain
            _ = domain_cookie
            ua_valid = bool(raw_ua)
            if getattr(self, "ua_runtime_validity", None) is not None:
                ua_valid = ua_valid and bool(self.ua_runtime_validity)
            self.auth_validity["ua"] = ua_valid
            self._set_auth_badge(self.ua_status, ua_valid)
        except Exception as e:
            self.log(f"Erreur statut cookies: {e}", level="error")

    def _schedule_runtime_status_update(self, *_args):
        """Planifie la mise à jour de la barre d'état."""
        self.run_on_ui(self.update_runtime_status)

    def update_runtime_status(self):
        """Met à jour la barre d'état de l'application."""
        try:
            current_url = self.url.get().strip()
            domain = self.get_domain_from_url(current_url) or "-"
            active_cookie = ""
            source = "-"
            cookie_sources = getattr(self, "cookie_sources", {}) or {}
            if domain == "fr":
                active_cookie = self.cookie_fr.get().strip()
                source = (cookie_sources.get("fr") or ("manual" if active_cookie else "none")).strip()
            elif domain == "net":
                active_cookie = self.cookie_net.get().strip()
                source = (cookie_sources.get("net") or ("manual" if active_cookie else "none")).strip()
            cookie_state = "présent" if active_cookie else "absent"
            source_display_map = {
                "manual": "manuel",
                "none": "aucun",
            }
            source_display = source_display_map.get(source.lower(), source or "aucun")
            self.runtime_status.set(
                f"Domaine actif: {domain} | Cookie: {cookie_state} ({source_display}) | Auth: manuel"
            )
        except Exception as exc:
            self.runtime_status.set(f"Statut indisponible: {exc}")

    def __init__(self):
        """Initialise l'interface graphique et charge les paramètres"""
        MangaApp.current_instance = self
        self.total_chapters_to_process = 0
        self.chapters_done = 0
        self.ui_queue = queue.Queue()
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")

        # Fenêtre modernisée: redimensionnable avec taille minimale confortable.
        self.root.geometry("1140x980")
        self.root.minsize(940, 760)
        self.root.resizable(True, True)
        self.log_entries = []
        self.max_log_entries = 5000
        self.log_ready = False
        self.configure_styles()
        
        # Variables Tkinter
        self.cbz_enabled = tk.BooleanVar(value=True)
        self.webp2jpg_enabled = tk.BooleanVar(value=True)
        self.verbose_logs = tk.BooleanVar(value=True)
        self.url = tk.StringVar()
        self.ua = tk.StringVar()
        self.cookie_fr = tk.StringVar()
        self.cookie_net = tk.StringVar()
        self.cookie_fr_label_var = tk.StringVar(value="Cookie (.fr) :")
        self.cookie_net_label_var = tk.StringVar(value="Cookie (.net) :")
        self.ua_label_var = tk.StringVar(value="User-Agent :")
        self.runtime_status = tk.StringVar(value="Prêt.")
        self.log_filter_level = tk.StringVar(value="all")
        self.log_autoscroll = tk.BooleanVar(value=True)
        self.console_logs_enabled = tk.BooleanVar(value=True)
        self.filter_placeholder_text = "Filtre"
        self.filter_placeholder_active = False
        self.auth_validity = {"fr": False, "net": False, "ua": False}
        self.local_ua_source = "manual"
        self.ua_runtime_validity = None
        self.url.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_fr.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_net.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_fr.trace_add("write", self._schedule_auth_status_update)
        self.cookie_net.trace_add("write", self._schedule_auth_status_update)
        self.ua.trace_add("write", self._schedule_auth_status_update)
        self.url.trace_add("write", self._schedule_auth_status_update)

        # Chargement du cache
        (
            cookies,
            ua,
            cbz,
            last_url,
            webp2jpg_enabled,
            verbose_logs_enabled,
            cookie_sources,
            cookie_user_agents,
            cookie_headers,
            cookie_updated_at,
        ) = load_cookie_cache()
        self.cookie_fr.set(cookies.get("fr", ""))
        self.cookie_net.set(cookies.get("net", ""))
        runtime_log(f"{APP_NAME} v{APP_VERSION}", level="info")
        runtime_log(f"Cache cookie : {COOKIE_CACHE_PATH}", level="info")
        runtime_log(f"Config : {CONFIG_PATH}", level="info")
        runtime_log("Mode authentification: manuel.", level="info")
        detected_ua, ua_source = detect_local_user_agent()
        self.local_ua_source = ua_source
        self.ua.set((ua or detected_ua or DEFAULT_USER_AGENT).strip())
        self.cookie_sources = {
            "fr": (cookie_sources.get("fr") or "").strip(),
            "net": (cookie_sources.get("net") or "").strip(),
        }
        self.cookie_user_agents = {
            "fr": (cookie_user_agents.get("fr") or "").strip(),
            "net": (cookie_user_agents.get("net") or "").strip(),
        }
        self.cookie_headers = {
            "fr": (cookie_headers.get("fr") or "").strip(),
            "net": (cookie_headers.get("net") or "").strip(),
        }
        self.cookie_updated_at = {
            "fr": (cookie_updated_at.get("fr") or "").strip(),
            "net": (cookie_updated_at.get("net") or "").strip(),
        }

        direct_ua = (self.ua.get() or DIRECT_USER_AGENT_DEFAULT).strip()
        for domain in ("fr", "net"):
            cookie_value = (cookies.get(domain) or "").strip()
            source = (self.cookie_sources.get(domain) or "").strip().lower()
            if source != "manual":
                source = "manual" if cookie_value else ""
            self.cookie_sources[domain] = source

            if cookie_value:
                self.cookie_user_agents[domain] = direct_ua
            else:
                self.cookie_user_agents[domain] = ""

            if not self.cookie_headers.get(domain):
                self.cookie_headers[domain] = f"cf_clearance={cookie_value}" if cookie_value else ""

        self.last_known_cookies = {
            "fr": (cookies.get("fr") or "").strip(),
            "net": (cookies.get("net") or "").strip(),
        }
        self.cbz_enabled.set(str(cbz).lower() in ("1", "true", "yes"))
        self.webp2jpg_enabled.set(str(webp2jpg_enabled).lower() in ("1", "true", "yes"))
        self.verbose_logs.set(str(verbose_logs_enabled).lower() in ("1", "true", "yes"))
        self.url.set(last_url)  
        MangaApp.last_url_used = last_url
        
        # Initialisation des composants UI
        self.check_vars = []
        self.check_items = []
        self.image_progress_index = None
        self.pairs = []
        self.title = ""
        self.cancel_event = threading.Event()
        self.cover_preview = None

        # Configuration de l'interface
        self.setup_ui()
        self.log_ready = True
        self.refresh_log_view()
        self.root.bind("<Return>", lambda _e: self.load_volumes())
        self.root.bind("<Control-s>", lambda _e: self.save_current_cookie())
        self.root.bind("<Escape>", lambda _e: self.cancel_download())
        self.root.after(30, self.process_ui_queue)
        self.update_cookie_status(validate=False)
        self.update_runtime_status()
        self.check_cookie_age_periodically()

        self.log(f"Application démarrée - {APP_NAME} v{APP_VERSION}.", level="info")
        self.root.mainloop()

    def log(self, message, level="info", context=None):
        """Ajoute une entrée de log unifiée (GUI + terminal)."""
        text = str(message or "").strip()
        if not text:
            return

        normalized_level = normalize_log_level(level)
        timestamp = time.strftime("%H:%M:%S")
        context_suffix = format_log_context(context)
        full_message = f"{text}{context_suffix}"
        entry = {
            "timestamp": timestamp,
            "level": normalized_level,
            "message": full_message,
        }
        self.log_entries.append(entry)
        if len(self.log_entries) > self.max_log_entries:
            self.log_entries = self.log_entries[-self.max_log_entries:]

        if getattr(self, "log_ready", False) and hasattr(self, "log_text"):
            self.run_on_ui(self._append_log_entry, entry)

        verbose_enabled = self.run_on_ui(
            self.verbose_logs.get,
            wait=True,
            default=True,
        )
        if normalized_level == "debug" and not verbose_enabled:
            return

        console_enabled = self.run_on_ui(
            self.console_logs_enabled.get,
            wait=True,
            default=True,
        )
        if console_enabled:
            emit_console_log(
                message=text,
                level=normalized_level,
                context=context,
                timestamp=timestamp,
                with_emoji=CONSOLE_USE_EMOJI,
            )

    def _should_display_log_entry(self, entry):
        """Filtre d'affichage du journal GUI."""
        level = normalize_log_level(entry.get("level", "info"))
        selected = (self.log_filter_level.get() or "all").strip().lower()
        verbose_enabled = bool(self.verbose_logs.get())
        if not verbose_enabled and level == "debug":
            return False
        if selected == "all":
            return True
        return level == selected

    def _format_log_entry(self, entry):
        """Formate une entrée pour affichage dans le widget log."""
        level = normalize_log_level(entry.get("level", "info"))
        timestamp = entry.get("timestamp") or time.strftime("%H:%M:%S")
        message = entry.get("message", "")
        emoji = LOG_EMOJIS.get(level, "") if GUI_USE_EMOJI else ""
        if emoji:
            return f"[{timestamp}] {emoji} {message}\n"
        return f"[{timestamp}] {message}\n"

    def _append_log_entry(self, entry):
        """Ajoute une entrée dans la vue GUI si elle passe les filtres."""
        if not self._should_display_log_entry(entry):
            return
        level = normalize_log_level(entry.get("level", "info"))
        formatted = self._format_log_entry(entry)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", formatted, level)
        self.log_text.configure(state="disabled")
        if self.log_autoscroll.get():
            self.log_text.see("end")

    def refresh_log_view(self, *_args):
        """Rafraîchit le journal GUI selon les filtres actifs."""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for entry in self.log_entries:
            if not self._should_display_log_entry(entry):
                continue
            level = normalize_log_level(entry.get("level", "info"))
            self.log_text.insert("end", self._format_log_entry(entry), level)
        self.log_text.configure(state="disabled")
        if self.log_autoscroll.get():
            self.log_text.see("end")

    def clear_log_entries(self):
        """Efface le journal en mémoire et dans l'UI."""
        self.log_entries.clear()
        self.refresh_log_view()

    def copy_visible_logs(self):
        """Copie le contenu visible du journal dans le presse-papiers."""
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            self.log("Le journal est vide.", level="warning")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.log("Journal copié dans le presse-papiers.", level="success")

    def export_visible_logs(self):
        """Exporte le journal visible dans un fichier texte."""
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            self.log("Le journal est vide.", level="warning")
            return
        default_name = f"sushidl_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        out_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Exporter le journal",
            defaultextension=".log",
            initialfile=default_name,
            filetypes=[("Fichier log", "*.log"), ("Texte", "*.txt"), ("Tous les fichiers", "*.*")],
        )
        if not out_path:
            return
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            self.log(f"Journal exporté: {out_path}", level="success")
        except Exception as exc:
            self.log(f"Erreur export journal: {exc}", level="error")

    def toast(self, message):
        """Affiche une notification temporaire"""
        def _show():
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.configure(bg="#333")
            x = self.root.winfo_x() + self.root.winfo_width() - 260
            y = self.root.winfo_y() + 40
            toast.geometry(f"250x30+{x}+{y}")
            tk.Label(toast, text=message, bg="#333", fg="white", font=("Segoe UI", 9)).pack(
                fill="both", expand=True
            )
            toast.after(2000, toast.destroy)

        self.run_on_ui(_show)

    def configure_styles(self):
        """Configure un style moderne inspire de Breeze (clair + accent bleu)."""
        style = ttk.Style(self.root)
        available = set(style.theme_names())
        preferred = "clam" if "clam" in available else style.theme_use()
        try:
            style.theme_use(preferred)
        except Exception:
            pass

        self.palette = {
            "app_bg": "#eff0f1",
            "card_bg": "#fcfcfc",
            "card_alt": "#f7f7f7",
            "text": "#31363b",
            "muted": "#5b6168",
            "accent": "#3daee9",
            "accent_hover": "#2d9cdb",
            "danger": "#da4453",
            "border": "#c7cdd4",
            "canvas_bg": "#f3f5f7",
            "log_bg": "#ffffff",
            "progress_trough": "#cfd6dd",
        }

        self.root.configure(bg=self.palette["app_bg"])

        style.configure(
            ".",
            font=("Segoe UI", 10),
            background=self.palette["app_bg"],
            foreground=self.palette["text"],
            troughcolor=self.palette["app_bg"],
            selectbackground=self.palette["accent"],
            selectforeground="#ffffff",
        )
        style.map(".", foreground=[("disabled", "#bbcbbe")])
        style.configure("App.TFrame", background=self.palette["app_bg"])
        style.configure("Card.TFrame", background=self.palette["card_bg"])
        style.configure(
            "Card.TLabelframe",
            background=self.palette["card_bg"],
            borderwidth=1,
            relief="solid",
            padding=12,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=self.palette["card_bg"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 10),
        )
        style.configure("App.TLabel", background=self.palette["app_bg"], foreground=self.palette["text"])
        style.configure("Card.TLabel", background=self.palette["card_bg"], foreground=self.palette["text"])
        style.configure("Muted.TLabel", background=self.palette["app_bg"], foreground=self.palette["muted"])
        style.configure("Title.TLabel", background=self.palette["app_bg"], foreground=self.palette["text"], font=("Segoe UI Semibold", 16))
        style.configure("Subtitle.TLabel", background=self.palette["app_bg"], foreground=self.palette["muted"], font=("Segoe UI", 9))

        style.configure("Card.TCheckbutton", background=self.palette["card_bg"], foreground=self.palette["text"], padding=(2, 1))
        style.map("Card.TCheckbutton", background=[("active", self.palette["card_bg"])])
        style.configure("Tome.TCheckbutton", background=self.palette["canvas_bg"], foreground=self.palette["text"], padding=(2, 1))
        style.map("Tome.TCheckbutton", background=[("active", self.palette["canvas_bg"])])

        style.configure(
            "Card.TEntry",
            fieldbackground=self.palette["card_alt"],
            foreground=self.palette["text"],
            background=self.palette["card_alt"],
            padding=4,
        )
        style.map("Card.TEntry", fieldbackground=[("disabled", "#eceff1")])
        style.configure(
            "Card.TCombobox",
            fieldbackground=self.palette["card_alt"],
            foreground=self.palette["text"],
            background=self.palette["card_alt"],
            padding=3,
        )
        style.map(
            "Card.TCombobox",
            fieldbackground=[("readonly", self.palette["card_alt"])],
            background=[("readonly", self.palette["card_alt"])],
            foreground=[("readonly", self.palette["text"])],
        )

        style.configure(
            "Primary.TButton",
            foreground="#ffffff",
            background=self.palette["accent"],
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", self.palette["accent_hover"]), ("disabled", "#94a3b8")],
            foreground=[("disabled", "#f8fafc")],
        )
        style.configure(
            "Download.TButton",
            foreground="#1f2937",
            background="#ADEBB3",
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
            borderwidth=1,
        )
        style.map(
            "Download.TButton",
            background=[("active", "#93d99c"), ("disabled", "#d8f4db")],
            foreground=[("disabled", "#5f6f67")],
        )
        style.configure(
            "Cancel.TButton",
            foreground="#ffffff",
            background="#FA003F",
            padding=(10, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Cancel.TButton",
            background=[("active", "#d00035"), ("disabled", "#f7a8bd")],
            foreground=[("disabled", "#fff4f8")],
        )
        style.configure(
            "Secondary.TButton",
            foreground=self.palette["text"],
            background=self.palette["card_alt"],
            padding=(10, 6),
            font=("Segoe UI", 9),
            borderwidth=1,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#e8ecef"), ("disabled", "#eef2f4")],
            foreground=[("disabled", "#9aa1a9")],
        )

        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=self.palette["progress_trough"],
            background=self.palette["accent"],
            thickness=14,
        )

    def setup_ui(self):
        """Configure tous les elements de l'interface graphique."""
        self.progress = tk.DoubleVar(value=0)

        main_frame = ttk.Frame(self.root, style="App.TFrame", padding=(18, 12))
        main_frame.pack(fill="both", expand=True)

        config_card = ttk.LabelFrame(main_frame, text="Configuration", style="Card.TLabelframe")
        config_card.pack(fill="x", pady=(0, 12))
        config_card.grid_columnconfigure(1, weight=1)

        font_label = ("Segoe UI", 10)
        font_entry = ("Segoe UI", 10)
        row = 0

        ttk.Label(
            config_card,
            textvariable=self.cookie_fr_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.cookie_fr_entry = ttk.Entry(
            config_card, textvariable=self.cookie_fr, width=64, font=font_entry, style="Card.TEntry"
        )
        self.cookie_fr_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.cookie_fr_status = tk.Label(
            config_card,
            text="Invalide",
            font=("Segoe UI Semibold", 9),
            fg="#ffffff",
            bg="#FA003F",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.cookie_fr_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        ttk.Label(
            config_card,
            textvariable=self.cookie_net_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.cookie_net_entry = ttk.Entry(
            config_card, textvariable=self.cookie_net, width=64, font=font_entry, style="Card.TEntry"
        )
        self.cookie_net_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.cookie_net_status = tk.Label(
            config_card,
            text="Invalide",
            font=("Segoe UI Semibold", 9),
            fg="#ffffff",
            bg="#FA003F",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.cookie_net_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        ttk.Label(
            config_card,
            textvariable=self.ua_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.ua_entry = ttk.Entry(config_card, textvariable=self.ua, font=font_entry, style="Card.TEntry")
        self.ua_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.ua_status = tk.Label(
            config_card,
            text="Invalide",
            font=("Segoe UI Semibold", 9),
            fg="#ffffff",
            bg="#FA003F",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.ua_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        options_row = ttk.Frame(config_card, style="Card.TFrame")
        options_row.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 2), padx=(4, 4))
        options_row.columnconfigure(0, weight=1)

        options_left = ttk.Frame(options_row, style="Card.TFrame")
        options_left.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options_left, text=".CBZ", variable=self.cbz_enabled, style="Card.TCheckbutton").pack(side="left", padx=(0, 10))
        ttk.Checkbutton(options_left, text="WEBP en JPG", variable=self.webp2jpg_enabled, style="Card.TCheckbutton").pack(side="left", padx=(0, 10))
        ttk.Checkbutton(
            options_left,
            text="Logs detailes",
            variable=self.verbose_logs,
            style="Card.TCheckbutton",
            command=self.refresh_log_view,
        ).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(
            options_left,
            text="Logs terminal",
            variable=self.console_logs_enabled,
            style="Card.TCheckbutton",
        ).pack(side="left")

        ttk.Button(
            options_row,
            text="Aide Cookie",
            command=lambda: self._open_external_link(
                get_manual_link(
                    "cookie_help",
                    get_manual_link(
                        "cloudflare_help",
                        "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
                    ),
                )
            ),
            style="Secondary.TButton",
        ).grid(row=0, column=1, sticky="e", padx=(0, 8))

        ttk.Button(
            options_row,
            text="Sauvegarder Parametres",
            command=self.save_current_cookie,
            style="Primary.TButton",
        ).grid(row=0, column=2, sticky="e")

        self._setup_auth_link_placeholders()

        source_card = ttk.LabelFrame(main_frame, text="Source", style="Card.TLabelframe")
        source_card.pack(fill="x", pady=(0, 10))

        url_cover_frame = ttk.Frame(source_card, style="Card.TFrame")
        url_cover_frame.pack(fill="x")

        self.cover_frame = tk.Frame(
            url_cover_frame,
            width=120,
            height=150,
            bg=self.palette["card_alt"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
        )
        self.cover_frame.pack_propagate(False)
        self.cover_frame.pack(side="left", padx=(4, 14), pady=2)
        self.cover_label = tk.Label(
            self.cover_frame,
            bg="#ffffff",
            relief="flat",
            borderwidth=0,
            text="Couverture",
            fg=self.palette["muted"],
            font=("Segoe UI", 9),
        )
        self.cover_label.pack(fill="both", expand=True)

        url_frame = ttk.Frame(url_cover_frame, style="Card.TFrame")
        url_frame.pack(side="left", fill="x", expand=True)

        ttk.Label(url_frame, text="URL du Manga/Manwha/BD :", style="Card.TLabel", font=font_label).pack(anchor="w")
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url, font=font_entry, style="Card.TEntry")
        self.url_entry.pack(fill="x", pady=(2, 0))
        self._attach_link_placeholder(
            self.url_entry,
            self.url,
            "https://www.sushiscan.fr|net/catalogue/xxx",
            None,
        )

        analyze_frame = ttk.Frame(url_frame, style="Card.TFrame")
        analyze_frame.pack(pady=(6, 0), anchor="w")
        ttk.Button(analyze_frame, text="Analyser", command=self.load_volumes, style="Primary.TButton").pack(side="left")
        self.status_label = ttk.Label(analyze_frame, text="", style="Card.TLabel", font=("Segoe UI", 9))
        self.status_label.pack(side="left", padx=(12, 0))

        center_card = ttk.LabelFrame(main_frame, text="Tomes / Chapitres", style="Card.TLabelframe")
        center_card.pack(fill="x", expand=False, pady=(0, 10))

        vol_header = ttk.Frame(center_card, style="Card.TFrame")
        vol_header.pack(fill="x", pady=(0, 6))

        left_group = ttk.Frame(vol_header, style="Card.TFrame")
        left_group.pack(side="left")

        filter_group = ttk.Frame(left_group, style="Card.TFrame")
        filter_group.pack(side="left")
        self.filter_text = tk.StringVar()

        filter_box = tk.Frame(
            filter_group,
            bg=self.palette["card_alt"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
        )
        filter_box.pack(side="left", padx=(0, 10))

        self.filter_entry = tk.Entry(
            filter_box,
            textvariable=self.filter_text,
            width=28,
            relief="flat",
            bd=0,
            bg=self.palette["card_alt"],
            fg=self.palette["muted"],
            disabledbackground=self.palette["card_alt"],
            disabledforeground="#8b95a5",
            insertbackground=self.palette["text"],
            font=font_entry,
        )
        self.filter_entry.pack(side="left", padx=(8, 0), pady=4)
        self.filter_entry.bind("<FocusIn>", self.on_filter_focus_in)
        self.filter_entry.bind("<FocusOut>", self.on_filter_focus_out)
        self.filter_entry.bind("<KeyRelease>", lambda e: self.apply_filter())
        self.clear_filter_button = tk.Button(
            filter_box,
            text="×",
            command=self.clear_filter,
            relief="solid",
            bd=1,
            width=2,
            padx=0,
            pady=0,
            bg=self.palette["card_bg"],
            fg=self.palette["muted"],
            activebackground="#fbe4ea",
            activeforeground="#7f1d1d",
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
        )
        self.clear_filter_button.pack(side="left", padx=(4, 6), pady=2)
        self.clear_filter_button.bind("<Enter>", self.on_clear_filter_enter)
        self.clear_filter_button.bind("<Leave>", self.on_clear_filter_leave)

        self.master_toggle_button = ttk.Button(
            left_group,
            text="Tout cocher",
            command=self.toggle_all_button_action,
            style="Secondary.TButton",
            state="disabled",
        )
        self.master_toggle_button.pack(side="left", padx=(0, 8))

        self.invert_button = ttk.Button(
            left_group,
            text="Inverser",
            command=self.invert_selection,
            style="Secondary.TButton",
            state="disabled",
        )
        self.invert_button.pack(side="left")

        download_group = ttk.Frame(vol_header, style="Card.TFrame")
        download_group.pack(side="right")

        self.dl_button = ttk.Button(
            download_group,
            text="Télécharger",
            command=self.download_selected,
            style="Download.TButton",
            state="disabled",
        )
        self.dl_button.pack(side="left", padx=(0, 8))

        self.cancel_button = ttk.Button(
            download_group,
            text="Annuler",
            command=self.cancel_download,
            style="Cancel.TButton",
            state="disabled",
        )
        self.cancel_button.pack(side="left")

        self.set_filter_placeholder()
        self.filter_entry.config(state="disabled")
        self.clear_filter_button.config(state="disabled")

        vol_frame_container = tk.Frame(
            center_card,
            bg=self.palette["canvas_bg"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
        )
        vol_frame_container.pack(fill="x", expand=False)

        canvas_frame = ttk.Frame(vol_frame_container, style="Card.TFrame")
        canvas_frame.pack(fill="both", expand=True, padx=1, pady=1)

        self.canvas = tk.Canvas(
            canvas_frame,
            bg=self.palette["canvas_bg"],
            highlightthickness=0,
            height=148,  # ~4 lignes visibles puis scroll.
        )
        self.scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.vol_frame = tk.Frame(self.canvas, bg=self.palette["canvas_bg"])
        self.canvas_window = self.canvas.create_window((0, 0), window=self.vol_frame, anchor="n")

        def center_volumes(event):
            canvas_width = event.width
            self.canvas.itemconfig(self.canvas_window, width=canvas_width)

        self.canvas.bind("<Configure>", center_volumes)
        self.vol_frame.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        progress_frame = ttk.Frame(main_frame, style="App.TFrame")
        progress_frame.pack(fill="x", pady=(0, 8))
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress,
            maximum=100,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(
            progress_frame,
            text="0%",
            style="Muted.TLabel",
            font=("Segoe UI Semibold", 9),
            width=5,
            anchor="e",
        )
        self.progress_label.pack(side="left", padx=(8, 0))

        status_frame = ttk.Frame(main_frame, style="Card.TFrame")
        status_frame.pack(side="bottom", fill="x")
        status_box = tk.Label(
            status_frame,
            textvariable=self.runtime_status,
            anchor="w",
            fg=self.palette["muted"],
            bg=self.palette["card_alt"],
            font=("Segoe UI", 8),
            padx=10,
            pady=6,
            relief="solid",
            borderwidth=1,
        )
        status_box.pack(fill="x")

        log_frame = ttk.LabelFrame(main_frame, text="Journal", style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True, pady=(0, 8))

        log_toolbar = ttk.Frame(log_frame, style="Card.TFrame")
        log_toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(log_toolbar, text="Niveau:", style="Card.TLabel", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.log_filter_combo = ttk.Combobox(
            log_toolbar,
            width=9,
            state="readonly",
            values=["all", "info", "success", "warning", "error", "debug", "cbz"],
            textvariable=self.log_filter_level,
            style="Card.TCombobox",
        )
        self.log_filter_combo.pack(side="left")
        self.log_filter_combo.bind("<<ComboboxSelected>>", self.refresh_log_view)
        self.log_filter_combo.set("all")
        ttk.Checkbutton(
            log_toolbar,
            text="Auto-scroll",
            variable=self.log_autoscroll,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(10, 0))
        ttk.Button(log_toolbar, text="Effacer", command=self.clear_log_entries, style="Secondary.TButton").pack(side="right", padx=(4, 0))
        ttk.Button(log_toolbar, text="Copier", command=self.copy_visible_logs, style="Secondary.TButton").pack(side="right", padx=(4, 0))
        ttk.Button(log_toolbar, text="Exporter", command=self.export_visible_logs, style="Secondary.TButton").pack(side="right")

        self.log_text = tk.Text(
            log_frame,
            height=8,
            state="disabled",
            wrap="word",
            bg=self.palette["log_bg"],
            fg=self.palette["text"],
            font=("Consolas", 9),
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.tag_config("debug", foreground="#64748b")
        self.log_text.tag_config("success", foreground="#27ae60")
        self.log_text.tag_config("info", foreground="#3daee9")
        self.log_text.tag_config("error", foreground="#da4453")
        self.log_text.tag_config("warning", foreground="#f67400")
        self.log_text.tag_config("cbz", foreground="#7c3aed")

    def _open_external_link(self, url):
        """Ouvre un lien externe dans le navigateur par défaut."""
        target = (url or "").strip()
        if not target:
            return
        try:
            webbrowser.open(target, new=2)
            self.log(f"Ouverture lien: {target}", level="info")
        except Exception as exc:
            self.log(f"Impossible d'ouvrir le lien {target}: {exc}", level="error")

    def _attach_link_placeholder(self, entry_widget, text_variable, placeholder_text, link_url):
        """
        Place un placeholder cliquable par-dessus un Entry sans modifier la valeur réelle.
        Le champ reste vide en interne tant que l'utilisateur n'a rien saisi.
        """
        if entry_widget is None:
            return
        parent = entry_widget.master
        if parent is None:
            return
        bg_color = "#ffffff"
        if hasattr(self, "palette"):
            bg_color = self.palette.get("input_bg", "#ffffff")

        placeholder = tk.Label(
            parent,
            text=placeholder_text,
            fg=self.palette.get("muted", "#7f8c8d"),
            bg=bg_color,
            font=("Segoe UI", 9),
            cursor="hand2" if link_url else "xterm",
            padx=2,
            pady=0,
        )

        state = {"visible": False}

        def show_placeholder():
            has_value = bool((text_variable.get() or "").strip())
            if has_value:
                if state["visible"]:
                    placeholder.place_forget()
                    state["visible"] = False
                return
            if not state["visible"]:
                placeholder.place(in_=entry_widget, x=6, y=4)
                state["visible"] = True

        def hide_placeholder():
            if state["visible"]:
                placeholder.place_forget()
                state["visible"] = False

        def on_focus_in(_event=None):
            hide_placeholder()

        def on_focus_out(_event=None):
            show_placeholder()

        def on_click(_event=None):
            if link_url:
                self._open_external_link(link_url)
            try:
                entry_widget.focus_set()
            except Exception:
                pass

        if link_url:
            placeholder.bind("<Button-1>", on_click)
        else:
            placeholder.bind("<Button-1>", lambda _e: entry_widget.focus_set())
        entry_widget.bind("<FocusIn>", on_focus_in, add="+")
        entry_widget.bind("<FocusOut>", on_focus_out, add="+")
        text_variable.trace_add("write", lambda *_args: show_placeholder())
        show_placeholder()

    def _setup_auth_link_placeholders(self):
        """Initialise les placeholders cliquables pour cookies et User-Agent."""
        ua_link = get_manual_link("user_agent", "https://httpbin.org/user-agent")
        self._attach_link_placeholder(
            self.cookie_fr_entry,
            self.cookie_fr,
            'Coller ici votre cookie cf_clearance. Cliquer sur "Aide Cookie" si besoin.',
            None,
        )
        self._attach_link_placeholder(
            self.cookie_net_entry,
            self.cookie_net,
            'Coller ici votre cookie cf_clearance. Cliquer sur "Aide Cookie" si besoin.',
            None,
        )
        self._attach_link_placeholder(
            self.ua_entry,
            self.ua,
            'Cliquer ici pour accéder à : Votre User-Agent ( Copier/Coller seulement la partie à droite entre les "" )',
            ua_link,
        )


    def get_domain_from_url(self, url):
        """Retourne 'fr' ou 'net' selon l'URL SushiScan."""
        return get_sushiscan_domain_from_url(url)

    def get_cookie(self, url):
        """Sélectionne automatiquement le cookie selon le domaine"""
        domain = self.get_domain_from_url(url)
        if domain == "fr":
            return self.run_on_ui(self.cookie_fr.get, wait=True, default="").strip()
        if domain == "net":
            return self.run_on_ui(self.cookie_net.get, wait=True, default="").strip()
        return ""

    def get_direct_user_agent(self):
        """UA direct (champ UI), utilisé avec cookies manuels."""
        return self.run_on_ui(self.ua.get, wait=True, default="").strip() or DIRECT_USER_AGENT_DEFAULT

    def sync_cookie_source_for_domain(self, domain):
        """Synchronise l'origine du cookie si l'utilisateur a saisi une nouvelle valeur."""
        if domain not in ("fr", "net"):
            return
        cookie_var = self.cookie_fr if domain == "fr" else self.cookie_net
        current_cookie = self.run_on_ui(cookie_var.get, wait=True, default="").strip()
        previous_cookie = (self.last_known_cookies.get(domain) or "").strip()

        if current_cookie and current_cookie != previous_cookie:
            self.cookie_sources[domain] = "manual"
            self.cookie_user_agents[domain] = self.get_direct_user_agent()
            self.cookie_headers[domain] = f"cf_clearance={current_cookie}"
            self.last_known_cookies[domain] = current_cookie
            self._mark_cookie_updated(domain, current_cookie)
        elif not current_cookie:
            self.cookie_sources[domain] = ""
            self.cookie_user_agents[domain] = ""
            self.cookie_headers[domain] = ""
            self.last_known_cookies[domain] = ""
            self._mark_cookie_updated(domain, "")

    def get_request_user_agent_for_domain(self, domain):
        """UA effectif pour un domaine selon l'origine du cookie."""
        self.sync_cookie_source_for_domain(domain)
        return self.get_direct_user_agent()

    def get_request_user_agent_for_url(self, url):
        domain = self.get_domain_from_url(url)
        return self.get_request_user_agent_for_domain(domain)

    def get_cookie_header_for_domain(self, domain, fallback_cookie=None):
        """Retourne l'en-tête Cookie effectif (complet si disponible)."""
        if domain not in ("fr", "net"):
            return ""
        header = (self.cookie_headers.get(domain) or "").strip()
        if header:
            return header
        cookie_var = self.cookie_fr if domain == "fr" else self.cookie_net
        cookie_value = (fallback_cookie or self.run_on_ui(cookie_var.get, wait=True, default="")).strip()
        if cookie_value:
            return f"cf_clearance={cookie_value}"
        return ""

    def get_cookie_header_for_url(self, url, fallback_cookie=None):
        domain = self.get_domain_from_url(url)
        return self.get_cookie_header_for_domain(domain, fallback_cookie=fallback_cookie)

    def persist_settings(self):
        """Sauvegarde silencieuse des paramètres courants."""
        direct_ua = self.get_direct_user_agent()
        cookies = {
            "fr": self.run_on_ui(self.cookie_fr.get, wait=True, default="").strip(),
            "net": self.run_on_ui(self.cookie_net.get, wait=True, default="").strip(),
        }

        # Si l'utilisateur a modifié manuellement un cookie, on repasse en mode UA direct.
        for domain in ("fr", "net"):
            current_cookie = (cookies.get(domain) or "").strip()
            previous_cookie = (self.last_known_cookies.get(domain) or "").strip()
            if current_cookie and current_cookie != previous_cookie:
                self.cookie_sources[domain] = "manual"
                self.cookie_user_agents[domain] = direct_ua
                self.cookie_headers[domain] = f"cf_clearance={current_cookie}"
                self.last_known_cookies[domain] = current_cookie
                self._mark_cookie_updated(domain, current_cookie)
            elif current_cookie:
                self.cookie_sources[domain] = "manual"
                self.cookie_user_agents[domain] = direct_ua
                self.cookie_headers[domain] = f"cf_clearance={current_cookie}"
            elif not current_cookie:
                self.cookie_sources[domain] = ""
                self.cookie_user_agents[domain] = ""
                self.cookie_headers[domain] = ""
                self.last_known_cookies[domain] = ""
                self._mark_cookie_updated(domain, "")

        cbz_enabled = bool(self.run_on_ui(self.cbz_enabled.get, wait=True, default=True))
        webp2jpg_enabled = bool(self.run_on_ui(self.webp2jpg_enabled.get, wait=True, default=True))
        verbose_logs_enabled = bool(self.run_on_ui(self.verbose_logs.get, wait=True, default=True))
        updated_at = save_cookie_cache(
            cookies,
            direct_ua,
            cbz_enabled,
            webp2jpg_enabled,
            verbose_logs_enabled,
            cookie_sources=self.cookie_sources,
            cookie_user_agents=self.cookie_user_agents,
            cookie_headers=self.cookie_headers,
        )
        if isinstance(updated_at, dict):
            self.cookie_updated_at = {
                "fr": (updated_at.get("fr") or "").strip(),
                "net": (updated_at.get("net") or "").strip(),
            }

    def ensure_cookie_for_domain(self, domain, force_refresh=False, probe_url=None):
        """
        Retourne le cookie manuel du domaine.
        Aucun rafraîchissement automatique n'est effectué.
        """
        _ = probe_url
        if domain not in ("fr", "net"):
            return ""

        cookie_var = self.cookie_fr if domain == "fr" else self.cookie_net
        cookie = self.run_on_ui(cookie_var.get, wait=True, default="").strip()
        direct_ua = self.get_direct_user_agent()

        if cookie:
            self.cookie_sources[domain] = "manual"
            self.cookie_user_agents[domain] = direct_ua
            self.cookie_headers[domain] = f"cf_clearance={cookie}"
            self.last_known_cookies[domain] = cookie
            self._mark_cookie_updated(domain, cookie)
            return cookie

        if force_refresh:
            self.log(
                f"Cookie .{domain} vide: renseigne cf_clearance manuellement pour ce domaine.",
                level="warning",
            )
        return ""

    def ensure_cookie_for_url(self, url, force_refresh=False):
        """Rafraîchit le cookie du domaine de l'URL si nécessaire."""
        domain = self.get_domain_from_url(url)
        if not domain:
            return self.get_cookie(url)
        return self.ensure_cookie_for_domain(domain, force_refresh=force_refresh, probe_url=url)

    def load_volumes(self):
        """Charge la liste des tomes/chapitres pour l'URL donnée"""
        self.update_cookie_status(validate=False)
        url = self.url.get().strip()
        if not re.match(REGEX_URL, url):
            self.log("URL invalide. Format attendu: https://sushiscan.fr|net/catalogue/slug/", level="error")
            self.toast("URL invalide")
            return
        cookie = self.get_cookie(url)
        ua_for_url = self.get_request_user_agent_for_url(url)
        domain = self.get_domain_from_url(url)
        if not cookie and domain in ("fr", "net"):
            self.log(
                f"Cookie .{domain} vide: si Cloudflare demande un challenge, renseigne cf_clearance manuellement.",
                level="warning",
            )
        self.filter_text.set("")  # Réinitialise le filtre

        try:
            # Récupération des données du manga
            self.title, self.pairs = fetch_manga_data(
                url, cookie, ua_for_url
            )
            self.ua_runtime_validity = bool((ua_for_url or "").strip())
            self.update_cookie_status(validate=True)
        except Exception as exc:
            self.log(f"Erreur : {str(exc)}", level="error")
            if "HTTP 403" in str(exc):
                self.log(
                    "HTTP 403 détecté: vérifie ton User-Agent (et qu'il correspond à ton navigateur courant).",
                    level="warning",
                )
                self.ua_runtime_validity = False
            self.update_cookie_status(validate=True)
            self.toast("Impossible de charger la liste")
            return

        try:
            
            # Tentative de récupération de la couverture
            try:
                r = make_request(url, cookie, ua_for_url)
                get_cover_image(r.text)
            except Exception as e:
                self.log(f"Erreur chargement couverture: {str(e)}", level="error")
            
            # Sauvegarde dans le cache
            MangaApp.last_url_used = url
            self.persist_settings()
        except Exception as e:
            self.log(f"Erreur : {str(e)}", level="error")
            self.toast("Impossible de charger la liste")
            return

        # Nettoyage de la zone d'affichage
        for widget in self.vol_frame.winfo_children():
            widget.destroy()

        # Création des checkboxes pour chaque tome
        self.check_vars = []
        self.check_items = []
        columns = 4  # Nombre de colonnes pour la grille
        
        for col in range(columns):
            self.vol_frame.grid_columnconfigure(col, weight=1)

        for i, (vol, link) in enumerate(self.pairs):
            var = tk.BooleanVar(value=True)
            self.check_vars.append(var)

            chk = ttk.Checkbutton(
                self.vol_frame,
                text=vol,
                variable=var,
                style="Tome.TCheckbutton",
                takefocus=False,
                command=self.update_master_toggle_button,
            )
            chk.grid(row=(i // columns) + 2, column=i % columns, padx=15, pady=5, sticky="n")
            self.check_items.append((chk, vol))

        # Activation des contrôles
        self.dl_button.config(state="normal")
        self.canvas.yview_moveto(0)
        self.log("Liste chargée avec succès.", level="success")
        self.filter_entry.config(state="normal")
        self.clear_filter_button.config(state="normal")
        if not self.filter_text.get().strip():
            self.set_filter_placeholder()
        self.master_toggle_button.config(state="normal")
        self.invert_button.config(state="normal")
        self.update_master_toggle_button()

    def are_all_volumes_selected(self):
        """Retourne True si toutes les cases sont cochées."""
        return bool(self.check_vars) and all(var.get() for var in self.check_vars)

    def update_master_toggle_button(self):
        """Met à jour le libellé du bouton global de sélection."""
        if not hasattr(self, "master_toggle_button"):
            return
        text = "Tout decocher" if self.are_all_volumes_selected() else "Tout cocher"
        self.master_toggle_button.config(text=text)

    def toggle_all_button_action(self):
        """Bascule globalement entre tout cocher et tout décocher."""
        target_state = not self.are_all_volumes_selected()
        self.toggle_all_volumes(target_state)

    def toggle_all_volumes(self, state):
        """Coche/décoche toutes les cases à cocher."""
        for var in self.check_vars:
            var.set(state)
        self.update_master_toggle_button()
    
    def invert_selection(self):
        """Inverse la sélection actuelle."""
        for var in self.check_vars:
            var.set(not var.get())
        self.update_master_toggle_button()

    def apply_filter(self):
        """Filtre la liste des tomes selon le texte saisi"""
        raw = ""
        if not self.filter_placeholder_active:
            raw = self.filter_text.get().strip().lower()
        
        row = 0
        col = 0
        for chk, label in self.check_items:
            label_lower = label.lower()
            
            # Filtre optimisé avec recherche de sous-chaîne
            if not raw or raw in label_lower or \
            (raw.endswith('*') and raw[:-1].isdigit() and label_lower.startswith(raw[:-1])):
                chk.grid(row=row, column=col, padx=15, pady=5, sticky="n")
                col += 1
                if col == 4:
                    col = 0
                    row += 1
            else:
                chk.grid_remove()

    def clear_filter(self):
        """Réinitialise le filtre et affiche tous les tomes"""
        self.filter_text.set("")
        self.filter_placeholder_active = False
        self.apply_filter()
        self.set_filter_placeholder()

    def set_filter_placeholder(self):
        """Affiche le placeholder du champ filtre si vide."""
        if self.filter_text.get().strip():
            return
        self.filter_placeholder_active = True
        self.filter_text.set(self.filter_placeholder_text)
        self.filter_entry.config(fg=self.palette["muted"])

    def clear_filter_placeholder(self):
        """Retire le placeholder du champ filtre."""
        if not self.filter_placeholder_active:
            return
        self.filter_placeholder_active = False
        self.filter_text.set("")
        self.filter_entry.config(fg=self.palette["text"])

    def on_filter_focus_in(self, _event=None):
        """Nettoie le placeholder quand le champ prend le focus."""
        if self.filter_placeholder_active:
            self.clear_filter_placeholder()

    def on_filter_focus_out(self, _event=None):
        """Restaure le placeholder si le champ est vide."""
        if not self.filter_text.get().strip():
            self.set_filter_placeholder()

    def on_clear_filter_enter(self, _event=None):
        """Survol du bouton de remise à zéro du filtre."""
        if str(self.clear_filter_button.cget("state")) == "disabled":
            return
        self.clear_filter_button.config(bg="#fbe4ea", fg="#7f1d1d")

    def on_clear_filter_leave(self, _event=None):
        """Fin de survol du bouton de remise à zéro du filtre."""
        if str(self.clear_filter_button.cget("state")) == "disabled":
            return
        self.clear_filter_button.config(bg=self.palette["card_bg"], fg=self.palette["muted"])

    def download_selected(self):
        """Lance le téléchargement des tomes sélectionnés"""
        self.cancel_event.clear()
        selected = []
        for (chk, label), (vol, link), var in zip(self.check_items, self.pairs, self.check_vars):
            if var.get() and chk.winfo_ismapped():  # Visible + sélectionné
                selected.append((vol, link))
                
        if not selected:
            self.log("Aucun tome sélectionné.", level="info")
            return

        # Configuration UI pour le téléchargement
        self._set_download_controls(True)
        self._set_progress_ui(0)
        cbz_enabled = self.cbz_enabled.get()
        webp2jpg_enabled = self.webp2jpg_enabled.get()

        def task():
            """Fonction exécutée dans un thread séparé pour le téléchargement"""
            failed = []
            
            # Traitement de chaque tome sélectionné
            for vol, link in selected:
                if self.cancel_event.wait(0.2):
                    break
                start_time = time.time()

                cookie = self.get_cookie(link)
                self.run_on_ui(self.root.title, f"SushiDL - {vol}")
                domain = self.get_domain_from_url(link)
                if not cookie and domain in ("fr", "net"):
                    self.log(
                        f"Cookie .{domain} vide pour {vol}: téléchargement possible seulement si le site ne demande pas de challenge.",
                        level="warning",
                    )
                self.log(
                    f"Téléchargement du tome: {vol}",
                    level="info",
                    context={"domain": domain, "tome": vol, "action": "download_start"},
                )
                
                # Vérification de l'existence du CBZ
                clean_title = sanitize_folder_name(self.title)
                clean_tome = sanitize_folder_name(normalize_tome_label(vol))
                cbz_path = os.path.join(ROOT_FOLDER, clean_title, f"{clean_title} - {clean_tome}.cbz")

                if os.path.exists(cbz_path) and os.path.getsize(cbz_path) > 10_000:
                    self.log(
                        f"CBZ déjà existant, saut du tome: {vol}",
                        level="info",
                        context={"domain": domain, "tome": vol, "action": "skip_existing"},
                    )
                    continue  # passe au tome suivant

                self.run_on_ui(self._set_progress_ui, 0)

                # Récupération des images
                ua = self.get_request_user_agent_for_url(link)
                images = get_images(link, cookie, ua)

                self.log(
                    f"{len(images)} image(s) trouvée(s)",
                    level="info",
                    context={"domain": domain, "tome": vol, "action": "images_count"},
                )

                if images:
                    progress_state = {"last_done": 0, "last_ts": 0.0}

                    def per_image_progress(done, total_images):
                        percent = round((done / total_images) * 100, 1) if total_images else 0
                        self.run_on_ui(self._set_progress_ui, percent)
                        now = time.time()
                        if (
                            done == total_images
                            or now - progress_state["last_ts"] >= 1.5
                            or done - progress_state["last_done"] >= 15
                        ):
                            progress_state["last_done"] = done
                            progress_state["last_ts"] = now
                            self.log(
                                f"Progression image : {done}/{total_images} ({int(percent)}%)",
                                level="info",
                            )

                    self.log(
                        "Début du téléchargement.",
                        level="success",
                        context={"domain": domain, "tome": vol, "action": "download_begin"},
                    )
                    dl_result = download_volume(
                        vol,
                        images,
                        self.title,
                        cookie,
                        ua,
                        self.log,
                        self.cancel_event,
                        cbz_enabled,
                        update_progress=per_image_progress,
                        webp2jpg_enabled=webp2jpg_enabled,
                        referer_url=link,
                    )
                    if dl_result is None and self.cancel_event.is_set():
                        break
                    if dl_result is False:
                        self.log(
                            "Tome non finalisé.",
                            level="warning",
                            context={"domain": domain, "tome": vol, "action": "download_incomplete"},
                        )
                        failed.append((vol, link))
                    else:
                        self.run_on_ui(self._set_progress_ui, 100)
                        elapsed = round(time.time() - start_time, 2)
                        self.log(
                            f"Temps écoulé : {elapsed} secondes",
                            level="info",
                            context={"domain": domain, "tome": vol, "action": "download_done"},
                        )
                else:
                    self.log(
                        "Échec récupération images.",
                        level="warning",
                        context={"domain": domain, "tome": vol, "action": "images_fetch_failed"},
                    )
                    failed.append((vol, link))

            # Tentative de récupération des échecs
            if not self.cancel_event.is_set() and failed:
                self.log(
                    f"Retry des tomes echoues ({len(failed)} restants)",
                    level="warning",
                )
                retry_failed = []

                for vol, link in failed:
                    if self.cancel_event.is_set():
                        break
                    cookie = self.get_cookie(link)
                    ua = self.get_request_user_agent_for_url(link)
                    images = get_images(link, cookie, ua)
                    if images:
                        self.log(f"Retry reussi : {vol}", level="info")
                        retry_result = download_volume(
                            vol,
                            images,
                            self.title,
                            cookie,
                            ua,
                            self.log,
                            self.cancel_event,
                            cbz_enabled,
                            update_progress=None,
                            webp2jpg_enabled=webp2jpg_enabled,
                            referer_url=link,
                        )
                        if retry_result is False:
                            retry_failed.append(vol)
                        if retry_result is None and self.cancel_event.is_set():
                            break
                    else:
                        self.log(f"Retry echoue : {vol}", level="error")
                        retry_failed.append(vol)

                if retry_failed:
                    self.log(
                        f"Tomes definitivement echoues : {', '.join(retry_failed)}",
                        level="error",
                    )

            # Finalisation
            if self.cancel_event.is_set():
                self.log("Téléchargement annulé !", level="warning")
                self.run_on_ui(self._set_progress_ui, 0)
            else:
                self.log("Tous les tomes ont été traités.", level="success")
            self.cancel_event.clear()
            self.run_on_ui(self._set_download_controls, False)
            self.run_on_ui(self.root.title, f"{APP_NAME} v{APP_VERSION}")

        # Lancement dans un thread séparé
        threading.Thread(target=task, daemon=True).start()

    def cancel_download(self):
        """Annule le téléchargement en cours"""
        self.cancel_event.set()
        self.log("Annulation demandée...", level="warning")
        self.cancel_button.config(state="disabled")

    def check_cookie_age_periodically(self):
        """Vérifie périodiquement l'âge des cookies"""
        try:
            if COOKIE_CACHE_PATH.exists():
                with COOKIE_CACHE_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                now = datetime.datetime.now(datetime.timezone.utc)
                cookie_map = data.get("cookies", {}) if isinstance(data.get("cookies"), dict) else {}
                per_cookie_ts = data.get("cookie_updated_at", {})
                if not isinstance(per_cookie_ts, dict):
                    per_cookie_ts = {}

                # Compat: fallback timestamp global si pas de timestamp par domaine
                global_ts_raw = data.get("timestamp")
                global_ts = None
                if global_ts_raw:
                    try:
                        global_ts = datetime.datetime.fromisoformat(global_ts_raw)
                    except Exception:
                        global_ts = None

                stale_domains = []
                for domain in ("fr", "net"):
                    cookie = (cookie_map.get(domain) or "").strip()
                    if not cookie:
                        continue

                    ts_raw = per_cookie_ts.get(domain) or ""
                    ts = None
                    if ts_raw:
                        try:
                            ts = datetime.datetime.fromisoformat(ts_raw)
                        except Exception:
                            ts = None
                    if ts is None:
                        ts = global_ts
                    if ts is None:
                        continue

                    age = (now - ts).total_seconds()
                    if age > COOKIE_MAX_AGE_SECONDS:
                        stale_domains.append(domain)

                if stale_domains:
                    self.log(
                        f"Cookie potentiellement expiré ({', '.join(stale_domains)}), "
                        "mise a jour manuelle recommandee.",
                        level="warning",
                    )
        except Exception as e:
            self.log(f"Erreur vérification cookie: {e}", level="error")
        self.root.after(3600000, self.check_cookie_age_periodically)  # Re-programme après 1h

    def save_current_cookie(self):
        """Sauvegarde les paramètres actuels dans le cache"""
        try:
            self.persist_settings()
            self.log("Cookies, UA, CBZ, WEBP->JPG et preferences logs sauvegardes !", level="success")
            self.update_cookie_status()
            self.update_runtime_status()
        except Exception as e:
            self.log(f"Erreur sauvegarde: {e}", level="error")


# Point d'entrée de l'application
if __name__ == "__main__":
    runtime_log(f"Lancement de {APP_NAME} v{APP_VERSION}", level="info")
    MangaApp()




