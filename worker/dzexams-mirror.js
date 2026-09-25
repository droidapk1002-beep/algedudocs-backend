/**
 * dzexams-mirror — Cloudflare Worker
 * =============================================================================
 * Problème : dzexams.com est derrière Cloudflare et renvoie 403 aux IP de
 * datacenter (Render, Railway, VPS...). Le backend, hébergé sur Render, se
 * fait donc bloquer. En revanche une IP Cloudflare est acceptée.
 *
 * Solution : ce Worker récupère la page depuis l.edge Cloudflare, la met en
 * cache (l'origine envoie s-maxage=86400, le contenu est public et stable) et
 * la sert au backend. Aucun limite de débit, aucun coût.
 *
 * Déploiement (dashboard Cloudflare > Workers & Pages > Create > Edit code) :
 *   1. Coller ce fichier.
 *   2. Remplacer ALLOWED_ORIGIN par le domaine du backend Render
 *      (ex: https://algedudocs-backend.onrender.com).
 *   3. Deploy, puis mettre DZEXAMS_MIRROR=https://<nom-du-worker>.workers.dev
 *      comme variable d'environnement sur Render.
 *
 * Le backend appelle :  GET <worker>/https://www.dzexams.com/fr/2as
 * et reçoit le HTML brut de dzexams.
 */

const ALLOWED_ORIGIN = 'https://algedudocs-backend.onrender.com';
const ALLOWED_HOSTS = ['www.dzexams.com', 'dzexams.com'];
const CACHE_TTL_SECONDS = 3600;
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

function corsHeaders(request) {
  const origin = request.headers.get('Origin') || '';
  return {
    'Access-Control-Allow-Origin': ALLOWED_ORIGIN.includes(origin) ? origin : ALLOWED_ORIGIN,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }
    if (request.method !== 'GET') {
      return new Response('method not allowed', { status: 405 });
    }

    const brut = new URL(request.url).pathname.replace(/^\//, '');
    if (!brut.startsWith('http://') && !brut.startsWith('https://')) {
      return new Response(
        'usage: GET <worker>/<url complete encodee>\n' +
        'ex: GET <worker>/https://www.dzexams.com/fr/2as\n',
        { status: 400, headers: corsHeaders(request) },
      );
    }

    let url;
    try {
      url = new URL(brut.startsWith('http%3A') ? decodeURIComponent(brut) : brut);
    } catch (_) {
      return new Response('url invalide', { status: 400, headers: corsHeaders(request) });
    }

    if (!ALLOWED_HOSTS.includes(url.hostname)) {
      return new Response('host non autorise', { status: 403, headers: corsHeaders(request) });
    }

    const cache = caches.default;
    const cle = new Request(url.toString(), { method: 'GET' });
    let reponse = await cache.match(cle);
    let depuisCache = true;

    if (!reponse) {
      depuisCache = false;
      const amont = await fetch(url.toString(), {
        headers: {
          'User-Agent': UA,
          'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
          'Accept-Language': 'ar,fr;q=0.9,en;q=0.8',
        },
        cf: { cacheTtl: CACHE_TTL_SECONDS, cacheEverything: true },
      });
      if (!amont.ok) {
        return new Response(
          `origine: ${amont.status} ${amont.statusText}`,
          { status: amont.status, headers: corsHeaders(request) },
        );
      }
      reponse = new Response(amont.body, amont);
      reponse.headers.set('X-Mirror-Cache', 'MISS');
      await cache.put(cle, reponse.clone());
    }

    const entetes = new Headers(reponse.headers);
    entetes.set('X-Mirror-Cache', depuisCache ? 'HIT' : 'MISS');
    for (const [k, v] of Object.entries(corsHeaders(request))) {
      entetes.set(k, v);
    }
    entetes.set('Cache-Control', `public, max-age=${CACHE_TTL_SECONDS}`);
    return new Response(reponse.body, { status: reponse.status, headers: entetes });
  },
};
