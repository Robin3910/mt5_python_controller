import os

# force mock MT5 before the node_client module is imported
os.environ.setdefault("MT5_MOCK", "true")
os.environ.setdefault("NODE_TOKEN", "test-token")
