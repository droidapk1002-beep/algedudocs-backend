#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════
#  DZExams Server v5  –  Modular (config + cloud + scrapers)
#  • Playwright headless  • multi-comptes  • découverte avant DL
#  • thème clair/sombre   • cartes/tableau • pagination • ZIP
# ═══════════════════════════════════════════════════════════════════

import os, re, json, threading, time, queue, uuid, zipfile, shutil, hashlib
from pathlib import Path
from flask import Flask, request, jsonify, Response, send_from_directory

import config
import cloud_upload
from scrapers import SITES, get_site
from scrapers.utils import job_manager

BASE_DIR   = Path(__file__).resolve().parent
DOWNLOADS  = BASE_DIR / "downloads"
ZIPS_DIR   = BASE_DIR / "zips"
DOWNLOADS.mkdir(exist_ok=True); ZIPS_DIR.mkdir(parents=True,exist_ok=True)

app = Flask(__name__, static_folder='.')

# ── Legacy globals (backward compat with existing JS) ───────────
log_queue     = queue.Queue()
status        = {"running":False,"total_ok":0,"total_fail":0,"total_uploaded":0,"current":""}
cloud_files_db= []

# ── Helpers ─────────────────────────────────────────────────────
def log(msg, level="info"):
    entry={"msg":msg,"level":level,"time":time.strftime("%H:%M:%S")}
    log_queue.put(entry)
    line = f"[{entry['time']}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        safe = line.encode('cp1252', errors='replace').decode('cp1252', errors='replace')
        print(safe)

def log_to_both(msg, level="info"):
    log(msg, level)

def _job_emitter(job_id):
    def emit(m):
        job_manager.emit(job_id, "log", {"msg":m})
        log(m)
    return emit

# ── LEGACY ROUTES (unchanged contract) ──────────────────────────

@app.route('/')
def index():
    return send_from_directory('.','dzexams-web.html')

@app.route('/env')
def get_env():
    accounts = {"mega":[],"gdrive":[],"dropbox":[],"onedrive":[]}
    for nom, infos in config._comptes_mega().items():
        if infos.get("pret"):
            accounts["mega"].append({"id":nom,"email":infos["email"],"folder":infos.get("folder","DZExams"),"pwd_ok":True})
    for nom, infos in config._comptes_gdrive_oauth2().items():
        if infos.get("pret"):
            accounts["gdrive"].append({"id":nom,"client_id":infos["client_id"][:12]+"...","folder":infos.get("root_folder","DZExams"),"token_ok":True})
    for nom, infos in config._comptes_dropbox().items():
        if infos.get("pret"):
            accounts["dropbox"].append({"id":nom,"folder":infos.get("folder","DZExams"),"token_preview":infos["access_token"][:12]+"..."})
    return jsonify({"destination":"local","local_folder":"downloads","delete_after":"false","accounts":accounts})

@app.route('/cloud-files')
def get_cloud_files():
    return jsonify(cloud_files_db)

@app.route('/stop',methods=['POST'])
def stop():
    status["running"]=False
    log("⏹ Arrêt demandé","warn")
    return jsonify({"ok":True})

@app.route('/status')
def get_status():
    return jsonify(status)

@app.route('/logs')
def stream_logs():
    def gen():
        while True:
            try:
                e=log_queue.get(timeout=30)
                yield f"data: {json.dumps(e)}\n\n"
            except queue.Empty:
                yield 'data: {"msg":"ping","level":"ping"}\n\n'
    return Response(gen(),mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

# ── LEGACY /start (preserved, uses new scrapers internally) ────
@app.route('/start',methods=['POST'])
def start():
    if status["running"]:
        return jsonify({"error":"Déjà en cours"}),400
    cfg=request.json
    while not log_queue.empty(): log_queue.get()
    t=threading.Thread(target=_legacy_download,args=(cfg,),daemon=True)
    t.start()
    return jsonify({"ok":True})

def _legacy_download(cfg):
    global status, cloud_files_db
    status.update({"running":True,"total_ok":0,"total_fail":0,"total_uploaded":0,"current":""})
    combos=cfg.get("combos",[])
    dest=cfg.get("destination","local").lower()
    delete_after=str(cfg.get("delete_after","false")).lower() in ("true","1")

    selected_mega=cfg.get("selected_mega",[])
    selected_gdrive=cfg.get("selected_gdrive",[])
    selected_dropbox=cfg.get("selected_dropbox",[])
    cycle_map=lambda l: "moyen" if l in ("1am","2am","3am","4am") else "primaire" if l in ("0ap","1ap","2ap","3ap","4ap","5ap") else "secondaire"

    total_ok=total_fail=total_uploaded=0
    try:
        job_id=uuid.uuid4().hex[:12]
        for combo in combos:
            if not status["running"]: break
            level=combo.get("level",""); mat=combo.get("mat",""); cat=combo.get("cat","")
            filiere=combo.get("filiere","")
            tl,ty="",""
            if cat in {"t1","t2","t3","d1","d2","d3","e1","e2","e3",
                        "as_d1","as_e1","as_d2","as_e2","as_d3","as_e3",
                        "se_d1","se_e1","se_d2","se_e2","se_d3","se_e3",
                        "lp_d1","lp_e1","lp_d2","lp_e2","lp_d3","lp_e3",
                        "ge_d1","ge_e1","ge_d2","ge_e2","ge_d3","ge_e3",
                        "tm_d1","tm_e1","tm_d2","tm_e2","tm_d3","tm_e3",
                        "rattrapage","cours","pedagogie","exercices","homeworks",""}:
                tl,ty = _cat_meta(cat)
            folder_name=f"{tl}_{ty}".strip("_") if tl else (ty or cat)
            if filiere: folder_name=f"{filiere}_{folder_name}"
            out_dir=DOWNLOADS / level / mat / folder_name
            out_dir.mkdir(parents=True,exist_ok=True)

            log(f"📂 {level}/{mat}/{cat}" + (f"/{filiere}" if filiere else ""))
            status["current"]=f"{level}/{mat}/{cat}"

            try:
                mod=get_site("dzexams")
                fiches=mod.decouvrir(job_id, cycle_map(level),
                                     level, matieres_filtre=[mat])
            except Exception as e:
                log(f"✗ Découverte échouée: {e}","error")
                total_fail+=1
                continue

            if not fiches:
                log("⚠ Aucun sujet trouvé","warn")
                continue

            # Filter by trimestre/type from category
            cat_trim = str(combo.get('trim', '') or '')
            cat_type = combo.get('type', '')
            if cat_trim:
                fiches = [f for f in fiches if str(f.get('trimestre', 0)) == cat_trim]
            if cat_type == 'devoir':
                fiches = [f for f in fiches if f.get('type_doc') == 'D']
            elif cat_type == 'compo':
                fiches = [f for f in fiches if f.get('type_doc') in ('E', '?')]
            log(f"  → {len(fiches)} fiches après filtrage catégorie")

            limit = cfg.get("limit")
            if limit and len(fiches) > limit:
                fiches = fiches[:limit]
                log(f"  → limité à {limit} fiches")

            if not fiches:
                log("⚠ Aucune fiche ne correspond à la catégorie","warn")
                continue

            try:
                resultat=mod.telecharger(job_id,fiches,out_dir,folder_name,separer_corrige=True)
                ok=resultat.get("ok",0); err=resultat.get("err",0)
                total_ok+=ok; total_fail+=err
                log(f"{level}/{mat}/{cat}: {ok} OK, {err} échecs")
                if dest in ("mega","gdrive","dropbox","all"):
                    _legacy_cloud_upload(dest,selected_mega,selected_gdrive,selected_dropbox,
                                         out_dir,folder_name,job_id)
                if ok>0 and (dest in ("mega","gdrive","dropbox","all")):
                    total_uploaded+=1
            except Exception as e:
                log(f"✗ Téléchargement échoué: {e}","error")
                total_fail+=1

        log(f"🏁 Terminé: {total_ok} OK, {total_fail} échecs","success")
    except Exception as e:
        log(f"💥 {e}","error")
    finally:
        status["running"]=False
        status["total_ok"]=total_ok
        status["total_fail"]=total_fail
        status["total_uploaded"]=total_uploaded


def _legacy_cloud_upload(dest, sel_mega, sel_gdrive, sel_dropbox, dir_path, dir_name, job_id):
    providers=[]
    if dest=="all":
        if sel_mega: providers.append(("mega",sel_mega[0]))
        if sel_gdrive: providers.append(("gdrive",sel_gdrive[0]))
        if sel_dropbox: providers.append(("dropbox",sel_dropbox[0]))
    elif dest in ("mega","gdrive","dropbox"):
        key={"mega":sel_mega,"gdrive":sel_gdrive,"dropbox":sel_dropbox}.get(dest,[])
        if key: providers.append((dest,key[0]))
    for prov,compte in providers:
        try:
            cloud_upload.uploader_dossier(prov,compte,dir_path,dir_name,
                                         job_id=job_id,emettre=lambda m:log(m))
        except Exception as e:
            log(f"☁ {prov}/{compte} échoué: {e}","error")

def _cat_meta(cat):
    return {  # (trim, type)
        "t1":("T1","dt"),"t2":("T2","dt"),"t3":("T3","dt"),
        "d1":("T1","devoir"),"e1":("T1","compo"),
        "d2":("T2","devoir"),"e2":("T2","compo"),
        "d3":("T3","devoir"),"e3":("T3","compo"),
        "as_d1":("T1","devoir"),"as_e1":("T1","compo"),
        "as_d2":("T2","devoir"),"as_e2":("T2","compo"),
        "as_d3":("T3","devoir"),"as_e3":("T3","compo"),
        "se_d1":("T1","devoir"),"se_e1":("T1","compo"),
        "se_d2":("T2","devoir"),"se_e2":("T2","compo"),
        "se_d3":("T3","devoir"),"se_e3":("T3","compo"),
        "lp_d1":("T1","devoir"),"lp_e1":("T1","compo"),
        "lp_d2":("T2","devoir"),"lp_e2":("T2","compo"),
        "lp_d3":("T3","devoir"),"lp_e3":("T3","compo"),
        "ge_d1":("T1","devoir"),"ge_e1":("T1","compo"),
        "ge_d2":("T2","devoir"),"ge_e2":("T2","compo"),
        "ge_d3":("T3","devoir"),"ge_e3":("T3","compo"),
        "tm_d1":("T1","devoir"),"tm_e1":("T1","compo"),
        "tm_d2":("T2","devoir"),"tm_e2":("T2","compo"),
        "tm_d3":("T3","devoir"),"tm_e3":("T3","compo"),
        "rattrapage":("","rattrapage"),"cours":("","cours"),
        "pedagogie":("","pedagogie"),"exercices":("","exercices"),
        "homeworks":("","devoir-maison"),"":("","officiel"),
    }.get(cat,("",""))

# Simplistic level mapper (old codes -> new)
nivel_map = {
    "1ap":"premiere-annee-primaire","2ap":"deuxieme-annee-primaire",
    "3ap":"troisieme-annee-primaire","4ap":"quatrieme-annee-primaire",
    "5ap":"cinquieme-annee-primaire",
    "1am":"premiere-annee-moyen","2am":"deuxieme-annee-moyen",
    "3am":"troisieme-annee-moyen","4am":"quatrieme-annee-moyen",
    "1as":"premiere-annee-secondaire","2as":"deuxieme-annee-secondaire",
    "3as":"troisieme-annee-secondaire",
}

# ── NEW MODULAR API ROUTES ─────────────────────────────────────

@app.route("/api/cloud-status")
def api_cloud_status():
    st=config.cloud_configure_status()
    connectes={}
    for prov in st.get("comptes",{}):
        for nom in st["comptes"][prov]:
            compte=config.get_compte(prov,nom)
            if compte and "client_id" in compte:
                cache=Path(config.BASE_DIR)/f"token_cache_gdrive_{nom}.json"
                connectes[f"{prov}::{nom}"] = cache.exists()
            elif compte and ("email" in compte or "access_token" in compte):
                connectes[f"{prov}::{nom}"] = True
    st["connectes"]=connectes
    return jsonify(st)

@app.route("/api/cycles")
def api_cycles():
    site_id = request.args.get("site", "dzexams")
    try:
        mod=get_site(site_id)
    except ValueError:
        return jsonify({"error":"site inconnu"}),404
    cycles=mod.lister_cycles()
    out={}
    for c,info in cycles.items():
        out[c]={"label":info.get("label",c),"niveaux":info["niveaux"]}
    return jsonify(out)

@app.route("/api/matieres",methods=["POST"])
def api_matieres():
    data=request.get_json(force=True)
    site_id=data.get("site","dzexams")
    cycle=data.get("cycle")
    niveau=data.get("niveau")
    try:
        mod=get_site(site_id)
    except ValueError:
        return jsonify({"error":"site inconnu"}),400
    if not niveau:
        return jsonify({"error":"niveau requis"}),400
    job_id=job_manager.creer_job("matieres")
    t=threading.Thread(target=_lancer_matieres,args=(job_id,site_id,cycle,niveau),daemon=True)
    t.start()
    return jsonify({"job_id":job_id})

def _lancer_matieres(job_id,site_id,cycle,niveau):
    try:
        mod=get_site(site_id)
        job_manager.emit(job_id,"log",{"msg":f"📋 Récupération des matières ({niveau})..."})
        matieres=mod.lister_matieres(job_id,cycle,niveau)
        job_manager.finish(job_id,result={"matieres":matieres})
    except Exception as e:
        job_manager.emit(job_id,"log",{"msg":f"✗ {e}"})
        job_manager.finish(job_id,error=str(e))

@app.route("/api/decouvrir",methods=["POST"])
def api_decouvrir():
    data=request.get_json(force=True)
    site_id=data.get("site","dzexams")
    cycle=data.get("cycle")
    niveau=data.get("niveau")
    matieres=data.get("matieres") or []
    try:
        mod=get_site(site_id)
    except ValueError:
        return jsonify({"error":"site inconnu"}),400
    if not niveau:
        return jsonify({"error":"niveau requis"}),400
    job_id=job_manager.creer_job("decouverte")
    t=threading.Thread(target=_lancer_decouverte,args=(job_id,site_id,cycle,niveau,matieres),daemon=True)
    t.start()
    return jsonify({"job_id":job_id})

def _lancer_decouverte(job_id,site_id,cycle,niveau,matieres):
    try:
        mod=get_site(site_id)
        job_manager.emit(job_id,"log",{"msg":f"🔍 Découverte ({niveau})..."})
        fiches=mod.decouvrir(job_id,cycle,niveau,matieres or None)
        if job_manager.is_cancelled(job_id):
            job_manager.emit(job_id,"log",{"msg":"⏹ Annulé."})
            job_manager.finish(job_id,result={"fiches":[],"cancelled":True})
            return
        job_manager.emit(job_id,"log",{"msg":f"✅ {len(fiches)} fiches trouvées."})
        job_manager.finish(job_id,result={"count":len(fiches),"fiches":fiches})
    except Exception as e:
        job_manager.emit(job_id,"log",{"msg":f"✗ {e}"})
        job_manager.finish(job_id,error=str(e))

@app.route("/api/telecharger",methods=["POST"])
def api_telecharger():
    data=request.get_json(force=True)
    site_id=data.get("site","dzexams")
    fiches=data.get("fiches")
    dossier_nom=data.get("dossier") or "export"
    provider=data.get("provider")
    compte_nom=data.get("compte")
    dest=data.get("dest")  # "local"/"mega"/"gdrive"/"dropbox"/"all"
    if not fiches:
        return jsonify({"error":"aucune fiche"}),400
    job_id=job_manager.creer_job("telechargement")
    t=threading.Thread(target=_lancer_telechargement,
                       args=(job_id,site_id,fiches,dossier_nom,provider,compte_nom,dest,data),daemon=True)
    t.start()
    return jsonify({"job_id":job_id})

def _lancer_telechargement(job_id,site_id,fiches,dossier_nom,provider=None,compte_nom=None,dest=None,cfg=None):
    try:
        mod=get_site(site_id)
        racine=DOWNLOADS/dossier_nom
        job_manager.emit(job_id,"log",{"msg":f"🚀 Téléchargement de {len(fiches)} fiches..."})
        resultat=mod.telecharger(job_id,fiches,racine,dossier_nom,separer_corrige=True)
        if job_manager.is_cancelled(job_id):
            job_manager.emit(job_id,"log",{"msg":"⏹ Annulé."})
            job_manager.finish(job_id,result={**resultat,"dossier":str(racine),"cancelled":True})
            return
        if provider and compte_nom and resultat.get("ok",0)>0:
            job_manager.emit(job_id,"log",{"msg":f"☁ Envoi vers {provider} ({compte_nom})..."})
            rc=cloud_upload.uploader_dossier(provider,compte_nom,racine,dossier_nom,
                                             job_id=job_id,emettre=_job_emitter(job_id))
            resultat["cloud"]=rc
        if dest=="all" and resultat.get("ok",0)>0:
            _pairs=[("mega",cfg.get("selected_mega",[])),("gdrive",cfg.get("selected_gdrive",[])),("dropbox",cfg.get("selected_dropbox",[]))]
            for prov,accs in _pairs:
                if accs:
                    job_manager.emit(job_id,"log",{"msg":f"☁ Envoi vers {prov} ({accs[0]})..."})
                    try:
                        rc=cloud_upload.uploader_dossier(prov,accs[0],racine,dossier_nom,
                                                         job_id=job_id,emettre=_job_emitter(job_id))
                        resultat.setdefault("cloud_all",[]).append({prov:rc})
                    except Exception as e:
                        job_manager.emit(job_id,"log",{"msg":f"✗ {prov} échoué: {e}"})
        job_manager.emit(job_id,"log",{"msg":f"🏁 {resultat['ok']} OK, {resultat['err']} échecs."})
        job_manager.finish(job_id,result={**resultat,"dossier":str(racine)})
    except Exception as e:
        job_manager.emit(job_id,"log",{"msg":f"✗ {e}"})
        job_manager.finish(job_id,error=str(e))

@app.route("/api/cloud-upload",methods=["POST"])
def api_cloud_upload():
    data=request.get_json(force=True)
    provider=data.get("provider")
    compte_nom=data.get("compte")
    dossier_nom=data.get("dossier")
    if not provider or not compte_nom or not dossier_nom:
        return jsonify({"error":"provider, compte, dossier requis"}),400
    job_id=job_manager.creer_job("cloud_upload")
    t=threading.Thread(target=_lancer_envoi_cloud,args=(job_id,provider,compte_nom,dossier_nom),daemon=True)
    t.start()
    return jsonify({"job_id":job_id})

def _lancer_envoi_cloud(job_id,provider,compte_nom,dossier_nom):
    try:
        racine=DOWNLOADS/dossier_nom
        if not racine.exists():
            raise RuntimeError(f"Dossier introuvable: {dossier_nom}")
        rc=cloud_upload.uploader_dossier(provider,compte_nom,racine,dossier_nom,
                                         job_id=job_id,emettre=_job_emitter(job_id))
        job_manager.finish(job_id,result=rc)
    except Exception as e:
        job_manager.finish(job_id,error=str(e))

@app.route("/api/zip/<dossier>")
def api_zip(dossier):
    nettoyer=request.args.get("cleanup")=="1"
    racine=DOWNLOADS/dossier
    if not racine.exists():
        return jsonify({"error":"dossier introuvable"}),404
    nom_zip=f"{dossier}.zip"
    chemin_zip=ZIPS_DIR/nom_zip
    with zipfile.ZipFile(chemin_zip,"w",zipfile.ZIP_DEFLATED) as zf:
        for p in racine.rglob("*"):
            if p.is_file():
                zf.write(p,arcname=str(p.relative_to(racine)))
    if nettoyer:
        shutil.rmtree(racine,ignore_errors=True)
    return jsonify({"ok":True,"fichier":str(chemin_zip),"nom":nom_zip})

@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    def gen():
        yield "retry: 2000\n\n"
        for chunk in job_manager.stream(job_id):
            yield chunk
    return Response(gen(),mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/annuler/<job_id>",methods=["POST"])
def api_annuler(job_id):
    job_manager.cancel(job_id)
    return jsonify({"ok":True})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    job=job_manager.get(job_id)
    if not job:
        return jsonify({"error":"inconnu"}),404
    return jsonify({"id":job["id"],"status":job["status"],
                    "result":job["result"],"error":job["error"]})

@app.route("/downloads/<path:filepath>")
def servir_fichier(filepath):
    return send_from_directory(DOWNLOADS,filepath,as_attachment=True)

# ── GDrive OAuth Routes ─────────────────────────────────────────
_gdrive_auth_results={}
_gdrive_flows={}

def _url_gdrive(compte,compte_nom):
    from google_auth_oauthlib.flow import Flow
    redir=compte.get("redirect_uri","http://localhost:5000/api/cloud/google/callback")
    flow=Flow.from_client_config(
        {"web":{"client_id":compte["client_id"],"client_secret":compte["client_secret"],
                "auth_uri":"https://accounts.google.com/o/oauth2/auth",
                "token_uri":"https://oauth2.googleapis.com/token","redirect_uris":[redir]}},
        scopes=["https://www.googleapis.com/auth/drive.file"])
    flow.redirect_uri=redir
    auth_url,_=flow.authorization_url(access_type="offline",prompt="consent",state=compte_nom)
    _gdrive_flows[compte_nom]=flow
    return auth_url

@app.route("/api/cloud/google/auth")
def api_google_auth():
    compte_nom=request.args.get("compte","")
    if not compte_nom:
        return jsonify({"error":"compte requis"}),400
    compte=config.get_compte("gdrive",compte_nom)
    if not compte or "client_id" not in compte:
        return jsonify({"error":"compte non trouvé"}),400
    if compte_nom in _gdrive_auth_results:
        del _gdrive_auth_results[compte_nom]
    try:
        url=_url_gdrive(compte,compte_nom)
    except Exception as e:
        return jsonify({"error":str(e)}),500
    return jsonify({"compte":compte_nom,"url":url,"message":"Fenêtre d'authentification Google à ouvrir."})

@app.route("/api/cloud/google/callback")
def api_google_callback():
    code=request.args.get("code")
    state=request.args.get("state","")
    erreur=request.args.get("error")
    if erreur:
        _gdrive_auth_results[state]={"error":f"Google: {erreur}"}
        return "<p>Erreur. Fermez.</p>"
    if not code:
        return "Code manquant",400
    compte_nom=state
    flow=_gdrive_flows.pop(compte_nom,None)
    if not flow:
        return "Session expirée",400
    try:
        flow.fetch_token(code=code)
        creds=flow.credentials
        cloud_upload.gdrive_oauth2_save_credentials(
            {"access_token":creds.token,"refresh_token":creds.refresh_token},compte_nom)
        _gdrive_auth_results[compte_nom]={"ok":True}
        return '<script>window.close()</script><p>✅ Authentifié !</p>'
    except Exception as e:
        _gdrive_auth_results[compte_nom]={"error":str(e)}
        return f"<p>❌ {e}</p>"

@app.route("/api/cloud/google/auth-status/<compte_nom>")
def api_google_auth_status(compte_nom):
    r=_gdrive_auth_results.get(compte_nom)
    if not r: return jsonify({"status":"pending"})
    if r.get("ok"): return jsonify({"status":"connected"})
    return jsonify({"status":"error","error":r.get("error","")})

# ── Entry ────────────────────────────────────────────────────────
if __name__=='__main__':
    print("="*60)
    print("  DZExams Server v5 - Educ-dz")
    print("  => http://localhost:5000")
    print("="*60)
    app.run(host='0.0.0.0',port=5000,debug=False,threaded=True)
