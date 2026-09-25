/**
 * dzexams-mirror — Cloudflare Worker
 * =============================================================================
 * Problème 1 : dzexams.com est derrière Cloudflare, qui renvoie 403 aux IP de
 * datacenter (Render, Railway, VPS...). Le backend hébergé sur Render se fait
 * donc bloquer. En revanche une IP Cloudflare est acceptée.
 *
 * Problème 2 : les PDF de /uploads/sujets/ exigent un en-tête
 * `Referer: https://www.dzexams.com/`. Sans lui ils répondent 403, même avec un
 * User-Agent de navigateur. Or une redirection 302 depuis le blog envoie un
 * Referer blogspot : le navigateur n'aurait rien. Il faut donc servir le PDF
 * soi-même, en proxy.
 *
 * Solution : ce Worker relaie les pages ET les PDF depuis l'edge Cloudflare,
 * avec le Referer correct, et les met en cache. Gratuit, sans limite de débit.
 *
 * Déploiement (dashboard Cloudflare > Workers & Pages > Create > Edit code) :
 *   1. Coller ce fichier.
 *   2. Remplacer ALLOWED_ORIGIN par le domaine du backend Render
 *      (ex: https://algedudocs-backend.onrender.com).
 *   3. Deploy, puis mettre DZEXAMS_MIRROR=https://<nom-du-worker>.workers.dev
 *      comme variable d'environnement sur Render.
 *
 * Appels du backend :
 *   GET <worker>/https://www.dzexams.com/fr/2as              -> HTML
 *   GET <worker>/https://www.dzexams.com/uploads/sujets/x.pdf -> PDF
 */

const ALLOWED_ORIGIN = 'https://algedudocs-backend.onrender.com';
const ALLOWED_HOSTS = ['www.dzexams.com', 'dzexams.com'];
const CACHE_TTL_HTML = 3600;
const CACHE_TTL_PDF = 86400;
const REFERER = 'https://www.dzexams.com/';
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

function corsHeaders(request) {
  const origin = request.headers.get('Origin') || '';
  return {
    'Access-Control-Allow-Origin': origin === ALLOWED_ORIGIN ? origin : ALLOWED_ORIGIN,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

function cible(racine) {
  const brut = new URL(racine).pathname.replace(/^\//, '');
  if (!brut.startsWith('http://') && !brut.startsWith('https://')) return null;
  const decode = brut.startsWith('http%3A') || brut.startsWith('https%3A')
    ? decodeURIComponent(brut)
    : brut;
  try {
    return new URL(decode);
  } catch (_) {
    return null;
  }
}

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('method not allowed', { status: 405 });
    }

    const url = cible(request.url);
    if (!url) {
      return new Response(
        'usage: GET <worker>/<url complete>\n' +
        'ex: GET <worker>/https://www.dzexams.com/fr/2as\n',
        { status: 400, headers: corsHeaders(request) },
      );
    }
    if (!ALLOWED_HOSTS.includes(url.hostname)) {
      return new Response('host non autorise', { status: 403, headers: corsHeaders(request) });
    }

    const estPdf = /\.pdf(\?|$)/i.test(url.pathname);
    const ttl = estPdf ? CACHE_TTL_PDF : CACHE_TTL_HTML;
    const cache = caches.default;
    const cle = new Request(url.toString(), { method: 'GET' });
    const entetesUpstream = {
      'User-Agent': UA,
      'Accept': estPdf
        ? 'application/pdf,*/*'
        : 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'Accept-Language': 'ar,fr;q=0.9,en;q=0.8',
    };
    if (estPdf) {
      entetesUpstream.Referer = REFERER;
    }

    let reponse = await cache.match(cle);
    let depuisCache = Boolean(reponse);

    if (!reponse) {
      const amont = await fetch(url.toString(), {
        headers: entetesUpstream,
        cf: { cacheTtl: ttl, cacheEverything: true },
      });
      if (!amont.ok) {
        return new Response(`origine: ${amont.status} ${amont.statusText}`, {
          status: amont.status,
          headers: corsHeaders(request),
        });
      }
      const entetes = new Headers(amont.headers);
      if (estPdf) {
        entetes.set('Content-Type', 'application/pdf');
        entetes.set('Content-Disposition', 'inline');
      }
      reponse = new Response(amont.body, amont);
      reponse = new Response(reponse.body, { status: amont.status, headers: entetes });
      try {
        await cache.put(cle, reponse.clone());
      } catch (_) {
        /* objet trop volumineux pour le cache : on sert quand même */
      }
    }

    const entetes = new Headers(reponse.headers);
    entetes.set('X-Mirror-Cache', depuisCache ? 'HIT' : 'MISS');
    if (estPdf) {
      entetes.set('Content-Type', 'application/pdf');
      entetes.set('Content-Disposition', 'inline');
    }
    for (const [k, v] of Object.entries(corsHeaders(request))) {
      entetes.set(k, v);
    }
    entetes.set('Cache-Control', `public, max-age=${ttl}`);
    return new Response(request.method === 'HEAD' ? null : reponse.body, {
      status: reponse.status,
      headers: entetes,
    });
  },
};
