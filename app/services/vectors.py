"""Vector store: a versioned collection read through an alias (W7), partitioned by tenant (W1)."""

from __future__ import annotations

import uuid

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.core.config import Settings


class VectorStore:
    def __init__(self, client: QdrantClient, alias: str, collection: str, dim: int) -> None:
        self._client = client
        self.alias = alias  # what the application uses, e.g. "chunks"
        self.collection = collection  # the physical, versioned collection, e.g. "chunks_v1"
        self.dim = dim

    @classmethod
    def from_settings(cls, settings: Settings) -> "VectorStore":
        client = QdrantClient(url=settings.qdrant_url, timeout=30)
        return cls(client, settings.embed_alias, settings.embed_collection, settings.embed_dim)

    def ensure_collection(self) -> None:
        """Idempotent. Safe when several workers start at the same moment."""
        if self.alias in {a.alias_name for a in self._client.get_aliases().aliases}:
            return
        if not self._client.collection_exists(self.collection):
            try:
                self._client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE),
                )
            except UnexpectedResponse:
                if not self._client.collection_exists(self.collection):  
                    raise
            # One collection for all tenants, partitioned by a tenant index (not one collection per tenant).
            self._client.create_payload_index(
                collection_name=self.collection,
                field_name="tenant_id",
                field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
            )
            self._client.create_payload_index(
                collection_name=self.collection,
                field_name="file_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        self._client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=self.collection, alias_name=self.alias)
                )
            ]
        )

    def upsert(self, points: list[models.PointStruct]) -> None:
        self._client.upsert(collection_name=self.alias, points=points, wait=True)

    def count_for_file(self, tenant_id: str, file_id: uuid.UUID) -> int:
        """The tenant filter is mandatory in the signature: no query without a tenant."""
        flt = models.Filter(
            must=[
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                models.FieldCondition(key="file_id", match=models.MatchValue(value=str(file_id))),
            ]
        )
        return self._client.count(collection_name=self.alias, count_filter=flt, exact=True).count