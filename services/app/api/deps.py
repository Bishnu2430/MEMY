"""Shared API dependencies: authentication and role checks."""
from fastapi import HTTPException, Request

from .. import db
from ..security import decode_token

RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


async def current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
    if not token:
        raise HTTPException(401, "not authenticated")
    try:
        uid = decode_token(token)
    except Exception:
        raise HTTPException(401, "invalid token")
    user = await db.fetchrow("SELECT id, email, name, is_owner, active FROM users WHERE id=%s", uid)
    if not user or not user["active"]:
        raise HTTPException(401, "user inactive")
    rows = await db.fetch("SELECT m.team_id, m.role, t.name, t.slug FROM memberships m JOIN teams t ON t.id=m.team_id"
                          " WHERE m.user_id=%s", uid)
    user = dict(user)
    user["roles"] = {str(r["team_id"]): ("owner" if user["is_owner"] else r["role"]) for r in rows}
    user["teams"] = [{"id": str(r["team_id"]), "name": r["name"], "slug": r["slug"],
                      "role": user["roles"][str(r["team_id"])]} for r in rows]
    if user["is_owner"]:
        for t in await db.fetch("SELECT id, name, slug FROM teams"):
            if str(t["id"]) not in user["roles"]:
                user["roles"][str(t["id"])] = "owner"
                user["teams"].append({"id": str(t["id"]), "name": t["name"], "slug": t["slug"], "role": "owner"})
    return user


def need(user: dict, team_id, min_role: str = "viewer"):
    role = user["roles"].get(str(team_id))
    if role is None or RANK[role] < RANK[min_role]:
        raise HTTPException(403, f"requires {min_role} role in this team")
    return role


def need_admin_any(user: dict):
    if not (user["is_owner"] or any(RANK[r] >= RANK["admin"] for r in user["roles"].values())):
        raise HTTPException(403, "admin role required")


