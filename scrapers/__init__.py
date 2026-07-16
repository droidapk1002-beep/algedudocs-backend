# =============================================================================
#  scrapers/__init__.py — Registre central des adaptateurs de sites
# =============================================================================

from . import dzexams, eddirasa, ency

SITES = {
    "dzexams": {
        "label": "DzExams",
        "url": "https://www.dzexams.com",
        "module": dzexams,
    },
    "eddirasa": {
        "label": "Eddirasa",
        "url": "https://eddirasa.com",
        "module": eddirasa,
    },
    "ency": {
        "label": "Ency-Education",
        "url": "https://www.ency-education.net",
        "module": ency,
    },
}


def get_site(site_id):
    info = SITES.get(site_id)
    if not info:
        raise ValueError(f"Site inconnu: {site_id}")
    return info["module"]
