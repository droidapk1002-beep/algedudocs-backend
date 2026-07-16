# =============================================================================
#  ency.py — Adaptateur Ency-education.net
#
#  Architecture réelle du site (deux générations de sites combinées) :
#    1) Domaine principal www.ency-education.net : site Wix moderne, SPA
#       fortement dépendante du JavaScript côté client. Les liens vers les
#       pages "matière" ne sont fiables qu'après rendu JS -> on utilise
#       TOUJOURS un navigateur Playwright pour cette partie.
#    2) Sous-domaines statiques type Weebly (ex: 3as.ency-education.com),
#       liés depuis les pages matière Wix, qui listent les fروض/اختبارات
#       avec des liens PDF directs en HTML simple -> `requests` suffit et
#       est nettement plus rapide ici.
#    3) Parfois ces sous-domaines pointent encore vers de vieilles pages
#       .html individuelles (ancien modèle) qu'il faut suivre un niveau de
#       plus pour atteindre le PDF final.
# =============================================================================

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .utils import slugify, detecter_annee, detecter_corrige, detecter_type_doc, detecter_trimestre, detecter_filiere, chemin_sur, ecrire_manifeste, LABELS_TRIMESTRE, USER_AGENT, job_manager

BASE = "https://www.ency-education.net"

NIVEAUX = {
    "primaire": {"1ap": "1ère Année Primaire", "2ap": "2ème Année Primaire",
                 "3ap": "3ème Année Primaire", "4ap": "4ème Année Primaire",
                 "5ap": "5ème Année Primaire"},
    "moyen": {"1am": "1ère Année Moyenne", "2am": "2ème Année Moyenne",
              "3am": "3ème Année Moyenne", "4am": "4ème Année Moyenne"},
    "lycee": {"1as": "1ère Année Secondaire", "2as": "2ème Année Secondaire",
              "3as": "3ème Année Secondaire"},
}

RE_EXAMS_LINK = re.compile(r'exam|devoir|فروض|اختبار', re.I)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fr,ar;q=0.8"})


def lister_cycles():
    return {c: {"label": c, "niveaux": NIVEAUX[c]} for c in NIVEAUX}


# ── Accès navigateur pour le domaine Wix (toujours nécessaire, JS requis) ──

class _NavigateurWix:
    """Navigateur Playwright persistant pour parcourir le site Wix principal."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None

    def _page_ready(self):
        if self._page:
            return self._page
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"])
        ctx = self._browser.new_context(user_agent=USER_AGENT, locale="fr-FR",
                                         viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self._page = ctx.new_page()
        return self._page

    def get(self, url, attente=2.5):
        try:
            page = self._page_ready()
            page.goto(url, wait_until="networkidle", timeout=35000)
            time.sleep(attente)
            return page.content(), page.url
        except Exception:
            try:
                # Retente avec une condition de chargement plus permissive
                page = self._page_ready()
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(attente + 1)
                return page.content(), page.url
            except Exception:
                return None, None

    def fermer(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def _get_statique(url, retries=2):
    """Accès HTTP simple pour le sous-domaine statique (pas de JS nécessaire)."""
    for _ in range(retries + 1):
        try:
            r = _session.get(url, timeout=20)
            if r.status_code == 200:
                return r.text, r.url
        except requests.RequestException:
            pass
        time.sleep(1.0)
    return None, None


def _collecter_matieres(nav, niveau_slug):
    """Page niveau Wix -> liens vers les pages matières (souvent /<niveau>-<matiere>/1)."""
    url = f"{BASE}/{niveau_slug}"
    html, url_finale = nav.get(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    matieres = {}
    pattern = re.compile(rf'/{re.escape(niveau_slug)}-([a-zA-Z\-]+)/?\d*$')
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = pattern.search(href)
        if m:
            slug = m.group(1)
            label = a.get_text(strip=True) or slug
            if slug not in matieres or len(label) > len(matieres[slug]):
                matieres[slug] = label
    return matieres


def lister_matieres(job_id, cycle, niveau_slug):
    """Récupère uniquement la liste des matières (rapide, pour pré-filtrage)."""
    nav = _NavigateurWix()
    try:
        return _collecter_matieres(nav, niveau_slug)
    finally:
        nav.fermer()


def _trouver_sous_domaine(nav, niveau_slug, matiere_slug):
    """Visite la page matière Wix et cherche un lien vers le sous-domaine statique
    (ex: 3as.ency-education.com) ou vers la nouvelle banque centralisée
    (ency-education.net/exams) menant aux 'exams'."""
    url = f"{BASE}/{niveau_slug}-{matiere_slug}/1"
    html, url_finale = nav.get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    liens_exams = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        texte = a.get_text(strip=True)
        host = urlparse(href).netloc
        est_sous_domaine_legacy = "ency-education.com" in host
        est_banque_centrale = "ency-education.net" in host and "exam" in href.lower()
        if (est_sous_domaine_legacy or est_banque_centrale) and RE_EXAMS_LINK.search(href + " " + texte):
            liens_exams.append(href)
    return list(dict.fromkeys(liens_exams))


def _get_page(url, nav=None):
    """Choisit automatiquement requests (rapide, pour les sous-domaines
    statiques .com) ou le navigateur Wix (nécessaire pour .net, rendu JS)."""
    if "ency-education.net" in url and nav is not None:
        return nav.get(url)
    return _get_statique(url)


def _extraire_pdfs_page_exams(url, profondeur=0, max_profondeur=2, vus=None, nav=None):
    """
    Extrait tous les liens PDF (et autres formats téléchargeables) d'une page
    'exams' du sous-domaine statique. Suit une fois les vieilles pages .html
    intermédiaires si aucun PDF direct n'est trouvé à ce niveau, et suit
    aussi un éventuel lien de redirection vers le nouveau système si le site
    a migré son contenu (cas observé sur plusieurs niveaux).
    """
    if vus is None:
        vus = set()
    if url in vus or profondeur > max_profondeur:
        return []
    vus.add(url)

    html, url_finale = _get_page(url, nav=nav)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(url_finale or url).netloc

    resultats = []
    sous_pages_a_suivre = []
    lien_redirection = None

    # Le site a partiellement migré : certaines anciennes pages (ex:
    # 1am.ency-education.com/...) ne contiennent plus de PDF mais un texte du
    # type "بنك الفروض والاختبارات الجديد ... اضغط هنا" qui pointe vers le
    # nouveau système (souvent sur ency-education.net). On détecte ce signal
    # et on suit ce lien en priorité, même s'il change de domaine.
    RE_REDIRECTION = re.compile(r'اضغط هنا|الجديد|تم تحويل|transf[ée]r[ée]', re.I)

    for a in soup.find_all("a", href=True):
        href = urljoin(url_finale or url, a["href"])
        texte = a.get_text(strip=True)
        href_l = href.lower()
        if href_l.endswith(".pdf") or href_l.endswith((".doc", ".docx", ".zip", ".pptx")):
            resultats.append({"url": href, "titre_brut": texte, "contexte": _contexte_texte(a)})
        elif href_l.endswith(".html") and urlparse(href).netloc == host:
            if texte and (re.match(r'^النموذج|^Modèle|^model', texte, re.I) or len(texte) <= 25):
                sous_pages_a_suivre.append((href, texte))
        elif not lien_redirection:
            contexte = _contexte_texte(a)
            if RE_REDIRECTION.search(texte) or RE_REDIRECTION.search(contexte):
                lien_redirection = href

    # Texte brut de la page (hors liens) : certaines pages annoncent juste
    # "تم تحويل محتوى الموقع إلى الرابط التالي https://..." sans lien <a>
    # cliquable autour de l'URL — on l'extrait directement du texte si besoin.
    if not lien_redirection and not resultats and not sous_pages_a_suivre:
        texte_page = soup.get_text(" ", strip=True)
        if RE_REDIRECTION.search(texte_page):
            m_url = re.search(r'https?://[^\s"\'<>]+', texte_page)
            if m_url:
                lien_redirection = m_url.group(0).rstrip(".,؛")

    if profondeur < max_profondeur:
        for href, texte_lien in sous_pages_a_suivre[:200]:
            sous = _extraire_pdfs_page_exams(href, profondeur + 1, max_profondeur, vus, nav=nav)
            for s in sous:
                s.setdefault("titre_brut", texte_lien)
            resultats.extend(sous)
            time.sleep(0.1)

        # On ne suit le lien de redirection que s'il pointe vers une page
        # qui semble déjà spécifique (contient le niveau ou la matière dans
        # son URL), pour éviter de remonter par erreur le contenu d'une
        # page générique listant tous les niveaux/matières confondus.
        if not resultats and lien_redirection and lien_redirection not in vus:
            cible_specifique = bool(re.search(r'/(1am|2am|3am|4am|1as|2as|3as|1ap|2ap|3ap|4ap|5ap)\b',
                                               lien_redirection, re.I))
            if cible_specifique:
                sous = _extraire_pdfs_page_exams(lien_redirection, profondeur + 1, max_profondeur, vus, nav=nav)
                resultats.extend(sous)

    return resultats


def _contexte_texte(a_tag):
    morceaux = [a_tag.get_text(strip=True)]
    suivant = a_tag.find_next_sibling()
    if suivant:
        t = suivant.get_text(strip=True)
        if t and len(t) < 40:
            morceaux.append(t)
    parent_text = a_tag.parent.get_text(" ", strip=True) if a_tag.parent else ""
    if parent_text and len(parent_text) < 120:
        morceaux.append(parent_text)
    return " ".join(morceaux)


def decouvrir(job_id, cycle, niveau_slug, matieres_filtre=None):
    """
    Découvre les fiches PDF pour un niveau ency-education.
    `cycle` est accepté pour uniformiser l'API entre adaptateurs mais n'est
    pas utilisé ici (le slug de niveau, ex: '3as', suffit).
    """
    nav = _NavigateurWix()
    toutes = []
    try:
        job_manager.emit(job_id, "log", {"msg": "🌐 Chargement du site (rendu JS, peut prendre quelques secondes)..."})
        matieres = _collecter_matieres(nav, niveau_slug)
        if matieres_filtre:
            matieres = {s: l for s, l in matieres.items() if s in matieres_filtre}
        if not matieres:
            job_manager.emit(job_id, "log", {"msg": "✗ Aucune matière trouvée sur ency-education."})
            return []

        job_manager.emit(job_id, "log", {"msg": f"✅ {len(matieres)} matières détectées (ency-education)."})
        total = len(matieres)
        for idx, (slug_mat, label_mat) in enumerate(matieres.items(), 1):
            if job_manager.is_cancelled(job_id):
                break
            job_manager.emit(job_id, "progress", {
                "phase": "discover", "current": idx, "total": total, "label": label_mat})

            liens_exams = _trouver_sous_domaine(nav, niveau_slug, slug_mat)
            if not liens_exams:
                job_manager.emit(job_id, "log", {"msg": f"  ⚠ {label_mat} : pas de page d'exercices trouvée."})
                continue

            job_manager.emit(job_id, "log", {
                "msg": f"  → {label_mat} : {len(liens_exams)} page(s) d'exercices, exploration..."})

            for url_exams in liens_exams:
                if job_manager.is_cancelled(job_id):
                    break
                pdfs = _extraire_pdfs_page_exams(url_exams, nav=nav)
                for p in pdfs:
                    titre = p["contexte"] or p["titre_brut"]
                    toutes.append({
                        "titre": titre,
                        "url": p["url"],
                        "matiere": label_mat,
                        "trimestre": detecter_trimestre(titre),
                        "type_doc": detecter_type_doc(titre),
                        "corrige": detecter_corrige(titre),
                        "annee": detecter_annee(titre),
                        "filiere": detecter_filiere(titre),
                        "site": "ency",
                    })
                job_manager.emit(job_id, "log", {
                    "msg": f"    • {len(pdfs)} fiches trouvées sur {url_exams[:60]}..."})
                if not pdfs:
                    job_manager.emit(job_id, "log", {
                        "msg": f"    ⚠ cette page semble vide ou son contenu a été déplacé "
                               f"(le site ency-education a migré une partie de son contenu vers "
                               f"un nouveau système qui n'est pas encore entièrement pris en charge)."})
    finally:
        nav.fermer()
    return toutes


def destination(fiche, racine: Path, separer_corrige=True) -> Path:
    tl = "Devoir" if fiche["type_doc"] == "D" else "Examen" if fiche["type_doc"] == "E" else "Documents"
    matiere = slugify(fiche["matiere"]) or "Autres"
    trim = LABELS_TRIMESTRE.get(fiche.get("trimestre", 0), "Autres")
    ext = Path(urlparse(fiche["url"]).path).suffix or ".pdf"
    base_nom = slugify(fiche["titre"]) or "fiche"
    if separer_corrige:
        segments = (trim, matiere, tl,
                     "Avec_corrigé" if fiche["corrige"] else "Sans_corrigé", base_nom)
    else:
        segments = (trim, matiere, tl, base_nom)
    return chemin_sur(racine, *segments, extension=ext)


def _telecharger_un(url, dest: Path):
    if dest.exists():
        return True, "déjà présent"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = _session.get(url, timeout=40, stream=True)
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
    """Télécharge en parallèle (requests, thread-safe et rapide)."""
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
        dest = destination(f, racine, separer_corrige)
        reussi, info = _telecharger_un(f["url"], dest)
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

    manifeste = ecrire_manifeste(racine, "ency", niveau_label, entrees)
    return {"ok": ok, "err": err, "manifeste": str(manifeste)}
