BEGIN;
-- ----------------------------------------------------------------------------
-- 1. components   (unified registry; type-specific pricing/metadata in JSONB)
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

COMMIT;
