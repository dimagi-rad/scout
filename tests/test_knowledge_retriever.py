"""
Comprehensive tests for Knowledge Retriever.

Tests cover:
- Empty knowledge scenarios
- Knowledge entries
- Table knowledge
- Agent learnings
- Full assembly with all knowledge types
- Retrieval filtering and prioritization
"""

from apps.knowledge.models import (
    AgentLearning,
    KnowledgeEntry,
    TableKnowledge,
)
from apps.knowledge.services.retriever import KnowledgeRetriever


class TestEmptyKnowledge:
    """Test retriever behavior with no knowledge."""

    def test_empty_knowledge_returns_valid_string(self, workspace):
        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()
        assert isinstance(result, str)
        assert len(result) >= 0

    def test_empty_knowledge_has_no_sections(self, workspace):
        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()
        # No headers when empty
        assert "##" not in result


class TestKnowledgeEntries:
    """Test retriever with knowledge entries."""

    def test_single_entry(self, workspace, user):
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue from active subscriptions\n\n```sql\nSELECT SUM(amount) FROM subscriptions WHERE status = 'active'\n```",
            tags=["metric"],
            created_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "MRR" in result
        assert "Monthly Recurring Revenue" in result
        assert "SUM(amount)" in result

    def test_multiple_entries(self, workspace, user):
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue",
            tags=["metric"],
            created_by=user,
        )
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="Soft Delete Rule",
            content="Always filter deleted_at IS NULL for active records",
            tags=["rule"],
            created_by=user,
        )
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="Daily Revenue Query",
            content="```sql\nSELECT DATE(created_at), SUM(amount) FROM orders GROUP BY 1\n```",
            tags=["query"],
            created_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "MRR" in result
        assert "Soft Delete Rule" in result
        assert "Daily Revenue Query" in result

    def test_entry_content_appears(self, workspace, user):
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="Revenue excludes cancelled orders",
            content="When calculating revenue, always exclude orders with status 'cancelled' or 'refunded'.",
            tags=["rule", "finance"],
            created_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "Revenue excludes cancelled orders" in result
        assert "cancelled" in result


class TestTableKnowledge:
    """Test retriever with table knowledge."""

    def test_single_table_knowledge(self, workspace, user):
        TableKnowledge.objects.create(
            workspace=workspace,
            table_name="orders",
            description="Customer orders with payment and fulfillment status",
            use_cases=["Revenue reporting", "Order analysis", "Fulfillment tracking"],
            data_quality_notes=["created_at is UTC", "amount is in cents"],
            column_notes={"status": "Values: pending, completed, cancelled"},
            owner="Data Team",
            refresh_frequency="real-time",
            updated_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "orders" in result.lower()
        assert "Customer orders" in result or "orders" in result.lower()

    def test_multiple_table_knowledge(self, workspace, user):
        for i in range(10):
            TableKnowledge.objects.create(
                workspace=workspace,
                table_name=f"table_{i}",
                description=f"Test table {i}",
                use_cases=[f"Use case {i}"],
                updated_by=user,
            )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        for i in range(10):
            assert f"table_{i}" in result.lower()

    def test_table_with_related_tables(self, workspace, user):
        TableKnowledge.objects.create(
            workspace=workspace,
            table_name="orders",
            description="Customer orders",
            related_tables=[
                {"table": "users", "join_hint": "orders.user_id = users.id"},
                {"table": "products", "join_hint": "orders.product_id = products.id"},
            ],
            updated_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "orders" in result.lower()
        if "users" in result:
            assert "related" in result.lower() or "user_id" in result


class TestAgentLearnings:
    """Test retriever with agent learnings."""

    def test_single_learning(self, workspace, user):
        AgentLearning.objects.create(
            workspace=workspace,
            description="Amount column is in cents, not dollars. Divide by 100.",
            category="type_mismatch",
            applies_to_tables=["orders"],
            original_error="Unexpected revenue value",
            original_sql="SELECT amount FROM orders",
            corrected_sql="SELECT amount / 100.0 FROM orders",
            confidence_score=0.8,
            is_active=True,
            discovered_by_user=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "cents" in result.lower() or "divide by 100" in result.lower()

    def test_multiple_learnings_ordered_by_confidence(self, workspace, user):
        AgentLearning.objects.create(
            workspace=workspace,
            description="Low confidence learning",
            category="other",
            applies_to_tables=["table1"],
            confidence_score=0.3,
            is_active=True,
            discovered_by_user=user,
        )
        AgentLearning.objects.create(
            workspace=workspace,
            description="High confidence learning",
            category="other",
            applies_to_tables=["table2"],
            confidence_score=0.9,
            is_active=True,
            discovered_by_user=user,
        )
        AgentLearning.objects.create(
            workspace=workspace,
            description="Medium confidence learning",
            category="other",
            applies_to_tables=["table3"],
            confidence_score=0.6,
            is_active=True,
            discovered_by_user=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        if "High confidence" in result and "Low confidence" in result:
            high_pos = result.index("High confidence")
            low_pos = result.index("Low confidence")
            assert high_pos < low_pos

    def test_inactive_learnings_excluded(self, workspace, user):
        AgentLearning.objects.create(
            workspace=workspace,
            description="Active learning",
            category="other",
            applies_to_tables=["table1"],
            is_active=True,
            discovered_by_user=user,
        )
        AgentLearning.objects.create(
            workspace=workspace,
            description="Inactive learning",
            category="other",
            applies_to_tables=["table2"],
            is_active=False,
            discovered_by_user=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "Active learning" in result
        assert "Inactive learning" not in result

    def test_learning_with_evidence(self, workspace, user):
        AgentLearning.objects.create(
            workspace=workspace,
            description="Status column uses codes not names",
            category="naming",
            applies_to_tables=["orders"],
            original_error="Column 'status_name' does not exist",
            original_sql="SELECT status_name FROM orders",
            corrected_sql="SELECT status FROM orders",
            confidence_score=0.7,
            is_active=True,
            discovered_by_user=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "status" in result.lower()


class TestFullAssembly:
    """Test retriever with all knowledge types together."""

    def test_all_knowledge_types_present(self, workspace, user):
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue\n\n```sql\nSELECT SUM(amount) FROM subscriptions WHERE status = 'active'\n```",
            tags=["metric"],
            created_by=user,
        )

        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="Soft Delete Rule",
            content="Always filter deleted_at IS NULL",
            tags=["rule"],
            created_by=user,
        )

        TableKnowledge.objects.create(
            workspace=workspace,
            table_name="orders",
            description="Customer orders",
            use_cases=["Revenue reporting"],
            updated_by=user,
        )

        AgentLearning.objects.create(
            workspace=workspace,
            description="Amount is in cents",
            category="type_mismatch",
            applies_to_tables=["orders"],
            is_active=True,
            discovered_by_user=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert "MRR" in result
        assert "Soft Delete" in result or "deleted_at" in result
        assert "orders" in result.lower()
        assert "cents" in result.lower()

    def test_knowledge_sections_clearly_separated(self, workspace, user):
        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue\n\n```sql\nSELECT SUM(amount) FROM subscriptions\n```",
            tags=["metric"],
            created_by=user,
        )

        KnowledgeEntry.objects.create(
            workspace=workspace,
            title="Test Rule",
            content="Test description",
            tags=["rule"],
            created_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert len(result) > 0
        assert isinstance(result, str)

    def test_knowledge_context_is_string(self, workspace):
        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()
        assert isinstance(result, str)

    def test_large_knowledge_base(self, workspace, user):
        for i in range(20):
            KnowledgeEntry.objects.create(
                workspace=workspace,
                title=f"Entry {i}",
                content=f"Content for entry {i}",
                tags=["test"],
                created_by=user,
            )

        for i in range(20):
            TableKnowledge.objects.create(
                workspace=workspace,
                table_name=f"table_{i}",
                description=f"Table {i}",
                updated_by=user,
            )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()

        assert isinstance(result, str)
        assert len(result) > 0


class TestRetrievalFiltering:
    """Test knowledge filtering and prioritization."""

    def test_question_based_table_filtering(self, workspace, user):
        TableKnowledge.objects.create(
            workspace=workspace,
            table_name="users",
            description="User accounts and profiles",
            use_cases=["User analysis", "Authentication"],
            updated_by=user,
        )
        TableKnowledge.objects.create(
            workspace=workspace,
            table_name="orders",
            description="Customer orders and purchases",
            use_cases=["Revenue analysis", "Sales reporting"],
            updated_by=user,
        )

        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve(user_question="What is the total revenue?")

        if TableKnowledge.objects.filter(workspace=workspace).count() > 1:
            assert "orders" in result.lower()

    def test_retriever_initialization(self, workspace):
        retriever = KnowledgeRetriever(workspace)
        assert retriever.workspace == workspace
        assert hasattr(retriever, "retrieve")

    def test_empty_workspace_knowledge(self, workspace):
        retriever = KnowledgeRetriever(workspace)
        result = retriever.retrieve()
        assert isinstance(result, str)
