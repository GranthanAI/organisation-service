"""Print a JWT for the dev stack, signed with JWT_SECRET_KEY and JWT_ISSUER like auth-service does (auth-service is
never edited or called). Default: a GranthanAI staff admin. For development and live checks only.

    uv run python -m scripts.make_staff_token                       # staff admin (roles admin, billing_admin, support)
    uv run python -m scripts.make_staff_token --roles support       # read-only staff
    uv run python -m scripts.make_staff_token --roles "" --sub <uuid> --email someone@amity.edu     # an ordinary user
"""

import argparse
import time
import uuid

import jwt

from app.core.config import settings

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--roles", default="admin,billing_admin,support")
parser.add_argument("--sub", default=None, help="user id (a new uuid by default)")
parser.add_argument("--email", default="staff@granthan.com")
parser.add_argument("--minutes", type=int, default=60)
args = parser.parse_args()

if settings.is_production:
    raise SystemExit("make_staff_token.py is for development only")
now = int(time.time())
claims = {
    "sub": args.sub or str(uuid.uuid4()), "email": args.email,
    "roles": [r for r in args.roles.split(",") if r], "jti": uuid.uuid4().hex,
    "iat": now, "exp": now + args.minutes * 60, "iss": settings.JWT_ISSUER,
}
print(jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM))
