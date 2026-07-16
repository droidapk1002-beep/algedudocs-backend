# =============================================================================
#  dzexams.py — Adaptateur DzExams (basé sur generer_base.py + telecharger_pdf.py)
# =============================================================================

import re, time, json
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

from .utils import slugify, detecter_annee, detecter_filiere, detecter_trimestre, chemin_sur, ecrire_manifeste, LABELS_TRIMESTRE, USER_AGENT, job_manager

BASE = "https://www.dzexams.com"
SOUS_PAGES_TRIM = ["t1", "t2", "t3", "acquis", "homeworks"]

# ── HTTP session (fast, no browser) ──────────────────────────────
_HTTP_SESSION = None
def _session():
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        import requests
        _HTTP_SESSION = requests.Session()
        _HTTP_SESSION.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ar,fr;q=0.9,en;q=0.8",
        })
        _HTTP_SESSION.max_redirects = 5
    return _HTTP_SESSION

def _http_get(url, timeout=15):
    r = _session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

CYCLES = {
    "primaire": {
        "label": "Primaire",
        "niveaux": {
            "0ap": "Classe Préparatoire", "1ap": "1ère Année Primaire",
            "2ap": "2ème Année Primaire", "3ap": "3ème Année Primaire",
            "4ap": "4ème Année Primaire", "5ap": "5ème Année Primaire",
            "bep": "Certif. Primaire",
        },
    },
    "moyen": {
        "label": "Moyen",
        "niveaux": {
            "1am": "1ère Année Moyenne", "2am": "2ème Année Moyenne",
            "3am": "3ème Année Moyenne", "4am": "4ème Année Moyenne",
            "bem": "Certif. BEM",
        },
    },
    "lycee": {
        "label": "Lycée",
        "niveaux": {
            "1as": "1ère Année Secondaire", "2as": "2ème Année Secondaire",
            "3as": "3ème Année Secondaire", "bac": "Baccalauréat",
        },
    },
}

RE_CORRIGE = re.compile(r'corrig[eé]', re.I)
RE_CORRIGE_OK = re.compile(r'[\u2705]')     # ✅ = corrigé
RE_CORRIGE_NON = re.compile(r'[\u274c]')    # ❌ = non corrigé
RE_NUM_SUJ = re.compile(r'Sujet\s+N[°o]?\s*:?\s*(\d+)', re.I)


def lister_cycles():
    return CYCLES


def _creer_contexte(pw):
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--disable-dev-shm-usage"])
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 768},
        locale="fr-FR",
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return browser, ctx


def _nav(page, url, attente=2, job_id=None):
    """Navigue vers une URL avec retry, et journalise précisément ce qui se
    passe pour faciliter le diagnostic si le site est inaccessible/bloque."""
    if job_id:
        job_manager.emit(job_id, "log", {"msg": f"   …chargement de {url}"})
    derniere_erreur = None
    for tentative in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            derniere_erreur = None
            break
        except PWTimeout:
            derniere_erreur = "timeout (20s) — le site met trop de temps à répondre"
        except Exception as e:
            derniere_erreur = f"{type(e).__name__}: {e}"
        if job_id:
            job_manager.emit(job_id, "log", {
                "msg": f"   ⚠ tentative {tentative + 1}/2 échouée ({derniere_erreur}), nouvelle tentative..."})
        time.sleep(2)
    if derniere_erreur:
        raise RuntimeError(f"Impossible de charger {url} : {derniere_erreur}")

    time.sleep(attente)
    try:
        titre = (page.title() or "").lower()
    except Exception:
        titre = ""
    if "robot" in titre or "verify" in titre or "captcha" in titre:
        if job_id:
            job_manager.emit(job_id, "log", {"msg": "   ⚠ page de vérification anti-bot détectée, nouvelle tentative dans 6s..."})
        time.sleep(6)
        try:
            page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(3)


def _nettoyer_label(texte_brut, slug):
    texte = re.sub(re.escape(slug), " ", texte_brut, count=1, flags=re.I).strip()
    texte = re.sub(r'\s*Documents?\s*:?\s*\d+.*$', '', texte, flags=re.I).strip()
    texte = re.sub(r'\s*:\s*\d+.*$', '', texte).strip()
    texte = re.sub(r'\s+\d+$', '', texte).strip()
    return texte if texte else slug.capitalize()


def _collecter_matieres(niveau_code, job_id=None):
    url = f"{BASE}/fr/{niveau_code.lower()}"
    if job_id:
        job_manager.emit(job_id, "log", {"msg": f"   ← chargement {url}"})
    html = _http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(rf'/{niveau_code.lower()}/([^/\s"\']+)$', re.I)
    matieres = {}
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            slug = m.group(1).lower()
            if slug in ("cours", "documents", "activites", "jsons", "upload",
                        "faqs", "news", "advices", "profile"):
                continue
            label = _nettoyer_label(a.get_text(separator=" ", strip=True), slug)
            if slug not in matieres:
                matieres[slug] = label
    return matieres


def _collecter_sous_pages(niveau_code, slug_matiere, job_id=None):
    url = f"{BASE}/fr/{niveau_code.lower()}/{slug_matiere}"
    if job_id:
        job_manager.emit(job_id, "log", {"msg": f"   ← {slug_matiere}"})
    html = _http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    sous = {}
    pattern = re.compile(rf'/{niveau_code.lower()}/{slug_matiere}/([^/\s"\']+)$', re.I)
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            sous[m.group(1).lower()] = a.get_text(strip=True)
    if not sous:
        for sp in SOUS_PAGES_TRIM:
            sous[sp] = sp
    return sous


def _construire_fiche(titre, url, matiere, slug_trim, label_trim):
    trim_map = {"t1": 1, "t2": 2, "t3": 3, "acquis": 0, "homeworks": 0}
    trimestre = trim_map.get(slug_trim)
    if trimestre is None:
        # Fallback : détection depuis le label (ex: "Trimestre 1", "الفصل الأول")
        trimestre = detecter_trimestre(f"{label_trim} {slug_trim}")
    if RE_CORRIGE_NON.search(titre):
        corrige = False
    elif RE_CORRIGE_OK.search(titre):
        corrige = True
    else:
        corrige = bool(RE_CORRIGE.search(titre))
    annee = detecter_annee(titre)
    m_num = RE_NUM_SUJ.search(titre)
    num_sujet = int(m_num.group(1)) if m_num else 0
    type_doc = "D"
    if "examen" in titre.lower() or "test" in titre.lower() or \
       "acquis" in slug_trim or "اختبار" in titre:
        type_doc = "E"
    return {
        "titre": titre, "url": url, "matiere": matiere,
        "slug_trim": slug_trim, "label_trim": label_trim,
        "trimestre": trimestre, "type_doc": type_doc,
        "corrige": corrige, "annee": annee, "num_sujet": num_sujet,
        "filiere": detecter_filiere(titre),
        "site": "dzexams",
    }


def _collecter_sujets(niveau_code, slug_matiere, label_matiere, slug_trim, label_trim, job_id=None):
    url = f"{BASE}/fr/{niveau_code.lower()}/{slug_matiere}/{slug_trim}"
    if job_id:
        job_manager.emit(job_id, "log", {"msg": f"   ← {slug_matiere}/{slug_trim}"})
    html = _http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    fiches = []

    # Liens directs vers /sujets/... (cas le plus courant, rapide).
    liens_sujets = soup.find_all("a", href=re.compile(r'/sujets/', re.I))
    if liens_sujets:
        for a in liens_sujets:
            href = a["href"].strip()
            if not href.startswith("http"):
                href = BASE + href
            titre = a.get_text(separator=" ", strip=True)
            if not titre or len(titre) < 5:
                continue
            fiches.append(_construire_fiche(titre, href, label_matiere, slug_trim, label_trim))
        return fiches

    # Fallback : liens data-id (encodés en base64)
    for a in soup.find_all("a", href=True):
        data_id = a.get("data-id") or ""
        if not data_id:
            continue
        try:
            import base64
            decoded = base64.b64decode(data_id).decode("latin1")
            decoded = "".join(chr(ord(c) - 8) for c in decoded)
            href = f"{BASE}/ar/sujets/{decoded}"
        except Exception:
            continue
        titre = a.get_text(separator=" ", strip=True)
        if titre and len(titre) >= 5:
            fiches.append(_construire_fiche(titre, href, label_matiere, slug_trim, label_trim))
    if fiches:
        return fiches

    # Fallback : deviner depuis le numéro dans le titre
    for a in soup.find_all(["a", "div", "li"], text=RE_NUM_SUJ):
        titre = a.get_text(separator=" ", strip=True)
        m = RE_NUM_SUJ.search(titre)
        if m:
            num = m.group(1)
            href = f"{BASE}/fr/{niveau_code.lower()}/{slug_matiere}/sujets/{num}"
            fiches.append(_construire_fiche(titre, href, label_matiere, slug_trim, label_trim))
    return fiches


def lister_matieres(job_id, cycle, niveau_code):
    """Récupère la liste des matières (HTTP rapide). Retourne dict {slug: label}."""
    return _collecter_matieres(niveau_code, job_id=job_id)


def decouvrir(job_id, cycle, niveau_code, matieres_filtre=None):
    """
    Découvre toutes les fiches pour un niveau DzExams.
    Utilise les requêtes HTTP (requests + BS4) — beaucoup plus rapide que
    Playwright pour la découverte pure (pas de navigateur).
    """
    import threading as th

    toutes = []
    job_manager.emit(job_id, "log", {"msg": f"📋 Collecte des matières pour {niveau_code.upper()}..."})
    try:
        matieres = _collecter_matieres(niveau_code, job_id=job_id)
    except Exception as e:
        job_manager.emit(job_id, "log", {"msg": f"✗ Impossible d'accéder à dzexams.com : {e}"})
        return []

    if matieres_filtre:
        matieres = {s: l for s, l in matieres.items() if s in matieres_filtre}
    if not matieres:
        job_manager.emit(job_id, "log", {"msg": "✗ Aucune matière trouvée."})
        return []
    job_manager.emit(job_id, "log", {"msg": f"✅ {len(matieres)} matières détectées."})

    total = len(matieres)
    cpt_lock = th.Lock()
    cpt = {"fait": 0}

    for idx, (slug_mat, label_mat) in enumerate(matieres.items(), 1):
        if job_manager.is_cancelled(job_id):
            break
        job_manager.emit(job_id, "log", {"msg": f"  🔍 [{idx}/{total}] {label_mat}..."})
        resultats_matiere = []
        try:
            sous = _collecter_sous_pages(niveau_code, slug_mat, job_id=job_id)
        except Exception as e:
            job_manager.emit(job_id, "log", {"msg": f"  ✗ {label_mat} : {e}"})
            continue
        for slug_trim, label_trim in sous.items():
            if job_manager.is_cancelled(job_id):
                break
            try:
                fiches = _collecter_sujets(niveau_code, slug_mat, label_mat, slug_trim, label_trim, job_id=job_id)
            except Exception as e:
                job_manager.emit(job_id, "log", {"msg": f"  ✗ {label_mat}/{slug_trim} : {e}"})
                continue
            resultats_matiere.extend(fiches)
            job_manager.emit(job_id, "log", {"msg": f"  → {label_mat}/{slug_trim} : {len(fiches)} fiches"})
        with cpt_lock:
            cpt["fait"] += 1
            job_manager.emit(job_id, "progress", {"phase": "discover", "current": cpt["fait"], "total": total, "label": label_mat})
        toutes.extend(resultats_matiere)
    return toutes


SELS_DL = [
    "a[href*='.pdf']", "a[download]", "a[href*='download']",
    "a[href*='telecharger']", "a:has-text('Télécharger')",
    "a:has-text('تحميل')", "a:has-text('Download')",
    "button:has-text('PDF')", ".btn-download", ".download-btn",
    "#downloadBtn", "[data-action='download']", "a[href*='/pdf/']",
]


def _trouver_bouton_dl(page):
    for sel in SELS_DL:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            pass
    return None


def _telecharger_fiche(page, fiche, dest: Path):
    if dest.exists():
        return True, "déjà présent"
    url_sujet = fiche["url"]
    if not url_sujet or url_sujet == BASE:
        return False, "URL manquante"
    try:
        page.goto(url_sujet, wait_until="domcontentloaded", timeout=25000)
        time.sleep(1.5)
    except PWTimeout:
        return False, "timeout chargement"

    bouton = _trouver_bouton_dl(page)
    if not bouton:
        return False, "bouton introuvable"

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with page.expect_download(timeout=90000) as dl_info:
            bouton.scroll_into_view_if_needed(timeout=3000)
            time.sleep(0.3)
            bouton.click(force=True, timeout=6000)
        dl = dl_info.value
        try:
            dl.save_as(str(dest))
        except OSError:
            # Probable dépassement de MAX_PATH malgré nos garde-fous (chemin
            # racine choisi par l'utilisateur particulièrement long) : on
            # retente avec un nom de fichier minimal, à la racine du dossier
            # matière plutôt que dans le sous-dossier trimestre.
            dest_secours = dest.parent.parent / f"sujet_{abs(hash(str(dest))) % 100000}.pdf"
            dl.save_as(str(dest_secours))
            dest = dest_secours
        taille = dest.stat().st_size
        if taille < 500:
            dest.unlink(missing_ok=True)
            return False, "fichier vide"
        return True, f"{taille // 1024} Ko"
    except PWTimeout:
        return False, "timeout téléchargement"
    except Exception as e:
        return False, str(e)


def destination(fiche, racine: Path, separer_corrige=True) -> Path:
    tl = "Devoir" if fiche["type_doc"] == "D" else "Examen" if fiche["type_doc"] == "E" else "Autre"
    matiere = slugify(fiche["matiere"]) or "Autres"
    trim = LABELS_TRIMESTRE.get(fiche.get("trimestre", 0), "Autres")
    base_nom = slugify(fiche["titre"]) or "fiche"
    if separer_corrige:
        segments = (trim, matiere, tl,
                     "Avec_corrigé" if fiche["corrige"] else "Sans_corrigé", base_nom)
    else:
        segments = (trim, matiere, tl, base_nom)
    return chemin_sur(racine, *segments, extension=".pdf")


def telecharger(job_id, fiches, racine: Path, niveau_label="", separer_corrige=True):
    """
    Télécharge les fiches en parallèle (plusieurs onglets du même navigateur)
    pour réduire fortement le temps total — chaque téléchargement implique
    une navigation + une attente de clic, payées auparavant séquentiellement.
    """
    import concurrent.futures
    import threading as th

    ok = err = 0
    entrees = []
    total = len(fiches)
    compteur_lock = th.Lock()

    def traiter_fiche(f):
        nonlocal ok, err
        if job_manager.is_cancelled(job_id):
            return None
        with sync_playwright() as pw:
            browser, ctx = _creer_contexte(pw)
            page_locale = ctx.new_page()
            try:
                dest = destination(f, racine, separer_corrige)
                reussi, info = _telecharger_fiche(page_locale, f, dest)
            finally:
                page_locale.close()
                browser.close()
        with compteur_lock:
            if reussi:
                ok += 1
            else:
                err += 1
            courant = ok + err
            job_manager.emit(job_id, "progress", {
                "phase": "download", "current": courant, "total": total,
                "ok": ok, "err": err, "titre": f["titre"][:60],
                "success": reussi, "info": info,
            })
        return {"fiche": f, "chemin": dest if reussi else None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for resultat in pool.map(traiter_fiche, fiches):
            if resultat is not None:
                entrees.append(resultat)

    manifeste = ecrire_manifeste(racine, "dzexams", niveau_label, entrees)
    return {"ok": ok, "err": err, "manifeste": str(manifeste)}
