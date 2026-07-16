# =============================================================================
#  eddirasa.py — Adaptateur Eddirasa.com (WordPress)
#  Structure : /ens-<cycle>/<niveau>/  →  /ens-<cycle>/<niveau>/<matiere>/
#              →  fiche individuelle  →  lien PDF direct wp-content/uploads/*.pdf
#
#  Le site est protégé par une protection anti-bot (Cloudflare ou équivalent).
#  On essaie d'abord `requests` (rapide, parallélisable) ; si le site bloque
#  (403/429/503), on bascule automatiquement sur un vrai navigateur Playwright
#  pour la suite de la découverte (séquentiel, plus lent mais fiable).
# =============================================================================

import re
import time
import concurrent.futures
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from .utils import slugify, detecter_annee, detecter_corrige, detecter_type_doc, detecter_trimestre, detecter_filiere, chemin_sur, ecrire_manifeste, LABELS_TRIMESTRE, USER_AGENT, job_manager

BASE = "https://eddirasa.com"

NIVEAUX = {
    "primaire": {
        "preparatory": "القسـم التحضيـري", "first-primary": "1ère Année Primaire",
        "second-primary": "2ème Année Primaire", "third-primary": "3ème Année Primaire",
        "fourth-primary": "4ème Année Primaire", "fifth-primary": "5ème Année Primaire",
    },
    "moyen": {
        "1am": "1ère Année Moyenne", "2am": "2ème Année Moyenne",
        "3am": "3ème Année Moyenne", "4am": "4ème Année Moyenne",
    },
    "lycee": {
        "1as": "1ère Année Secondaire", "2as": "2ème Année Secondaire",
        "3as": "3ème Année Secondaire",
    },
}

# Filet de sécurité ultime : sur certaines pages niveau, le site n'affiche
# les matières ni en vrais liens <a>, ni en shortcode [button] (juste en
# texte descriptif simple, ex: "الرياضيات اللغة العربية ..." sans liens du
# tout). Dans ce cas, on retombe sur cette liste de slugs connus, vérifiée
# par recherche directe sur le site pour chaque cycle. Les pages individuelles
# /ens-<cycle>/<niveau>/<slug>/ existent même si elles ne sont pas linkées
# depuis la page niveau elle-même.
MATIERES_CONNUES = {
    "primaire": {
        "maths": "الرياضيات", "arabic": "اللغة العربية", "islam": "التربية الإسلامية",
        "science": "التربية العلمية والتكنولوجية", "mond": "التربية المدنية",
        "music": "التربية الموسيقية", "art": "التربية الفنية",
        "francais": "اللغة الفرنسية", "english": "اللغة الإنجليزية", "amazigh": "اللغة الأمازيغية",
    },
    "moyen": {
        "maths": "الرياضيات", "arabic": "اللغة العربية", "science": "علوم الطبيعة والحياة",
        "physic": "العلوم الفيزيائية", "francais": "اللغة الفرنسية", "english": "اللغة الإنجليزية",
        "mond": "التربية المدنية", "his-geo": "التاريخ والجغرافيا", "islam": "التربية الإسلامية",
        "informatique": "الإعلام الآلي", "art": "التربية الفنية", "music": "التربية الموسيقية",
        "amazigh": "اللغة الأمازيغية",
    },
    "lycee": {
        "maths": "الرياضيات", "arabic": "اللغة العربية", "sciences": "علوم الطبيعة والحياة",
        "physics": "العلوم الفيزيائية", "francais": "اللغة الفرنسية", "english": "اللغة الإنجليزية",
        "islamic": "العلوم الإسلامية", "his-geo": "التاريخ والجغرافيا", "philo": "الفلسفة",
    },
}

PREFIXE_CYCLE = {"primaire": "ens-pri", "moyen": "ens-cm", "lycee": "ens-sec"}


def lister_cycles():
    return {c: {"label": c, "niveaux": NIVEAUX[c]} for c in NIVEAUX}


# ── Récupération HTML avec bascule automatique requests -> Playwright ──────

class _Acces:
    """État partagé : session requests + navigateur Playwright (créé à la demande)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.mode_navigateur = False
        self._pw = None
        self._browser = None
        self._page = None

    def _navigateur(self):
        if self._page:
            return self._page
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"])
        ctx = self._browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self._page = ctx.new_page()
        return self._page

    def get(self, url, retries=2, job_id=None):
        if self.mode_navigateur:
            return self._get_navigateur(url, job_id=job_id)
        for tentative in range(retries + 1):
            try:
                r = self.session.get(url, timeout=20)
                if r.status_code == 200:
                    if self._semble_etre_un_challenge(r.text):
                        if job_id:
                            job_manager.emit(job_id, "log", {
                                "msg": f"     ↻ page de vérification détectée (accès rapide), bascule vers navigateur complet..."})
                        self.mode_navigateur = True
                        return self._get_navigateur(url, job_id=job_id)
                    return r.text
                if r.status_code in (403, 429, 503):
                    if job_id:
                        job_manager.emit(job_id, "log", {
                            "msg": f"     ↻ HTTP {r.status_code} (accès rapide), bascule vers navigateur complet..."})
                    self.mode_navigateur = True
                    return self._get_navigateur(url, job_id=job_id)
            except requests.RequestException as e:
                if job_id:
                    job_manager.emit(job_id, "log", {
                        "msg": f"     ⚠ tentative {tentative + 1}/{retries + 1} échouée ({type(e).__name__})"})
            time.sleep(1.0)
        # Dernier recours : tenter le navigateur avant d'abandonner
        if job_id:
            job_manager.emit(job_id, "log", {"msg": "     ↻ accès rapide épuisé, tentative navigateur complet..."})
        self.mode_navigateur = True
        return self._get_navigateur(url, job_id=job_id)

    @staticmethod
    def _semble_etre_un_challenge(html):
        """Détecte une page de vérification anti-bot (Cloudflare ou
        équivalent) reçue avec un statut 200 mais sans le vrai contenu —
        reconnaissable par sa taille très réduite et des mots-clés typiques."""
        if not html or len(html) > 6000:
            return False
        indices = ("just a moment", "attention required", "checking your browser",
                   "cf-browser-verification", "cloudflare", "captcha", "ddos-guard")
        html_l = html.lower()
        return any(ind in html_l for ind in indices)

    def _get_navigateur(self, url, job_id=None):
        try:
            page = self._navigateur()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.2)
            titre = (page.title() or "").lower()
            if "just a moment" in titre or "attention" in titre:
                if job_id:
                    job_manager.emit(job_id, "log", {
                        "msg": f"     ↻ challenge anti-bot affiché par le navigateur, nouvelle tentative dans 5s..."})
                time.sleep(5)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                time.sleep(1.5)
            html = page.content()
            if job_id:
                titre_final = (page.title() or "")[:60]
                encore_bloque = self._semble_etre_un_challenge(html)
                job_manager.emit(job_id, "log", {
                    "msg": f"     📄 page reçue ({len(html)} car., titre: {titre_final!r})"
                           + (" — ⚠ ressemble toujours à une page de vérification" if encore_bloque else "")})
            return html
        except Exception as e:
            if job_id:
                job_manager.emit(job_id, "log", {"msg": f"     ✗ navigateur : {type(e).__name__}: {e}"})
            return None

    def fermer(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


RE_SHORTCODE_BUTTON = re.compile(
    r'\[button\s+link=(?:&quot;|[\"\u201d])?\s*(https?://[^\"\u201d\s\]]+?)(?:&quot;|[\"\u201d])?\s+text=(?:&quot;|[\"\u201d])?([^\"\u201d\]]+?)(?:&quot;|[\"\u201d])?\s+sizes',
    re.I)


def _liens_depuis_shortcodes(texte_page):
    """
    Le site utilise un plugin de boutons (shortcode [button link="..."
    text="..."]) pour afficher les liens de matières. Sur certaines requêtes
    (notamment via accès rapide sans rendu JS/PHP complet), ce shortcode
    n'est pas traité et apparaît tel quel en texte brut dans la page — il
    faut alors l'extraire directement par regex plutôt que de chercher un
    vrai <a href>. Les guillemets peuvent être droits ("), courbes (”) ou
    échappés en entité HTML (&quot;) selon le contexte de rendu.
    Retourne une liste de (url, texte).
    """
    resultats = []
    for m in RE_SHORTCODE_BUTTON.finditer(texte_page):
        url = m.group(1).rstrip('".,؛\u201d')
        texte = m.group(2).strip().rstrip('".,؛\u201d').strip()
        resultats.append((url, texte))
    return resultats


RE_LIEN_EXAMENS = re.compile(r'إختبارات|اختبارات|exams?', re.I)


def _trouver_page_examens(acces, cycle, niveau_slug, job_id=None):
    """
    Cherche, depuis la page niveau, le lien vers la page d'examens centralisée
    (ex: /exams/exams-1am/ ou /exams/1am-exams/ — le site utilise les deux
    conventions selon les niveaux). Retourne l'URL absolue ou None.
    """
    prefixe = PREFIXE_CYCLE[cycle]
    url = f"{BASE}/{prefixe}/{niveau_slug}/"
    html = acces.get(url, job_id=job_id)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    # 1) Vrais liens <a> vers /exams/...
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/exams" in href.lower() and RE_LIEN_EXAMENS.search(href + " " + a.get_text()):
            return href

    # 2) Filet de sécurité : shortcode [button] non rendu en HTML
    for lien_url, texte in _liens_depuis_shortcodes(html):
        if "/exams" in lien_url.lower() or RE_LIEN_EXAMENS.search(texte):
            return lien_url

    return None


def _collecter_matieres(acces, cycle, niveau_slug, job_id=None):
    """Page niveau -> liste de liens matières (icônes ou boutons shortcode)."""
    prefixe = PREFIXE_CYCLE[cycle]
    url = f"{BASE}/{prefixe}/{niveau_slug}/"
    html = acces.get(url, job_id=job_id)
    if not html:
        if job_id:
            job_manager.emit(job_id, "log", {"msg": f"     ✗ page niveau totalement inaccessible : {url}"})
        return {}
    soup = BeautifulSoup(html, "html.parser")
    matieres = {}
    tous_hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    total_liens = len(tous_hrefs)
    # Le slug capturé ne doit contenir ni '#' (ancres de commentaires type
    # #comment-87003 ou #respond) ni '?' (paramètres de requête) — seulement
    # un vrai segment de chemin alphanumérique/tirets. On accepte le chemin
    # qu'il soit relatif ou absolu, et qu'il soit sur eddirasa.com ou .net
    # (le site utilise les deux domaines, parfois mélangés sur une même page).
    pattern = re.compile(rf'/{prefixe}/{re.escape(niveau_slug)}/([a-zA-Z0-9_\-]+)/?(?:$|[?#])')
    EXCLUS = {"average", "respond", "comments", "feed"}

    for href in tous_hrefs:
        m = pattern.search(href)
        if m:
            slug = m.group(1)
            if slug in EXCLUS:
                continue
            a_tag = next((a for a in soup.find_all("a", href=href)), None)
            label = (a_tag.get_text(strip=True) if a_tag else "") or slug
            label = re.sub(r'\s+', ' ', label).strip()
            if slug not in matieres:
                matieres[slug] = label

    # Filet de sécurité : si le plugin de boutons n'a pas été rendu en HTML,
    # le shortcode [button link="..." text="..."] apparaît en texte brut.
    if not matieres:
        for lien_url, texte in _liens_depuis_shortcodes(html):
            m = pattern.search(lien_url)
            if m:
                slug = m.group(1)
                if slug in EXCLUS:
                    continue
                if slug not in matieres:
                    matieres[slug] = texte

    # Dernier filet de sécurité : ni vrais liens ni shortcodes (page purement
    # textuelle, observé sur certains niveaux). On retombe sur la liste de
    # slugs connus pour ce cycle, en vérifiant rapidement que chaque page
    # existe réellement pour CE niveau avant de la proposer (évite de lister
    # des matières qui n'existeraient pas à ce niveau précis).
    if not matieres and cycle in MATIERES_CONNUES:
        if job_id:
            job_manager.emit(job_id, "log", {
                "msg": f"     ↻ aucun lien de matière exploitable sur la page, "
                       f"vérification d'une liste de matières connues pour ce cycle..."})
        candidats = list(MATIERES_CONNUES[cycle].items())

        def _verifier(slug_label):
            slug, label = slug_label
            url_test = f"{BASE}/{prefixe}/{niveau_slug}/{slug}/"
            html_test = acces.get(url_test)
            return slug, label, bool(html_test and len(html_test) > 1000)

        if acces.mode_navigateur:
            resultats_verif = [_verifier(c) for c in candidats]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                resultats_verif = list(pool.map(_verifier, candidats))

        for slug, label, existe in resultats_verif:
            if existe:
                matieres[slug] = label

        if job_id:
            job_manager.emit(job_id, "log", {
                "msg": f"     ✓ {len(matieres)}/{len(candidats)} matières connues confirmées existantes pour ce niveau."})

    if job_id and not matieres:
        echantillon = [h for h in tous_hrefs if niveau_slug in h][:8] or tous_hrefs[:8]
        job_manager.emit(job_id, "log", {
            "msg": f"     ⚠ 0 matière sur {total_liens} liens trouvés sur {url}"})
        job_manager.emit(job_id, "log", {
            "msg": f"     🔍 échantillon de liens contenant '{niveau_slug}' : " + " | ".join(echantillon)})

    return matieres



def lister_matieres(job_id, cycle, niveau_slug):
    """Récupère uniquement la liste des matières (rapide, pour pré-filtrage)."""
    acces = _Acces()
    try:
        return _collecter_matieres(acces, cycle, niveau_slug, job_id=job_id)
    finally:
        acces.fermer()


_EXCLUS_RE = re.compile(
    r'/(ens-sec|ens-cm|ens-pri|ens-uni|cat|tag|forum|forums|post|questions|'
    r'upload|contactez-nous|about|privacy-policy|bac|bem|cinq|livre-|books-)'
    r'(/|$)', re.I)


def _collecter_fiches(acces, cycle, niveau_slug, matiere_slug, matiere_label=None,
                       job_id=None, url_examens_niveau=None):
    """
    Liste les fiches référencées sur la page d'une matière. Essaie d'abord
    sous la page d'examens centralisée (/exams/exams-<niveau>/<matiere>/ ou
    variante) si son URL de base est connue, car c'est généralement là que
    se trouvent les devoirs/examens téléchargeables — les pages /ens-.../
    étant plutôt des pages de cours sans fiches PDF pour certains niveaux.
    Le slug de la matière peut différer entre les deux arborescences (ex:
    "maths" sous /ens-.../ mais "math" sous /exams/...) : on cherche donc le
    lien réel vers la matière depuis la page d'examens niveau plutôt que de
    deviner le même slug. Si rien n'y est trouvé, retombe sur l'ancien
    chemin /ens-.../ par cohérence avec les niveaux où celui-ci fonctionne.
    """
    prefixe = PREFIXE_CYCLE[cycle]
    candidats_urls = []

    if url_examens_niveau:
        url_matiere_examens = _trouver_lien_matiere_dans_page(
            acces, url_examens_niveau, matiere_label or matiere_slug, job_id=job_id)
        if url_matiere_examens:
            candidats_urls.append(url_matiere_examens)
        else:
            # Repli : suppose le même slug sous la page d'examens
            candidats_urls.append(f"{url_examens_niveau.rstrip('/')}/{matiere_slug}/")

    candidats_urls.append(f"{BASE}/{prefixe}/{niveau_slug}/{matiere_slug}/")

    for url in candidats_urls:
        html = acces.get(url, job_id=job_id)
        base_path = urlparse(url).path
        fiches = _extraire_fiches_depuis_html(html, url, base_path)

        if not fiches and not acces.mode_navigateur:
            if job_id:
                job_manager.emit(job_id, "log", {
                    "msg": f"     ↻ 0 fiche en accès rapide sur {url}, nouvelle tentative avec navigateur complet..."})
            html_navigateur = acces._get_navigateur(url, job_id=job_id)
            fiches = _extraire_fiches_depuis_html(html_navigateur, url, base_path)

        if fiches:
            return fiches

    if job_id:
        job_manager.emit(job_id, "log", {
            "msg": f"     ⚠ 0 fiche trouvée pour cette matière (testé : {', '.join(candidats_urls)})"})

    return []


def _trouver_lien_matiere_dans_page(acces, url_page_examens, matiere_label, job_id=None):
    """
    Cherche, dans la page d'examens niveau, le lien dont le texte correspond
    le mieux au label de la matière recherchée (comparaison sur les premiers
    mots significatifs, car les libellés peuvent légèrement varier).
    """
    html = acces.get(url_page_examens, job_id=job_id)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    mots_cle = set(matiere_label.split()[:2])  # 1-2 premiers mots du label suffisent généralement
    if not mots_cle:
        return None

    meilleur = None
    for a in soup.find_all("a", href=True):
        texte = a.get_text(strip=True)
        if not texte:
            continue
        if any(mot in texte for mot in mots_cle):
            meilleur = a["href"]
            break

    if not meilleur:
        # Filet de sécurité shortcode
        for lien_url, texte in _liens_depuis_shortcodes(html):
            if any(mot in texte for mot in mots_cle):
                meilleur = lien_url
                break

    return meilleur


def _extraire_fiches_depuis_html(html, url, base_path):
    """
    Extrait les liens de fiches (devoirs/examens) contenus dans la page.
    `base_path` est le chemin de la page elle-même (pour l'exclure des
    résultats) ; accepté tel quel, indépendamment du préfixe/niveau/matière
    d'origine, pour fonctionner aussi bien avec /ens-cm/.../  qu'avec la
    page d'examens centralisée /exams/exams-.../  dont le format peut varier.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    contenu = soup.select_one("article, .entry-content, .post-content, main") or soup

    fiches = []
    vus = set()
    base_path = base_path.rstrip("/")

    for a in contenu.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(("http://", "https://", "/")):
            continue
        href_abs = urljoin(url, href)
        host = urlparse(href_abs).netloc
        if "eddirasa.com" not in host and "eddirasa.net" not in host:
            continue
        chemin = urlparse(href_abs).path.rstrip("/")
        if chemin == base_path or _EXCLUS_RE.search(chemin):
            continue
        titre = a.get_text(strip=True)
        if not titre or len(titre) < 3 or href_abs in vus:
            continue
        vus.add(href_abs)
        fiches.append({"url": href_abs, "titre": titre})

    return fiches


def _extraire_pdf_fiche(acces, url_fiche):
    """Ouvre la page d'une fiche et cherche le vrai lien PDF (bouton '.: تحميل :.')."""
    html = acces.get(url_fiche)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r'wp-content/uploads/.*\.pdf$', href, re.I):
            return urljoin(url_fiche, href)

    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return urljoin(url_fiche, a["href"])

    for a in soup.find_all("a", href=True):
        m = re.search(r'[?&]file=([^&"\']+\.pdf)', a["href"], re.I)
        if m:
            return unquote(m.group(1))

    return None


def decouvrir(job_id, cycle, niveau_slug, matieres_filtre=None):
    """Découvre les fiches (PDF) pour un niveau eddirasa."""
    acces = _Acces()
    toutes = []
    try:
        matieres = _collecter_matieres(acces, cycle, niveau_slug, job_id=job_id)
        if matieres_filtre:
            matieres = {s: l for s, l in matieres.items() if s in matieres_filtre}
        if not matieres:
            job_manager.emit(job_id, "log", {"msg": "✗ Aucune matière trouvée sur eddirasa."})
            return []

        job_manager.emit(job_id, "log", {"msg": f"✅ {len(matieres)} matières détectées (eddirasa)."})

        url_examens_niveau = _trouver_page_examens(acces, cycle, niveau_slug, job_id=job_id)
        if url_examens_niveau:
            job_manager.emit(job_id, "log", {
                "msg": f"📂 Page d'examens centralisée trouvée : {url_examens_niveau}"})

        total = len(matieres)
        for idx, (slug_mat, label_mat) in enumerate(matieres.items(), 1):
            if job_manager.is_cancelled(job_id):
                break
            job_manager.emit(job_id, "progress", {
                "phase": "discover", "current": idx, "total": total, "label": label_mat})

            fiches_brutes = _collecter_fiches(acces, cycle, niveau_slug, slug_mat, matiere_label=label_mat,
                                               job_id=job_id, url_examens_niveau=url_examens_niveau)
            job_manager.emit(job_id, "log", {
                "msg": f"  → {label_mat} : {len(fiches_brutes)} fiches trouvées, extraction des PDF..."})

            if acces.mode_navigateur:
                # Navigateur unique non thread-safe : on reste séquentiel
                resultats = [(art, _extraire_pdf_fiche(acces, art["url"])) for art in fiches_brutes]
            else:
                # requests est thread-safe : on parallélise pour aller plus vite
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    futures = {pool.submit(_extraire_pdf_fiche, acces, art["url"]): art
                               for art in fiches_brutes}
                    resultats = []
                    for fut in concurrent.futures.as_completed(futures):
                        art = futures[fut]
                        try:
                            resultats.append((art, fut.result()))
                        except Exception:
                            resultats.append((art, None))

            for art, pdf_url in resultats:
                if job_manager.is_cancelled(job_id):
                    break
                if not pdf_url:
                    continue
                titre = art["titre"]
                toutes.append({
                    "titre": titre,
                    "url": pdf_url,
                    "page_url": art["url"],
                    "matiere": label_mat,
                    "trimestre": detecter_trimestre(titre),
                    "type_doc": detecter_type_doc(titre),
                    "corrige": detecter_corrige(titre),
                    "annee": detecter_annee(titre),
                    "filiere": detecter_filiere(titre),
                    "site": "eddirasa",
                })
            time.sleep(0.15)
    finally:
        acces.fermer()
    return toutes


def destination(fiche, racine: Path, separer_corrige=True) -> Path:
    tl = "Devoir" if fiche["type_doc"] == "D" else "Examen" if fiche["type_doc"] == "E" else "Documents"
    matiere = slugify(fiche["matiere"]) or "Autres"
    trim = LABELS_TRIMESTRE.get(fiche.get("trimestre", 0), "Autres")
    base_nom = slugify(fiche["titre"]) or "fiche"
    if separer_corrige:
        segments = (trim, matiere, tl,
                     "Avec_corrigé" if fiche["corrige"] else "Sans_corrigé", base_nom)
    else:
        segments = (trim, matiere, tl, base_nom)
    return chemin_sur(racine, *segments, extension=".pdf")


def _telecharger_un(session, url, dest: Path):
    if dest.exists():
        return True, "déjà présent"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = session.get(url, timeout=40, stream=True)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                fh.write(chunk)
        taille = dest.stat().st_size
        if taille < 500:
            dest.unlink(missing_ok=True)
            return False, "fichier vide"
        return True, f"{taille // 1024} Ko"
    except Exception as e:
        return False, str(e)


def telecharger(job_id, fiches, racine: Path, niveau_label="", separer_corrige=True):
    """Télécharge en parallèle (requests est thread-safe et rapide ici, donc
    on peut utiliser davantage de workers que pour les sites Playwright)."""
    import concurrent.futures
    import threading as th

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    ok = err = 0
    entrees = []
    total = len(fiches)
    compteur_lock = th.Lock()

    def traiter_fiche(f):
        nonlocal ok, err
        if job_manager.is_cancelled(job_id):
            return None
        dest = destination(f, racine, separer_corrige)
        reussi, info = _telecharger_un(session, f["url"], dest)
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for resultat in pool.map(traiter_fiche, fiches):
            if resultat is not None:
                entrees.append(resultat)

    manifeste = ecrire_manifeste(racine, "eddirasa", niveau_label, entrees)
    return {"ok": ok, "err": err, "manifeste": str(manifeste)}
