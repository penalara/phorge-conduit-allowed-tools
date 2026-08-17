from typing import Any, Callable, Dict, List, Optional, Tuple


def page_data(
    response: Any, operation: str
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Validate and unpack one cursor-based Conduit search page."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise ValueError("{} returned an inconsistent response".format(operation))
    cursor = response.get("cursor") or {}
    if not isinstance(cursor, dict):
        raise ValueError("{} returned an inconsistent cursor".format(operation))
    after = cursor.get("after")
    if after is not None and not isinstance(after, str):
        raise ValueError("{} returned an invalid cursor".format(operation))
    return response["data"], after


def read_all_pages(
    search: Callable[..., Dict[str, Any]], operation: str, **kwargs: Any
) -> List[Dict[str, Any]]:
    """Read every cursor page and reject cursors that do not advance."""
    data: List[Dict[str, Any]] = []
    after: Optional[str] = None
    seen = set()
    while True:
        page, next_after = page_data(
            search(after=after, limit=100, **kwargs), operation
        )
        data.extend(page)
        if next_after is None:
            return data
        if next_after in seen:
            raise ValueError("{} returned a non-advancing cursor".format(operation))
        seen.add(next_after)
        after = next_after


def _add_pagination_metadata(result: dict, cursor: dict = None) -> dict:
    """
    Add pagination metadata to search results.

    Args:
        result: Original search result
        cursor: Pagination cursor from API

    Returns:
        Result with enhanced pagination metadata
    """
    if cursor:
        result["pagination"] = {
            "cursor": cursor,
            "has_more": cursor.get("after") is not None,
            "limit": cursor.get("limit", 100),
        }

    return result


def _apply_smart_pagination(data: list[Any], limit: int = None) -> dict:
    """
    Apply smart pagination to data with token optimization.

    Args:
        data: List of data items
        limit: Maximum number of items to return (optional)

    Returns:
        Paginated response with metadata
    """
    if limit is None:
        limit = 100  # Default limit

    # Apply limit if data is larger than limit
    if len(data) > limit:
        paginated_data = data[:limit]
        has_more = True
        total_count = len(data)
        suggestion = f"Use pagination to retrieve remaining {total_count - limit} items"
    else:
        paginated_data = data
        has_more = False
        total_count = len(data)
        suggestion = None

    return {
        "data": paginated_data,
        "pagination": {
            "total": total_count,
            "returned": len(paginated_data),
            "has_more": has_more,
        },
        "suggestion": suggestion,
    }


def _truncate_text_response(text: str, max_length: int = 2000) -> dict:
    """
    Truncate long text responses with helpful guidance.

    Args:
        text: The text to truncate
        max_length: Maximum allowed length

    Returns:
        Truncated response with guidance
    """
    if len(text) <= max_length:
        return {"content": text, "truncated": False}

    truncated_text = text[:max_length]
    remaining_length = len(text) - max_length

    return {
        "content": truncated_text,
        "truncated": True,
        "original_length": len(text),
        "remaining_length": remaining_length,
        "suggestion": f"Content was truncated. {remaining_length} characters remaining. Use specific search parameters to reduce results.",
    }
