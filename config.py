import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _charger_env(path: Path):
    if not path.exists():
        return
    try:
        for ligne in path.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, _, valeur = ligne.partition("=")
            cle = cle.strip()
            valeur = valeur.strip().strip('"').strip("'")
            if cle and cle not in os.environ:
                os.environ[cle] = valeur
    except Exception:
        pass


try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    _charger_env(ENV_PATH)


def get(cle: str, defaut=None):
    return os.environ.get(cle, defaut)


def get_bool(cle: str, defaut=False) -> bool:
    val = os.environ.get(cle)
    if val is None:
        return defaut
    return val.strip().lower() in ("1", "true", "yes", "oui", "on")


def get_list(cle: str):
    val = get(cle, "")
    return [v.strip() for v in val.split(",") if v.strip()]


CLOUD_AUTO_UPLOAD = get_bool("CLOUD_AUTO_UPLOAD", False)


def _comptes_gdrive_oauth2():
    comptes = {}
    # Nouveau format: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ...
    indices = set()
    if get("GOOGLE_CLIENT_ID"):
        indices.add(0)
    for i in range(20):
        if get(f"GOOGLE_CLIENT_ID_{i}"):
            indices.add(i)
    for idx in sorted(indices):
        suffix = f"_{idx}" if idx > 0 else ""
        nom = get(f"GOOGLE_ACCOUNT_LABEL{suffix}") or f"gdrive_{idx}"
        comptes[nom] = {
            "client_id": get(f"GOOGLE_CLIENT_ID{suffix}"),
            "client_secret": get(f"GOOGLE_CLIENT_SECRET{suffix}"),
            "redirect_uri": get(f"GOOGLE_REDIRECT_URI{suffix}",
                                "http://localhost:5000/api/cloud/google/callback"),
            "root_folder": get(f"GOOGLE_DRIVE_ROOT_FOLDER{suffix}", "DZExams"),
            "pret": True,
        }
    # Ancien format: credentials.json (fichier unique) — pas de multi-compte
    if not comptes:
        creds_file = Path(BASE_DIR) / "credentials.json"
        token_file = Path(BASE_DIR) / "token.json"
        if creds_file.exists() and token_file.exists():
            import json
            try:
                data = json.loads(creds_file.read_text(encoding="utf-8"))
                web = data.get("web", {})
                if web.get("client_id"):
                    comptes["principale"] = {
                        "client_id": web["client_id"],
                        "client_secret": web.get("client_secret", ""),
                        "redirect_uri": web.get("redirect_uris", ["http://localhost:5000/api/cloud/google/callback"])[0],
                        "root_folder": "DZExams",
                        "pret": True,
                    }
            except Exception:
                pass
    return comptes


def _comptes_mega():
    comptes = {}
    # Nouveau format: MEGA_ACCOUNTS=principale,secondaire + MEGA_PRINCIPALE_EMAIL=...
    for nom in get_list("MEGA_ACCOUNTS"):
        prefixe = f"MEGA_{nom.upper()}_"
        email = get(f"{prefixe}EMAIL", "")
        password = get(f"{prefixe}PASSWORD", "")
        dossier = get(f"{prefixe}FOLDER", "DZExams")
        comptes[nom] = {
            "email": email,
            "password": password,
            "folder": dossier,
            "pret": bool(email and password),
        }
    # Ancien format: MEGA_EMAIL_1, MEGA_PASSWORD_1, MEGA_ACCOUNT_LABEL_1
    if not comptes:
        for i in range(0, 20):
            suffix = f"_{i}" if i > 0 else ""
            email = get(f"MEGA_EMAIL{suffix}")
            if not email:
                continue
            label = get(f"MEGA_ACCOUNT_LABEL{suffix}") or email
            password = get(f"MEGA_PASSWORD{suffix}")
            dossier = get(f"MEGA_FOLDER{suffix}") or "DZExams"
            comptes[label] = {
                "email": email,
                "password": password,
                "folder": dossier,
                "pret": bool(email and password),
            }
    return comptes


def _comptes_dropbox():
    comptes = {}
    # Nouveau format: DROPBOX_ACCOUNTS=...
    for nom in get_list("DROPBOX_ACCOUNTS"):
        prefixe = f"DROPBOX_{nom.upper()}_"
        token = get(f"{prefixe}ACCESS_TOKEN", "")
        dossier = get(f"{prefixe}FOLDER", "DZExams")
        comptes[nom] = {
            "access_token": token,
            "folder": dossier,
            "pret": bool(token),
        }
    # Ancien format: DROPBOX_ACCESS_TOKEN, DROPBOX_ACCESS_TOKEN_1, ...
    if not comptes:
        for i in range(0, 10):
            suffix = f"_{i}" if i > 0 else ""
            token = get(f"DROPBOX_ACCESS_TOKEN{suffix}")
            if not token:
                continue
            label = get(f"DROPBOX_ACCOUNT_LABEL{suffix}") or f"dropbox_{i}"
            dossier = get(f"DROPBOX_FOLDER{suffix}") or "DZExams"
            comptes[label] = {
                "access_token": token,
                "folder": dossier,
                "pret": True,
            }
    return comptes


def _comptes_onedrive():
    comptes = {}
    for nom in get_list("ONEDRIVE_ACCOUNTS"):
        prefixe = f"ONEDRIVE_{nom.upper()}_"
        client_id = get(f"{prefixe}CLIENT_ID", "")
        tenant = get(f"{prefixe}TENANT", "consumers")
        dossier = get(f"{prefixe}FOLDER_PATH", "DZExams")
        comptes[nom] = {
            "client_id": client_id,
            "tenant": tenant,
            "folder_path": dossier,
            "pret": bool(client_id),
        }
    return comptes


def tous_les_comptes() -> dict:
    resultat = {}
    gdrive = _comptes_gdrive_oauth2()
    if gdrive:
        resultat["gdrive"] = gdrive
    for provider, fn in (("mega", _comptes_mega), ("dropbox", _comptes_dropbox), ("onedrive", _comptes_onedrive)):
        comptes = {nom: infos for nom, infos in fn().items() if infos["pret"]}
        if comptes:
            resultat[provider] = comptes
    return resultat


def compte_existe(provider: str, nom_compte: str) -> bool:
    comptes = tous_les_comptes()
    return provider in comptes and nom_compte in comptes[provider]


def get_compte(provider: str, nom_compte: str) -> dict:
    comptes = tous_les_comptes()
    return comptes.get(provider, {}).get(nom_compte)


def cloud_configure_status() -> dict:
    comptes = tous_les_comptes()
    if not comptes:
        return {"pret": False, "comptes": {}, "detail": "Aucun compte cloud configuré"}
    return {
        "pret": True,
        "comptes": {provider: list(noms.keys()) for provider, noms in comptes.items()},
        "detail": "Configuré",
    }
