-- A project may hold several knowledge maps; only (project, slug) stays unique.
DROP INDEX IF EXISTS maps_one_knowledge_per_project;
