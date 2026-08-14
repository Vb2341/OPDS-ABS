"""Personalized feed generator."""
# Standard library imports
import asyncio
import logging
from typing import Any, Dict, List, Optional

# Local application imports
from opds_abs.api.client import fetch_from_api, get_download_urls_from_item
from opds_abs.config import ITEMS_PER_PAGE, PAGINATION_ENABLED
from opds_abs.core.feed_generator import BaseFeedGenerator
from opds_abs.feeds.series_feed import SeriesFeedGenerator
from opds_abs.utils import dict_to_xml
from opds_abs.utils.error_utils import handle_exception, log_error

# Set up logging
logger = logging.getLogger(__name__)


class PersonalizedFeedGenerator(BaseFeedGenerator):
    """Generator for personalized OPDS feeds.

    This generator consumes Audiobookshelf's personalized library view and exposes
    OPDS feeds for sectioned content such as continue-listening and continue-series.
    """

    async def _fetch_personalized_sections(
        self,
        username: str,
        library_id: str,
        token: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch personalized sections for a library from Audiobookshelf."""
        data = await fetch_from_api(
            f"/libraries/{library_id}/personalized",
            username=username,
            token=token,
        )

        if isinstance(data, list):
            return [section for section in data if isinstance(section, dict)]

        if isinstance(data, dict):
            for key in ("results", "sections", "items", "data"):
                sections = data.get(key)
                if isinstance(sections, list):
                    return [section for section in sections if isinstance(section, dict)]

        logger.warning("Unexpected personalized response format for library %s", library_id)
        return []

    @staticmethod
    def _find_section(sections: List[Dict[str, Any]], section_id: str) -> Dict[str, Any]:
        """Find a personalized section by its id."""
        for section in sections:
            if section.get("id") == section_id:
                return section
        return {}

    @staticmethod
    def _extract_entities(section: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get entities list from a section safely."""
        entities = section.get("entities", [])
        if isinstance(entities, list):
            return [entity for entity in entities if isinstance(entity, dict)]
        return []

    @staticmethod
    def _has_ebook(item: Dict[str, Any]) -> bool:
        """Check whether a library item has an ebook file/format."""
        media = item.get("media", {})
        return bool(media.get("ebookFile")) or bool(media.get("ebookFormat"))

    def _extract_library_item(self, entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a library item from personalized entity shapes."""
        if self._has_ebook(entity) or isinstance(entity.get("media"), dict):
            return entity

        for key in ("libraryItem", "item", "book"):
            value = entity.get(key)
            if isinstance(value, dict):
                return value

        return None

    def _filter_continue_listening_items(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter continue-listening entities to ebook-capable library items only."""
        filtered_items: List[Dict[str, Any]] = []
        seen_ids = set()

        for entity in entities:
            # Most personalized continue-listening entities are already library items.
            if self._has_ebook(entity):
                item_id = entity.get("id")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    filtered_items.append(entity)
                continue

            item = self._extract_library_item(entity)
            if item and self._has_ebook(item):
                item_id = item.get("id")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    filtered_items.append(item)
        return filtered_items

    def _filter_continue_series_items(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter continue-series entities to series that include at least one ebook."""
        filtered_series: List[Dict[str, Any]] = []
        grouped_series: Dict[str, Dict[str, Any]] = {}

        for entity in entities:
            # Case 1: The entity is a library item (flat shape) with series metadata.
            # In this case we group items into synthetic series objects.
            library_item = self._extract_library_item(entity)
            if library_item and self._has_ebook(library_item):
                media = library_item.get("media", {})
                metadata = media.get("metadata", {})
                series_meta = metadata.get("series", {})
                if isinstance(series_meta, dict):
                    series_id = series_meta.get("id")
                    series_name = series_meta.get("name")
                    if series_id and series_name:
                        if series_id not in grouped_series:
                            grouped_series[series_id] = {
                                "id": series_id,
                                "name": series_name,
                                "books": [],
                            }
                        grouped_series[series_id]["books"].append(library_item)
                        continue

            # Some responses may wrap the series object in a nested key.
            series_obj = entity.get("series") if isinstance(entity.get("series"), dict) else entity
            if not isinstance(series_obj, dict):
                continue

            books = entity.get("books", series_obj.get("books", []))
            if not isinstance(books, list):
                continue

            ebook_books: List[Dict[str, Any]] = []
            for book in books:
                if not isinstance(book, dict):
                    continue

                # Some section payloads contain raw library items; others wrap them.
                normalized_book = book if self._has_ebook(book) else self._extract_library_item(book)
                if normalized_book and self._has_ebook(normalized_book):
                    ebook_books.append(normalized_book)

            if not ebook_books:
                continue

            normalized_series = dict(series_obj)
            normalized_series["books"] = ebook_books
            filtered_series.append(normalized_series)

        # Merge grouped synthetic series extracted from flat library-item entities.
        for series in grouped_series.values():
            filtered_series.append(series)

        return filtered_series

    def _paginate(self, items: List[Dict[str, Any]], page: int, per_page: Optional[int]):
        """Paginate a list with shared app pagination settings."""
        if not PAGINATION_ENABLED:
            return items, 1, 1, True

        effective_per_page = ITEMS_PER_PAGE if per_page is None else per_page
        if effective_per_page <= 0:
            return items, 1, 1, True

        total_items = len(items)
        total_pages = (total_items + effective_per_page - 1) // effective_per_page

        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages

        start_idx = (page - 1) * effective_per_page
        end_idx = min(start_idx + effective_per_page, total_items)
        return items[start_idx:end_idx], page, total_pages, False

    def _add_pagination_links(
        self,
        feed,
        base_url: str,
        current_page: int,
        total_pages: int,
        token: Optional[str] = None,
    ):
        """Add first/previous/next/last pagination links."""
        token_param = f"&token={token}" if token else ""
        links = []

        if current_page > 1:
            links.append({
                "_attrs": {
                    "rel": "first",
                    "href": f"{base_url}?page=1{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog",
                }
            })
            links.append({
                "_attrs": {
                    "rel": "previous",
                    "href": f"{base_url}?page={current_page-1}{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog",
                }
            })

        if current_page < total_pages:
            links.append({
                "_attrs": {
                    "rel": "next",
                    "href": f"{base_url}?page={current_page+1}{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog",
                }
            })
            links.append({
                "_attrs": {
                    "rel": "last",
                    "href": f"{base_url}?page={total_pages}{token_param}",
                    "type": "application/atom+xml;profile=opds-catalog",
                }
            })

        for link in links:
            dict_to_xml(feed, {"link": link})

    async def generate_personalized_feed(
        self,
        username: str,
        library_id: str,
        token: Optional[str] = None,
    ):
        """Generate a personalized navigation feed for a library."""
        try:
            sections = await self._fetch_personalized_sections(username, library_id, token=token)
            listening_section = self._find_section(sections, "continue-listening")
            series_section = self._find_section(sections, "continue-series")

            listening_total = listening_section.get("total", len(self._extract_entities(listening_section)))
            series_total = series_section.get("total", len(self._extract_entities(series_section)))

            feed = self.create_base_feed(username, library_id, token=token)
            feed_data = {
                "id": {"_text": f"{library_id}/personalized"},
                "title": {"_text": f"{username}'s personalized feed"},
            }
            dict_to_xml(feed, feed_data)

            base_url = f"/opds/{username}/libraries/{library_id}/personalized"

            entries = [
                {
                    "title": "Continue Listening",
                    "description": f"{listening_total} item(s) available",
                    "path": "continue-listening",
                },
                {
                    "title": "Continue Series",
                    "description": f"{series_total} item(s) available",
                    "path": "continue-series",
                },
            ]

            for entry in entries:
                href = f"{base_url}/{entry['path']}"
                if token:
                    href = f"{href}?token={token}"

                entry_data = {
                    "entry": {
                        "title": {"_text": entry["title"]},
                        "updated": {"_text": self.get_current_timestamp()},
                        "content": {"_text": entry["description"]},
                        "link": {
                            "_attrs": {
                                "href": href,
                                "rel": "subsection",
                                "type": "application/atom+xml;profile=opds-catalog",
                            }
                        },
                    }
                }
                dict_to_xml(feed, entry_data)

            return self.create_response(feed)

        except Exception as e:
            context = f"Generating personalized feed for user {username}, library {library_id}"
            log_error(e, context=context)
            return handle_exception(e, context=context)

    async def generate_continue_listening_feed(
        self,
        username: str,
        library_id: str,
        token: Optional[str] = None,
        page: int = 1,
        per_page: Optional[int] = None,
    ):
        """Generate an OPDS feed for the continue-listening personalized section."""
        try:
            sections = await self._fetch_personalized_sections(username, library_id, token=token)
            listening_section = self._find_section(sections, "continue-listening")
            entities = self._extract_entities(listening_section)
            items = self._filter_continue_listening_items(entities)

            feed = self.create_base_feed(username, library_id, token=token)
            feed_data = {
                "id": {"_text": f"{library_id}/personalized/continue-listening"},
                "title": {"_text": "Continue Listening"},
            }
            dict_to_xml(feed, feed_data)

            if not items:
                dict_to_xml(feed, {
                    "entry": {
                        "title": {"_text": "No items found"},
                        "content": {"_text": "No continue-listening ebook items were found."},
                    }
                })
                return self.create_response(feed)

            paged_items, current_page, total_pages, no_pagination = self._paginate(items, page, per_page)

            if not no_pagination:
                base_url = f"/opds/{username}/libraries/{library_id}/personalized/continue-listening"
                self._add_pagination_links(feed, base_url, current_page, total_pages, token)

            tasks = []
            for book in paged_items:
                book_id = book.get("id", "")
                if book_id:
                    tasks.append(get_download_urls_from_item(book_id, username=username, token=token))
                else:
                    tasks.append(asyncio.sleep(0, result=[]))

            ebook_inos_list = await asyncio.gather(*tasks)
            for book, ebook_inos in zip(paged_items, ebook_inos_list):
                if ebook_inos:
                    self.add_book_to_feed(feed, book, ebook_inos, "", token)

            return self.create_response(feed)

        except Exception as e:
            context = f"Generating continue-listening feed for user {username}, library {library_id}"
            log_error(e, context=context)
            return handle_exception(e, context=context)

    async def generate_continue_series_feed(
        self,
        username: str,
        library_id: str,
        token: Optional[str] = None,
        page: int = 1,
        per_page: Optional[int] = None,
    ):
        """Generate an OPDS feed for the continue-series personalized section."""
        try:
            sections = await self._fetch_personalized_sections(username, library_id, token=token)
            series_section = self._find_section(sections, "continue-series")
            entities = self._extract_entities(series_section)
            series_items = self._filter_continue_series_items(entities)

            feed = self.create_base_feed(username, library_id, token=token)
            feed_data = {
                "id": {"_text": f"{library_id}/personalized/continue-series"},
                "title": {"_text": "Continue Series"},
            }
            dict_to_xml(feed, feed_data)

            if not series_items:
                dict_to_xml(feed, {
                    "entry": {
                        "title": {"_text": "No series found"},
                        "content": {"_text": "No continue-series items with ebooks were found."},
                    }
                })
                return self.create_response(feed)

            paged_series, current_page, total_pages, no_pagination = self._paginate(series_items, page, per_page)

            if not no_pagination:
                base_url = f"/opds/{username}/libraries/{library_id}/personalized/continue-series"
                self._add_pagination_links(feed, base_url, current_page, total_pages, token)

            series_generator = SeriesFeedGenerator()
            for series in paged_series:
                await series_generator.add_series_to_feed(username, library_id, feed, series, token)

            return self.create_response(feed)

        except Exception as e:
            context = f"Generating continue-series feed for user {username}, library {library_id}"
            log_error(e, context=context)
            return handle_exception(e, context=context)
