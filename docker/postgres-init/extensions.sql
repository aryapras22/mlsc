-- The startup gate in mlsc/bootstrap.py refuses to run unless both extensions are
-- installed. The image ships them; only timescaledb is created automatically.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
