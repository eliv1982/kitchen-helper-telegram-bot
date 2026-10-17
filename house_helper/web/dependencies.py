"""FastAPI dependencies shared by the routes."""

from fastapi import Request

from house_helper.web.database import DatabaseClosed, WebDatabase


async def get_database(request: Request) -> WebDatabase:
    """The application's database. `async def` so FastAPI does not spend a worker
    thread on it. Use it as `Depends(get_database)` and call `await db.run(...)`."""
    try:
        return request.app.state.database
    except AttributeError:
        raise DatabaseClosed(
            "the database is not available: the application has not started"
        ) from None
