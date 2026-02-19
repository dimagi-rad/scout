"""
Tests for artifact sharing API endpoints.

Tests cover:
- Creating share links with different access levels
- Listing share links for an artifact
- Revoking share links
- Permission checks (creator vs admin vs regular user)
"""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.artifacts.models import AccessLevel, Artifact, ArtifactType, SharedArtifact
from apps.projects.models import DatabaseConnection, Project, ProjectMembership, ProjectRole
from apps.users.models import User


class ArtifactShareAPITestCase(TestCase):
    """Base test case with common setup for share API tests."""

    @classmethod
    def setUpTestData(cls):
        """Set up test data for all tests in the class."""
        # Create users
        cls.creator = User.objects.create_user(
            email="creator@example.com",
            password="testpass123",
        )
        cls.admin_user = User.objects.create_user(
            email="admin@example.com",
            password="testpass123",
        )
        cls.regular_user = User.objects.create_user(
            email="regular@example.com",
            password="testpass123",
        )
        cls.other_user = User.objects.create_user(
            email="other@example.com",
            password="testpass123",
        )

        # Create a database connection and project
        cls.db_conn = DatabaseConnection(
            name="Test Connection",
            db_host="localhost",
            db_port=5432,
            db_name="testdb",
        )
        cls.db_conn.db_user = "testuser"
        cls.db_conn.db_password = "testpassword"
        cls.db_conn.save()

        cls.project = Project.objects.create(
            name="Test Project",
            slug="test-project",
            database_connection=cls.db_conn,
            db_schema="public",
        )

        # Create project memberships
        ProjectMembership.objects.create(
            user=cls.creator,
            project=cls.project,
            role=ProjectRole.ANALYST,
        )
        ProjectMembership.objects.create(
            user=cls.admin_user,
            project=cls.project,
            role=ProjectRole.ADMIN,
        )
        ProjectMembership.objects.create(
            user=cls.regular_user,
            project=cls.project,
            role=ProjectRole.VIEWER,
        )

        # Create an artifact
        cls.artifact = Artifact.objects.create(
            project=cls.project,
            created_by=cls.creator,
            title="Test Artifact",
            description="A test artifact",
            artifact_type=ArtifactType.REACT,
            code="function App() { return <div>Hello</div>; }",
            conversation_id="test-conversation-123",
        )

    def setUp(self):
        """Set up API client for each test."""
        self.client = APIClient()


class CreateShareViewTests(ArtifactShareAPITestCase):
    """Tests for POST /api/artifacts/{id}/share/"""

    def test_create_share_as_creator(self):
        """Artifact creator can create a share link."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {"access_level": "public"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("share_token", response.data)
        self.assertIn("share_url", response.data)
        self.assertEqual(response.data["access_level"], "public")

    def test_create_share_as_admin(self):
        """Project admin can create a share link."""
        self.client.force_authenticate(user=self.admin_user)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {"access_level": "project"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["access_level"], "project")

    def test_create_share_as_regular_user_forbidden(self):
        """Regular project member cannot create share links."""
        self.client.force_authenticate(user=self.regular_user)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {"access_level": "public"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_share_unauthenticated(self):
        """Unauthenticated users cannot create share links."""
        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {"access_level": "public"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_share_with_expiration(self):
        """Share link can have an expiration date."""
        self.client.force_authenticate(user=self.creator)

        expires_at = timezone.now() + timedelta(days=7)
        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {
            "access_level": "public",
            "expires_at": expires_at.isoformat(),
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data["is_expired"])

    def test_create_share_with_past_expiration_fails(self):
        """Share link cannot have expiration in the past."""
        self.client.force_authenticate(user=self.creator)

        expires_at = timezone.now() - timedelta(days=1)
        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {
            "access_level": "public",
            "expires_at": expires_at.isoformat(),
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_share_specific_access_requires_users(self):
        """Specific access level requires allowed_users."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {"access_level": "specific"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_users", response.data)

    def test_create_share_specific_with_users(self):
        """Specific access level with allowed users succeeds."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {
            "access_level": "specific",
            "allowed_users": [self.other_user.id],
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["access_level"], "specific")
        self.assertIn(self.other_user.email, response.data["allowed_user_emails"])

    def test_create_share_default_access_level(self):
        """Default access level is 'project'."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["access_level"], "project")

    def test_create_share_nonexistent_artifact(self):
        """Creating share for nonexistent artifact returns 404."""
        self.client.force_authenticate(user=self.creator)

        fake_id = uuid.uuid4()
        url = reverse("artifacts:create_share", kwargs={"artifact_id": fake_id})
        response = self.client.post(url, {"access_level": "public"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ListSharesViewTests(ArtifactShareAPITestCase):
    """Tests for GET /api/artifacts/{id}/shares/"""

    def setUp(self):
        super().setUp()
        # Create some share links
        self.share1 = SharedArtifact.objects.create(
            artifact=self.artifact,
            created_by=self.creator,
            share_token="token1234567890",
            access_level=AccessLevel.PUBLIC,
        )
        self.share2 = SharedArtifact.objects.create(
            artifact=self.artifact,
            created_by=self.creator,
            share_token="token0987654321",
            access_level=AccessLevel.PROJECT,
        )

    def test_list_shares_as_creator(self):
        """Artifact creator can list share links."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:list_shares", kwargs={"artifact_id": self.artifact.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_list_shares_as_admin(self):
        """Project admin can list share links."""
        self.client.force_authenticate(user=self.admin_user)

        url = reverse("artifacts:list_shares", kwargs={"artifact_id": self.artifact.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_list_shares_as_regular_user_forbidden(self):
        """Regular project member cannot list share links."""
        self.client.force_authenticate(user=self.regular_user)

        url = reverse("artifacts:list_shares", kwargs={"artifact_id": self.artifact.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_shares_unauthenticated(self):
        """Unauthenticated users cannot list share links."""
        url = reverse("artifacts:list_shares", kwargs={"artifact_id": self.artifact.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_shares_returns_expected_fields(self):
        """List response includes expected fields."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:list_shares", kwargs={"artifact_id": self.artifact.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share = response.data[0]
        self.assertIn("id", share)
        self.assertIn("share_token", share)
        self.assertIn("share_url", share)
        self.assertIn("access_level", share)
        self.assertIn("expires_at", share)
        self.assertIn("is_expired", share)
        self.assertIn("view_count", share)


class RevokeShareViewTests(ArtifactShareAPITestCase):
    """Tests for DELETE /api/artifacts/{id}/shares/{token}/"""

    def setUp(self):
        super().setUp()
        self.share = SharedArtifact.objects.create(
            artifact=self.artifact,
            created_by=self.creator,
            share_token="tokentorevoke123",
            access_level=AccessLevel.PUBLIC,
        )

    def test_revoke_share_as_creator(self):
        """Artifact creator can revoke share links."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:revoke_share", kwargs={
            "artifact_id": self.artifact.id,
            "share_token": self.share.share_token,
        })
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SharedArtifact.objects.filter(pk=self.share.pk).exists())

    def test_revoke_share_as_admin(self):
        """Project admin can revoke share links."""
        self.client.force_authenticate(user=self.admin_user)

        url = reverse("artifacts:revoke_share", kwargs={
            "artifact_id": self.artifact.id,
            "share_token": self.share.share_token,
        })
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SharedArtifact.objects.filter(pk=self.share.pk).exists())

    def test_revoke_share_as_regular_user_forbidden(self):
        """Regular project member cannot revoke share links."""
        self.client.force_authenticate(user=self.regular_user)

        url = reverse("artifacts:revoke_share", kwargs={
            "artifact_id": self.artifact.id,
            "share_token": self.share.share_token,
        })
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(SharedArtifact.objects.filter(pk=self.share.pk).exists())

    def test_revoke_share_unauthenticated(self):
        """Unauthenticated users cannot revoke share links."""
        url = reverse("artifacts:revoke_share", kwargs={
            "artifact_id": self.artifact.id,
            "share_token": self.share.share_token,
        })
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_revoke_nonexistent_share(self):
        """Revoking nonexistent share returns 404."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:revoke_share", kwargs={
            "artifact_id": self.artifact.id,
            "share_token": "nonexistent-token",
        })
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ShareTokenGenerationTests(ArtifactShareAPITestCase):
    """Tests for share token uniqueness and security."""

    def test_tokens_are_unique(self):
        """Each share link gets a unique token."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})

        tokens = set()
        for _ in range(5):
            response = self.client.post(url, {"access_level": "public"})
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            tokens.add(response.data["share_token"])

        # All tokens should be unique
        self.assertEqual(len(tokens), 5)

    def test_token_length(self):
        """Share tokens have sufficient length for security."""
        self.client.force_authenticate(user=self.creator)

        url = reverse("artifacts:create_share", kwargs={"artifact_id": self.artifact.id})
        response = self.client.post(url, {"access_level": "public"})

        # secrets.token_urlsafe(32) produces ~43 character tokens
        self.assertGreaterEqual(len(response.data["share_token"]), 40)
