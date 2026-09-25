# Miroir Cloudflare Worker — débloquer le 403 de dzexams.com

## Le problème

`dzexams.com` est derrière Cloudflare. Cloudflare renvoie **403 Forbidden** aux
IP de datacenter (Render, Railway, VPS…). Le backend hébergé sur Render se fait
donc bloquer, alors que la même requête depuis une IP résidentielle passe
normalement.

Preuve : le backend répond `200` sur `/api/cycles/dzexams` (aucune requête
dzexams), mais le job `/api/matieres` échoue avec
`403 Client Error: Forbidden for url: https://www.dzexams.com/fr/2as`.

## Le second problème : l'aperçu des PDF

Les PDF de `/uploads/sujets/*.pdf` exigent un en-tête
**`Referer: https://www.dzexams.com/`**. Vérifié :

| Requête | Résultat |
|---|---|
| PDF sans Referer | `403` |
| PDF avec `Referer: dzexams.com` | `200` + `%PDF-` |
| PDF avec `Referer: blogspot.com` | `403` |

Une redirection 302 depuis le blog envoie un Referer blogspot : le navigateur
aurait donc lui aussi obtenu un 403. Le PDF doit être **servi** par le Worker,
qui ajoute le bon Referer en amont. C'est ce que fait ce Worker, et
`/api/resolve-pdf` redirige vers lui quand `DZEXAMS_MIRROR` est défini.

**Sans le Worker, l'icône 👁 ne peut pas fonctionner.** La découverte et le
téléchargement, eux, fonctionnent grâce au relais.

## La solution : un Worker qui relaie depuis l'edge Cloudflare

Une IP Cloudflare n'est pas bloquée par Cloudflare. Ce Worker récupère la page
ou le PDF, la met en cache, et la sert au backend. Gratuit, sans limite de débit.

## Mise en place

1. Dashboard Cloudflare → **Workers & Pages** → **Create** → **Start from scratch** → **Hello World** → **Deploy**
2. **Edit code** → remplacer le contenu par `dzexams-mirror.js` de ce dossier
3. Remplacer `ALLOWED_ORIGIN` en haut du fichier par le domaine de votre backend :
   ```
   const ALLOWED_ORIGIN = 'https://algedudocs-backend.onrender.com';
   ```
4. **Deploy**
5. Sur Render, dans les variables d'environnement du service :
   ```
   DZEXAMS_MIRROR = https://<nom-du-worker>.<votre-sous-domaine>.workers.dev
   ```
6. Redéployer le service Render (Deploy → Manual Deploy).

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `DZEXAMS_MIRROR` | *(vide)* | URL du Worker. **Recommandé** : c'est le chemin le plus fiable. |
| `DZEXAMS_RELAY` | `jina` | Relais de secours si le direct échoue. `off` pour désactiver. |
| `JINA_API_KEY` | *(vide)* | Clé r.jina.ai. Fortement conseillée : sans clé, le quota gratuit est bas. |
| `DZEXAMS_CACHE_TTL` | `1800` | Durée du cache HTML en secondes. `0` désactive. |
| `DZEXAMS_PROXY` | *(vide)* | Proxy HTTP classique, ex. `http://user:pass@host:port`. |

## Ordre de résolution

1. Cache local (TTL `DZEXAMS_CACHE_TTL`)
2. Requête directe à dzexams.com
3. Miroir Worker (`DZEXAMS_MIRROR`)
4. Relais jina (`DZEXAMS_RELAY`)

## Notes

- Une découverte complète d'un niveau = ~200 pages. Le quota gratuit de
  r.jina.ai ne suffit pas : **le Worker miroir est la solution à privilégier**,
  le jina n'est qu'un filet de sécurité.
- Les relais (jina, proxies) **refusent les User-Agents qui usurpent un
  navigateur** (403). Le code s'identifie donc honnêtement comme
  `algedudocs-backend/1.0` auprès des relais. C'est volontaire.
- Le Worker n'accepte que `dzexams.com` et `www.dzexams.com` (sans www).
- **En-têtes de cache** : `CACHE_TTL_HTML` = 1 h, `CACHE_TTL_PDF` = 24 h
  (en tête de fichier). Le PDF peut être volumineux : si le cache le refuse,
  le Worker sert quand même la réponse.
- Vérifier que le miroir répond pour un PDF :
  ```bash
  curl -I "https://<worker>/https://www.dzexams.com/uploads/sujets/xxx.pdf"
  ```
  Attendu : `200`, `Content-Type: application/pdf`.
- Alternative plus radicale si le miroir ne suffit pas : changer d'hébergement
  (VPS, ou chez vous avec un tunnel Cloudflare).
