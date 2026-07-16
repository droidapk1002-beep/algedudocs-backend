import json, time, re
from pathlib import Path

import config
from scrapers.utils import job_manager


def comptes_disponibles() -> dict:
    return config.cloud_configure_status().get("comptes", {})


def uploader_dossier(provider, nom_compte, racine_locale, nom_dossier_distant, job_id=None, emettre=None):
    def log(msg):
        if emettre:
            emettre(msg)

    compte = config.get_compte(provider, nom_compte)
    if not compte:
        log(f"☁ ✗ Compte cloud introuvable ou mal configuré : {provider}/{nom_compte}")
        return {"ok": 0, "err": 0, "provider": provider, "compte": nom_compte, "fichiers": []}

    fichiers = [p for p in Path(racine_locale).rglob("*") if p.is_file()]
    if not fichiers:
        return {"ok": 0, "err": 0, "provider": provider, "compte": nom_compte, "fichiers": []}

    niveau_school = re.sub(r"_(?:cloud|zip)_\d+$", "", nom_dossier_distant)

    if provider == "gdrive":
        return _upload_gdrive(compte, fichiers, racine_locale, niveau_school, log, nom_compte, job_id)
    if provider == "mega":
        return _upload_mega(compte, fichiers, racine_locale, niveau_school, log, nom_compte, job_id)
    if provider == "dropbox":
        return _upload_dropbox(compte, fichiers, racine_locale, niveau_school, log, nom_compte, job_id)
    if provider == "onedrive":
        return _upload_onedrive(compte, fichiers, racine_locale, niveau_school, log, nom_compte, job_id)
    return {"ok": 0, "err": len(fichiers), "provider": provider, "compte": nom_compte, "fichiers": []}


_gdrive_services = {}
_gdrive_dossiers_cache = {}


def _gdrive_oauth2_cache_path(nom_compte):
    return Path(config.BASE_DIR) / f"token_cache_gdrive_{nom_compte}.json"


def gdrive_oauth2_save_credentials(token_data, nom_compte):
    _gdrive_oauth2_cache_path(nom_compte).write_text(
        json.dumps(token_data, indent=2, ensure_ascii=False), encoding="utf-8")


def _gdrive_oauth2_load_credentials(compte, nom_compte):
    from google.oauth2.credentials import Credentials
    cache_path = _gdrive_oauth2_cache_path(nom_compte)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return Credentials(
            token=data.get("access_token") or data.get("token", ""),
            refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=compte["client_id"],
            client_secret=compte["client_secret"],
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
    except Exception:
        return None


def _gdrive_oauth2_get_service(compte, nom_compte, log):
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    creds = _gdrive_oauth2_load_credentials(compte, nom_compte)
    if not creds:
        raise RuntimeError(f"Compte '{nom_compte}' pas connecté")
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            gdrive_oauth2_save_credentials(
                {"access_token": creds.token, "refresh_token": creds.refresh_token}, nom_compte)
        else:
            raise RuntimeError(f"Token invalide pour '{nom_compte}'")
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    _gdrive_services[nom_compte] = service
    return service


def _gdrive_root_id(service, compte):
    from googleapiclient.errors import HttpError
    root = compte.get("root_folder", "DZExams").lstrip("/")
    try:
        q = f"name = '{root}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        resultats = service.files().list(q=q, fields="files(id)").execute().get("files", [])
        if resultats:
            return resultats[0]["id"]
    except HttpError:
        pass
    meta = {"name": root, "mimeType": "application/vnd.google-apps.folder"}
    dossier = service.files().create(body=meta, fields="id").execute()
    return dossier["id"]


def _gdrive_dossier_id(service, root_id, nom_compte, chemin_relatif_parts):
    cache = _gdrive_dossiers_cache.setdefault(nom_compte, {})
    cle = "/".join(chemin_relatif_parts)
    if cle in cache:
        return cache[cle]
    parent_id = root_id
    chemin_construit = []
    from googleapiclient.errors import HttpError
    for nom in chemin_relatif_parts:
        chemin_construit.append(nom)
        sous_cle = "/".join(chemin_construit)
        if sous_cle in cache:
            parent_id = cache[sous_cle]
            continue
        try:
            q = (f"name = '{nom}' and mimeType = 'application/vnd.google-apps.folder' "
                 f"and '{parent_id}' in parents and trashed = false")
            resultats = service.files().list(q=q, fields="files(id)").execute().get("files", [])
            if resultats:
                dossier_id = resultats[0]["id"]
            else:
                meta = {"name": nom, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
                dossier_id = service.files().create(body=meta, fields="id").execute()["id"]
        except HttpError as e:
            if e.resp.status == 404:
                cache.clear()
                return _gdrive_dossier_id(service, root_id, nom_compte, chemin_relatif_parts)
            raise
        cache[sous_cle] = dossier_id
        parent_id = dossier_id
    return parent_id


def _upload_gdrive(compte, fichiers, racine_locale, nom_dossier_distant, log, nom_compte, job_id=None):
    from googleapiclient.http import MediaFileUpload
    try:
        service = _gdrive_oauth2_get_service(compte, nom_compte, log)
    except Exception as e:
        log(f"☁ ✗ Connexion Google Drive ({nom_compte}) impossible : {e}")
        return {"ok": 0, "err": len(fichiers), "provider": "gdrive", "compte": nom_compte, "fichiers": []}

    root_id = _gdrive_root_id(service, compte)
    ok = err = 0
    infos = []
    leaf_folders = {}

    for fichier in fichiers:
        if job_id and job_manager.is_cancelled(job_id):
            log("☁ ⏹ Envoi annulé.")
            break
        rel = fichier.relative_to(racine_locale)
        if rel.name == "manifeste.json" and not rel.parent.parts:
            continue
        taille = fichier.stat().st_size
        sous_dossiers = (nom_dossier_distant,) + rel.parent.parts
        leaf_key = str(rel.parent)
        leaf_folders[leaf_key] = rel.parent.parts

        try:
            dossier_id = _gdrive_dossier_id(service, root_id, nom_compte, sous_dossiers)
            q = f"name = '{rel.name}' and '{dossier_id}' in parents and trashed = false"
            existants = service.files().list(q=q, fields="files(id, name)").execute().get("files", [])

            if existants:
                stem = Path(rel.name).stem
                ext = Path(rel.name).suffix
                suffix = 1
                while True:
                    nom_test = f"{stem}_{suffix}{ext}"
                    q2 = f"name = '{nom_test}' and '{dossier_id}' in parents and trashed = false"
                    if not service.files().list(q=q2, fields="files(id)").execute().get("files", []):
                        break
                    suffix += 1
                nom_fichier = nom_test
            else:
                nom_fichier = rel.name

            media = MediaFileUpload(str(fichier), resumable=False)
            meta = {"name": nom_fichier, "parents": [dossier_id]}
            cree = service.files().create(body=meta, media_body=media, fields="id").execute()
            file_id = cree["id"]
            try:
                service.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
            except Exception:
                pass
            url_partage = f"https://drive.google.com/uc?id={file_id}&export=download"
            url_cloud = f"https://drive.google.com/file/d/{file_id}/view?usp=drivesdk"
            ok += 1
            infos.append({"nom": nom_fichier, "chemin_relatif": str(rel),
                           "url_cloud": url_cloud, "url_partage": url_partage, "taille_octets": taille, "ok": True, "provider": "google"})
            log(f"☁ ✓ envoyé sur Drive ({nom_compte}) : {rel}")
        except Exception as e:
            err += 1
            infos.append({"nom": rel.name, "chemin_relatif": str(rel),
                           "url_partage": None, "taille_octets": taille, "ok": False, "erreur": str(e)})
            log(f"☁ ✗ échec envoi Drive ({nom_compte}) {rel} : {e}")

    if (racine_locale / "manifeste.json").exists() and leaf_folders:
        try:
            manifest_data = json.loads((racine_locale / "manifeste.json").read_text(encoding="utf-8"))
        except Exception:
            manifest_data = None
        for leaf_key, parts in leaf_folders.items():
            try:
                sous_dossiers = (nom_dossier_distant,) + parts
                dossier_id = _gdrive_dossier_id(service, root_id, nom_compte, sous_dossiers)
                q = f"name = 'manifeste.json' and '{dossier_id}' in parents and trashed = false"
                if service.files().list(q=q, fields="files(id)").execute().get("files", []):
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    nom_manifest = f"manifeste_{ts}.json"
                else:
                    nom_manifest = "manifeste.json"
                if manifest_data:
                    infos_map = {i["chemin_relatif"].replace("\\", "/"): i for i in infos}
                    leaf_prefix = str(Path(*parts)).replace("\\", "/") + "/"
                    filtres = [e for e in manifest_data.get("fichiers", [])
                               if e.get("fichier_local", "").replace("\\", "/").startswith(leaf_prefix)]
                    if not filtres:
                        continue
                    for e in filtres:
                        chemin_local = e.get("fichier_local", "").replace("\\", "/")
                        info_match = infos_map.get(chemin_local)
                        if info_match:
                            e["url_cloud"] = info_match.get("url_cloud") or info_match.get("url_partage")
                            e["url_partage"] = info_match.get("url_partage")
                            e["taille_octets"] = info_match.get("taille_octets", e.get("taille_octets"))
                            e["provider"] = info_match.get("provider")
                    mini = dict(manifest_data)
                    mini["fichiers"] = filtres
                    temp = racine_locale / f"__mini_{nom_manifest}"
                    temp.write_text(json.dumps(mini, ensure_ascii=False, indent=2), encoding="utf-8")
                    media = MediaFileUpload(str(temp), resumable=False)
                    meta = {"name": nom_manifest, "parents": [dossier_id]}
                    service.files().create(body=meta, media_body=media, fields="id").execute()
                    temp.unlink(missing_ok=True)
                else:
                    media = MediaFileUpload(str(manifest_local), resumable=False)
                    meta = {"name": nom_manifest, "parents": [dossier_id]}
                    service.files().create(body=meta, media_body=media, fields="id").execute()
                log(f"☁ ✓ manifeste ({nom_manifest}) → {leaf_key}")
            except Exception as e:
                log(f"☁ ✗ manifeste → {leaf_key} : {e}")

        if manifest_data and any("url_cloud" in e for e in manifest_data.get("fichiers", [])):
            try:
                manifest_local.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    return {"ok": ok, "err": err, "provider": "gdrive", "compte": nom_compte, "fichiers": infos}


def _mega_build_tree(client):
    all_files = client.get_files() or {}
    tree = {}
    for h, info in all_files.items():
        p = info.get("p") or ""
        attr = info.get("a", {})
        name = attr.get("n") if isinstance(attr, dict) else ""
        if name:
            tree.setdefault(p, {})[name] = h
    return tree


def _mega_dossier(client, chemin_parts, cache, tree):
    chemin_parts = tuple(p.strip("/") for p in chemin_parts if p.strip("/"))
    if not chemin_parts:
        return None
    cle = "/".join(chemin_parts)
    if cle in cache:
        return cache[cle]
    parent_handle = None
    parent_key = ""
    for idx, nom in enumerate(chemin_parts):
        sous_cle = "/".join(chemin_parts[:idx + 1])
        if sous_cle in cache:
            parent_handle = cache[sous_cle]
            parent_key = parent_handle or ""
            continue
        children = tree.get(parent_key, {})
        if nom in children:
            handle = children[nom]
        else:
            result = client.create_folder(nom, parent_handle) if parent_handle else client.create_folder(nom)
            handle = result.get(nom) if isinstance(result, dict) else result
            tree.setdefault(parent_key, {})[nom] = handle
        cache[sous_cle] = handle
        parent_handle = handle
        parent_key = handle or ""
    return parent_handle


def _mega_nom_unique(tree, dossier_handle, nom_original):
    stem = Path(nom_original).stem
    ext = Path(nom_original).suffix
    nom_test = nom_original
    suffix = 1
    enfants = tree.get(dossier_handle or "", {})
    while nom_test in enfants:
        nom_test = f"{stem}_{suffix}{ext}"
        suffix += 1
    return nom_test


import asyncio as _asyncio
if not hasattr(_asyncio, "coroutine"):
    _asyncio.coroutine = lambda f: f

_mega_instances = {}


def _mega_get_client(compte, nom_compte):
    if nom_compte in _mega_instances:
        return _mega_instances[nom_compte]
    from mega import Mega
    m = Mega()
    client = m.login(compte["email"], compte["password"])
    _mega_instances[nom_compte] = client
    return client


def _upload_mega(compte, fichiers, racine_locale, nom_dossier_distant, log, nom_compte, job_id=None):
    try:
        client = _mega_get_client(compte, nom_compte)
    except Exception as e:
        log(f"☁ ✗ Connexion Mega ({nom_compte}) impossible : {e}")
        return {"ok": 0, "err": len(fichiers), "provider": "mega", "compte": nom_compte, "fichiers": []}

    cache = {}
    tree = _mega_build_tree(client)
    ok = err = 0
    infos = []
    leaf_folders = {}

    for fichier in fichiers:
        if job_id and job_manager.is_cancelled(job_id):
            log("☁ ⏹ Envoi annulé.")
            break
        rel = fichier.relative_to(racine_locale)
        if rel.name == "manifeste.json" and not rel.parent.parts:
            continue
        taille = fichier.stat().st_size
        sous_dossiers = (compte["folder"], nom_dossier_distant) + rel.parent.parts
        leaf_key = str(rel.parent)
        leaf_folders[leaf_key] = rel.parent.parts

        try:
            dossier_handle = _mega_dossier(client, sous_dossiers, cache, tree)
            if not dossier_handle:
                raise RuntimeError("Impossible de créer/trouver le dossier MEGA de destination")
            nom_unique = _mega_nom_unique(tree, dossier_handle, rel.name)
            fichier_uploade = client.upload(str(fichier), dossier_handle, dest_filename=nom_unique)
            url_partage = None
            try:
                url_partage = client.get_upload_link(fichier_uploade)
            except Exception:
                pass
            ok += 1
            infos.append({"nom": nom_unique, "chemin_relatif": str(rel),
                            "url_cloud": url_partage, "url_partage": url_partage, "taille_octets": taille, "ok": True, "provider": "mega"})
            log(f"☁ ✓ envoyé sur Mega ({nom_compte}) : {rel}")
        except Exception as e:
            err += 1
            infos.append({"nom": rel.name, "chemin_relatif": str(rel),
                           "url_partage": None, "taille_octets": taille, "ok": False, "erreur": str(e)})
            log(f"☁ ✗ échec envoi Mega ({nom_compte}) {rel} : {e}")

    # Upload manifeste.json to the root folder
    if (racine_locale / "manifeste.json").exists():
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            sous_dossiers = (compte["folder"], nom_dossier_distant)
            dossier_handle = _mega_dossier(client, sous_dossiers, cache, tree)
            nom_manifest = "manifeste.json"
            if tree.get(dossier_handle or "", {}).get(nom_manifest):
                nom_manifest = f"manifeste_{ts}.json"
            # Ajouter les URLs cloud au manifeste
            infos_map = {i["chemin_relatif"].replace("\\", "/"): i for i in infos}
            manifest_data = json.loads((racine_locale / "manifeste.json").read_text(encoding="utf-8"))
            for e in manifest_data.get("fichiers", []):
                chemin_local = e.get("fichier_local", "").replace("\\", "/")
                info_match = infos_map.get(chemin_local)
                if info_match:
                    e["url_cloud"] = info_match.get("url_cloud") or info_match.get("url_partage")
                    e["url_partage"] = info_match.get("url_partage")
                    e["taille_octets"] = info_match.get("taille_octets", e.get("taille_octets"))
                    e["provider"] = info_match.get("provider")
            temp = racine_locale / f"__{nom_manifest}"
            temp.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            client.upload(str(temp), dossier_handle, dest_filename=nom_manifest)
            temp.unlink(missing_ok=True)
            # Sauvegarder le manifest racine avec les URLs cloud
            (racine_locale / "manifeste.json").write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"☁ ✓ manifeste ({nom_manifest}) → {compte['folder']}/{nom_dossier_distant}")
        except Exception as e:
            log(f"☁ ✗ manifeste → {e}")

    return {"ok": ok, "err": err, "provider": "mega", "compte": nom_compte, "fichiers": infos}


_dropbox_instances = {}


def _dropbox_get_client(compte, nom_compte):
    if nom_compte in _dropbox_instances:
        return _dropbox_instances[nom_compte]
    import dropbox
    dbx = dropbox.Dropbox(compte["access_token"])
    _dropbox_instances[nom_compte] = dbx
    return dbx


def _upload_dropbox(compte, fichiers, racine_locale, nom_dossier_distant, log, nom_compte, job_id=None):
    try:
        dbx = _dropbox_get_client(compte, nom_compte)
    except Exception as e:
        log(f"☁ ✗ Connexion Dropbox ({nom_compte}) impossible : {e}")
        return {"ok": 0, "err": len(fichiers), "provider": "dropbox", "compte": nom_compte, "fichiers": []}

    ok = err = 0
    infos = []

    for fichier in fichiers:
        if job_id and job_manager.is_cancelled(job_id):
            log("☁ ⏹ Envoi annulé.")
            break
        rel = fichier.relative_to(racine_locale)
        if rel.name == "manifeste.json" and not rel.parent.parts:
            continue
        taille = fichier.stat().st_size
        dest_path = f"/{compte['folder']}/{nom_dossier_distant}/{rel}".replace("\\", "/")

        try:
            CHUNK = 50 * 1024 * 1024
            fsize = fichier.stat().st_size
            if fsize > CHUNK:
                with open(fichier, "rb") as fh:
                    session = dbx.files_upload_session_start(fh.read(CHUNK))
                    cursor = dropbox.files.UploadSessionCursor(session_id=session.session_id, offset=fsize)
                    commit = dropbox.files.CommitInfo(path=dest_path)
                    dbx.files_upload_session_finish(fh.read(), cursor, commit)
            else:
                with open(fichier, "rb") as fh:
                    dbx.files_upload(fh.read(), dest_path, mute=True)

            try:
                link = dbx.sharing_create_shared_link_with_settings(dest_path)
                url_partage = link.url
            except Exception:
                url_partage = None

            ok += 1
            infos.append({"nom": rel.name, "chemin_relatif": str(rel),
                            "url_cloud": url_partage, "url_partage": url_partage, "taille_octets": taille, "ok": True, "provider": "dropbox"})
            log(f"☁ ✓ envoyé sur Dropbox ({nom_compte}) : {rel}")
        except Exception as e:
            err += 1
            infos.append({"nom": rel.name, "chemin_relatif": str(rel),
                           "url_partage": None, "taille_octets": taille, "ok": False, "erreur": str(e)})
            log(f"☁ ✗ échec envoi Dropbox ({nom_compte}) {rel} : {e}")

    # Upload manifeste.json to the root folder
    if (racine_locale / "manifeste.json").exists():
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest_root = f"/{compte['folder']}/{nom_dossier_distant}/"
            nom_manifest = "manifeste.json"
            try:
                dbx.files_get_metadata(dest_root + nom_manifest)
                nom_manifest = f"manifeste_{ts}.json"
            except Exception:
                pass
            # Ajouter les URLs cloud au manifeste
            infos_map = {i["chemin_relatif"].replace("\\", "/"): i for i in infos}
            manifest_data = json.loads((racine_locale / "manifeste.json").read_text(encoding="utf-8"))
            for e in manifest_data.get("fichiers", []):
                chemin_local = e.get("fichier_local", "").replace("\\", "/")
                info_match = infos_map.get(chemin_local)
                if info_match:
                    e["url_cloud"] = info_match.get("url_cloud") or info_match.get("url_partage")
                    e["url_partage"] = info_match.get("url_partage")
                    e["taille_octets"] = info_match.get("taille_octets", e.get("taille_octets"))
                    e["provider"] = info_match.get("provider")
            temp = racine_locale / f"__{nom_manifest}"
            temp.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            with open(temp, "rb") as fh:
                dbx.files_upload(fh.read(), dest_root + nom_manifest, mute=True)
            temp.unlink(missing_ok=True)
            # Sauvegarder le manifest racine avec les URLs cloud
            (racine_locale / "manifeste.json").write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"☁ ✓ manifeste ({nom_manifest}) → {compte['folder']}/{nom_dossier_distant}")
        except Exception as e:
            log(f"☁ ✗ manifeste → {e}")

    return {"ok": ok, "err": err, "provider": "dropbox", "compte": nom_compte, "fichiers": infos}


GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _onedrive_cache_path(nom_compte):
    return Path(config.BASE_DIR) / f"token_cache_onedrive_{nom_compte}.json"


def _onedrive_get_token(compte, nom_compte, log):
    import msal
    cache_path = _onedrive_cache_path(nom_compte)
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    app_msal = msal.PublicClientApplication(
        compte["client_id"],
        authority=f"https://login.microsoftonline.com/{compte['tenant']}",
        token_cache=cache,
    )
    scopes = ["Files.ReadWrite"]
    comptes_connus = app_msal.get_accounts()
    resultat = None
    if comptes_connus:
        resultat = app_msal.acquire_token_silent(scopes, account=comptes_connus[0])

    if not resultat:
        flow = app_msal.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"Impossible de démarrer l'authentification OneDrive : {flow}")
        log(f"☁ 🔑 Connexion OneDrive ({nom_compte}) — ouvrez {flow['verification_uri']} "
            f"et entrez le code : {flow['user_code']}")
        resultat = app_msal.acquire_token_by_device_flow(flow)

    if "access_token" not in resultat:
        raise RuntimeError(f"Authentification OneDrive échouée : {resultat.get('error_description', resultat)}")

    if cache.has_state_changed:
        try:
            cache_path.write_text(cache.serialize(), encoding="utf-8")
        except Exception:
            pass
    return resultat["access_token"]


def _onedrive_dossier_id(token, compte, chemin_parts, cache):
    import requests as req
    cle = "/".join(chemin_parts)
    if cle in cache:
        return cache[cle]
    headers = {"Authorization": f"Bearer {token}"}
    chemin_courant = compte["folder_path"].strip("/")
    parent_id = None
    for nom in chemin_parts:
        chemin_courant = f"{chemin_courant}/{nom}" if chemin_courant else nom
        if chemin_courant in cache:
            parent_id = cache[chemin_courant]
            continue
        r = req.get(f"{GRAPH_BASE}/me/drive/root:/{chemin_courant}", headers=headers)
        if r.status_code == 200:
            dossier_id = r.json()["id"]
        else:
            chemin_parent = "/".join(chemin_courant.split("/")[:-1])
            url_creation = (f"{GRAPH_BASE}/me/drive/root:/{chemin_parent}:/children"
                             if chemin_parent else f"{GRAPH_BASE}/me/drive/root/children")
            resp = req.post(url_creation, headers=headers, json={
                "name": nom, "folder": {}, "@microsoft.graph.conflictBehavior": "replace",
            })
            resp.raise_for_status()
            dossier_id = resp.json()["id"]
        cache[chemin_courant] = dossier_id
        parent_id = dossier_id
    return parent_id


def _upload_onedrive(compte, fichiers, racine_locale, nom_dossier_distant, log, nom_compte, job_id=None):
    import requests as req
    try:
        token = _onedrive_get_token(compte, nom_compte, log)
    except Exception as e:
        log(f"☁ ✗ Connexion OneDrive ({nom_compte}) impossible : {e}")
        return {"ok": 0, "err": len(fichiers), "provider": "onedrive", "compte": nom_compte, "fichiers": []}

    cache = {}
    headers = {"Authorization": f"Bearer {token}"}
    ok = err = 0
    infos = []
    leaf_folders = {}
    for fichier in fichiers:
        if job_id and job_manager.is_cancelled(job_id):
            log("☁ ⏹ Envoi annulé.")
            break
        rel = fichier.relative_to(racine_locale)
        if rel.name == "manifeste.json" and not rel.parent.parts:
            continue
        sous_dossiers = (nom_dossier_distant,) + rel.parent.parts
        taille = fichier.stat().st_size
        try:
            _onedrive_dossier_id(token, compte, sous_dossiers, cache)
            chemin_distant = "/".join((compte["folder_path"].strip("/"), nom_dossier_distant,
                                        *rel.parent.parts, fichier.name)).strip("/")
            if taille <= 4 * 1024 * 1024:
                with open(fichier, "rb") as fh:
                    r = req.put(f"{GRAPH_BASE}/me/drive/root:/{chemin_distant}:/content",
                                headers=headers, data=fh.read())
                r.raise_for_status()
                item = r.json()
            else:
                r = req.post(f"{GRAPH_BASE}/me/drive/root:/{chemin_distant}:/createUploadSession",
                             headers=headers, json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
                r.raise_for_status()
                upload_url = r.json()["uploadUrl"]
                with open(fichier, "rb") as fh:
                    data = fh.read()
                r2 = req.put(upload_url, headers={"Content-Range": f"bytes 0-{taille - 1}/{taille}"}, data=data)
                r2.raise_for_status()
                item = r2.json()

            url_partage = item.get("webUrl")
            if not url_partage:
                try:
                    item_id = item.get("id")
                    rlink = req.post(f"{GRAPH_BASE}/me/drive/items/{item_id}/createLink",
                                      headers=headers, json={"type": "view", "scope": "anonymous"})
                    rlink.raise_for_status()
                    url_partage = rlink.json().get("link", {}).get("webUrl")
                except Exception:
                    pass
            ok += 1
            infos.append({"nom": fichier.name, "chemin_relatif": str(rel),
                            "url_cloud": url_partage, "url_partage": url_partage, "taille_octets": taille, "ok": True, "provider": "onedrive"})
            log(f"☁ ✓ envoyé sur OneDrive ({nom_compte}) : {rel}")
        except Exception as e:
            err += 1
            infos.append({"nom": fichier.name, "chemin_relatif": str(rel),
                           "url_partage": None, "taille_octets": taille, "ok": False, "erreur": str(e)})
            log(f"☁ ✗ échec envoi OneDrive ({nom_compte}) {rel} : {e}")
    # Upload manifeste.json to the root folder
    if (racine_locale / "manifeste.json").exists():
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            chemin_distant = f"{compte['folder_path'].strip('/')}/{nom_dossier_distant}"
            root_id = _onedrive_dossier_id(token, compte, [nom_dossier_distant], cache)
            nom_manifest = "manifeste.json"
            r = req.get(f"{GRAPH_BASE}/me/drive/root:/{chemin_distant}:/children",
                         headers=headers, params={"$filter": f"name eq '{nom_manifest}'"})
            if r.status_code == 200 and r.json().get("value"):
                nom_manifest = f"manifeste_{ts}.json"
            # Ajouter les URLs cloud au manifeste
            infos_map = {i["chemin_relatif"].replace("\\", "/"): i for i in infos}
            manifest_data = json.loads((racine_locale / "manifeste.json").read_text(encoding="utf-8"))
            for e in manifest_data.get("fichiers", []):
                chemin_local = e.get("fichier_local", "").replace("\\", "/")
                info_match = infos_map.get(chemin_local)
                if info_match:
                    e["url_cloud"] = info_match.get("url_cloud") or info_match.get("url_partage")
                    e["url_partage"] = info_match.get("url_partage")
                    e["taille_octets"] = info_match.get("taille_octets", e.get("taille_octets"))
                    e["provider"] = info_match.get("provider")
            temp = racine_locale / f"__{nom_manifest}"
            temp.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            with open(temp, "rb") as fh:
                data = fh.read()
            r = req.put(f"{GRAPH_BASE}/me/drive/root:/{chemin_distant}/{nom_manifest}:/content",
                         headers=headers, data=data)
            r.raise_for_status()
            temp.unlink(missing_ok=True)
            # Sauvegarder le manifest racine avec les URLs cloud
            (racine_locale / "manifeste.json").write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"☁ ✓ manifeste ({nom_manifest}) → {nom_dossier_distant}")
        except Exception as e:
            log(f"☁ ✗ manifeste → {e}")

    return {"ok": ok, "err": err, "provider": "onedrive", "compte": nom_compte, "fichiers": infos}
