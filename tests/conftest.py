import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/hr")
os.environ.setdefault("TEMPORAL_ADDRESS", "localhost:7233")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_BUCKET", "hr-artifacts")
os.environ.setdefault("S3_ACCESS_KEY", "minio")
os.environ.setdefault("S3_SECRET_KEY", "minio12345")
os.environ.setdefault("API_INTERNAL_URL", "http://localhost:8000")
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")
