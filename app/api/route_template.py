"""The route template of a request ("/admin/org/v1/orgs/{org_id}/suspend"), with its path parameters put back.

FastAPI nests included routers lazily, so a route object only knows its own relative path; rebuilding the template from
the request is the same for every nesting. Used to scope idempotency keys and to label request-duration metrics.
"""

from fastapi import Request


def route_template(request: Request) -> str:
    path = request.url.path
    for name, value in request.path_params.items():
        path = path.replace(str(value), "{" + name + "}", 1)
    return path
