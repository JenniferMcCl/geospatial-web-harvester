"""Parse the synthetic HTML contract into field-preserving observations.

Selectors describe fixtures only; the example portal's DOM and publication-date
semantics are unverified. This module performs no HTTP or database operations.
Domain interpretation, date validation and geometry checks belong to transform.
"""

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import Observation


class ParseError(ValueError):
    """The response is not a recognized fixture listing."""


@dataclass(frozen=True)
class Selectors:
    """Immutable fixture CSS selectors injectable into parse_page for testing."""

    cards: str = ".planning-notice, .notice-card, .notice-item"
    listing: str = ".notice-list"
    title: str = ".notice-title"
    link: str = ".notice-link"
    summary: str = ".summary"
    coordinates: str = ".coordinates"


DEFAULT_SELECTORS = Selectors()


@dataclass
class ParsedPage:
    """Collect identifiable observations, pagination and record-level diagnostics.

    ``fetched_cards`` includes quarantined cards. ``errors`` describes cards with
    no stable identity; those cards are omitted from ``observations``. A recognized
    empty listing is valid and contains no observations. ``next_url`` is an
    absolute canonical URL; acquisition enforces pagination limits separately.
    """

    observations: list[Observation]
    fetched_cards: int
    next_url: str | None
    errors: list[dict] = field(default_factory=list)


def canonical_url(value: str | None, base_url: str) -> str | None:
    """Resolve an HTTP(S) URL and normalize its scheme/host while removing fragments.

    Preserve path case and all query parameters, which may carry identity.
    Return None for blank, fragment-only, credential-bearing or malformed URLs.
    This normalization alone does not prove that a URL identifies an item.
    """
    if not value or not value.strip() or value.strip().startswith("#"):
        return None
    try:
        parts = urlsplit(urljoin(base_url, value.strip()))
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
        # Accessing port also validates malformed port syntax.
        port = f":{parts.port}" if parts.port else ""
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return urlunsplit(
            (parts.scheme.lower(), host + port, parts.path, parts.query, "")
        )
    except ValueError:
        return None


def parse_page(
    html: str, page_url: str, selectors: Selectors = DEFAULT_SELECTORS
) -> ParsedPage:
    """Extract fixture cards while preserving which fields were actually supplied.

    Missing elements produce absent keys; explicit empty text produces None.
    Coordinates and dates remain source strings. Relative item and next-page
    links resolve against page_url, and links back to the listing cannot serve
    as item identities. Cards lacking both source ID and item URL are quarantined.

    Raise ParseError when the listing container is absent, so an unrelated page
    cannot be treated as a successful empty scan. No approval or location claim
    is verified here. Returned observations default to the fixture namespace;
    live orchestration replaces that namespace explicitly.
    """
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one(selectors.listing) is None:
        raise ParseError("Unrecognized HTML: missing fixture listing container")
    cards = soup.select(selectors.cards)
    observations = []
    errors = []
    fields = {
        "published": "publication_date_raw",
        "procedure-status": "procedure_status",
        "land-use": "land_use",
        "municipality": "municipality",
        "location-text": "location_text",
        "planning-type": "planning_type",
    }
    for position, card in enumerate(cards, 1):
        values = {}
        if card.has_attr("data-record-id"):
            values["source_id"] = str(card["data-record-id"]) or None
        for name, selector in {
            "title": selectors.title,
            "description": selectors.summary,
        }.items():
            element = card.select_one(selector)
            if element is not None:
                values[name] = element.get_text(" ", strip=True) or None
        link = card.select_one(selectors.link)
        if link is not None:
            url = canonical_url(link.get("href"), page_url)
            base = urlsplit(page_url)
            target = urlsplit(url) if url else None
            # A link back to the listing is never an individual-record identity.
            if target and (target.netloc, target.path.rstrip("/")) == (
                base.netloc,
                base.path.rstrip("/"),
            ):
                url = None
            values["source_url"] = url
        for html_field, name in fields.items():
            element = card.select_one(f'[data-field="{html_field}"]')
            if element is not None:
                values[name] = element.get_text(" ", strip=True) or None
                if html_field == "published":
                    time = element.select_one("time[datetime]")
                    values["publication_date_machine"] = (
                        time.get("datetime") if time else None
                    )
        coord = card.select_one(selectors.coordinates)
        if coord is not None:
            values["coordinates"] = {
                key: coord.get("data-" + key)
                for key in ("crs", "lat", "lon", "easting", "northing")
                if coord.has_attr("data-" + key)
            }
        observation = Observation(values)
        if observation.source_key is None:
            errors.append(
                {
                    "card": position,
                    "page_url": page_url,
                    "fields": ["source_id", "source_url"],
                    "reason": "missing_stable_identity",
                }
            )
        else:
            observations.append(observation)
    next_link = soup.select_one('a[rel~="next"]')
    return ParsedPage(
        observations,
        len(cards),
        canonical_url(next_link.get("href"), page_url) if next_link else None,
        errors,
    )
