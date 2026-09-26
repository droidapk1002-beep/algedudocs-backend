import json, os, threading, webbrowser, uuid, zipfile, shutil, time, re
from pathlib import Path
from urllib.parse import urljoin

from flask import Flask, render_template, request, jsonify, Response, send_from_directory, redirect

import config
import cloud_upload
from scrapers import SITES, get_site
from scrapers.utils import job_manager, fetch_html, MIRROR_DEFAUT
from scrapers.clean_pdf import clean_pdf_file
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

_decouvertes = {}


@app.route("/")
def index():
    sites_info = {sid: {"label": s["label"], "url": s["url"]} for sid, s in SITES.items()}
    return render_template("index.html", sites=sites_info)


@app.route("/api/cycles/<site_id>")
def api_cycles(site_id):
    try:
        mod = get_site(site_id)
    except ValueError:
        return jsonify({"error": "site inconnu"}), 404
    cycles = mod.lister_cycles()
    out = {}
    for c, info in cycles.items():
        out[c] = {"label": info.get("label", c), "niveaux": info["niveaux"]}
    return jsonify(out)


DUREE_MAX_MATIERES = 3 * 60
DUREE_MAX_DECOUVERTE = 15 * 60
DUREE_MAX_TELECHARGEMENT = 60 * 60


def _avec_timeout(fn, args, duree_max, job_id, label):
    resultat = {"valeur": None, "erreur": None, "fini": False}

    def cible():
        try:
            resultat["valeur"] = fn(*args)
        except Exception as e:
            resultat["erreur"] = e
        finally:
            resultat["fini"] = True

    t = threading.Thread(target=cible, daemon=True)
    t.start()
    t.join(timeout=duree_max)

    if not resultat["fini"]:
        minutes = duree_max // 60
        duree_txt = f"{minutes} minutes" if minutes >= 1 else f"{duree_max} secondes"
        job_manager.emit(job_id, "log", {
            "msg": f"✗ {label} interrompu(e) après {duree_txt} sans réponse. "
                   f"Le site est probablement inaccessible, trop lent, ou a changé de structure."})
        raise TimeoutError(f"{label} : délai maximum dépassé ({duree_max}s)")

    if resultat["erreur"] is not None:
        raise resultat["erreur"]

    return resultat["valeur"]


def _nettoyer_fichiers_pdf(job_id, racine, clean=True, cover_footer=False):
    if not clean and not cover_footer:
        return 0
    count = 0
    for p in sorted(Path(racine).rglob("*.pdf")):
        try:
            if clean_pdf_file(p, clean=clean, cover_footer=cover_footer):
                count += 1
        except Exception as e:
            logger.warning("Erreur nettoyage %s: %s", p.name, e)
    if count:
        job_manager.emit(job_id, "log", {
            "msg": f"🧹 PDF nettoyés : {count} fichier(s) (liens et/ou filigranes supprimés)."})
    return count


def _lancer_liste_matieres(job_id, site_id, cycle, niveau):
    try:
        mod = get_site(site_id)
        job_manager.emit(job_id, "log", {"msg": f"📋 Récupération des matières disponibles ({site_id} / {niveau})..."})
        matieres = _avec_timeout(
            mod.lister_matieres, (job_id, cycle, niveau),
            DUREE_MAX_MATIERES, job_id, "Récupération des matières")
        job_manager.finish(job_id, result={"matieres": matieres})
    except Exception as e:
        job_manager.emit(job_id, "log", {"msg": f"✗ Erreur : {e}"})
        job_manager.finish(job_id, error=str(e))


@app.route("/api/matieres", methods=["POST"])
def api_matieres():
    data = request.get_json(force=True)
    site_id = data.get("site")
    cycle = data.get("cycle")
    niveau = data.get("niveau")

    if site_id not in SITES:
        return jsonify({"error": "site inconnu"}), 400
    if not niveau:
        return jsonify({"error": "niveau requis"}), 400

    job_id = job_manager.creer_job("matieres")
    t = threading.Thread(target=_lancer_liste_matieres, args=(job_id, site_id, cycle, niveau), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _lancer_decouverte(job_id, site_id, cycle, niveau, matieres):
    try:
        mod = get_site(site_id)
        job_manager.emit(job_id, "log", {"msg": f"🚀 Démarrage de la découverte ({site_id} / {niveau})"})

        fiches = _avec_timeout(
            mod.decouvrir, (job_id, cycle, niveau, matieres or None),
            DUREE_MAX_DECOUVERTE, job_id, "Découverte")

        if job_manager.is_cancelled(job_id):
            job_manager.emit(job_id, "log", {"msg": "⏹ Découverte annulée."})
            job_manager.finish(job_id, result={"fiches": [], "cancelled": True})
            return

        _decouvertes[job_id] = fiches
        job_manager.emit(job_id, "log", {"msg": f"✅ Découverte terminée : {len(fiches)} fiches trouvées."})
        job_manager.finish(job_id, result={"count": len(fiches), "fiches": fiches})
    except Exception as e:
        job_manager.emit(job_id, "log", {"msg": f"✗ Erreur : {e}"})
        job_manager.finish(job_id, error=str(e))


@app.route("/api/decouvrir", methods=["POST"])
def api_decouvrir():
    data = request.get_json(force=True)
    site_id = data.get("site")
    cycle = data.get("cycle")
    niveau = data.get("niveau")
    matieres = data.get("matieres") or []

    if site_id not in SITES:
        return jsonify({"error": "site inconnu"}), 400
    if not niveau:
        return jsonify({"error": "niveau requis"}), 400

    job_id = job_manager.creer_job("decouverte")
    t = threading.Thread(target=_lancer_decouverte, args=(job_id, site_id, cycle, niveau, matieres), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _lancer_telechargement(job_id, site_id, fiches, dossier_nom, niveau_label="",
                           separer_corrige=True, clean_pdf_opt=True, cover_footer=False):
    try:
        mod = get_site(site_id)
        racine = DOWNLOADS_DIR / dossier_nom
        job_manager.emit(job_id, "log", {"msg": f"🚀 Démarrage du téléchargement de {len(fiches)} fiches..."})
        resultat = _avec_timeout(
            mod.telecharger, (job_id, fiches, racine, niveau_label, separer_corrige),
            DUREE_MAX_TELECHARGEMENT, job_id, "Téléchargement")
        if job_manager.is_cancelled(job_id):
            job_manager.emit(job_id, "log", {"msg": "⏹ Téléchargement annulé."})
        if resultat.get("ok", 0) > 0:
            _nettoyer_fichiers_pdf(job_id, racine, clean=clean_pdf_opt, cover_footer=cover_footer)
        job_manager.emit(job_id, "log", {
            "msg": f"🏁 Terminé : {resultat['ok']} réussis, {resultat['err']} échecs."})
        job_manager.finish(job_id, result={**resultat, "dossier": str(racine)})
    except Exception as e:
        job_manager.emit(job_id, "log", {"msg": f"✗ Erreur : {e}"})
        job_manager.finish(job_id, error=str(e))


@app.route("/api/telecharger", methods=["POST"])
def api_telecharger():
    data = request.get_json(force=True)
    site_id = data.get("site")
    fiches = data.get("fiches")
    dossier_nom = data.get("dossier") or f"{site_id}_export"
    niveau_label = data.get("niveau") or ""
    separer_corrige = data.get("separer_corrige", True)
    clean_pdf_opt = data.get("clean_pdf", True)
    cover_footer = data.get("cover_footer", False)

    if site_id not in SITES:
        return jsonify({"error": "site inconnu"}), 400
    if not fiches:
        return jsonify({"error": "aucune fiche sélectionnée"}), 400

    dossier_nom = "".join(c for c in dossier_nom if c.isalnum() or c in "_-") or "export"

    job_id = job_manager.creer_job("telechargement")
    t = threading.Thread(target=_lancer_telechargement,
                          args=(job_id, site_id, fiches, dossier_nom, niveau_label,
                                separer_corrige, clean_pdf_opt, cover_footer), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _lancer_telechargement_cloud(job_id, site_id, fiches, dossier_nom, niveau_label,
                                  provider, compte, separer_corrige=True, clean_pdf_opt=True, cover_footer=False):
    try:
        mod = get_site(site_id)
        racine = DOWNLOADS_DIR / dossier_nom
        job_manager.emit(job_id, "log", {"msg": f"🚀 Téléchargement de {len(fiches)} fiches → puis envoi vers {provider} ({compte})..."})

        job_manager.emit(job_id, "progress", {
            "phase": "download", "current": 0, "total": len(fiches), "ok": 0, "err": 0,
            "label": "Téléchargement local…"})
        resultat = _avec_timeout(
            mod.telecharger, (job_id, fiches, racine, niveau_label, separer_corrige),
            DUREE_MAX_TELECHARGEMENT, job_id, "Téléchargement")

        if job_manager.is_cancelled(job_id):
            job_manager.emit(job_id, "log", {"msg": "⏹ Opération annulée."})
            job_manager.finish(job_id, result={**resultat, "dossier": str(racine), "cancelled": True})
            return

        job_manager.emit(job_id, "log", {
            "msg": f"✅ Téléchargement terminé : {resultat['ok']} réussis, {resultat['err']} échecs."})

        if resultat.get("ok", 0) > 0:
            _nettoyer_fichiers_pdf(job_id, racine, clean=clean_pdf_opt, cover_footer=cover_footer)
            job_manager.emit(job_id, "log", {"msg": f"☁ Envoi vers {provider} ({compte}) en cours..."})
            job_manager.emit(job_id, "progress", {
                "phase": "cloud", "current": 0, "total": resultat["ok"], "ok": 0, "err": 0,
                "label": f"Envoi cloud ({provider}/{compte})…"})
            resultat_cloud = cloud_upload.uploader_dossier(
                provider, compte, racine, dossier_nom, job_id=job_id,
                emettre=lambda m: job_manager.emit(job_id, "log", {"msg": m}))
            resultat["cloud"] = resultat_cloud
            job_manager.emit(job_id, "log", {
                "msg": f"☁ Envoi cloud terminé : {resultat_cloud['ok']} réussis, {resultat_cloud['err']} échecs."})
        else:
            job_manager.emit(job_id, "log", {"msg": "☁ ⏭ Envoi cloud ignoré (aucun fichier téléchargé)."})

        if racine.exists() and "_cloud_" in dossier_nom:
            shutil.rmtree(racine, ignore_errors=True)
            job_manager.emit(job_id, "log", {"msg": "🧹 Dossier temporaire nettoyé."})

        job_manager.finish(job_id, result={**resultat, "dossier": str(racine)})
    except Exception as e:
        job_manager.emit(job_id, "log", {"msg": f"✗ Erreur : {e}"})
        if racine.exists() and "_cloud_" in dossier_nom:
            shutil.rmtree(racine, ignore_errors=True)
        job_manager.finish(job_id, error=str(e))


@app.route("/api/telecharger-cloud", methods=["POST"])
def api_telecharger_cloud():
    data = request.get_json(force=True)
    site_id = data.get("site")
    fiches = data.get("fiches")
    dossier_nom = data.get("dossier") or f"{site_id}_export"
    niveau_label = data.get("niveau") or ""
    provider = data.get("provider")
    compte = data.get("compte")
    separer_corrige = data.get("separer_corrige", True)
    clean_pdf_opt = data.get("clean_pdf", True)
    cover_footer = data.get("cover_footer", False)

    if site_id not in SITES:
        return jsonify({"error": "site inconnu"}), 400
    if not fiches:
        return jsonify({"error": "aucune fiche sélectionnée"}), 400
    if not provider or not compte:
        return jsonify({"error": "provider et compte requis"}), 400

    dossier_nom = "".join(c for c in dossier_nom if c.isalnum() or c in "_-") or "export"

    job_id = job_manager.creer_job("telechargement_cloud")
    t = threading.Thread(target=_lancer_telechargement_cloud,
                          args=(job_id, site_id, fiches, dossier_nom, niveau_label,
                                provider, compte, separer_corrige, clean_pdf_opt, cover_footer), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _lancer_envoi_cloud(job_id, provider, compte, dossier_nom):
    try:
        racine = DOWNLOADS_DIR / dossier_nom
        if not racine.exists():
            raise RuntimeError(f"Dossier introuvable : {dossier_nom}")
        job_manager.emit(job_id, "log", {"msg": f"☁ Envoi vers {provider} ({compte}) en cours..."})
        resultat = cloud_upload.uploader_dossier(
            provider, compte, racine, dossier_nom, job_id=job_id,
            emettre=lambda m: job_manager.emit(job_id, "log", {"msg": m}))
        job_manager.emit(job_id, "log", {
            "msg": f"☁ Envoi cloud terminé : {resultat['ok']} réussis, {resultat['err']} échecs."})
        job_manager.finish(job_id, result=resultat)
    except Exception as e:
        job_manager.emit(job_id, "log", {"msg": f"✗ Erreur : {e}"})
        job_manager.finish(job_id, error=str(e))


@app.route("/api/cloud-upload", methods=["POST"])
def api_cloud_upload():
    data = request.get_json(force=True)
    provider = data.get("provider")
    compte = data.get("compte")
    dossier_nom = data.get("dossier")

    if not provider or not compte:
        return jsonify({"error": "provider et compte requis"}), 400
    if not dossier_nom:
        return jsonify({"error": "dossier requis"}), 400

    dossier_nom = "".join(c for c in dossier_nom if c.isalnum() or c in "_-") or "export"

    job_id = job_manager.creer_job("cloud_upload")
    t = threading.Thread(target=_lancer_envoi_cloud, args=(job_id, provider, compte, dossier_nom), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


_gdrive_auth_results = {}
_gdrive_flows = {}


def _generer_auth_url_gdrive(compte, compte_nom):
    from google_auth_oauthlib.flow import Flow
    redirect_uri = compte.get("redirect_uri", "http://localhost:3000/api/cloud/google/callback")
    flow = Flow.from_client_config(
        {"web": {
            "client_id": compte["client_id"],
            "client_secret": compte["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }},
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=compte_nom)
    _gdrive_flows[compte_nom] = flow
    return auth_url


@app.route("/api/cloud/google/auth")
def api_google_auth():
    compte_nom = request.args.get("compte", "")
    if not compte_nom:
        return jsonify({"error": "paramètre 'compte' requis"}), 400
    compte = config.get_compte("gdrive", compte_nom)
    if not compte or "client_id" not in compte:
        return jsonify({"error": f"compte '{compte_nom}' non trouvé ou pas OAuth2"}), 400
    if compte_nom in _gdrive_auth_results:
        del _gdrive_auth_results[compte_nom]
    try:
        auth_url = _generer_auth_url_gdrive(compte, compte_nom)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    webbrowser.open(auth_url)
    return jsonify({
        "compte": compte_nom,
        "message": "Une fenêtre navigateur s'ouvre pour l'authentification Google. "
                   "Après autorisation, retournez au dashboard.",
    })


@app.route("/api/cloud/google/callback")
def api_google_callback():
    code = request.args.get("code")
    state = request.args.get("state", "")
    erreur = request.args.get("error")

    if erreur:
        _gdrive_auth_results[state] = {"error": f"Google a refusé : {erreur}"}
        return f"<p>Erreur : {erreur}</p><p>Vous pouvez fermer cette fenêtre.</p>"

    if not code:
        return "Code manquant", 400

    compte_nom = state
    flow = _gdrive_flows.pop(compte_nom, None)
    if not flow:
        return "Session expirée, veuillez recommencer l'authentification.", 400

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        cloud_upload.gdrive_oauth2_save_credentials(
            {"access_token": creds.token, "refresh_token": creds.refresh_token}, compte_nom)
        _gdrive_auth_results[compte_nom] = {"ok": True}
        return '<script>window.close()</script><p>✅ Authentifié ! Vous pouvez fermer cette fenêtre.</p>'
    except Exception as e:
        _gdrive_auth_results[compte_nom] = {"error": str(e)}
        return f"<p>❌ Erreur : {e}</p>"


@app.route("/api/cloud/google/auth-status/<compte_nom>")
def api_google_auth_status(compte_nom):
    result = _gdrive_auth_results.get(compte_nom)
    if not result:
        return jsonify({"status": "pending"})
    if result.get("ok"):
        return jsonify({"status": "connected"})
    return jsonify({"status": "error", "error": result.get("error", "Erreur inconnue")})


@app.route("/api/cloud-status")
def api_cloud_status():
    status = config.cloud_configure_status()
    connectes = {}
    for provider in status.get("comptes", {}):
        for nom in status["comptes"][provider]:
            compte = config.get_compte(provider, nom)
            if compte and "client_id" in compte:
                cache_path = Path(config.BASE_DIR) / f"token_cache_gdrive_{nom}.json"
                connectes[f"{provider}::{nom}"] = cache_path.exists()
            elif compte and ("email" in compte or "access_token" in compte):
                connectes[f"{provider}::{nom}"] = True
    status["connectes"] = connectes
    return jsonify(status)


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    def gen():
        yield "retry: 2000\n\n"
        import time as _time
        last_keepalive = _time.time()
        for chunk in job_manager.stream(job_id):
            yield chunk
            now = _time.time()
            if now - last_keepalive > 15:
                yield ": keep-alive\n\n"
                last_keepalive = now
    return Response(gen(), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/annuler/<job_id>", methods=["POST"])
def api_annuler(job_id):
    job_manager.cancel(job_id)
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = job_manager.get(job_id)
    if not job:
        return jsonify({"error": "job inconnu"}), 404
    return jsonify({
        "id": job["id"], "status": job["status"],
        "result": job["result"], "error": job["error"],
    })


ZIPS_DIR = BASE_DIR / "téléchargement" / "zip"
ZIPS_DIR.mkdir(parents=True, exist_ok=True)


@app.route("/api/fichiers/<dossier>")
def api_lister_fichiers(dossier):
    racine = DOWNLOADS_DIR / dossier
    if not racine.exists():
        return jsonify({"fichiers": []})
    fichiers = []
    for p in racine.rglob("*"):
        if p.is_file():
            fichiers.append(str(p.relative_to(DOWNLOADS_DIR)))
    return jsonify({"fichiers": fichiers})


@app.route("/downloads/<path:filepath>")
def servir_fichier(filepath):
    return send_from_directory(DOWNLOADS_DIR, filepath, as_attachment=True)


@app.route("/api/zip/<dossier>")
def api_telecharger_zip(dossier):
    nettoyer = request.args.get("cleanup") == "1"
    dossier_sain = "".join(c for c in dossier if c.isalnum() or c in "_-") or dossier
    racine = DOWNLOADS_DIR / dossier_sain
    if not racine.exists() or not racine.is_dir():
        return jsonify({"error": "dossier introuvable"}), 404

    nom_zip = f"{dossier_sain}.zip"
    chemin_zip = ZIPS_DIR / nom_zip

    with zipfile.ZipFile(chemin_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in racine.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(racine)))

    if nettoyer:
        shutil.rmtree(racine, ignore_errors=True)

    return jsonify({"ok": True, "fichier": str(chemin_zip), "nom": nom_zip})


def _resoudre_pdf_dzexams(url):
    try:
        html = fetch_html(url, timeout=20)
        soup = BeautifulSoup(html, "html.parser")
        selectors = ["a#actions-download[href]", "a[href$='.pdf']", "a[href*='.pdf?']", "a[href*='/download']"]
        for sel in selectors:
            a = soup.select_one(sel)
            if a and a.get("href"):
                href = a["href"].strip()
                return urljoin(url, href)
        return None
    except Exception:
        return None


def _resoudre_pdf_ency(url):
    if url.lower().endswith(".pdf"):
        return url
    try:
        html = fetch_html(url, timeout=20)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            h = a["href"].lower()
            if h.endswith(".pdf") or ".pdf?" in h:
                return urljoin(url, a["href"])
        for tag in soup.select("iframe[src], embed[src]"):
            src = tag.get("src", "")
            if ".pdf" in src.lower():
                return urljoin(url, src)
        return None
    except Exception:
        return None


def _resoudre_pdf_eddirasa(url):
    if url.lower().endswith(".pdf"):
        return url
    try:
        html = fetch_html(url, timeout=20)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if "wp-content/uploads" in h and h.lower().endswith(".pdf"):
                return urljoin(url, h)
        for tag in soup.select("iframe[src], embed[src]"):
            src = tag.get("src", "")
            if ".pdf" in src.lower():
                return urljoin(url, src)
        return None
    except Exception:
        return None


def _via_miroir(url_pdf):
    """Passe l'URL du PDF par le miroir Cloudflare Worker. Indispensable : les
    PDF de dzexams exigent un Referer dzexams.com, donc une redirection 302
    depuis le blog se ferait bloquer en 403. Le Worker sert le PDF lui-même."""
    base = os.environ.get("DZEXAMS_MIRROR", "").strip() or MIRROR_DEFAUT
    if not base:
        return url_pdf
    return base.rstrip("/") + "/" + url_pdf


@app.route("/api/resolve-pdf")
def api_resolve_pdf():
    url = request.args.get("url", "")
    site = request.args.get("site", "dzexams")
    if not url:
        return jsonify({"error": "url requis"}), 400
    if site == "ency":
        pdf_url = _resoudre_pdf_ency(url)
    elif site == "eddirasa":
        pdf_url = _resoudre_pdf_eddirasa(url)
    else:
        pdf_url = _resoudre_pdf_dzexams(url)
    if pdf_url:
        return redirect(_via_miroir(pdf_url), code=302)
    return jsonify({"error": "PDF introuvable", "url": url,
                    "conseil": "verifiez que DZEXAMS_MIRROR est configure sur Render"}), 404


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print("=" * 60)
    print("  AlgEduDocs — Algerian Education Documents Dashboard")
    print(f"  Serveur demarre sur http://{host}:{port}")
    print("=" * 60)
    app.run(host=host, port=port, debug=False, threaded=True)
