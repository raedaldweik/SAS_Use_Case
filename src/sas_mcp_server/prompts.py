# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt templates for the use-case MCP server.

Two short workflows that chain the tools the way a good analyst would. They
are registered via ``register_prompts(mcp)`` on the FastMCP instance; clients
that support MCP prompts (Claude Desktop, for example) offer them as
slash-commands.
"""

from fastmcp.prompts import Message


def register_prompts(mcp) -> None:
    """Register all prompt templates on *mcp*."""

    @mcp.prompt()
    def explore_use_case(focus: str | None = None) -> list[Message]:
        """Profile the use-case dataset: size, key columns, distributions, and a chart or two."""
        focus_line = f"\nFocus on: {focus}." if focus else ""
        return [
            Message(
                role="user",
                content=(
                    "Give me an overview of the data this assistant works on.\n\n"
                    "1. Call get_use_case to learn the table, its columns and the models available.\n"
                    "2. Use query_data to summarise the table: the row count, the distribution of the most "
                    "important categorical columns (counts per value, top 10), and the average/min/max of the "
                    "key numeric columns. Aggregate in SQL; do not pull raw rows.\n"
                    "3. Chart the one or two most informative summaries with render_chart.\n"
                    "4. Finish with five short bullet findings and one suggested next question."
                    f"{focus_line}"
                ),
            )
        ]

    @mcp.prompt()
    def score_and_explain(record: str, model: str | None = None) -> list[Message]:
        """Score a described record against a ready model and explain the result in plain language."""
        model_line = (
            f"Use the model '{model}'." if model else "Use the model from get_use_case (ask if there are several)."
        )
        return [
            Message(
                role="user",
                content=(
                    f"Score this record and explain the result:\n\n{record}\n\n"
                    f"{model_line}\n"
                    "1. Call describe_model to see the exact inputs the model expects.\n"
                    "2. Build input_data from the description; if a required input is missing, either look "
                    "it up in the data with query_data (when the record can be identified there) or ask "
                    "for it before scoring.\n"
                    "3. Call score_data, then explain the outputs in plain language: what the score means, "
                    "how confident it is, and which inputs most likely drove it. Mention anything that "
                    "was scored as missing."
                ),
            )
        ]
