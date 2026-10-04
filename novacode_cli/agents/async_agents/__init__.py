"""The async (background) agents: nine graphs served by a LangGraph server.

Nova launches that server itself, on the first ``start_async_task``
(``agents/server_launcher.py``), from the ``langgraph.json`` in this folder. The
graph ids clients address are the keys in that file, not the module names.
"""
