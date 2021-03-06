"""urllib utilities."""
import functools
import urllib.parse

from ..config import CACHE_MAXSIZE__URL_NETLOC


@functools.lru_cache(CACHE_MAXSIZE__URL_NETLOC)
def url_to_netloc(url: str) -> str:
    """Return the netloc for the given URL."""
    parse_result = urllib.parse.urlparse(url)
    if parse_result.scheme == "":
        url = f"https://{url}"  # Without this, the returned netloc is erroneous.
        parse_result = urllib.parse.urlparse(url)
    netloc = parse_result.netloc.casefold()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc
