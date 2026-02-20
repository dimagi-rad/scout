"""
Graph nodes for the Scout data agent platform.

This module defines the specialized nodes used in the LangGraph agent graph
for self-correction and error handling:

- check_result_node: Examines tool results for errors or suspicious patterns
- diagnose_and_retry_node: Asks the agent to diagnose and fix errors

These nodes enable the agent's self-healing capability, allowing it to
automatically retry failed queries with corrections up to a configurable limit.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

if TYPE_CHECKING:
    from apps.agents.graph.state import AgentState

logger = logging.getLogger(__name__)


def reset_retry_on_new_message(state: AgentState) -> dict[str, Any]:
    """
    Reset retry count and correction context when a new user message arrives.

    This node should be called at the entry point to check if the latest message
    is a new HumanMessage (not a correction/retry). If so, it resets the retry
    state to allow fresh correction attempts for new queries.

    Args:
        state: The current agent state containing messages and context.

    Returns:
        Updated state dict with reset `retry_count` and `correction_context`
        if a new user message is detected, otherwise empty dict.
    """
    messages = state.get("messages", [])

    if not messages:
        return {}

    # Check if the last message is a HumanMessage (new user input)
    last_message = messages[-1]
    if isinstance(last_message, HumanMessage):
        # Reset retry state for new user messages
        logger.debug("New user message detected, resetting retry count")
        return {
            "retry_count": 0,
            "correction_context": {},
        }

    return {}


def check_result_node(state: AgentState) -> dict[str, Any]:
    """
    Examine tool results for errors or suspicious patterns.

    This node inspects the most recent tool message(s) to determine if
    correction is needed. It checks for:
    - SQL execution errors in the tool response
    - Empty results when data was expected
    - Database-level errors

    The node sets `needs_correction=True` in the state if any issues are
    detected, along with context information for the retry logic.

    Args:
        state: The current agent state containing messages and context.

    Returns:
        Updated state dict with `needs_correction` and `correction_context` set.

    Example:
        If the SQL tool returns {"error": "column 'usr_id' does not exist"},
        this node will set:
        - needs_correction: True
        - correction_context: {"error_type": "execution", "error_message": "..."}
    """
    messages = state.get("messages", [])

    if not messages:
        return {
            "needs_correction": False,
            "correction_context": {"failed_sql": "", "tables_accessed": []},
        }

    # Look at the most recent messages to find tool results
    last_message = messages[-1]

    # If the last message is not a ToolMessage, nothing to check
    if not isinstance(last_message, ToolMessage):
        return {
            "needs_correction": False,
            "correction_context": {"failed_sql": "", "tables_accessed": []},
        }

    # Parse the tool result content
    content = last_message.content

    # Handle string content (typical for tool results)
    if isinstance(content, str):
        try:
            result = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # Not JSON — only flag as error if this is an error status from LangChain
            # (tool raised an exception). Don't substring-match normal text content
            # since words like "error" or "invalid" appear in regular data/responses.
            if last_message.status == "error":
                return {
                    "needs_correction": True,
                    "correction_context": {
                        "error_type": "execution",
                        "error_message": content,
                        "tool_name": last_message.name or "unknown",
                        "failed_sql": "",
                        "tables_accessed": [],
                    },
                }
            return {
                "needs_correction": False,
                "correction_context": {"failed_sql": "", "tables_accessed": []},
            }

        # Check for error in the result (supports both formats)
        if isinstance(result, dict):
            error_message = _extract_error(result)
            if error_message:
                logger.info(
                    "Detected error in tool result: %s",
                    error_message[:100] + "..." if len(error_message) > 100 else error_message,
                )

                data = result.get("data", result)
                return {
                    "needs_correction": True,
                    "correction_context": {
                        "error_type": _classify_error(error_message),
                        "error_message": error_message,
                        "tool_name": last_message.name or "unknown",
                        "failed_sql": data.get("sql_executed", ""),
                        "tables_accessed": data.get("tables_accessed", []),
                    },
                }

    # Handle dict content directly
    elif isinstance(content, dict):
        error_message = _extract_error(content)
        if error_message:
            data = content.get("data", content)
            return {
                "needs_correction": True,
                "correction_context": {
                    "error_type": _classify_error(error_message),
                    "error_message": error_message,
                    "tool_name": last_message.name or "unknown",
                    "failed_sql": data.get("sql_executed", ""),
                    "tables_accessed": data.get("tables_accessed", []),
                },
            }

    # No errors detected
    return {
        "needs_correction": False,
        "correction_context": {"failed_sql": "", "tables_accessed": []},
    }


def diagnose_and_retry_node(state: AgentState) -> dict[str, Any]:
    """
    Ask the agent to diagnose and fix an error.

    When a query fails, this node injects a diagnostic prompt that guides
    the agent to:
    1. Understand what went wrong
    2. Check relevant knowledge (business rules, learnings)
    3. Generate a corrected query
    4. Explain the fix

    The node tracks retry count and gives up after MAX_RETRIES (default 3),
    at which point it instructs the agent to explain the issue to the user.

    Args:
        state: The current agent state containing the error context and
               retry count.

    Returns:
        Updated state dict with:
        - A new system message containing diagnosis instructions
        - Updated retry_count
        - needs_correction reset to False (to allow the agent to proceed)

    Example:
        On first retry:
        - Injects: "Your previous query encountered an error: [error]. Please..."
        - Sets: retry_count = 1, needs_correction = False

        After 3 retries:
        - Injects: "You've tried 3 times. Explain the issue to the user..."
        - Sets: retry_count = 3, needs_correction = False
    """
    MAX_RETRIES = 3

    correction_context = state.get("correction_context", {})
    retry_count = state.get("retry_count", 0)
    messages = list(state.get("messages", []))

    error_message = correction_context.get("error_message", "Unknown error")
    error_type = correction_context.get("error_type", "unknown")
    failed_sql = correction_context.get("failed_sql", "")
    tool_name = correction_context.get("tool_name", "execute_sql")

    if retry_count >= MAX_RETRIES:
        # Give up and explain to the user
        logger.warning(
            "Max retries (%d) reached for error: %s",
            MAX_RETRIES,
            error_message[:100],
        )

        give_up_prompt = f"""You have attempted to fix this query {MAX_RETRIES} times without success.

**Error encountered:** {error_message}

**Last attempted SQL:**
```sql
{failed_sql}
```

Please:
1. Explain to the user what you were trying to do
2. Describe the error you encountered in plain language
3. Suggest alternative approaches they might take:
   - Could they rephrase their question?
   - Is there a simpler query that might work?
   - Should they check the data dictionary for correct table/column names?
4. If you discovered any patterns that might help future queries, use the save_learning tool to record them

Be helpful and constructive. The goal is to help the user get unstuck."""

        return {
            "messages": messages + [SystemMessage(content=give_up_prompt)],
            "retry_count": retry_count,
            "needs_correction": False,
            "correction_context": {},
        }

    # Build the diagnosis prompt
    diagnosis_prompt = _build_diagnosis_prompt(
        error_message=error_message,
        error_type=error_type,
        failed_sql=failed_sql,
        tool_name=tool_name,
        retry_number=retry_count + 1,
        max_retries=MAX_RETRIES,
    )

    logger.info(
        "Initiating retry %d/%d for error type '%s'",
        retry_count + 1,
        MAX_RETRIES,
        error_type,
    )

    return {
        "messages": messages + [SystemMessage(content=diagnosis_prompt)],
        "retry_count": retry_count + 1,
        "needs_correction": False,
        "correction_context": correction_context,  # Preserve for potential learning
    }


def _extract_error(result: dict) -> str | None:
    """
    Extract an error message from a tool result dict.

    Supports two formats:
    - Direct: {"error": "message string"}
    - MCP envelope: {"success": false, "error": {"code": "...", "message": "..."}}

    Returns:
        The error message string, or None if no error detected.
    """
    # MCP envelope format: {"success": false, "error": {"code": ..., "message": ...}}
    if result.get("success") is False:
        err = result.get("error", {})
        if isinstance(err, dict):
            return err.get("message", str(err))
        return str(err) if err else None

    # Direct format: {"error": "message string"}
    err = result.get("error")
    if err:
        return str(err)

    return None


def _classify_error(error_message: str) -> str:
    """
    Classify an error message into a category for better diagnosis.

    Categories:
    - syntax: SQL syntax errors
    - column_not_found: Invalid column references
    - table_not_found: Invalid table references
    - permission: Access denied errors
    - timeout: Query timeout
    - type_mismatch: Data type errors
    - execution: Other execution errors

    Args:
        error_message: The error message from the database or validator.

    Returns:
        Error category string.
    """
    error_lower = error_message.lower()

    if "syntax error" in error_lower or "parse error" in error_lower:
        return "syntax"

    if "column" in error_lower and ("does not exist" in error_lower or "not found" in error_lower):
        return "column_not_found"

    if "relation" in error_lower and "does not exist" in error_lower:
        return "table_not_found"

    if "table" in error_lower and ("does not exist" in error_lower or "not found" in error_lower):
        return "table_not_found"

    if "permission denied" in error_lower or "access denied" in error_lower:
        return "permission"

    if "timeout" in error_lower or "cancelled" in error_lower or "timed out" in error_lower:
        return "timeout"

    if "type" in error_lower and ("mismatch" in error_lower or "cannot" in error_lower):
        return "type_mismatch"

    return "execution"


def _build_diagnosis_prompt(
    error_message: str,
    error_type: str,
    failed_sql: str,
    tool_name: str,
    retry_number: int,
    max_retries: int,
) -> str:
    """
    Build a diagnosis prompt tailored to the error type.

    Args:
        error_message: The error message from the failed operation.
        error_type: Category of the error (from _classify_error).
        failed_sql: The SQL that failed (if applicable).
        tool_name: Name of the tool that failed.
        retry_number: Current retry attempt (1, 2, or 3).
        max_retries: Maximum retries allowed.

    Returns:
        Formatted prompt string for the agent.
    """
    base_prompt = f"""Your previous query encountered an error:

**Error:** {error_message}
"""

    if failed_sql:
        base_prompt += f"""
**Failed SQL:**
```sql
{failed_sql}
```
"""

    # Add error-specific guidance
    guidance = _get_error_guidance(error_type)

    base_prompt += f"""
**Error Type:** {error_type}

{guidance}

**Instructions:**
1. Diagnose what went wrong based on the error message
2. Check your context for relevant information:
   - Look at the data dictionary for correct table/column names
   - Check business rules that might apply
   - Review any learned corrections for similar issues
3. Generate a corrected query that addresses the issue
4. Explain what you changed and why

**Important:**
- This is retry {retry_number} of {max_retries}
- If you successfully fix the issue, consider using the save_learning tool to record the pattern for future reference
- If you're unsure about column names or table structure, use the describe_table tool first
"""

    return base_prompt


def _get_error_guidance(error_type: str) -> str:
    """
    Get error-type-specific guidance for the diagnosis prompt.

    Args:
        error_type: Category of the error.

    Returns:
        Guidance text specific to the error type.
    """
    guidance_map = {
        "syntax": """**Syntax Error Guidance:**
- Check for missing or extra commas, parentheses, or quotes
- Verify SQL keywords are spelled correctly
- Ensure string literals use single quotes, not double quotes
- Check that table aliases are used consistently""",
        "column_not_found": """**Column Not Found Guidance:**
- Use the describe_table tool to see the exact column names
- Column names are case-sensitive in some contexts
- Check if you're using the correct table alias
- Look for typos in the column name""",
        "table_not_found": """**Table Not Found Guidance:**
- Check the data dictionary for the correct table name
- Table names are usually lowercase in PostgreSQL
- Verify you're not referencing a table from another schema
- The table might have an underscore where you used a hyphen (or vice versa)""",
        "permission": """**Permission Error Guidance:**
- You can only run SELECT queries
- You cannot access tables outside the project's schema
- System tables (pg_catalog, information_schema) are not accessible
- Some tables may be explicitly excluded from this project""",
        "timeout": """**Timeout Error Guidance:**
- The query took too long to execute
- Add more specific WHERE conditions to reduce the data scanned
- Consider using LIMIT to sample the data first
- Avoid SELECT * on large tables; select only needed columns
- Break complex queries into smaller steps""",
        "type_mismatch": """**Type Mismatch Guidance:**
- Check the column types in the data dictionary
- Use explicit casts when comparing different types: column::type
- Common issues: comparing text to integers, timestamp formats
- NULL comparisons need IS NULL, not = NULL""",
        "execution": """**General Error Guidance:**
- Read the error message carefully for hints
- Check the data dictionary for correct names and types
- Verify the logic of your JOINs and WHERE conditions
- Try simplifying the query to isolate the problem""",
    }

    return guidance_map.get(error_type, guidance_map["execution"])


__all__ = [
    "check_result_node",
    "diagnose_and_retry_node",
    "reset_retry_on_new_message",
]
