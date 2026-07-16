# =============================================================================
#  utils.py — Fonctions partagées par tous les scrapers (DzExams, Eddirasa, Ency)
# =============================================================================

import re
import time
import uuid
import threading
from pathlib import Path
from queue import Queue, Empty


def slugify(texte: str, n: int = 60) -> str:
    """Transforme un titre en nom de fichier/dossier sûr (cross-plateforme).
    `n` est volontairement modeste par défaut (60) car le nom final doit
    encore tenir sous la limite MAX_PATH de Windows (260 caractères) une fois
    combiné avec le chemin du dossier de destination choisi par l'utilisateur
    + les sous-dossiers type/matière/trimestre, qui peuvent déjà être longs."""
    if not texte:
        return ""
    # Caractères interdits sur Windows + ceux qui posent souci d'encodage
    # selon la page de code active (°, ², etc.) sont remplacés/retirés.
    texte = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', texte)
    texte = re.sub(r'[✅❌📅🔗📂📥]', '', texte)
    texte = texte.replace('°', '').replace('º', '')
    texte = re.sub(r'\s+', '_', texte.strip())
    texte = re.sub(r'_+', '_', texte)
    return texte[:n].rstrip('_').rstrip('.')


def chemin_sur(racine: Path, *segments: str, extension: str = ".pdf", limite: int = 240) -> Path:
    """
    Construit un chemin de fichier à partir de segments déjà slugifiés, et
    garantit que la longueur totale du chemin (en caractères) reste sous la
    limite Windows MAX_PATH (260 par défaut, on vise 240 par marge de
    sécurité pour le préfixe `\\\\?\\` ou des variations d'OS).
    Si le chemin est trop long, le dernier segment (nom de fichier) est
    raccourci automatiquement plutôt que de planter au moment d'écrire le
    fichier.
    """
    base = Path(racine, *segments[:-1])
    nom_fichier = segments[-1] + extension
    chemin = base / nom_fichier
    longueur_actuelle = len(str(chemin))
    if longueur_actuelle <= limite:
        return chemin

    # Raccourcir le nom de fichier (dernier segment) pour rentrer dans la limite,
    # en gardant un minimum de 20 caractères pour que le nom reste identifiable.
    trop = longueur_actuelle - limite
    nom_sans_ext = segments[-1]
    nouvelle_longueur = max(20, len(nom_sans_ext) - trop)
    nom_raccourci = nom_sans_ext[:nouvelle_longueur].rstrip('_') + extension
    return base / nom_raccourci


def detecter_annee(texte: str) -> int:
    """Cherche une année 19xx/20xx dans une chaîne, sinon 0."""
    m = re.findall(r'\b(19\d{2}|20\d{2})\b', texte or "")
    return int(m[-1]) if m else 0


def detecter_corrige(texte: str) -> bool:
    """Détecte si un titre/texte signale un corrigé / une solution.
    Priorité :
      1. ❌ (croix rouge) → non corrigé
      2. ✅ (carré vert) → corrigé
      3. Sinon, détection par mots-clés (corrigé, solution, حل, …).
    """
    if not texte:
        return False
    if '\u274c' in texte:      # ❌ = explicitement non corrigé
        return False
    if '\u2705' in texte:      # ✅ = explicitement corrigé
        return True
    motifs = [
        r'corrig[ée]', r'solution', r'حل\b', r'حلول', r'تصحيح',
    ]
    return any(re.search(p, texte, re.I) for p in motifs)


def detecter_trimestre(texte: str) -> int:
    """Détecte le numéro de trimestre (1/2/3) à partir d'indices textuels
    arabes ou français, sinon 0 (inconnu/non applicable)."""
    if not texte:
        return 0
    t = texte
    if re.search(r'الفصل\s*الأول|trimestre\s*1|1\s*ᵉʳ\s*trimestre|premier\s*trimestre', t, re.I):
        return 1
    if re.search(r'الفصل\s*الثاني|trimestre\s*2|2\s*ᵉ\s*trimestre|deuxi[èe]me\s*trimestre', t, re.I):
        return 2
    if re.search(r'الفصل\s*الثالث|trimestre\s*3|3\s*ᵉ\s*trimestre|troisi[èe]me\s*trimestre', t, re.I):
        return 3
    return 0


def detecter_type_doc(texte: str) -> str:
    """Retourne 'D' (devoir) ou 'E' (examen/test) à partir d'indices textuels."""
    if not texte:
        return "?"
    t = texte.lower()
    motifs_examen = ['examen', 'test', 'بكالوريا', 'bac', 'bem', 'اختبار',
                      'شهادة', 'composition']
    motifs_devoir = ['devoir', 'فرض', 'homework']
    if any(re.search(p, t, re.I) for p in motifs_examen):
        return "E"
    if any(re.search(p, t, re.I) for p in motifs_devoir):
        return "D"
    return "?"


LABELS_TRIMESTRE = {1: "Trimestre_1", 2: "Trimestre_2", 3: "Trimestre_3", 0: "Autres"}
LABELS_TYPE_DOC = {"D": "Devoir", "E": "Examen", "?": "Documents"}


def ecrire_manifeste(racine: Path, site: str, niveau: str, entrees: list):
    """
    Écrit un fichier `manifeste.json` à la racine du dossier téléchargé,
    listant pour chaque fichier : matière, trimestre, type (devoir/examen),
    année, URL source, chemin local relatif, taille en octets, et si présent
    le statut de l'envoi cloud. `entrees` est une liste de dicts avec au
    moins les clés `fiche` (les métadonnées d'origine) et `chemin` (Path
    local du fichier réellement écrit sur disque, ou None si échec).
    """
    racine = Path(racine)
    fichiers_info = []
    for entree in entrees:
        fiche = entree.get("fiche", {})
        chemin = entree.get("chemin")
        taille = None
        chemin_relatif = None
        if chemin and Path(chemin).exists():
            taille = Path(chemin).stat().st_size
            try:
                chemin_relatif = str(Path(chemin).relative_to(racine))
            except ValueError:
                chemin_relatif = str(chemin)
        fichiers_info.append({
            "titre": fiche.get("titre"),
            "matiere": fiche.get("matiere"),
            "trimestre": fiche.get("trimestre", 0),
            "trimestre_label": LABELS_TRIMESTRE.get(fiche.get("trimestre", 0), "Autres"),
            "type_doc": fiche.get("type_doc"),
            "type_doc_label": LABELS_TYPE_DOC.get(fiche.get("type_doc"), "Documents"),
            "annee": fiche.get("annee") or None,
            "corrige": bool(fiche.get("corrige")),
            "url_source": fiche.get("url"),
            "urlPdf": fiche.get("url"),
            "source": site,
            "fichier_local": chemin_relatif,
            "taille_octets": taille,
            "reussi": chemin is not None and taille is not None,
        })

    manifeste = {
        "site": site,
        "niveau": niveau,
        "genere_le": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "nombre_fichiers": sum(1 for f in fichiers_info if f["reussi"]),
        "nombre_echecs": sum(1 for f in fichiers_info if not f["reussi"]),
        "fichiers": fichiers_info,
    }

    import json
    chemin_manifeste = racine / "manifeste.json"
    try:
        racine.mkdir(parents=True, exist_ok=True)
        with open(chemin_manifeste, "w", encoding="utf-8") as fh:
            json.dump(manifeste, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass  # le manifeste est une commodité, jamais bloquant
    return chemin_manifeste


RE_FILIERE = re.compile(
    r'\b(SE|Sciences?[\s\-]?exp[ée]rimentales?|'
    r'TM|Techniques?[\s\-]?math[ée]matiques?|'
    r'GE|Gestion[\s\-]?[ée]conomie|'
    r'LP|Lettres?[\s\-]?philosophie|'
    r'LE|Langues?[\s\-]?[ée]trang[èe]res?)\b|'
    r'(علوم\s*تجريبية|تقني\s*رياضي|تسيير\s*واقتصاد|آداب\s*وفلسفة|لغات\s*أجنبية)',
    re.I)

_FILIERE_NORMALISATION = {
    "se": "Sciences Expérimentales", "sciences expérimentales": "Sciences Expérimentales",
    "tm": "Techniques Mathématiques", "techniques mathématiques": "Techniques Mathématiques",
    "ge": "Gestion-Économie", "gestion économie": "Gestion-Économie", "gestion-économie": "Gestion-Économie",
    "lp": "Lettres-Philosophie", "lettres philosophie": "Lettres-Philosophie", "lettres-philosophie": "Lettres-Philosophie",
    "le": "Langues Étrangères", "langues étrangères": "Langues Étrangères",
    "علوم تجريبية": "علوم تجريبية (Sciences Exp.)",
    "تقني رياضي": "تقني رياضي (Techniques Math.)",
    "تسيير واقتصاد": "تسيير واقتصاد (Gestion-Éco.)",
    "آداب وفلسفة": "آداب وفلسفة (Lettres-Philo.)",
    "لغات أجنبية": "لغات أجنبية (Langues Étr.)",
}


def detecter_filiere(texte: str) -> str:
    """Détecte la filière du bac (Sciences Exp., Techniques Math., etc.) à
    partir d'indices textuels (français ou arabe) dans le titre du sujet.
    Retourne une chaîne vide si aucune filière n'est détectée (cas général,
    hors niveau bac)."""
    if not texte:
        return ""
    m = RE_FILIERE.search(texte)
    if not m:
        return ""
    brut = next(g for g in m.groups() if g)
    cle = brut.lower().replace("-", " ").strip()
    cle = re.sub(r'\s+', ' ', cle)
    return _FILIERE_NORMALISATION.get(cle, brut)


class JobManager:
    """
    Gère les jobs de scraping/téléchargement en arrière-plan.
    Chaque job a un id, un statut, une queue d'événements (pour SSE),
    et un dict de résultats partagés.
    """

    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def creer_job(self, kind: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "status": "running",   # running | done | error | cancelled
                "queue": Queue(),
                "result": None,
                "error": None,
                "cancel_flag": threading.Event(),
                "created_at": time.time(),
            }
        return job_id

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def emit(self, job_id, event: str, data: dict):
        job = self.get(job_id)
        if not job:
            return
        job["queue"].put({"event": event, "data": data})

    def finish(self, job_id, result=None, error=None):
        job = self.get(job_id)
        if not job:
            return
        job["status"] = "error" if error else "done"
        job["result"] = result
        job["error"] = error
        job["queue"].put({"event": "end", "data": {"status": job["status"]}})

    def cancel(self, job_id):
        job = self.get(job_id)
        if job:
            job["cancel_flag"].set()

    def is_cancelled(self, job_id) -> bool:
        job = self.get(job_id)
        return bool(job and job["cancel_flag"].is_set())

    def stream(self, job_id):
        """Générateur SSE : yield les événements au fur et à mesure."""
        job = self.get(job_id)
        if not job:
            yield "event: error\ndata: {}\n\n"
            return
        q: Queue = job["queue"]
        while True:
            try:
                item = q.get(timeout=15)
            except Empty:
                yield ": keep-alive\n\n"
                continue
            import json
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            if item["event"] == "end":
                break


# Instance globale unique (importée par app.py et les scrapers)
job_manager = JobManager()


def safe_dest_path(racine: Path, *parts) -> Path:
    """Construit un chemin de destination en garantissant des composants slugifiés."""
    p = Path(racine)
    for part in parts:
        p = p / slugify(str(part), n=80)
    return p


USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
