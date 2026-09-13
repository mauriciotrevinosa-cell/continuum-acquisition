"""Bibliographic providers. Each returns provider-neutral node dicts:

    {"provider", "id", "title", "aliases", "type", "medium", "completed",
     "latest_chapter", "volumes", "status_text", "year", "publishers",
     "authors", "url", "official_urls", "related": [(relation, id, name)]}

Providers only READ public bibliographic data. None of them downloads media.
"""
