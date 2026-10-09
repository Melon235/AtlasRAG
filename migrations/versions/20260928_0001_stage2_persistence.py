"""Canonical PostgreSQL persistence; migration history is append-only."""

from alembic import op

revision = "20260928_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE documents (
            document_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            relative_source_path TEXT NOT NULL,
            source_type TEXT NOT NULL,
            observed_content_hash TEXT NOT NULL,
            current_revision_id TEXT,
            current_content_hash TEXT,
            current_pipeline_fingerprint TEXT,
            building_revision_id TEXT,
            building_content_hash TEXT,
            building_pipeline_fingerprint TEXT,
            ingestion_status TEXT NOT NULL,
            failed_stage TEXT,
            parse_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_documents PRIMARY KEY (document_id),
            CONSTRAINT uq_documents_relative_source_path UNIQUE (relative_source_path),
            CONSTRAINT ck_documents_source_type CHECK (
                source_type IN ('PDF', 'DOCX', 'XLSX', 'MD', 'TXT')
            ),
            CONSTRAINT ck_documents_ingestion_status CHECK (
                ingestion_status IN (
                    'RECEIVED', 'PARSING', 'PARSED', 'CHUNKING',
                    'CHUNKED', 'INDEXING', 'READY', 'FAILED'
                )
            ),
            CONSTRAINT ck_documents_failed_stage CHECK (
                failed_stage IN (
                    'PARSE', 'CHUNK', 'REPRESENTATION',
                    'EMBEDDING', 'INDEX_WRITE', 'INDEX_VERIFY'
                )
            ),
            CONSTRAINT ck_documents_failure_consistency CHECK (
                (ingestion_status = 'FAILED') = (failed_stage IS NOT NULL)
            ),
            CONSTRAINT ck_documents_current_revision_complete CHECK (
                (current_revision_id IS NULL AND current_content_hash IS NULL
                    AND current_pipeline_fingerprint IS NULL)
                OR
                (current_revision_id IS NOT NULL AND current_content_hash IS NOT NULL
                    AND current_pipeline_fingerprint IS NOT NULL)
            ),
            CONSTRAINT ck_documents_building_revision_complete CHECK (
                (building_revision_id IS NULL AND building_content_hash IS NULL
                    AND building_pipeline_fingerprint IS NULL)
                OR
                (building_revision_id IS NOT NULL AND building_content_hash IS NOT NULL
                    AND building_pipeline_fingerprint IS NOT NULL)
            ),
            CONSTRAINT ck_documents_parse_metadata_object CHECK (
                jsonb_typeof(parse_metadata) = 'object'
            )
        )
    """)
    op.execute("""
        CREATE TABLE document_elements (
            element_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            element_type TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_anchor JSONB,
            structured_content JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_document_elements PRIMARY KEY (element_id),
            CONSTRAINT fk_document_elements_document FOREIGN KEY (document_id)
                REFERENCES documents (document_id),
            CONSTRAINT ck_document_elements_type_nonblank
                CHECK (element_type ~ '[^[:space:]]'),
            CONSTRAINT ck_document_elements_order_nonnegative CHECK (order_index >= 0),
            CONSTRAINT ck_document_elements_section_path_array CHECK (
                jsonb_typeof(section_path) = 'array'
            ),
            CONSTRAINT ck_document_elements_source_anchor_object CHECK (
                source_anchor IS NULL OR jsonb_typeof(source_anchor) = 'object'
            ),
            CONSTRAINT ck_document_elements_structured_content_object CHECK (
                jsonb_typeof(structured_content) = 'object'
            ),
            CONSTRAINT ck_document_elements_metadata_object CHECK (
                jsonb_typeof(metadata) = 'object'
            )
        )
    """)
    op.execute("""
        CREATE TABLE chunks (
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            chunk_type TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_anchor JSONB,
            sheet_name TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            strategy_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_chunks PRIMARY KEY (chunk_id),
            CONSTRAINT fk_chunks_document FOREIGN KEY (document_id)
                REFERENCES documents (document_id),
            CONSTRAINT uq_chunks_identity_revision
                UNIQUE (chunk_id, document_id, revision_id) NOT DEFERRABLE,
            CONSTRAINT fk_chunks_parent_revision
                FOREIGN KEY (parent_id, document_id, revision_id)
                REFERENCES chunks (chunk_id, document_id, revision_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_chunks_chunk_type CHECK (
                chunk_type IN ('TEXT_PARENT', 'TEXT_CHILD', 'TABLE')
            ),
            CONSTRAINT ck_chunks_child_requires_parent CHECK (
                chunk_type <> 'TEXT_CHILD' OR parent_id IS NOT NULL
            ),
            CONSTRAINT ck_chunks_text_parent_has_no_parent CHECK (
                chunk_type <> 'TEXT_PARENT' OR parent_id IS NULL
            ),
            CONSTRAINT ck_chunks_no_self_parent CHECK (
                parent_id IS NULL OR parent_id <> chunk_id
            ),
            CONSTRAINT ck_chunks_sheet_name_nonblank CHECK (
                sheet_name IS NULL OR sheet_name ~ '[^[:space:]]'
            ),
            CONSTRAINT ck_chunks_section_path_array CHECK (
                jsonb_typeof(section_path) = 'array'
            ),
            CONSTRAINT ck_chunks_source_anchor_object CHECK (
                source_anchor IS NULL OR jsonb_typeof(source_anchor) = 'object'
            ),
            CONSTRAINT ck_chunks_metadata_object CHECK (
                jsonb_typeof(metadata) = 'object'
            ),
            CONSTRAINT ck_chunks_strategy_metadata_object CHECK (
                jsonb_typeof(strategy_metadata) = 'object'
            )
        )
    """)
    # Resolve final stored rows: deferred NEW may describe an intermediate state.
    # Parent updates must also validate any children that already reference it.
    op.execute("""
        CREATE FUNCTION atlasrag_check_chunk_parent_type() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM chunks AS child
                WHERE child.chunk_type = 'TEXT_CHILD'
                  AND (child.chunk_id = NEW.chunk_id OR child.parent_id = NEW.chunk_id)
                  AND NOT EXISTS (
                      SELECT 1 FROM chunks AS parent
                      WHERE parent.chunk_id = child.parent_id
                        AND parent.document_id = child.document_id
                        AND parent.revision_id = child.revision_id
                        AND parent.chunk_type = 'TEXT_PARENT'
                      FOR SHARE
                  )
            ) THEN
                RAISE EXCEPTION 'TEXT_CHILD requires a TEXT_PARENT in its document/revision'
                    USING ERRCODE = '23514', CONSTRAINT = 'ct_chunks_parent_type';
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER ct_chunks_parent_type
        AFTER INSERT OR UPDATE ON chunks
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION atlasrag_check_chunk_parent_type()
    """)
    op.execute("""
        CREATE TABLE runtime_metadata (
            metadata_key TEXT NOT NULL DEFAULT 'runtime',
            query_cache_invalidation_required BOOLEAN NOT NULL DEFAULT false,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_runtime_metadata PRIMARY KEY (metadata_key),
            CONSTRAINT ck_runtime_metadata_singleton CHECK (metadata_key = 'runtime')
        )
    """)
    op.execute("""
        INSERT INTO runtime_metadata (
            metadata_key, query_cache_invalidation_required, updated_at
        ) VALUES ('runtime', false, CURRENT_TIMESTAMP)
    """)
    op.execute("""
        CREATE INDEX ix_documents_current_revision_id ON documents (current_revision_id)
    """)
    op.execute("""
        CREATE INDEX ix_documents_building_revision_id ON documents (building_revision_id)
    """)
    op.execute("""
        CREATE INDEX ix_document_elements_document_revision
        ON document_elements (document_id, revision_id)
    """)
    op.execute("""
        CREATE INDEX ix_chunks_document_revision ON chunks (document_id, revision_id)
    """)
    op.execute("CREATE INDEX ix_chunks_parent_id ON chunks (parent_id)")
    op.execute("CREATE INDEX ix_chunks_chunk_type ON chunks (chunk_type)")


def downgrade() -> None:
    raise RuntimeError(
        "Append-only migration history: destructive downgrade is unsupported"
    )
