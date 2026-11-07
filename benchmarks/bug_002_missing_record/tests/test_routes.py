from users_api.repository import User, UserRepository
from users_api.routes import get_user_profile
from users_api.service import UserService


def make_service() -> UserService:
    return UserService(UserRepository({123: User(id=123, email="ada@example.com")}))


def test_existing_user_returns_profile():
    assert get_user_profile(make_service(), 123) == (200, {"id": 123, "email": "ada@example.com"})


def test_missing_user_returns_404():
    status, _ = get_user_profile(make_service(), 999)
    assert status == 404
