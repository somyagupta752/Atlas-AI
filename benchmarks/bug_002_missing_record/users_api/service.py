from users_api.repository import User, UserRepository


class UserService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    def get_user(self, user_id: int) -> User | None:
        return self.repository.find_by_id(user_id)
