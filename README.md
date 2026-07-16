# DZExams Scraper v4 — Documentation complète

## 📁 Fichiers inclus

| Fichier | Rôle |
|---|---|
| `dzexams_server.py` | Serveur Flask (backend Selenium + Cloud) |
| `dzexams-web.html` | Interface web (7 étapes + filières + tableau fichiers) |
| `.env.example` | Template de configuration multi-comptes |
| `README.md` | Cette documentation |

---

## 🚀 Installation rapide

### 1. Prérequis
- Python 3.8+ (testé jusqu'à 3.12)
- Google Chrome installé

### 2. Dépendances de base
```cmd
pip install selenium webdriver-manager requests flask python-dotenv
```

### 3. Dépendances optionnelles (selon vos besoins)
```cmd
pip install pycryptodome
```
```cmd
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```
```cmd
pip install dropbox
```
```cmd
pip install pypdf
```

> `pycryptodome` = MEGA · `google-api-*` = Google Drive · `dropbox` = Dropbox · `pypdf` = comptage de pages PDF

### 4. Configurer
```cmd
copy .env.example .env
```
Éditez `.env` avec vos identifiants (voir sections ci-dessous).

### 5. Lancer
```cmd
python dzexams_server.py
```
Ouvrez **http://localhost:5000**

---

## 🎓 Filières — Enseignement Secondaire

Pour **1AS, 2AS, 3AS**, dzexams.com organise les devoirs/examens **par filière**. L'interface affiche désormais un sélecteur de filière obligatoire à l'étape "Trimestres & Types" :

| Filière | Code | Matières concernées |
|---|---|---|
| Filières Scientifiques | `as` | Maths, Physique, SciNat (tronc commun scientifique) |
| Sciences Expérimentales | `se` | Spécifique 2AS/3AS |
| Lettres et Philosophie | `lp` | Toutes matières, filière littéraire |
| Gestion et Économie | `ge` | Toutes matières, filière éco |
| Techniques Mathématiques | `tm` | Toutes matières, filière technique |

**Important** : toutes les filières n'existent pas pour toutes les combinaisons matière/année. Si le téléchargement renvoie "Aucun sujet", essayez une autre filière ou consultez directement la page dzexams.com correspondante.

---

## ☁ Multi-comptes Cloud

Vous pouvez configurer **plusieurs comptes** par service cloud. Le système les utilisera en rotation (chaque combo niveau/matière/catégorie est uploadé sur **tous** les comptes configurés).

### Exemple `.env` avec 2 comptes MEGA :
```env
MEGA_EMAIL=compte1@email.com
MEGA_PASSWORD=motdepasse1
MEGA_FOLDER=/DZExams

MEGA_EMAIL_2=compte2@email.com
MEGA_PASSWORD_2=motdepasse2
MEGA_FOLDER_2=/DZExams_backup
```

Numérotez avec `_2`, `_3`, `_4`, `_5` (jusqu'à 5 comptes MEGA/Dropbox, 3 comptes GDrive).

**Cas d'usage :**
- Répartir le stockage entre plusieurs comptes gratuits (ex: 2× MEGA 50 Go = 100 Go)
- Avoir une sauvegarde redondante sur 2 services différents (`UPLOAD_DESTINATION=all`)

---

## 📦 Informations fichiers récupérées

Après chaque upload, le système affiche dans l'interface un **tableau récapitulatif** :

| Colonne | Source |
|---|---|
| Nom du fichier | Généré localement |
| Taille | Mesurée après téléchargement |
| Pages | Comptées via `pypdf` (si installé) |
| Année | Extraite de dzexams.com |
| Corrigé | Extrait de dzexams.com |
| Cloud | Service(s) + n° de compte utilisé(s) |
| Lien | URL de partage cloud cliquable |

**Limites par service :**
- **MEGA** : lien de partage simplifié (l'API REST publique ne génère pas de lien complet sans bibliothèque officielle)
- **Google Drive** : lien direct `drive.google.com/file/d/{id}/view`, fichier rendu public automatiquement
- **Dropbox** : lien de partage généré via `sharing_create_shared_link_with_settings`

---

## ☁ Configuration détaillée par service

### 🔴 MEGA.nz (50 Go gratuits/compte)

Aucune dépendance lourde — implémentation directe via l'API REST MEGA (compatible Python 3.12+, contrairement à `mega.py` qui est cassé sur les versions récentes).

```cmd
pip install pycryptodome
```

```env
MEGA_EMAIL=votre@email.com
MEGA_PASSWORD=votre_mot_de_passe
MEGA_FOLDER=/DZExams
```

---

### 🟡 Google Drive (15 Go gratuits/compte)

```cmd
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```

**Étapes :**
1. [console.cloud.google.com](https://console.cloud.google.com) → créer un projet
2. **API et services** → **Bibliothèque** → activer **Google Drive API**
3. **Identifiants** → **Créer** → **ID client OAuth 2.0** → type **Application de bureau**
4. ⚠ **Important (fix `redirect_uri_mismatch`)** : dans la configuration de l'app OAuth, ajoutez `http://localhost:8080` aux **URI de redirection autorisés**
5. Téléchargez le JSON → renommez `credentials.json` → placez-le à côté de `dzexams_server.py`

```env
GDRIVE_CREDENTIALS_FILE=credentials.json
GDRIVE_FOLDER_NAME=DZExams
```

Au premier upload, une fenêtre navigateur s'ouvre pour autorisation. `token.json` est créé automatiquement ensuite.

**Pour un 2ème compte**, créez un second projet/credentials et ajoutez :
```env
GDRIVE_CREDENTIALS_FILE_2=credentials_2.json
GDRIVE_FOLDER_NAME_2=DZExams_2
```

---

### 🔵 Dropbox (2 Go gratuits/compte)

```cmd
pip install dropbox
```

**Étapes :**
1. [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) → **Create app**
2. **Scoped access** → **Full Dropbox** → nommez l'app → **Create app**
3. ⚠ **Onglet Permissions** → cochez **`files.content.write`** et **`files.content.read`** → **Submit** *(étape souvent oubliée, cause l'erreur `missing scope`)*
4. **Onglet Settings** → **OAuth 2** → **Generated access token** → **Generate**
5. Si vous aviez déjà un token généré **avant** l'étape 3, régénérez-le

```env
DROPBOX_ACCESS_TOKEN=sl.votre_token
DROPBOX_FOLDER=/DZExams
```

---

## 🖥 Interface web — Rubrique Configuration Cloud

À l'étape **"Destination"**, sélectionner MEGA/GDrive/Dropbox/Tous affiche automatiquement :
- Le guide pas-à-pas pour ce service
- Le bloc `.env` à copier-coller
- Le **statut en temps réel** : comptes détectés, emails/dossiers configurés, authentification GDrive (token présent ou non)

Cette lecture est faite via l'endpoint `/env` du serveur (mots de passe jamais affichés en clair).

---

## 📂 Structure des fichiers téléchargés

```
downloads/
  3as/
    mathematiques/
      as_T1_devoir/
        dzexams_3as_mathematiques_as_T1_devoir_0029_2024.pdf
      as_T1_compo/
        dzexams_3as_mathematiques_as_T1_compo_0012_2024_corrige.pdf
      lp_T1_devoir/
        dzexams_3as_mathematiques_lp_T1_devoir_0008_2023.pdf
```

**Format :**
```
dzexams_{niveau}_{matière}_{filière}_{trimestre}_{type}_{numéro}_{année}[_corrige].pdf
```
> Le segment `{filière}` n'apparaît que pour 1AS/2AS/3AS.

---

## ❓ Problèmes fréquents

| Problème | Solution |
|---|---|
| `ModuleNotFoundError: flask` | `pip install flask` |
| MEGA: `asyncio.coroutine` error | Inutile avec ce script — implémentation REST directe |
| Dropbox: `missing scope files.content.write` | Cochez les permissions dans l'onglet **Permissions** de votre app, puis régénérez le token |
| GDrive: `redirect_uri_mismatch` | Ajoutez `http://localhost:8080` dans les URI de redirection autorisés de votre client OAuth |
| Aucun sujet pour une filière secondaire | Cette filière n'existe peut-être pas pour cette matière/année — essayez une autre filière |
| Pages PDF affichent "—" | Installez `pip install pypdf` |
| Port 5000 déjà utilisé | Modifiez `port=5000` en bas de `dzexams_server.py` |
