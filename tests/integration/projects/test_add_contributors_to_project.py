import random
import string
from unittest.mock import patch
from unittest.mock import MagicMock
from unittest.mock import PropertyMock

import src.superannotate as sa
from src.superannotate import controller
from src.superannotate.lib.core.entities import TeamEntity
from src.superannotate.lib.core.entities import ProjectEntity
from src.superannotate.lib.core.entities import UserEntity

from tests.integration.base import BaseTestCase


class TestProject(BaseTestCase):
    PROJECT_NAME = "add_contributors_to_project"
    PROJECT_TYPE = "Vector"
    PROJECT_DESCRIPTION = "DESCRIPTION"
    TEST_EMAILS = ()

    @property
    def random_email(self):
        return f"{''.join(random.choice(string.ascii_letters) for _ in range(7))}@gmail.com"

    @patch("lib.infrastructure.controller.Controller.get_team")
    @patch("lib.infrastructure.controller.Controller.backend_client", new_callable=PropertyMock)
    def test_invite_contributors(self, client, get_team_mock):
        random_emails = [self.random_email for i in range(20)]
        client.return_value.invite_contributors.return_value = random_emails[:3], []
        team_users = [UserEntity(email=email, user_role=3) for email in random_emails[: 10]]
        to_add_emails = random_emails[8: 18]
        pending_users = [dict(email=email, user_role=3) for email in random_emails[15: 20]]

        team_data = MagicMock()
        get_team_mock.return_value = team_data
        team_data.data = TeamEntity(
            uuid=controller.team_id,
            users=team_users,
            pending_invitations=pending_users
        )

        added, skipped = sa.invite_contributors_to_team(to_add_emails, False)
        self.assertEqual(len(added), 3)
        self.assertEqual(len(skipped), 5)

    def test_(self):
        sa.search_team_contributors(email="vaghinak@superannotate.com", first_name="Vaghinak")