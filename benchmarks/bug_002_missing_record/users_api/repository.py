from dataclasses import dataclass


@dataclass
class User:
    id: int
    email: str


class UserRepository:
    def __init__(self, users: dict[int, User] | None = None) -> None:
        self._users = users or {}

    def find_by_id(self, user_id: int) -> User | None:
        """Return the user, or None when no record exists."""
        return self._users.get(user_id)
