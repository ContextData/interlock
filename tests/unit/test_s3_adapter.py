"""Unit tests for the S3 file listing adapter."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interlock.models import S3FileInfo

# ---------------------------------------------------------------------------
# S3FileInfo model tests
# ---------------------------------------------------------------------------


class TestS3FileInfo:
    def test_basic_fields(self):
        info = S3FileInfo(key="data/file.csv", size=1024)
        assert info.key == "data/file.csv"
        assert info.size == 1024
        assert info.last_modified is None
        assert info.etag is None

    def test_full_fields(self):
        now = datetime.now(UTC)
        info = S3FileInfo(
            key="docs/report.pdf",
            size=2048,
            last_modified=now,
            etag='"abc123"',
        )
        assert info.last_modified == now
        assert info.etag == '"abc123"'


# ---------------------------------------------------------------------------
# S3Adapter tests - aiobotocore NOT available
# ---------------------------------------------------------------------------


class TestS3AdapterUnavailable:
    """Tests when aiobotocore is not installed."""

    @pytest.fixture(autouse=True)
    def _hide_aiobotocore(self, monkeypatch):
        """Ensure the adapter thinks aiobotocore is missing."""
        # Re-import the module with aiobotocore blocked
        monkeypatch.setitem(sys.modules, "aiobotocore", None)
        monkeypatch.setitem(sys.modules, "aiobotocore.session", None)
        # Patch the module-level flag
        import interlock.connections.adapters.s3 as s3_mod

        monkeypatch.setattr(s3_mod, "_HAS_AIOBOTOCORE", False)
        self.s3_mod = s3_mod

    @pytest.mark.asyncio
    async def test_initialize_sets_unavailable(self):
        adapter = self.s3_mod.S3Adapter(bucket="test-bucket")
        await adapter.initialize()
        assert adapter.available is False

    @pytest.mark.asyncio
    async def test_list_files_returns_empty(self):
        adapter = self.s3_mod.S3Adapter(bucket="test-bucket")
        await adapter.initialize()
        result = await adapter.list_files()
        assert result == []

    @pytest.mark.asyncio
    async def test_download_raises_when_unavailable(self):
        adapter = self.s3_mod.S3Adapter(bucket="test-bucket")
        await adapter.initialize()
        with pytest.raises(RuntimeError, match="not available"):
            await adapter.download_file("some/key.txt", "/tmp/out.txt")


# ---------------------------------------------------------------------------
# S3Adapter tests - aiobotocore available (mocked)
# ---------------------------------------------------------------------------


class TestS3AdapterAvailable:
    """Tests with aiobotocore mocked as present."""

    @pytest.fixture(autouse=True)
    def _mock_aiobotocore(self, monkeypatch):
        import interlock.connections.adapters.s3 as s3_mod

        monkeypatch.setattr(s3_mod, "_HAS_AIOBOTOCORE", True)
        self.s3_mod = s3_mod

    @pytest.mark.asyncio
    async def test_initialize_sets_available(self):
        adapter = self.s3_mod.S3Adapter(bucket="my-bucket", prefix="data/")
        await adapter.initialize()
        assert adapter.available is True

    @pytest.mark.asyncio
    async def test_list_files_with_extension_filter(self):
        adapter = self.s3_mod.S3Adapter(bucket="b", prefix="p/")
        await adapter.initialize()

        page_data = {
            "Contents": [
                {"Key": "p/a.pdf", "Size": 100, "LastModified": None, "ETag": None},
                {"Key": "p/b.txt", "Size": 200, "LastModified": None, "ETag": None},
                {"Key": "p/c.pdf", "Size": 300, "LastModified": None, "ETag": None},
            ]
        }

        # Build mock chain: session -> client -> paginator -> pages
        mock_paginator = MagicMock()

        async def _async_pages(**kwargs):
            yield page_data

        mock_paginator.paginate = _async_pages

        mock_client = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_client

        with patch.object(self.s3_mod, "_get_aio_session", create=True, return_value=mock_session):
            files = await adapter.list_files(extensions={".pdf"})

        assert len(files) == 2
        assert all(f.key.endswith(".pdf") for f in files)

    @pytest.mark.asyncio
    async def test_list_files_max_files(self):
        adapter = self.s3_mod.S3Adapter(bucket="b")
        await adapter.initialize()

        page_data = {"Contents": [{"Key": f"file{i}.txt", "Size": i * 10} for i in range(50)]}

        mock_paginator = MagicMock()

        async def _async_pages(**kwargs):
            yield page_data

        mock_paginator.paginate = _async_pages

        mock_client = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_client

        with patch.object(self.s3_mod, "_get_aio_session", create=True, return_value=mock_session):
            files = await adapter.list_files(max_files=5)

        assert len(files) == 5
