import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import requests


FRIBB_FILE = (
    Path(__file__).resolve().parents[2]
    / "anime-list-full.json"
)

CINEMETA_URL = "https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"

SHIKIMORI_SEARCH_URL = "https://shikimori.one/api/animes"

JIKAN_ANIME_URL = "https://api.jikan.moe/v4/anime/{mal_id}"
IMDB_SUGGESTION_URL = (
    "https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
)

SHIKIMORI_MAX_CANDIDATES = 5
SHIKIMORI_DELAY = 0.5

JIKAN_DELAY = 0.35

# Process-local caches.
ANIDB_MAL_CACHE = {}
MAL_IMDB_CACHE = {}
JIKAN_CACHE = {}
IMDB_SUGGESTION_CACHE = {}


SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": "MAL-Stremio-Cinemeta-Resolver/1.0"
    }
)


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
                    f"Rate limited by {url}. "
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

    title = re.sub(
        r"[^\w\s]",
        " ",
        title,
        flags=re.UNICODE,
    )

    title = re.sub(r"\s+", " ", title)

    return title.strip()


def _similarity(a, b):
    return SequenceMatcher(
        None,
        _normalize_title(a),
        _normalize_title(b),
    ).ratio()


def _get_record_title(record):
    for key in (
        "title",
        "name",
        "anime_title",
    ):
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
    print(
        f"Loading Fribb database from {FRIBB_FILE}..."
    )

    if not FRIBB_FILE.exists():
        raise FileNotFoundError(
            f"Fribb database not found: {FRIBB_FILE}"
        )

    with FRIBB_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(
            "anime-list-full.json must contain a JSON list."
        )

    imdb_index = {}

    for record in data:
        if not isinstance(record, dict):
            continue

        imdb_ids = record.get(
            "imdb_id",
            [],
        )

        if isinstance(imdb_ids, str):
            imdb_ids = [imdb_ids]

        if not isinstance(imdb_ids, list):
            continue

        for imdb_id in imdb_ids:
            if imdb_id:
                imdb_index.setdefault(
                    imdb_id,
                    [],
                ).append(record)

    mal_index = {}

    for imdb_id, records in imdb_index.items():
        for record in records:
            mal_id = record.get("mal_id")

            if mal_id is None:
                continue

            try:
                mal_id = int(mal_id)
            except (TypeError, ValueError):
                continue

            # Keep the first valid IMDb mapping.
            mal_index.setdefault(
                mal_id,
                imdb_id,
            )

    print(
        f"Fribb loaded: {len(data):,} records, "
        f"{len(imdb_index):,} IMDb IDs, "
        f"{len(mal_index):,} MAL IDs."
    )

    return imdb_index, mal_index


IMDB_INDEX, MAL_INDEX = _load_fribb()


def resolve_mal_to_imdb(
    mal_id,
    title=None,
    year=None,
    media_type=None,
):
    """
    Resolve a MAL ID to an IMDb/Cinemeta ID.

    Resolution order:

    1. Fribb MAL -> IMDb
    2. Jikan MAL -> title/year/type
    3. IMDb suggestion search
    4. Strong title/year/type verification

    Returns:
        IMDb ID such as "tt35346717"
        or None.
    """

    try:
        mal_id = int(mal_id)
    except (TypeError, ValueError):
        return None

    cached = MAL_IMDB_CACHE.get(mal_id)

    if cached is not None:
        print(
            f"MAL {mal_id} -> IMDb {cached} "
            f"(cache)"
        )
        return cached

    # ========================================================
    # 1. Fribb
    # ========================================================

    imdb_id = MAL_INDEX.get(mal_id)

    if imdb_id:
        MAL_IMDB_CACHE[mal_id] = imdb_id

        print(
            f"MAL {mal_id} -> IMDb {imdb_id} "
            f"(Fribb)"
        )

        return imdb_id

    print(
        f"MAL {mal_id}: no Fribb IMDb mapping. "
        f"Trying Jikan fallback..."
    )

    # ========================================================
    # 2. Use MAL data already supplied by the catalog
    # ========================================================

    titles = []

    if title:
        titles.append(str(title))

    if not titles:
        print(
            f"MAL {mal_id}: no title available. "
            f"Trying Jikan fallback..."
        )

        jikan_data = _get_jikan_anime(mal_id)

        if jikan_data:
            data = jikan_data.get("data")

            if isinstance(data, dict):
                for key in (
                    "title_english",
                    "title",
                    "title_japanese",
                ):
                    value = data.get(key)

                    if value and value not in titles:
                        titles.append(value)

                synonym_list = data.get(
                    "titles",
                    [],
                )

                if isinstance(
                    synonym_list,
                    list,
                ):
                    for item in synonym_list:
                        if not isinstance(
                            item,
                            dict,
                        ):
                            continue

                        synonym = item.get("title")

                        if (
                            synonym
                            and synonym not in titles
                        ):
                            titles.append(synonym)

                if year is None:
                    aired = data.get("aired")

                    if isinstance(
                        aired,
                        dict,
                    ):
                        from_date = aired.get("from")

                        if (
                            from_date
                            and len(from_date) >= 4
                        ):
                            try:
                                year = int(
                                    from_date[:4]
                                )
                            except ValueError:
                                pass

                if not media_type:
                    media_type = (
                        str(
                            data.get("type")
                            or ""
                        )
                        .strip()
                        .lower()
                    )

        if not titles:
            print(
                f"MAL {mal_id}: Jikan lookup failed."
            )

            return None

    print(
        f"MAL {mal_id}: title(s): "
        f"{titles[:4]}"
    )

    print(
        f"MAL {mal_id}: "
        f"year={year}, "
        f"type={media_type}"
    )

    # ========================================================
    # 3. IMDb suggestion lookup
    # ========================================================

    candidates = []

    for title in titles:
        suggestions = _get_imdb_suggestions(title)

        if not suggestions:
            continue

        candidates.extend(suggestions)

        # English/canonical title should normally be enough.
        # Don't need to spam IMDb with every synonym.
        if len(candidates) >= 20:
            break

    if not candidates:
        print(
            f"MAL {mal_id}: IMDb suggestion search "
            f"returned no candidates."
        )

        return None

    # ========================================================
    # 4. Verify candidates
    # ========================================================

    best_id = None
    best_score = 0.0
    best_title = None

    seen_ids = set()

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        candidate_id = candidate.get("id")

        if not candidate_id:
            continue

        if candidate_id in seen_ids:
            continue

        seen_ids.add(candidate_id)

        candidate_title = (
            candidate.get("l")
            or candidate.get("s")
            or ""
        )

        candidate_year = candidate.get("y")
        candidate_kind = (
            str(candidate.get("q") or "")
            .lower()
        )

        # ----------------------------------------------------
        # Title score
        # ----------------------------------------------------

        title_score = 0.0

        for source_title in titles:
            title_score = max(
                title_score,
                _similarity(
                    source_title,
                    candidate_title,
                ),
            )

        # ----------------------------------------------------
        # Year score
        # ----------------------------------------------------

        year_score = 0.0

        if year and candidate_year:
            try:
                candidate_year_int = int(
                    candidate_year
                )

                year_difference = abs(
                    year - candidate_year_int
                )

                if year_difference == 0:
                    year_score = 1.0
                elif year_difference == 1:
                    year_score = 0.5

            except (TypeError, ValueError):
                pass

        elif not year:
            year_score = 0.5

        # ----------------------------------------------------
        # Type score
        # ----------------------------------------------------

        type_score = 0.0

        if media_type in {
            "tv",
            "ona",
            "ova",
            "special",
        }:
            if any(
                kind in candidate_kind
                for kind in (
                    "tv series",
                    "tv mini series",
                    "tv movie",
                    "tv special",
                    "series",
                )
            ):
                type_score = 1.0
            elif candidate_kind in {
                "movie",
                "video game",
            }:
                type_score = -1.0

        elif media_type == "movie":
            if candidate_kind == "movie":
                type_score = 1.0

        else:
            type_score = 0.5

        # ----------------------------------------------------
        # Combined score
        # ----------------------------------------------------

        score = (
            title_score * 0.70
            + year_score * 0.20
            + type_score * 0.10
        )

        if score > best_score:
            best_score = score
            best_id = candidate_id
            best_title = candidate_title

    print(
        f"MAL {mal_id}: best IMDb candidate "
        f"{best_id} | {best_title} | "
        f"score={best_score:.3f}"
    )

    # Require a reasonably strong title match.
    if not best_id or best_score < 0.72:
        print(
            f"MAL {mal_id}: IMDb fallback rejected "
            f"(score too low)."
        )

        return None

    MAL_IMDB_CACHE[mal_id] = best_id

    print(
        f"MAL {mal_id} -> IMDb {best_id} "
        f"(Jikan -> IMDb fallback)"
    )

    return best_id


def _get_jikan_anime(mal_id):
    cached = JIKAN_CACHE.get(mal_id)

    if cached is not None:
        return cached

    time.sleep(JIKAN_DELAY)

    try:
        result = _http_get_json(
            JIKAN_ANIME_URL.format(
                mal_id=mal_id,
            )
        )
    except requests.RequestException as error:
        print(
            f"Jikan request failed for MAL "
            f"{mal_id}: {error}"
        )
        return None

    if result is not None:
        JIKAN_CACHE[mal_id] = result

    return result


def _get_imdb_suggestions(title):
    normalized = _normalize_title(title)

    if not normalized:
        return []

    cached = IMDB_SUGGESTION_CACHE.get(
        normalized
    )

    if cached is not None:
        return cached

    query = quote(
        normalized,
        safe="",
    )

    url = IMDB_SUGGESTION_URL.format(
        query=query,
    )

    try:
        result = _http_get_json(url)
    except requests.RequestException as error:
        print(
            f"IMDb suggestion request failed for "
            f"'{title}': {error}"
        )
        return []

    if not isinstance(result, dict):
        return []

    suggestions = result.get("d", [])

    if not isinstance(suggestions, list):
        suggestions = []

    IMDB_SUGGESTION_CACHE[
        normalized
    ] = suggestions

    return suggestions


def _get_cinemeta_title(imdb_id):
    try:
        data = _http_get_json(
            CINEMETA_URL.format(
                imdb_id=imdb_id
            )
        )
    except requests.RequestException as error:
        print(
            f"Cinemeta request failed for "
            f"{imdb_id}: {error}"
        )
        return None

    meta = data.get("meta")

    if not isinstance(meta, dict):
        return None

    return (
        meta.get("name")
        or meta.get("originalName")
    )


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
        print(
            f"Shikimori search failed: {error}"
        )
        return []

    if not isinstance(results, list):
        return []

    scored = []

    for candidate in results:
        name = candidate.get(
            "name",
            "",
        )

        scored.append(
            (
                _similarity(
                    title,
                    name,
                ),
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
        in scored[
            :SHIKIMORI_MAX_CANDIDATES
        ]
    ]


def _get_shikimori_links(shikimori_id):
    url = (
        "https://shikimori.one/api/"
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
    cached = ANIDB_MAL_CACHE.get(
        anidb_id
    )

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

        links = _get_shikimori_links(
            shikimori_id
        )

        linked_anidb = _extract_anidb_id(
            links
        )

        if linked_anidb != anidb_id:
            time.sleep(
                SHIKIMORI_DELAY
            )
            continue

        mal_id = _extract_mal_id(
            links
        )

        if mal_id is None:
            time.sleep(
                SHIKIMORI_DELAY
            )
            continue

        ANIDB_MAL_CACHE[
            anidb_id
        ] = mal_id

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

    if not isinstance(
        content_id,
        str,
    ):
        return None

    parts = content_id.strip().split(":")

    if len(parts) != 3:
        return None

    imdb_id = parts[0]

    if not re.fullmatch(
        r"tt\d+",
        imdb_id,
    ):
        return None

    try:
        season = int(parts[1])
        episode = int(parts[2])

    except ValueError:
        return None

    if season < 0 or episode < 1:
        return None

    matches = IMDB_INDEX.get(
        imdb_id,
        [],
    )

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
        offset = _get_episode_offset(
            record
        )

        mal_episode = (
            episode - offset
        )

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
            "mal_id": int(
                record["mal_id"]
            ),
            "episode": int(
                mal_episode
            ),
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
            if record.get("anidb_id")
            is not None
        ),
        None,
    )

    if anidb_record is None:
        return None

    anidb_id = int(
        anidb_record["anidb_id"]
    )

    title = _get_cinemeta_title(
        imdb_id
    )

    if not title:
        return None

    mal_id = _resolve_anidb_to_mal(
        anidb_id,
        title,
    )

    if mal_id is None:
        return None

    return {
        "mal_id": mal_id,
        "episode": episode,
        "confidence": 1.0,
        "method": "shikimori_anidb",
    }