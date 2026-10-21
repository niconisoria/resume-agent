import os

# Test env must exist before app.config is imported.
os.environ["ALLOWED_ORIGIN"] = "https://allowed.example"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
