from users_api.service import UserService


def get_user_profile(service: UserService, user_id: int) -> tuple[int, dict]:
    """GET /users/{user_id}. Returns (status_code, body)."""
    try:
        user = service.get_user(user_id)
        return 200, {"id": user.id, "email": user.email}
    except Exception:
        return 500, {"error": "internal server error"}
