#!/usr/bin/env python3
"""
Helge Morning Briefing
Generiert HTML-Dashboard + Audio-Podcast, versendet per E-Mail, pushed zu GitHub Pages.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

# .env laden
load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Pfade
SCRIPT_DIR = Path(__file__).parent
DOCS_DIR = SCRIPT_DIR / "docs"
ARCHIV_DIR = DOCS_DIR / "archiv"
TEMPLATE_DIR = SCRIPT_DIR / "templates"

# Keys aus .env
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "bIHbv24MWmeRgasZH58o")

# Claude Client
claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
MODEL_PRIMARY = "claude-opus-4-6"
MODEL_FALLBACK = "claude-sonnet-4-5-20250929"

# Deutsche Monatsnamen
MONATE = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember"
]
WOCHENTAGE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag"
]


def datum_formatiert(dt):
    return f"{WOCHENTAGE[dt.weekday()]}, {dt.day}. {MONATE[dt.month]} {dt.year}"


# ─── API CALL MIT RETRY ───────────────────────────────────────────

def call_claude(system_prompt, user_prompt, max_search=0, retries=2):
    """Claude API Call mit Retry + Fallback auf Sonnet."""
    model = MODEL_PRIMARY
    for attempt in range(retries + 1):
        try:
            kwargs = {
                "model": model,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            if max_search > 0:
                kwargs["tools"] = [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max_search,
                }]

            response = claude.messages.create(**kwargs)
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            result = "\n".join(text_parts) if text_parts else None

            if result:
                logger.info(f"Claude OK ({model}): {len(result)} Zeichen")
                return result
            else:
                logger.warning(f"Keine Textantwort (Versuch {attempt + 1})")

        except Exception as e:
            logger.error(f"Claude Error (Versuch {attempt + 1}, {model}): {e}")
            if attempt == 0 and model == MODEL_PRIMARY:
                logger.info("Fallback auf Sonnet...")
                model = MODEL_FALLBACK
            elif attempt < retries:
                time.sleep(5)
    return None


def parse_json_response(text):
    """Extrahiert JSON aus Claude-Response."""
    if not text:
        return None
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON Parse Error: {e}")
        # Reparaturversuch: bis zur letzten schließenden Klammer
        try:
            last_brace = text.rfind("}")
            if last_brace > 0:
                return json.loads(text[:last_brace + 1])
        except json.JSONDecodeError:
            pass
        # Letzter Versuch: Claude reparieren lassen
        logger.warning("JSON-Reparatur via Sonnet...")
        try:
            repair = call_claude(
                "Repariere dieses JSON. Antworte NUR mit dem reparierten JSON.",
                f"```json\n{text}\n```",
            )
            return parse_json_response(repair) if repair else None
        except Exception:
            logger.error("JSON-Reparatur fehlgeschlagen")
            return None


# ─── TEIL 1: MEDIENLANDSCHAFT ──────────────────────────────────────

def gather_medienlandschaft(datum_str):
    """Medienlandschaft — Was schreibt wer?"""
    logger.info("=== Teil 1: Medienlandschaft ===")

    system = """Du bist ein Medien-Analyst, der dem Chefredakteur von WELT jeden Morgen einen Überblick gibt.
WICHTIG: Es geht NICHT primär darum, was in der Welt passiert. Es geht darum, was die KONKURRENZ schreibt, welche Positionen sie einnehmen, worüber sie diskutieren und was bei ihnen funktioniert.
Denke wie jemand in einer Redaktionskonferenz: "Die SZ hat heute auf Seite 1...", "Der Spiegel fährt einen großen Kommentar zu...", "In der Presseschau dominiert..."
Antworte AUSSCHLIESSLICH mit validem JSON, ohne Erklärungen davor oder danach."""

    prompt = f"""Heute ist {datum_str}. Erstelle einen Medienlandschaft-Überblick für den Chefredakteur.

SUCHE NACH (in dieser Reihenfolge):

1. TERMINE DES TAGES — Das ist ZENTRAL und darf NICHT fehlen!
   Suche ZUERST gezielt nach:
   - "Termine heute {datum_str} Politik Deutschland"
   - "Bundestag Tagesordnung heute"
   - "EU Gipfel Treffen heute"
   - "Pressekonferenz heute Berlin"
   - "Bundesregierung Termine heute"
   Was steht heute an? Gipfeltreffen, Konferenzen, Bundestags-Abstimmungen, Gerichtsurteile, Pressekonferenzen, Wahlen, Streiks?

2. Deutschlandfunk Presseschau — Was sind die Themen? Welche Zeitung positioniert sich wie?

3. Was berichten die großen Medien HEUTE? — SZ, Spiegel, FAZ, Zeit, Handelsblatt, Tagesspiegel, tagesschau
   → WER berichtet über WELCHES Thema und welche Schwerpunkte setzen sie?

4. Wichtige Kommentare und Meinungsstücke — Wer schreibt was?

ANTWORTE NUR MIT DIESEM JSON-FORMAT:
```json
{{
    "termine": [
        {{"was": "Name des Events", "wann": "Uhrzeit oder ganztägig", "wo": "Ort", "details": "Was wird besprochen? (2-3 Sätze)", "url": "URL wenn verfügbar"}}
    ],
    "presseschau": [
        {{"zeitung": "Name", "thema": "Thema in 3-5 Wörtern", "position": "Position und Argumentation (2-3 Sätze)"}}
    ],
    "konkurrenz": [
        {{"medium": "SZ/Spiegel/FAZ/etc.", "schwerpunkt": "Top-Thema heute?", "details": "Wie berichten sie? Angle? (2-3 Sätze)", "url": "URL wenn verfügbar"}}
    ],
    "kommentare": [
        {{"zeitung": "Name", "autor": "Autor", "thema": "Worum es geht", "position": "Kernaussage (2-3 Sätze)"}}
    ],
    "bundestag": [
        {{"thema": "Thema", "details": "Was ansteht (2-3 Sätze)"}}
    ],
    "tagesueberblick": "2-3 Sätze: Dominierende Themen in der deutschen Medienlandschaft heute"
}}
```

WICHTIG:
- Termine: ALLE wichtigen politischen Termine!
- Presseschau: mindestens 4-6 Zeitungen
- Konkurrenz: 5-8 Einträge
- Kommentare: 3-5 Meinungsstücke mit Autor
- Wenn keine Termine gefunden: leeres Array, aber SUCHE INTENSIV"""

    response = call_claude(system, prompt, max_search=10)
    data = parse_json_response(response)

    if data:
        counts = {k: len(data.get(k, [])) for k in ["termine", "presseschau", "konkurrenz", "kommentare", "bundestag"]}
        logger.info(f"Medienlandschaft: {counts}")
    return data


# ─── TEIL 2: WELT ÜBERBLICK ───────────────────────────────────────

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
MAX_AGE_HOURS = 18


def check_article_meta(url):
    """Prüft Veröffentlichungsdatum und Paywall-Status."""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        html = resp.text
        dates = re.findall(
            r'(?:datePublished|publicationDate)[^0-9]*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z?)',
            html
        )
        date_str = dates[0] if dates else None
        is_plus = (
            '"isPaid":true' in html
            or '"isPremiumContent":true' in html
            or '"isPaywalled":true' in html
        )
        return date_str, is_plus
    except Exception:
        return None, False


def is_fresh(date_str, max_hours=MAX_AGE_HOURS):
    """Prüft ob Datum innerhalb der letzten max_hours liegt."""
    if not date_str:
        return False
    try:
        dt_str = date_str.replace("Z", "+00:00")
        pub_date = datetime.fromisoformat(dt_str)
        now = datetime.now(timezone.utc)
        return (now - pub_date) < timedelta(hours=max_hours)
    except Exception:
        return False


def scrape_welt_articles():
    """Crawlt welt.de Startseite."""
    logger.info("Crawle welt.de Startseite...")
    try:
        resp = requests.get("https://www.welt.de/", headers=HTTP_HEADERS, timeout=15)
        html = resp.text
    except Exception as e:
        logger.error(f"welt.de nicht erreichbar: {e}")
        return []

    ressort_pattern = r'href="(/(politik|wirtschaft|sport|kultur|wissen|panorama|meinung|finanzen)[^"]*article[^"]+)"[^>]*>([^<]+)<'
    matches = re.findall(ressort_pattern, html)

    candidates = []
    seen_urls = set()
    for url, ressort, title in matches:
        full_url = f"https://www.welt.de{url}"
        title = title.strip()
        if full_url not in seen_urls and len(title) > 10 and "liveticker" not in url.lower():
            seen_urls.add(full_url)
            candidates.append({
                "titel": title,
                "ressort": ressort.capitalize(),
                "url": full_url,
                "plus": "/plus" in url,
            })

    logger.info(f"welt.de: {len(candidates)} Kandidaten, prüfe Aktualität...")

    articles = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(check_article_meta, a["url"]): a for a in candidates}
        for future in as_completed(future_map):
            article = future_map[future]
            date_str, is_plus = future.result()
            if is_fresh(date_str):
                article["datum"] = date_str
                article["plus"] = is_plus or article.get("plus", False)
                articles.append(article)

    logger.info(f"welt.de: {len(articles)} frische Artikel (letzte {MAX_AGE_HOURS}h)")
    return articles


def scrape_welt_plus_articles():
    """Crawlt Welt+ Artikel mit Playwright-Login (voller Text)."""
    welt_email = os.environ.get("WELT_EMAIL")
    welt_password = os.environ.get("WELT_PASSWORD")

    if not welt_email or not welt_password:
        logger.info("Kein Welt+ Login konfiguriert, überspringe Plus-Artikel")
        return {}

    cookie_file = SCRIPT_DIR / "welt_cookies.json"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright nicht installiert, überspringe Welt+ Crawl")
        return {}

    logger.info("=== Welt+ Artikel crawlen (Playwright) ===")
    plus_articles = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HTTP_HEADERS["User-Agent"])

            # Gespeicherte Cookies laden wenn vorhanden
            if cookie_file.exists():
                try:
                    cookies = json.loads(cookie_file.read_text())
                    context.add_cookies(cookies)
                    logger.info("Gespeicherte Cookies geladen")
                except Exception:
                    logger.warning("Cookie-Datei korrupt, Login nötig")

            page = context.new_page()

            # Prüfe ob Login nötig
            page.goto("https://www.welt.de/meinewelt/", timeout=15000)
            time.sleep(2)

            if "login" in page.url.lower() or "anmelden" in page.content().lower():
                logger.info("Login bei welt.de...")
                page.goto("https://login.welt.de/", timeout=15000)
                time.sleep(2)

                # E-Mail eingeben
                email_input = page.query_selector('input[type="email"], input[name="email"], #email')
                if email_input:
                    email_input.fill(welt_email)

                # Passwort eingeben
                pw_input = page.query_selector('input[type="password"], input[name="password"], #password')
                if pw_input:
                    pw_input.fill(welt_password)

                # Login-Button klicken
                login_btn = page.query_selector('button[type="submit"], input[type="submit"]')
                if login_btn:
                    login_btn.click()
                    time.sleep(3)

                # Cookies speichern
                cookies = context.cookies()
                cookie_file.write_text(json.dumps(cookies))
                logger.info("Login erfolgreich, Cookies gespeichert")

            # Jetzt Plus-Artikel crawlen
            page.goto("https://www.welt.de/", timeout=15000)
            time.sleep(2)

            # Alle Plus-Links finden
            plus_links = page.eval_on_selector_all(
                'a[href*="/plus/"]',
                'els => els.map(e => ({href: e.href, text: e.textContent.trim()})).filter(e => e.text.length > 10).slice(0, 10)'
            )

            for link in plus_links[:5]:  # Max 5 Plus-Artikel
                try:
                    page.goto(link["href"], timeout=15000)
                    time.sleep(1)

                    # Artikeltext extrahieren
                    article_body = page.query_selector('article, .c-article-text, [class*="article-body"]')
                    if article_body:
                        text = article_body.inner_text()
                        if len(text) > 200:
                            plus_articles[link["href"]] = {
                                "titel": link["text"],
                                "text": text[:2000],  # Max 2000 Zeichen
                                "url": link["href"],
                            }
                            logger.info(f"Welt+ Artikel gelesen: {link['text'][:50]}...")
                except Exception as e:
                    logger.warning(f"Welt+ Artikel Fehler: {e}")

            browser.close()

    except Exception as e:
        logger.error(f"Playwright Fehler: {e}")

    logger.info(f"Welt+ Artikel gecrawlt: {len(plus_articles)}")
    return plus_articles


def gather_welt_uebersicht(datum_str):
    """Teil 2: Welt Überblick — Echte Daten + Claude-Einordnung"""
    logger.info("=== Teil 2: Welt Überblick ===")

    raw_articles = scrape_welt_articles()
    plus_data = scrape_welt_plus_articles()

    if not raw_articles:
        logger.warning("Keine Artikel von welt.de — Fallback auf Web Search")
        return _welt_fallback(datum_str)

    # Nach Ressort gruppieren
    grouped = defaultdict(list)
    for a in raw_articles:
        ressort = a["ressort"]
        if ressort == "Finanzen":
            ressort = "Wirtschaft"
        # Plus-Text anhängen wenn verfügbar
        if a["url"] in plus_data:
            a["plus_text"] = plus_data[a["url"]]["text"]
        grouped[ressort].append(a)

    # Artikelliste für Zusammenfassung
    articles_text = ""
    for ressort, arts in sorted(grouped.items()):
        articles_text += f"\n{ressort}:\n"
        for a in arts[:6]:
            line = f"  - {a['titel']}"
            if a.get("plus_text"):
                line += f" (Auszug: {a['plus_text'][:200]}...)"
            articles_text += line + "\n"

    summary = ""
    try:
        summary = call_claude(
            "Du bist ein Redaktionsanalyst. Antworte mit 2-3 Sätzen, kein JSON, kein Markdown.",
            f"Was sind die Schwerpunkte auf welt.de heute ({datum_str})?\n{articles_text}\n\nFasse in 2-3 Sätzen zusammen.",
            max_search=0,
        ) or ""
    except Exception as e:
        logger.warning(f"Zusammenfassung fehlgeschlagen: {e}")

    # Datenstruktur aufbauen
    ressorts_data = {}
    for ressort, arts in sorted(grouped.items()):
        arts_sorted = sorted(arts, key=lambda x: x.get("datum", ""), reverse=True)
        ressorts_data[ressort] = []
        for a in arts_sorted[:5]:
            zeit = ""
            if a.get("datum"):
                try:
                    dt = datetime.fromisoformat(a["datum"].replace("Z", "+00:00"))
                    dt_local = dt + timedelta(hours=1)
                    zeit = dt_local.strftime("%H:%M Uhr")
                except Exception:
                    pass

            entry = {
                "titel": a["titel"],
                "autor": None,
                "beschreibung": a.get("plus_text", "")[:300] if a.get("plus_text") else None,
                "url": a["url"],
                "zeit": zeit,
                "plus": a.get("plus", False),
            }
            ressorts_data[ressort].append(entry)

    data = {
        "zusammenfassung": summary.strip(),
        "ressorts": ressorts_data,
        "meistgelesen": [],
    }

    total = sum(len(v) for v in data["ressorts"].values())
    logger.info(f"Welt Überblick: {total} Artikel in {len(data['ressorts'])} Ressorts")
    return data


def _welt_fallback(datum_str):
    """Fallback: Web Search wenn Crawling fehlschlägt."""
    response = call_claude(
        "Du bist ein Redaktionsanalyst. Antworte AUSSCHLIESSLICH mit validem JSON.",
        f'Suche nach aktuellen Artikeln auf welt.de vom {datum_str}.\n\nANTWORTE NUR MIT JSON:\n```json\n{{"zusammenfassung": "...", "ressorts": {{"Politik": [{{"titel": "...", "url": "..."}}]}}, "meistgelesen": []}}\n```',
        max_search=10,
    )
    return parse_json_response(response)


# ─── AUDIO ─────────────────────────────────────────────────────────

def generate_audio_script(lage, welt, datum_str, datum_lang):
    """Generiert TTS-optimiertes Audio-Skript."""
    logger.info("=== Audio-Skript generieren ===")

    system = """Du bist ein kompetenter Nachrichtenredakteur, der seinem Chefredakteur morgens die Lage erklärt.
Du sprichst wie in einer Redaktionskonferenz: knapp, auf den Punkt, aber natürlich.
Kein JSON, sondern Fließtext zum Vorlesen.

STRENG WICHTIG für TTS:
- Zahlen IMMER ausschreiben (2026 → zweitausendsechsundzwanzig, 50% → fünfzig Prozent)
- Abkürzungen ausschreiben (SZ → Süddeutsche Zeitung, FAZ → Frankfurter Allgemeine, CDU → C D U)
- Natürliche Sprechpausen durch Punkte und Kommata
- Keine Sonderzeichen, keine Emojis, keine Formatierung
- Schreibe "welt punkt d e" statt "welt.de"

ABSOLUT WICHTIG — KEINE EIGENE MEINUNG:
- NIEMALS bewerten, wie gut oder schlecht ein Medium berichtet
- KEINE Formulierungen wie "treffend analysiert", "problematisch dargestellt", "gut beleuchtet"
- NUR neutral berichten: WAS berichtet WER über WELCHES Thema
- Du bist ein Nachrichtenüberblick, kein Medienkritiker

LÄNGE: MAXIMAL 1200 Wörter gesamt. Sei KOMPAKT."""

    lage_json = json.dumps(lage, ensure_ascii=False, indent=2) if lage else "{}"
    welt_json = json.dumps(welt, ensure_ascii=False, indent=2) if welt else "{}"

    prompt = f"""Erstelle ein Audio-Briefing für den Chefredakteur Helge. Heute ist {datum_lang}.

MAXIMAL 1200 Wörter. Sei knapp und präzise.

STRUKTUR:

1. BEGRÜSSUNG (1-2 Sätze):
"Hallo Helge, guten Morgen. Hier ist dein Briefing für {datum_lang}."

2. TEIL 1 — MEDIENLANDSCHAFT (~600 Wörter):
- Was dominiert die Medienagenda?
- Presseschau: Welche Themen?
- Konkurrenz: Was berichten SZ, Spiegel, FAZ?
- Kommentare: Wer hat was geschrieben?
- KEIN Bundestag im Audio! KEINE Termine im Audio!
WICHTIG: KEINE eigene Bewertung! Nur NEUTRAL berichten.
Daten: {lage_json}

3. ÜBERLEITUNG (1 Satz):
"So viel zu den anderen. Kommen wir zu uns."

4. TEIL 2 — WELT ÜBERBLICK (~500 Wörter):
- Schwerpunkte auf welt punkt d e
- Welche Artikel stechen heraus?
Daten: {welt_json}

5. VERABSCHIEDUNG (1 Satz):
"Das wars für heute Morgen. Guten Start in den Tag!"

STIL: Wie ein Kollege, der den Chef kurz und kompetent brieft."""

    response = call_claude(system, prompt, max_search=0)
    if response:
        word_count = len(response.split())
        logger.info(f"Audio-Skript: {len(response)} Zeichen, ~{word_count} Wörter")
    return response


def generate_audio(text, output_path):
    """Generiert MP3 via ElevenLabs mit Retry."""
    logger.info("=== Audio generieren (ElevenLabs) ===")
    if not text:
        logger.error("Kein Text für Audio")
        return False

    # Splitten bei >4500 Zeichen
    chunks = []
    if len(text) > 4500:
        split_markers = ["So viel zu den anderen", "Kommen wir zu uns", "Kommen wir nun zu welt"]
        split_pos = None
        for marker in split_markers:
            pos = text.lower().find(marker.lower())
            if pos > 0:
                split_pos = pos
                break
        if split_pos:
            chunks = [text[:split_pos].strip(), text[split_pos:].strip()]
        else:
            mid = len(text) // 2
            dot_pos = text.find(". ", mid)
            chunks = [text[:dot_pos + 1].strip(), text[dot_pos + 2:].strip()] if dot_pos > 0 else [text]
    else:
        chunks = [text]

    audio_parts = []
    for i, chunk in enumerate(chunks):
        logger.info(f"ElevenLabs {i + 1}/{len(chunks)} ({len(chunk)} Zeichen)...")

        for attempt in range(3):  # 3 Versuche
            try:
                url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
                resp = requests.post(url, json={
                    "text": chunk,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {"stability": 0.6, "similarity_boost": 0.75},
                }, headers={
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                    "xi-api-key": ELEVENLABS_API_KEY,
                }, timeout=60)

                if resp.status_code == 200:
                    audio_parts.append(resp.content)
                    logger.info(f"Audio-Teil {i + 1}: {len(resp.content)} Bytes")
                    break
                else:
                    logger.warning(f"ElevenLabs {resp.status_code} (Versuch {attempt + 1})")
                    if attempt < 2:
                        time.sleep(5)
            except Exception as e:
                logger.error(f"ElevenLabs Error (Versuch {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(5)
        else:
            logger.error(f"ElevenLabs: Alle Versuche fehlgeschlagen für Chunk {i + 1}")
            return False

    # Zusammenfügen
    if len(audio_parts) == 1:
        Path(output_path).write_bytes(audio_parts[0])
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            concat_list = Path(tmpdir) / "concat.txt"
            entries = []
            for i, part in enumerate(audio_parts):
                part_path = Path(tmpdir) / f"part{i}.mp3"
                part_path.write_bytes(part)
                entries.append(f"file '{part_path}'")
            concat_list.write_text("\n".join(entries))

            try:
                subprocess.run([
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list), "-c", "copy", str(output_path)
                ], capture_output=True, check=True, timeout=30)
            except (subprocess.CalledProcessError, FileNotFoundError):
                with open(output_path, "wb") as f:
                    for part in audio_parts:
                        f.write(part)

    logger.info(f"Audio gespeichert: {output_path}")
    return True


# ─── HTML RENDERN ──────────────────────────────────────────────────

def render_html(lage, welt, datum_lang, audio_available):
    """Rendert HTML-Dashboard."""
    logger.info("=== HTML rendern ===")

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("briefing.html")

    ressort_classes = {
        "politik": "card-politik", "wirtschaft": "card-wirtschaft",
        "sport": "card-sport", "wissen": "card-wissen",
        "panorama": "card-panorama", "meinung": "card-meinung",
        "kultur": "card-kultur",
    }

    if not lage:
        lage = {"termine": [], "presseschau": [], "konkurrenz": [], "kommentare": [], "bundestag": [], "tagesueberblick": ""}
    if not welt:
        welt = {"ressorts": {}, "meistgelesen": [], "zusammenfassung": ""}

    now = datetime.now()
    generated_at = f"{now.strftime('%H:%M')} Uhr, {datum_formatiert(now)}"

    return template.render(
        datum_lang=datum_lang, lage=lage, welt=welt,
        ressort_classes=ressort_classes,
        audio_available=audio_available, generated_at=generated_at,
    )


# ─── E-MAIL VERSAND ───────────────────────────────────────────────

def send_email(html_content, audio_path, datum_lang):
    """Versendet Briefing per E-Mail via Resend."""
    api_key = os.environ.get("RESEND_API_KEY")
    email_to = os.environ.get("EMAIL_TO")
    email_cc = os.environ.get("EMAIL_CC")
    email_from = os.environ.get("EMAIL_FROM", "briefing@helge-briefing.github.io")

    if not api_key or not email_to:
        logger.warning("E-Mail nicht konfiguriert (RESEND_API_KEY / EMAIL_TO fehlt)")
        return False

    logger.info(f"=== E-Mail senden an {email_to} ===")

    try:
        import resend
        resend.api_key = api_key

        attachments = []
        if audio_path and Path(audio_path).exists():
            audio_bytes = Path(audio_path).read_bytes()
            import base64
            attachments.append({
                "filename": f"briefing-{datetime.now().strftime('%Y-%m-%d')}.mp3",
                "content": base64.b64encode(audio_bytes).decode(),
            })

        params = {
            "from": f"Helge Briefing <{email_from}>",
            "to": [email_to],
            "subject": f"Morning Briefing — {datum_lang}",
            "html": html_content,
        }
        if email_cc:
            params["cc"] = [email_cc]
        if attachments:
            params["attachments"] = attachments

        result = resend.Emails.send(params)
        logger.info(f"E-Mail gesendet: {result}")
        return True

    except Exception as e:
        logger.error(f"E-Mail Fehler: {e}")
        return False


# ─── GITHUB PAGES PUSH ────────────────────────────────────────────

def push_to_github():
    """Pushed docs/ Ordner zu GitHub Pages."""
    logger.info("=== GitHub Pages Push ===")

    try:
        # Git-Operationen im Projektverzeichnis
        os.chdir(SCRIPT_DIR)

        subprocess.run(["git", "add", "docs/"], check=True, capture_output=True)

        # Prüfe ob es Änderungen gibt
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if result.returncode == 0:
            logger.info("Keine Änderungen — Skip Push")
            return True

        datum = datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(
            ["git", "commit", "-m", f"Briefing {datum}"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "push"], check=True, capture_output=True, timeout=30)
        logger.info("GitHub Pages Push erfolgreich")
        return True

    except Exception as e:
        logger.error(f"GitHub Push Fehler: {e}")
        return False


# ─── VALIDIERUNG ───────────────────────────────────────────────────

def validate_briefing(lage, welt, audio_ok, html):
    """Doppelcheck: Ist das Briefing vollständig und brauchbar?"""
    issues = []

    if not lage:
        issues.append("KRITISCH: Keine Medienlandschaft-Daten")
    elif len(lage.get("konkurrenz", [])) < 3:
        issues.append(f"WARNUNG: Nur {len(lage.get('konkurrenz', []))} Konkurrenz-Einträge (min. 3)")

    if not welt:
        issues.append("KRITISCH: Keine Welt-Daten")
    elif sum(len(v) for v in welt.get("ressorts", {}).values()) < 5:
        issues.append("WARNUNG: Weniger als 5 Welt-Artikel")

    if not audio_ok:
        issues.append("WARNUNG: Audio-Generierung fehlgeschlagen")

    if not html or len(html) < 1000:
        issues.append("KRITISCH: HTML zu kurz oder leer")

    for issue in issues:
        logger.warning(f"VALIDIERUNG: {issue}")

    critical = [i for i in issues if i.startswith("KRITISCH")]
    return len(critical) == 0, issues


# ─── MAIN ──────────────────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info("HELGE MORNING BRIEFING")
    logger.info("=" * 50)

    now = datetime.now()
    datum_lang = datum_formatiert(now)
    datum_str = f"{now.day}. {MONATE[now.month]} {now.year}"
    today_str = now.strftime("%Y-%m-%d")

    logger.info(f"Datum: {datum_lang}")

    # Verzeichnisse erstellen
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIV_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Daten sammeln
    lage = gather_medienlandschaft(datum_str)
    welt = gather_welt_uebersicht(datum_str)

    if not lage and not welt:
        logger.error("Keine Daten — Abbruch")
        sys.exit(1)

    # 2. Audio-Skript + Audio
    audio_script = generate_audio_script(lage, welt, datum_str, datum_lang)
    audio_path = DOCS_DIR / "briefing.mp3"
    audio_ok = generate_audio(audio_script, audio_path) if audio_script else False

    # 3. HTML rendern
    html = render_html(lage, welt, datum_lang, audio_ok)

    # 4. Validierung — Doppelcheck
    is_valid, issues = validate_briefing(lage, welt, audio_ok, html)
    if not is_valid:
        logger.error("VALIDIERUNG FEHLGESCHLAGEN — Briefing wird NICHT versendet")
        logger.error(f"Issues: {issues}")
        sys.exit(1)

    # 5. Dateien speichern
    html_path = DOCS_DIR / "index.html"
    html_path.write_text(html, encoding="utf-8")
    logger.info(f"HTML: {html_path}")

    # Audio-Skript speichern
    if audio_script:
        (DOCS_DIR / "audio-skript.txt").write_text(audio_script, encoding="utf-8")

    # Metadaten
    meta = {
        "generated_at": now.isoformat(),
        "datum": datum_lang,
        "audio_generated": audio_ok,
        "issues": issues,
    }
    (DOCS_DIR / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # 6. Archiv — Kopie des heutigen Briefings
    archiv_html = ARCHIV_DIR / f"{today_str}.html"
    shutil.copy2(html_path, archiv_html)
    if audio_ok:
        shutil.copy2(audio_path, ARCHIV_DIR / f"{today_str}.mp3")
    logger.info(f"Archiv: {archiv_html}")

    # 7. E-Mail senden
    email_ok = send_email(html, audio_path if audio_ok else None, datum_lang)

    # 8. GitHub Pages Push
    push_ok = push_to_github()

    # Zusammenfassung
    logger.info("=" * 50)
    logger.info("FERTIG!")
    logger.info(f"  HTML: {'OK' if html else 'FEHLER'}")
    logger.info(f"  Audio: {'OK' if audio_ok else 'FEHLER'}")
    logger.info(f"  E-Mail: {'OK' if email_ok else 'FEHLER/SKIP'}")
    logger.info(f"  GitHub: {'OK' if push_ok else 'FEHLER/SKIP'}")
    if issues:
        logger.info(f"  Warnungen: {issues}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
