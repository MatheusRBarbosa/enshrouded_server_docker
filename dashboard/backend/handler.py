"""Lambda entry point. Function URL requests arrive as HTTP events; EventBridge Scheduler runs
and the dashboard's own async continuations arrive as {"task": ...}. See api.py."""
import os

import api

_app = None


def app():
    global _app
    if _app is None:
        import cloud
        import store

        _app = api.App(store.DynamoStore(os.environ["TABLE"]), cloud.Cloud(os.environ))
    return _app


def lambda_handler(event, context):
    if "requestContext" in event and "http" in event.get("requestContext", {}):
        return app().http(event)
    if "task" in event:
        return app().task(event)
    raise ValueError("Unsupported event")
