import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests


FRIBB_FILE = Path("/app/anime-list-full.json")

CINEMETA_URL = "https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
SHIKIMORI_SEARCH_URL = "https://shikimori.one/api/animes"

SHIKIMORI_MAX_CANDIDATES = 5
SHIKIMORI_DELAY = 0.5

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "MAL-Stremio-Cinemeta-Resolver/1.0"
})

# Process-local cache:
# AniDB ID -> MAL ID
ANIDB_MAL_CACHE = {}


def _http_get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            response = SESSION.get(
                url,
                params=params,
                timeout=20,
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")

                try:
                    wait = float(retry_after)
                except (TypeError, ValueError):
                    wait = 2 ** attempt

                wait = min(max(wait, 1.0), 10.0)

                print(
                    f"Shikimori rate limited us. "
                    f"Waiting {wait:.1f}s..."
                )

                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException:
            if attempt == retries - 1:
                raise

            time.sleep(2 ** attempt)

    return None


def _normalize_title(title):
    if not title:
        return ""

    title = str(title).lower()
    title = re.sub(r"[^\w\s]", " ", title, flags=re.UNICODE)
    title = re.sub(r"\s+", " ", title)

    return title.strip()


def _similarity(a, b):
    return SequenceMatcher(
        None,
        _normalize_title(a),
        _normalize_title(b),
    ).ratio()


def _get_record_title(record):
    for key in ("title", "name", "anime_title"):
        value = record.get(key)
        if value:
            return str(value)

    slug = record.get("anime-planet_id")
    if slug:
        return slug.replace("-", " ")

    return None


def _get_fribb_season(record):
    season = record.get("season")

    if not isinstance(season, dict):
        return None

    value = season.get("tvdb")

    if value is None:
        value = season.get("tmdb")

    return value


def _get_episode_offset(record):
    offset = record.get("episode_offset")

    if not isinstance(offset, dict):
        return 0

    if offset.get("tvdb") is not None:
        return offset["tvdb"]

    if offset.get("tmdb") is not None:
        return offset["tmdb"]

    return 0


def _load_fribb():
    print(f"Loading Fribb database from {FRIBB_FILE}...")

    if not FRIBB_FILE.exists():
        raise FileNotFoundError(
            f"Fribb database not found: {FRIBB_FILE}"
        )

    with FRIBB_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(
            "anime-list-full.json must contain a JSON list."
        )

    imdb_index = {}

    for record in data:
        if not isinstance(record, dict):
            continue

        imdb_ids = record.get("imdb_id", [])

        if isinstance(imdb_ids, str):
            imdb_ids = [imdb_ids]

        if not isinstance(imdb_ids, list):
            continue

        for imdb_id in imdb_ids:
            if imdb_id:
                imdb_index.setdefault(imdb_id, []).append(record)

    print(
        f"Fribb loaded: {len(data):,} records, "
        f"{len(imdb_index):,} IMDb IDs."
    )

    return imdb_index


IMDB_INDEX = _load_fribb()


def _get_cinemeta_title(imdb_id):
    try:
        data = _http_get_json(
            CINEMETA_URL.format(imdb_id=imdb_id)
        )
    except requests.RequestException as error:
        print(
            f"Cinemeta request failed for {imdb_id}: {error}"
        )
        return None

    meta = data.get("meta")

    if not isinstance(meta, dict):
        return None

    return meta.get("name") or meta.get("originalName")


def _extract_mal_id(links):
    for link in links:
        if not isinstance(link, dict):
            continue

        if link.get("kind") != "myanimelist":
            continue

        match = re.search(
            r"/anime/(\d+)",
            link.get("url", ""),
        )

        if match:
            return int(match.group(1))

    return None


def _extract_anidb_id(links):
    for link in links:
        if not isinstance(link, dict):
            continue

        if link.get("kind") != "anime_db":
            continue

        match = re.search(
            r"(?:aid=|anime/)(\d+)",
            link.get("url", ""),
        )

        if match:
            return int(match.group(1))

    return None


def _search_shikimori(title):
    try:
        results = _http_get_json(
            SHIKIMORI_SEARCH_URL,
            params={
                "search": title,
                "limit": 20,
                "order": "ranked",
            },
        )
    except requests.RequestException as error:
        print(f"Shikimori search failed: {error}")
        return []

    if not isinstance(results, list):
        return []

    scored = []

    for candidate in results:
        name = candidate.get("name", "")

        scored.append(
            (
                _similarity(title, name),
                candidate,
            )
        )

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        candidate
        for _, candidate
        in scored[:SHIKIMORI_MAX_CANDIDATES]
    ]


def _get_shikimori_links(shikimori_id):
    url = (
        f"https://shikimori.one/api/"
        f"animes/{shikimori_id}/external_links"
    )

    try:
        return _http_get_json(url)
    except requests.RequestException as error:
        print(
            f"Shikimori links failed for "
            f"{shikimori_id}: {error}"
        )
        return []


def _resolve_anidb_to_mal(anidb_id, title):
    # Fast path: already resolved during this process.
    cached = ANIDB_MAL_CACHE.get(anidb_id)

    if cached is not None:
        print(
            f"AniDB {anidb_id} -> MAL {cached} "
            f"(cache)"
        )
        return cached

    candidates = _search_shikimori(title)

    for candidate in candidates:
        shikimori_id = candidate.get("id")

        if not shikimori_id:
            continue

        links = _get_shikimori_links(shikimori_id)

        linked_anidb = _extract_anidb_id(links)

        if linked_anidb != anidb_id:
            time.sleep(SHIKIMORI_DELAY)
            continue

        mal_id = _extract_mal_id(links)

        if mal_id is None:
            time.sleep(SHIKIMORI_DELAY)
            continue

        ANIDB_MAL_CACHE[anidb_id] = mal_id

        print(
            f"AniDB {anidb_id} -> MAL {mal_id} "
            f"(Shikimori exact match)"
        )

        return mal_id

    return None


def resolve_cinemeta_episode(content_id):
    """
    Resolve:
        tt1234567:season:episode

    Returns:
        {
            "mal_id": int,
            "episode": int,
            "confidence": float,
            "method": str,
        }

    or None.
    """

    if not isinstance(content_id, str):
        return None

    parts = content_id.strip().split(":")

    if len(parts) != 3:
        return None

    imdb_id = parts[0]

    if not re.fullmatch(r"tt\d+", imdb_id):
        return None

    try:
        season = int(parts[1])
        episode = int(parts[2])
    except ValueError:
        return None

    if season < 0 or episode < 1:
        return None

    matches = IMDB_INDEX.get(imdb_id, [])

    if not matches:
        return None

    exact_season = [
        record
        for record in matches
        if _get_fribb_season(record) == season
    ]

    if not exact_season:
        return None

    # ========================================================
    # 1. Fribb -> MAL directly
    # ========================================================

    mal_candidates = [
        record
        for record in exact_season
        if record.get("mal_id") is not None
    ]

    valid = []

    for record in mal_candidates:
        offset = _get_episode_offset(record)
        mal_episode = episode - offset

        if mal_episode >= 1:
            valid.append(
                (
                    record,
                    mal_episode,
                    offset,
                )
            )

    if valid:
        valid.sort(
            key=lambda item: item[2],
            reverse=True,
        )

        record, mal_episode, offset = valid[0]

        return {
            "mal_id": int(record["mal_id"]),
            "episode": int(mal_episode),
            "confidence": 1.0,
            "method": "fribb",
        }

    # ========================================================
    # 2. Fribb -> AniDB -> Shikimori -> MAL
    # ========================================================

    anidb_record = next(
        (
            record
            for record in exact_season
            if record.get("anidb_id") is not None
        ),
        None,
    )

    if anidb_record is None:
        return None

    anidb_id = int(anidb_record["anidb_id"])

    title = _get_cinemeta_title(imdb_id)

    if not title:
        return None

    mal_id = _resolve_anidb_to_mal(
        anidb_id,
        title,
    )

    if mal_id is None:
        return None

    # This is already the season-specific MAL entry.
    return {
        "mal_id": mal_id,
        "episode": episode,
        "confidence": 1.0,
        "method": "shikimori_anidb",
    }