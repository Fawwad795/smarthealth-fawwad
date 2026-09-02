"""Shared pagination: every list endpoint in this project accepts the same
two query parameters and returns the same envelope shape, so this is the
one place that decides the default page size and the cap.
"""

from dataclasses import dataclass

from fastapi import Query


@dataclass
class PaginationParams:
    """Plain value object -- no FastAPI machinery on it, so a service
    function can accept one without importing anything from fastapi, and a
    test can build one directly: PaginationParams(limit=5, offset=0).
    """

    limit: int = 20
    offset: int = 0


def pagination_params(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginationParams:
    """The actual FastAPI dependency -- routers use Depends(pagination_params).

    Split from PaginationParams itself because Query(...) objects only
    resolve to real integers inside a request FastAPI is handling. Made a
    default argument of PaginationParams directly, PaginationParams() built
    by hand (as tests do) would get the Query object itself as `limit`,
    not the number 20.
    """
    return PaginationParams(limit=limit, offset=offset)
