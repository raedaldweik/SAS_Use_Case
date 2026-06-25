# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for viya_utils module.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from sas_mcp_server.viya_utils import (
    get_context_id,
    create_session,
    submit_job,
    wait_job,
    run_one_snippet,
    run_query_rows,
    fetch_session_table_rows,
    fetch_full_job_log,
    fetch_full_job_listing,
    fetch_full_session_log,
    _get_text,
    _get_paged_lines
)


@pytest.mark.asyncio
async def test_get_context_id_success(mock_httpx_client, mock_context_response, mock_env_vars):
    """Test successful context ID retrieval."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=mock_context_response)
    mock_httpx_client.get.return_value = mock_response
    
    context_id = await get_context_id(mock_httpx_client, "Test Context")
    
    assert context_id == "test-context-id"
    mock_httpx_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_context_id_not_found(mock_httpx_client, mock_env_vars):
    """Test context ID retrieval when context is not found."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value={"items": []})
    mock_httpx_client.get.return_value = mock_response
    
    with pytest.raises(RuntimeError, match="Compute context not found"):
        await get_context_id(mock_httpx_client, "NonExistent Context")


@pytest.mark.asyncio
async def test_create_session(mock_httpx_client, mock_session_response, mock_env_vars):
    """Test session creation."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=mock_session_response)
    mock_httpx_client.post.return_value = mock_response
    
    session_id = await create_session(mock_httpx_client, "test-context-id", "test-session")
    
    assert session_id == "test-session-id"
    mock_httpx_client.post.assert_called_once()
    call_args = mock_httpx_client.post.call_args
    assert call_args[1]["json"]["name"] == "test-session"


@pytest.mark.asyncio
async def test_submit_job(mock_httpx_client, mock_job_response, sample_sas_code, mock_env_vars):
    """Test job submission."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=mock_job_response)
    mock_httpx_client.post.return_value = mock_response
    
    job_id = await submit_job(mock_httpx_client, "test-session-id", sample_sas_code)
    
    assert job_id == "test-job-id"
    mock_httpx_client.post.assert_called_once()
    call_args = mock_httpx_client.post.call_args
    assert "code" in call_args[1]["json"]
    assert isinstance(call_args[1]["json"]["code"], list)


@pytest.mark.asyncio
async def test_wait_job_completed(mock_httpx_client, mock_job_log, mock_job_listing, mock_env_vars):
    """Test waiting for job completion."""
    # Mock state response
    mock_state_response = AsyncMock()
    mock_state_response.text = "completed"
    
    # Mock log response
    mock_log_response = AsyncMock()
    mock_log_response.json = MagicMock(return_value=mock_job_log)
    
    # Mock listing response
    mock_listing_response = AsyncMock()
    mock_listing_response.json = MagicMock(return_value=mock_job_listing)
    
    # Set up the client to return different responses
    mock_httpx_client.get.side_effect = [
        mock_state_response,
        mock_log_response,
        mock_listing_response
    ]
    
    state, log, listing = await wait_job(mock_httpx_client, "test-session-id", "test-job-id", poll=0.01)
    
    assert state == "completed"
    assert "NOTE: DATA statement used" in log
    assert "Obs    x    y" in listing


@pytest.mark.asyncio
async def test_wait_job_error_state(mock_httpx_client, mock_job_log, mock_job_listing, mock_env_vars):
    """Test waiting for job that ends in error state."""
    mock_state_response = AsyncMock()
    mock_state_response.text = "error"
    
    mock_log_response = AsyncMock()
    mock_log_response.json = MagicMock(return_value={
        "items": [{"line": "ERROR: Something went wrong"}]
    })
    
    mock_listing_response = AsyncMock()
    mock_listing_response.json = MagicMock(return_value={"items": []})
    
    mock_httpx_client.get.side_effect = [
        mock_state_response,
        mock_log_response,
        mock_listing_response
    ]
    
    state, log, listing = await wait_job(mock_httpx_client, "test-session-id", "test-job-id", poll=0.01)
    
    assert state == "error"
    assert "ERROR: Something went wrong" in log


@pytest.mark.asyncio
async def test_get_text_success(mock_httpx_client):
    """Test _get_text when text/plain is returned."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/plain"}
    mock_response.text = "Sample text output"
    mock_httpx_client.get.return_value = mock_response
    
    result = await _get_text("/test/endpoint", mock_httpx_client)
    
    assert result == "Sample text output"


@pytest.mark.asyncio
async def test_get_text_fallback(mock_httpx_client):
    """Test _get_text fallback when first attempt fails."""
    mock_response_1 = AsyncMock()
    mock_response_1.status_code = 404
    mock_response_1.headers = {}
    
    mock_response_2 = AsyncMock()
    mock_response_2.status_code = 200
    mock_response_2.headers = {"Content-Type": "text/plain"}
    mock_response_2.text = "Sample text output"
    
    mock_httpx_client.get.side_effect = [mock_response_1, mock_response_2]
    
    result = await _get_text("/test/endpoint", mock_httpx_client)
    
    assert result == "Sample text output"
    assert mock_httpx_client.get.call_count == 2


@pytest.mark.asyncio
async def test_get_text_failure(mock_httpx_client):
    """Test _get_text when both attempts fail."""
    mock_response = AsyncMock()
    mock_response.status_code = 404
    mock_response.headers = {}
    
    mock_httpx_client.get.return_value = mock_response
    
    result = await _get_text("/test/endpoint", mock_httpx_client)
    
    assert result is None


@pytest.mark.asyncio
async def test_get_paged_lines(mock_httpx_client):
    """Test _get_paged_lines pagination."""
    # First page
    mock_response_1 = AsyncMock()
    mock_response_1.raise_for_status = MagicMock()
    mock_response_1.json = MagicMock(return_value={
        "items": [
            {"line": "Line 1"},
            {"line": "Line 2"}
        ]
    })
    
    # Second page (empty, ends pagination)
    mock_response_2 = AsyncMock()
    mock_response_2.raise_for_status = MagicMock()
    mock_response_2.json = MagicMock(return_value={"items": []})
    
    mock_httpx_client.get.side_effect = [mock_response_1, mock_response_2]
    
    result = await _get_paged_lines("/test/endpoint", mock_httpx_client, page_limit=2)
    
    assert result == "Line 1\nLine 2"
    assert mock_httpx_client.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_full_job_log(mock_httpx_client):
    """Test fetching full job log."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/plain"}
    mock_response.text = "Job log output"
    mock_httpx_client.get.return_value = mock_response
    
    result = await fetch_full_job_log(mock_httpx_client, "session-id", "job-id")
    
    assert result == "Job log output"


@pytest.mark.asyncio
async def test_fetch_full_job_listing(mock_httpx_client):
    """Test fetching full job listing."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/plain"}
    mock_response.text = "Job listing output"
    mock_httpx_client.get.return_value = mock_response
    
    result = await fetch_full_job_listing(mock_httpx_client, "session-id", "job-id")
    
    assert result == "Job listing output"


@pytest.mark.asyncio
async def test_run_one_snippet_success(sample_sas_code, mock_access_token, mock_env_vars):
    """Test successful execution of a SAS code snippet."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        # Mock all the API calls
        mock_context_response = AsyncMock()
        mock_context_response.json = MagicMock(return_value={"items": [{"id": "ctx-id"}]})
        
        mock_session_response = AsyncMock()
        mock_session_response.json = MagicMock(return_value={"id": "sess-id"})
        
        mock_job_response = AsyncMock()
        mock_job_response.json = MagicMock(return_value={"id": "job-id"})
        
        mock_state_response = AsyncMock()
        mock_state_response.text = "completed"
        
        mock_log_response = AsyncMock()
        mock_log_response.json = MagicMock(return_value={"items": [{"line": "Log output"}]})
        
        mock_listing_response = AsyncMock()
        mock_listing_response.json = MagicMock(return_value={"items": [{"line": "Listing output"}]})
        
        mock_delete_response = AsyncMock()
        
        mock_client.get.side_effect = [
            mock_context_response,
            mock_state_response,
            mock_log_response,
            mock_listing_response
        ]
        mock_client.post.side_effect = [mock_session_response, mock_job_response]
        mock_client.delete.return_value = mock_delete_response
        
        result = await run_one_snippet(sample_sas_code, "1", mock_access_token)
        
        assert result[0] == "1"  # snippet_id
        assert result[1] == "completed"  # state
        assert "Log output" in result[2]  # log
        assert "Listing output" in result[3]  # listing


@pytest.mark.asyncio
async def test_run_one_snippet_with_bearer_prefix(sample_sas_code, mock_env_vars):
    """Test that Bearer prefix is handled correctly."""
    token_with_bearer = "Bearer test-token"
    
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        # Mock minimal responses for the test
        mock_context_response = AsyncMock()
        mock_context_response.json = MagicMock(return_value={"items": [{"id": "ctx-id"}]})
        
        mock_session_response = AsyncMock()
        mock_session_response.json = MagicMock(return_value={"id": "sess-id"})
        
        mock_job_response = AsyncMock()
        mock_job_response.json = MagicMock(return_value={"id": "job-id"})
        
        mock_state_response = AsyncMock()
        mock_state_response.text = "completed"
        
        mock_log_response = AsyncMock()
        mock_log_response.json = MagicMock(return_value={"items": []})
        
        mock_listing_response = AsyncMock()
        mock_listing_response.json = MagicMock(return_value={"items": []})
        
        mock_delete_response = AsyncMock()
        
        mock_client.get.side_effect = [
            mock_context_response,
            mock_state_response,
            mock_log_response,
            mock_listing_response
        ]
        mock_client.post.side_effect = [mock_session_response, mock_job_response]
        mock_client.delete.return_value = mock_delete_response
        
        result = await run_one_snippet(sample_sas_code, "1", token_with_bearer)

        # Verify the client was created with Bearer token
        call_kwargs = mock_client_class.call_args[1]
        assert "Authorization" in call_kwargs["headers"]
        assert call_kwargs["headers"]["Authorization"] == token_with_bearer


@pytest.mark.asyncio
async def test_fetch_session_table_rows(mock_httpx_client):
    """Columns + rows are zipped into row dicts via the Compute data API."""
    col_resp = AsyncMock()
    col_resp.raise_for_status = MagicMock()
    col_resp.json = MagicMock(return_value={
        "items": [{"name": "nationality"}, {"name": "n"}]})
    row_resp = AsyncMock()
    row_resp.raise_for_status = MagicMock()
    row_resp.json = MagicMock(return_value={
        "items": [{"cells": ["KW", 10]}, {"cells": ["AE", 7]}]})
    mock_httpx_client.get.side_effect = [col_resp, row_resp]

    cols, rows = await fetch_session_table_rows(
        mock_httpx_client, "sess-1", "WORK", "_MCPQ", limit=50)

    assert cols == ["nationality", "n"]
    assert rows == [{"nationality": "KW", "n": 10}, {"nationality": "AE", "n": 7}]
    # The two calls hit the columns and rows sub-resources of the session table.
    col_url = mock_httpx_client.get.call_args_list[0][0][0]
    row_url = mock_httpx_client.get.call_args_list[1][0][0]
    assert "/compute/sessions/sess-1/data/WORK/_MCPQ/columns" in col_url
    assert "/compute/sessions/sess-1/data/WORK/_MCPQ/rows" in row_url


@pytest.mark.asyncio
async def test_run_query_rows_success(mock_access_token, mock_env_vars):
    """run_query_rows wraps the SELECT, runs it, and returns structured rows."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        def _resp(json_data=None, text=None):
            r = AsyncMock()
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=json_data or {})
            if text is not None:
                r.text = text
            return r

        # GET order: context, job-state, log, listing, columns, rows
        mock_client.get.side_effect = [
            _resp({"items": [{"id": "ctx-id"}]}),
            _resp(text="completed"),
            _resp({"items": [{"line": "NOTE: ok"}]}),
            _resp({"items": [{"line": "out"}]}),
            _resp({"items": [{"name": "bad"}, {"name": "n"}]}),
            _resp({"items": [{"cells": [0, 4771]}, {"cells": [1, 1189]}]}),
        ]
        mock_client.post.side_effect = [
            _resp({"id": "sess-id"}),   # create_session
            _resp({"id": "job-id"}),    # submit_job
        ]
        mock_client.delete.return_value = _resp()

        result = await run_query_rows(
            "select bad, count(*) as n from Public.HMEQ group by bad",
            mock_access_token, limit=10)

        assert result["error"] is False
        assert result["state"] == "completed"
        assert result["columns"] == ["bad", "n"]
        assert result["rows"] == [{"bad": 0, "n": 4771}, {"bad": 1, "n": 1189}]
        assert result["rowCount"] == 2

        # The SELECT is wrapped in proc sql writing to WORK._MCPQ.
        submitted_code = "\n".join(mock_client.post.call_args_list[1][1]["json"]["code"])
        assert "create table work._mcpq as" in submitted_code
        assert "group by bad" in submitted_code


@pytest.mark.asyncio
async def test_run_query_rows_error_returns_log(mock_access_token, mock_env_vars):
    """A SAS error returns the log instead of rows so the caller can correct it."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        def _resp(json_data=None, text=None):
            r = AsyncMock()
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=json_data or {})
            if text is not None:
                r.text = text
            return r

        mock_client.get.side_effect = [
            _resp({"items": [{"id": "ctx-id"}]}),
            _resp(text="error"),
            _resp({"items": [{"line": "ERROR: Syntax error"}]}),
            _resp({"items": []}),
        ]
        mock_client.post.side_effect = [
            _resp({"id": "sess-id"}),
            _resp({"id": "job-id"}),
        ]
        mock_client.delete.return_value = _resp()

        result = await run_query_rows("select oops", mock_access_token)

        assert result["error"] is True
        assert result["state"] == "error"
        assert "ERROR: Syntax error" in result["log"]
        assert result["rows"] == []
