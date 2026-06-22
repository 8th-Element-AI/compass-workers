-- ============================================================================
-- Compass Observability — Seed Data (v5 schema)
-- Scope: registry tables + bindings ONLY. Thresholds come later.
--
-- Two solutions:
--   sol_docprocess  -> 1 endpoint, 1 workflow (wf_docunderstand), 5 agents
--   sol_support     -> 3 endpoints, 2 workflows (wf_docunderstand REUSED + wf_triage)
--
-- Coverage:
--   * all 6 component types: model, tool, knowledgebase, skill, function, memory
--   * all 6 binding shapes [1..6] (annotated per row)
--   * workflow reuse (wf_docunderstand under both ep_extract_v1 and ep_doc)
--
-- UUIDs are explicit & deterministic so bindings can reference parents and the
-- file is idempotent-friendly. Drop the explicit `id` values and let
-- gen_random_uuid() fire if you prefer auto-generation.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. solutions
-- ----------------------------------------------------------------------------
INSERT INTO solutions (id, solution_id, solution_name, description) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000001', 'sol_docprocess', 'DocProcess AI',
   'Document ingestion and structured extraction pipeline'),
  ('aaaaaaaa-0000-0000-0000-000000000002', 'sol_support', 'Support Copilot',
   'AI assistant for customer support ticket handling');

-- ----------------------------------------------------------------------------
-- 2. endpoints   (each belongs to exactly one solution)
-- ----------------------------------------------------------------------------
INSERT INTO endpoints
  (id, endpoint_id, solution_id, endpoint_name, path, method, auth_type,
   request_schema, response_schema, description) VALUES
  ('bbbbbbbb-0000-0000-0000-000000000001', 'ep_extract_v1',
   'aaaaaaaa-0000-0000-0000-000000000001', 'Document Extract API',
   '/api/v1/extract', 'POST', 'bearer',
   '{"document_url":"string","doc_type":"string"}'::jsonb,
   '{"entities":"array","confidence":"number"}'::jsonb,
   'Synchronous document extraction entry point'),

  ('bbbbbbbb-0000-0000-0000-000000000002', 'ep_doc',
   'aaaaaaaa-0000-0000-0000-000000000002', 'Document Understanding',
   '/api/v1/documents', 'POST', 'bearer',
   '{"attachment_url":"string"}'::jsonb,
   '{"summary":"string","entities":"array"}'::jsonb,
   'Understands documents attached to a support ticket (reuses wf_docunderstand)'),

  ('bbbbbbbb-0000-0000-0000-000000000003', 'ep_ticket',
   'aaaaaaaa-0000-0000-0000-000000000002', 'Ticket Triage',
   '/api/v1/tickets', 'POST', 'api_key',
   '{"ticket_id":"string","body":"string"}'::jsonb,
   '{"priority":"string","queue":"string"}'::jsonb,
   'Classifies and routes inbound tickets'),

  ('bbbbbbbb-0000-0000-0000-000000000004', 'ep_quick',
   'aaaaaaaa-0000-0000-0000-000000000002', 'Quick Answer',
   '/api/v1/quick', 'POST', 'bearer',
   '{"question":"string"}'::jsonb,
   '{"answer":"string"}'::jsonb,
   'Low-latency direct dispatch with no workflow');

-- ----------------------------------------------------------------------------
-- 3. workflows   (global definitions, independent of solutions/endpoints)
-- ----------------------------------------------------------------------------
INSERT INTO workflows (id, workflow_id, workflow_name, workflow_version, description) VALUES
  ('cccccccc-0000-0000-0000-000000000001', 'wf_docunderstand', 'Document Understanding',
   'v1.0', 'Classify, extract, retrieve, enrich and review document content'),
  ('cccccccc-0000-0000-0000-000000000002', 'wf_triage', 'Ticket Triage',
   'v1.0', 'Classify and route inbound support tickets');

-- ----------------------------------------------------------------------------
-- 4. agents   (reusable actor containers; capabilities come from bindings)
-- ----------------------------------------------------------------------------
INSERT INTO agents (id, agent_id, agent_name, description) VALUES
  ('dddddddd-0000-0000-0000-000000000001', 'agt_classify',  'Classifier', 'Document type classifier'),
  ('dddddddd-0000-0000-0000-000000000002', 'agt_extract',   'Extractor',  'Structured field extractor'),
  ('dddddddd-0000-0000-0000-000000000003', 'agt_retrieve',  'Retriever',  'RAG retriever over product docs'),
  ('dddddddd-0000-0000-0000-000000000004', 'agt_enrich',    'Enricher',   'External enrichment via tool calls'),
  ('dddddddd-0000-0000-0000-000000000005', 'agt_review',    'Reviewer',   'Validates, scores and remembers results'),
  ('dddddddd-0000-0000-0000-000000000006', 'agt_triage',    'Triager',    'Ticket priority classifier'),
  ('dddddddd-0000-0000-0000-000000000007', 'agt_route',     'Router',     'Routes ticket to a queue'),
  ('dddddddd-0000-0000-0000-000000000008', 'agt_responder', 'Responder',  'Single-shot quick answer agent');

-- ----------------------------------------------------------------------------
-- 5. components   (unified registry; type-specific pricing/metadata in JSONB)
--    pricing: model -> input_per_1k/output_per_1k/cached_input_per_1k
--             tool  -> per_call ; knowledgebase -> per_query ; others -> {}
-- ----------------------------------------------------------------------------
INSERT INTO components
  (id, component_id, component_type, component_name, provider, pricing, metadata, description) VALUES
  ('eeeeeeee-0000-0000-0000-000000000001', 'model_gpt4o', 'model', 'gpt-4o', 'openai',
   '{"input_per_1k":0.005,"output_per_1k":0.015,"cached_input_per_1k":0.0025}'::jsonb,
   '{"model_type":"llm","model_version":"2024-08-06"}'::jsonb,
   'OpenAI GPT-4o multimodal model'),

  ('eeeeeeee-0000-0000-0000-000000000002', 'model_claude', 'model', 'claude-sonnet-4', 'anthropic',
   '{"input_per_1k":0.003,"output_per_1k":0.015,"cached_input_per_1k":0.0003}'::jsonb,
   '{"model_type":"llm","model_version":"4-20250514"}'::jsonb,
   'Anthropic Claude Sonnet 4'),

  ('eeeeeeee-0000-0000-0000-000000000003', 'model_embed', 'model', 'text-embedding-3-small', 'openai',
   '{"input_per_1k":0.00002,"output_per_1k":0,"cached_input_per_1k":0}'::jsonb,
   '{"model_type":"embedding","model_version":"1"}'::jsonb,
   'OpenAI small embedding model'),

  ('eeeeeeee-0000-0000-0000-000000000004', 'tool_websearch', 'tool', 'web_search', 'serp_api',
   '{"per_call":0.005}'::jsonb,
   '{}'::jsonb,
   'Web search via SerpAPI'),

  ('eeeeeeee-0000-0000-0000-000000000005', 'kb_product_docs', 'knowledgebase', 'product_docs', 'pinecone',
   '{"per_query":0.0001}'::jsonb,
   '{"kb_type":"vector_store","embedding_model":"text-embedding-3-small"}'::jsonb,
   'Product documentation vector store'),

  ('eeeeeeee-0000-0000-0000-000000000006', 'skill_ner', 'skill', 'ner_extraction', 'internal',
   '{}'::jsonb,
   '{}'::jsonb,
   'Named Entity Recognition skill'),

  ('eeeeeeee-0000-0000-0000-000000000007', 'func_validate', 'function', 'validate_schema', 'internal',
   '{}'::jsonb,
   '{}'::jsonb,
   'JSON schema validation function'),

  ('eeeeeeee-0000-0000-0000-000000000008', 'mem_conv', 'memory', 'conversation_history', 'redis',
   '{}'::jsonb,
   '{"memory_type":"conversation"}'::jsonb,
   'Short-term conversation memory');

-- ============================================================================
-- 6. bindings
-- Rule: solution_id + endpoint_id are ALWAYS set. The deepest non-NULL among
-- (workflow_id, agent_id, component_id) is the target; higher levels are context.
-- Each row is annotated with its binding shape [1..6].
-- ============================================================================

-- ---- Solution 1 : ep_extract_v1 -> wf_docunderstand ------------------------
INSERT INTO bindings (id, solution_id, endpoint_id, workflow_id, agent_id, component_id, config) VALUES
-- [4] endpoint runs workflow
('ffffffff-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001',NULL,NULL,'{}'::jsonb),
-- [5] workflow contains agent
('ffffffff-0000-0000-0000-000000000002','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000001',NULL,'{}'::jsonb),
('ffffffff-0000-0000-0000-000000000003','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000002',NULL,'{}'::jsonb),
('ffffffff-0000-0000-0000-000000000004','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000003',NULL,'{}'::jsonb),
('ffffffff-0000-0000-0000-000000000005','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000004',NULL,'{}'::jsonb),
('ffffffff-0000-0000-0000-000000000006','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005',NULL,'{}'::jsonb),
-- [6] agent uses component inside workflow
('ffffffff-0000-0000-0000-000000000007','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000001','eeeeeeee-0000-0000-0000-000000000001','{"temperature":0.2,"max_tokens":1024,"top_p":1.0,"response_format":"json_object"}'::jsonb),
('ffffffff-0000-0000-0000-000000000008','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000002','eeeeeeee-0000-0000-0000-000000000002','{"temperature":0.0,"max_tokens":4096,"top_p":1.0}'::jsonb),
('ffffffff-0000-0000-0000-000000000009','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000003','eeeeeeee-0000-0000-0000-000000000005','{"top_k":5,"similarity_threshold":0.75,"rerank":true,"chunk_size":512}'::jsonb),
('ffffffff-0000-0000-0000-000000000010','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000003','eeeeeeee-0000-0000-0000-000000000003','{"dimensions":1536}'::jsonb),
('ffffffff-0000-0000-0000-000000000011','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000004','eeeeeeee-0000-0000-0000-000000000004','{"rate_limit":60,"timeout_ms":8000,"retry_policy":"exponential"}'::jsonb),
('ffffffff-0000-0000-0000-000000000012','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005','eeeeeeee-0000-0000-0000-000000000006','{"confidence_threshold":0.8,"max_entities":50,"language":"en"}'::jsonb),
('ffffffff-0000-0000-0000-000000000013','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005','eeeeeeee-0000-0000-0000-000000000007','{"strict_mode":true,"schema_version":"v2","output_format":"json"}'::jsonb),
('ffffffff-0000-0000-0000-000000000014','aaaaaaaa-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005','eeeeeeee-0000-0000-0000-000000000008','{"max_history":20,"ttl_seconds":3600,"scope":"session"}'::jsonb);

-- ---- Solution 2 : ep_doc -> wf_docunderstand  (REUSE — fresh path rows) -----
INSERT INTO bindings (id, solution_id, endpoint_id, workflow_id, agent_id, component_id, config) VALUES
('ffffffff-0000-0000-0000-000000000015','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001',NULL,NULL,'{}'::jsonb),                                                                                              -- [4]
('ffffffff-0000-0000-0000-000000000016','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000001',NULL,'{}'::jsonb),                                                       -- [5]
('ffffffff-0000-0000-0000-000000000017','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000002',NULL,'{}'::jsonb),                                                       -- [5]
('ffffffff-0000-0000-0000-000000000018','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000003',NULL,'{}'::jsonb),                                                       -- [5]
('ffffffff-0000-0000-0000-000000000019','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000004',NULL,'{}'::jsonb),                                                       -- [5]
('ffffffff-0000-0000-0000-000000000020','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005',NULL,'{}'::jsonb),                                                       -- [5]
('ffffffff-0000-0000-0000-000000000021','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000001','eeeeeeee-0000-0000-0000-000000000001','{"temperature":0.2,"max_tokens":1024}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000022','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000002','eeeeeeee-0000-0000-0000-000000000002','{"temperature":0.0,"max_tokens":4096}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000023','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000003','eeeeeeee-0000-0000-0000-000000000005','{"top_k":5,"similarity_threshold":0.78,"rerank":true}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000024','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000003','eeeeeeee-0000-0000-0000-000000000003','{"dimensions":1536}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000025','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000004','eeeeeeee-0000-0000-0000-000000000004','{"rate_limit":60,"timeout_ms":8000}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000026','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005','eeeeeeee-0000-0000-0000-000000000006','{"confidence_threshold":0.8,"language":"en"}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000027','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005','eeeeeeee-0000-0000-0000-000000000007','{"strict_mode":true,"schema_version":"v2"}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000028','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000005','eeeeeeee-0000-0000-0000-000000000008','{"max_history":10,"ttl_seconds":1800,"scope":"session"}'::jsonb);  -- [6]

-- ---- Solution 2 : ep_ticket -> wf_triage -----------------------------------
INSERT INTO bindings (id, solution_id, endpoint_id, workflow_id, agent_id, component_id, config) VALUES
('ffffffff-0000-0000-0000-000000000029','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000003','cccccccc-0000-0000-0000-000000000002',NULL,NULL,'{}'::jsonb),                                                       -- [4]
('ffffffff-0000-0000-0000-000000000030','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000003','cccccccc-0000-0000-0000-000000000002','dddddddd-0000-0000-0000-000000000006',NULL,'{}'::jsonb),                  -- [5]
('ffffffff-0000-0000-0000-000000000031','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000003','cccccccc-0000-0000-0000-000000000002','dddddddd-0000-0000-0000-000000000007',NULL,'{}'::jsonb),                  -- [5]
('ffffffff-0000-0000-0000-000000000032','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000003','cccccccc-0000-0000-0000-000000000002','dddddddd-0000-0000-0000-000000000006','eeeeeeee-0000-0000-0000-000000000001','{"temperature":0.1,"max_tokens":512}'::jsonb),  -- [6]
('ffffffff-0000-0000-0000-000000000033','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000003','cccccccc-0000-0000-0000-000000000002','dddddddd-0000-0000-0000-000000000007','eeeeeeee-0000-0000-0000-000000000004','{"timeout_ms":5000}'::jsonb);  -- [6]

-- ---- Solution 2 : ep_quick -> direct dispatch (NO workflow) -----------------
INSERT INTO bindings (id, solution_id, endpoint_id, workflow_id, agent_id, component_id, config) VALUES
-- [1] endpoint calls component directly
('ffffffff-0000-0000-0000-000000000034','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000004',NULL,NULL,'eeeeeeee-0000-0000-0000-000000000001','{"temperature":0.0,"max_tokens":256}'::jsonb),
-- [2] endpoint runs an agent (no workflow, no component on this row)
('ffffffff-0000-0000-0000-000000000035','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000004',NULL,'dddddddd-0000-0000-0000-000000000008',NULL,'{}'::jsonb),
-- [3] agent uses component, no workflow
('ffffffff-0000-0000-0000-000000000036','aaaaaaaa-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000004',NULL,'dddddddd-0000-0000-0000-000000000008','eeeeeeee-0000-0000-0000-000000000002','{"temperature":0.3,"max_tokens":1024}'::jsonb);

COMMIT;

-- ============================================================================
-- Quick sanity checks
-- ============================================================================
-- Row counts:
--   SELECT 'solutions' t, count(*) FROM solutions UNION ALL
--   SELECT 'endpoints', count(*) FROM endpoints UNION ALL
--   SELECT 'workflows', count(*) FROM workflows UNION ALL
--   SELECT 'agents', count(*) FROM agents UNION ALL
--   SELECT 'components', count(*) FROM components UNION ALL
--   SELECT 'bindings', count(*) FROM bindings;
--
-- Binding shape distribution (workflow/agent/component NULL pattern):
--   SELECT (workflow_id IS NOT NULL) wf, (agent_id IS NOT NULL) ag,
--          (component_id IS NOT NULL) comp, count(*)
--   FROM bindings GROUP BY 1,2,3 ORDER BY 1,2,3;
